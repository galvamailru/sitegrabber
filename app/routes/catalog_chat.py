import logging
import re
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.llm import complete_chat
from app.models import CatalogChatMessage, Page, Product, ProductSpec, SiteProject
from app.schemas import CatalogChatRequest

router = APIRouter(prefix="/api", tags=["catalog-chat"])
logger = logging.getLogger(__name__)

_MAX_PRODUCTS = 500
_MAX_SYSTEM_PROMPT_CHARS = 120000
_MAX_SPECS_PER_PRODUCT = 15
_MAX_CONTENT_PAGES = 100
_MAX_HISTORY_MESSAGES = 40
_MAX_HISTORY_CONTENT_CHARS = 50000

_DEFAULT_CATALOG_CHAT_SYSTEM_PROMPT = (
    "Ты консультант по ассортименту и услугам. Единственный надёжный источник — переданные ниже данные: полный перечень товаров "
    "(названия, категории, цены, описания, характеристики) и блок услуг/информационных разделов.\n"
    "Не опирайся на URL, slug и «перейдите по ссылке»; отвечай по сути из каталога своими словами.\n"
    "Если позиции нет в переданных списках — скажи, что в текущих данных её нет.\n"
    "Если вопрос про оформление: можно предложить корзину и контакт менеджера.\n"
    "Учитывай предыдущие реплики в диалоге, если они есть.\n"
    "Формат: кратко и по делу."
)


def _short_text(t: str | None, limit: int) -> str:
    if not t:
        return ""
    s = re.sub(r"\s+", " ", t).strip()
    return (s[: limit - 1] + "…") if len(s) > limit else s


def _trim_history_to_budget(messages: list[dict[str, str]], max_chars: int) -> list[dict[str, str]]:
    if not messages:
        return messages
    trimmed = list(messages)
    while trimmed and sum(len(m.get("content", "")) for m in trimmed) > max_chars:
        trimmed.pop(0)
    return trimmed


async def _get_history_for_llm(
    db: AsyncSession,
    site_project_id: UUID,
    user_id: str,
    dialog_id: str,
) -> list[dict[str, str]]:
    result = await db.execute(
        select(CatalogChatMessage)
        .where(
            CatalogChatMessage.site_project_id == site_project_id,
            CatalogChatMessage.user_id == user_id,
            CatalogChatMessage.dialog_id == dialog_id,
        )
        .order_by(desc(CatalogChatMessage.created_at))
        .limit(_MAX_HISTORY_MESSAGES)
    )
    rows = list(reversed(result.scalars().all()))
    out = [{"role": r.role, "content": r.content} for r in rows if r.role in ("user", "assistant")]
    return _trim_history_to_budget(out, _MAX_HISTORY_CONTENT_CHARS)


def _catalog_turn_user_content(
    project: SiteProject,
    product_context: list[str],
    page_context: list[str],
    question: str,
) -> str:
    return (
        f"Проект: {project.name}\n\n"
        f"Товары (полный список, до {_MAX_PRODUCTS} позиций):\n"
        f"{chr(10).join(product_context) if product_context else '- нет'}\n\n"
        f"Услуги и информационные страницы:\n"
        f"{chr(10).join(page_context) if page_context else '- нет'}\n\n"
        f"Вопрос клиента: {question}"
    )


@router.post("/catalog-chat")
async def catalog_chat(body: CatalogChatRequest, db: AsyncSession = Depends(get_db)):
    try:
        project_id = UUID(body.site_project_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid site_project_id")

    uid = body.user_id.strip()
    if not uid:
        raise HTTPException(status_code=422, detail="user_id required")
    dialog_id = (body.dialog_id or "default").strip() or "default"
    if len(dialog_id) > 255:
        dialog_id = dialog_id[:255]

    project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
    if not project:
        raise HTTPException(status_code=404, detail="site_project not found")

    products = (
        await db.execute(
            select(Product).where(Product.site_project_id == project.id).order_by(Product.created_at.desc()).limit(_MAX_PRODUCTS)
        )
    ).scalars().all()
    spec_rows = (
        await db.execute(
            select(ProductSpec)
            .join(Product, Product.id == ProductSpec.product_id)
            .where(Product.site_project_id == project.id)
            .order_by(ProductSpec.product_id, ProductSpec.sort_order)
        )
    ).scalars().all()
    specs_by_product: dict[str, list[str]] = {}
    for s in spec_rows:
        pid = str(s.product_id)
        bucket = specs_by_product.setdefault(pid, [])
        if len(bucket) >= _MAX_SPECS_PER_PRODUCT:
            continue
        bucket.append(f"{s.key}: {s.value}")

    product_context = []
    for p in products:
        specs_text = "; ".join(specs_by_product.get(str(p.id), [])) or "-"
        desc = _short_text(p.rewritten_description or p.original_description, 400)
        vis = "на витрине" if p.catalog_visible else "только в базе (не на витрине)"
        line = (
            f"- {p.rewritten_name or p.name} | категория: {p.category or '-'} | цена от: {p.price_from or 'n/a'} {p.currency or 'RUB'} | "
            f"{vis} | описание: {desc or '-'} | характеристики: {specs_text}"
        )
        product_context.append(line)

    pages = (
        await db.execute(
            select(Page)
            .where(Page.site_project_id == project.id, Page.page_type != "product")
            .order_by(Page.url_path)
            .limit(_MAX_CONTENT_PAGES)
        )
    ).scalars().all()
    page_context = []
    for pg in pages:
        if (pg.url_path or "").strip() in ("/", ""):
            continue
        snippet = (pg.meta_description or "").strip()
        if not snippet and isinstance(pg.original_texts, dict):
            snippet = str(pg.original_texts.get("text") or "")
        snippet = _short_text(snippet, 500)
        page_context.append(f"- {pg.title or pg.url_path}: {snippet or '-'}")

    custom_base = (project.catalog_chat_system_prompt or "").strip()
    system_prompt = custom_base if custom_base else _DEFAULT_CATALOG_CHAT_SYSTEM_PROMPT
    catalog_table = (project.catalog_prompt_table or "").strip()
    if catalog_table:
        system_prompt = f"{system_prompt}\n\n{catalog_table}"
    if len(system_prompt) > _MAX_SYSTEM_PROMPT_CHARS:
        system_prompt = (
            system_prompt[: _MAX_SYSTEM_PROMPT_CHARS - 80]
            + "\n\n[… контекст каталога обрезан по длине, задайте уточняющий вопрос …]"
        )

    history = await _get_history_for_llm(db, project.id, uid, dialog_id)
    last_user_content = _catalog_turn_user_content(project, product_context, page_context, body.question)
    messages_for_llm = [*history, {"role": "user", "content": last_user_content}]

    try:
        answer = await complete_chat(messages=messages_for_llm, system_prompt=system_prompt)
    except RuntimeError as e:
        if str(e) == "llm_api_key_missing":
            raise HTTPException(
                status_code=503,
                detail="Чат недоступен: задайте LLM_API_KEY или DEEPSEEK_API_KEY в окружении.",
            )
        raise
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Превышено время ожидания ответа LLM. Сократите вопрос или повторите позже.",
        )
    except httpx.RequestError as e:
        logger.warning("catalog_chat request_error: %s", e)
        raise HTTPException(
            status_code=503,
            detail="Не удалось связаться с сервисом LLM. Проверьте сеть и LLM_URL.",
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=503 if e.response.status_code >= 500 else 502,
            detail="Ошибка LLM при генерации ответа",
        )
    except Exception:
        logger.exception("catalog_chat unexpected error")
        raise HTTPException(status_code=500, detail="Временная ошибка catalog-chat")

    q_stored = body.question.strip()
    db.add(
        CatalogChatMessage(
            site_project_id=project.id,
            user_id=uid,
            dialog_id=dialog_id,
            role="user",
            content=q_stored,
        )
    )
    db.add(
        CatalogChatMessage(
            site_project_id=project.id,
            user_id=uid,
            dialog_id=dialog_id,
            role="assistant",
            content=answer or "",
        )
    )
    return {"answer": answer}
