import re
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.llm import complete_chat
from app.models import Page, Product, ProductSpec, SiteProject
from app.schemas import CatalogChatRequest

router = APIRouter(prefix="/api", tags=["catalog-chat"])

_MAX_PRODUCTS = 500
_MAX_SPECS_PER_PRODUCT = 15
_MAX_CONTENT_PAGES = 100


def _short_text(t: str | None, limit: int) -> str:
    if not t:
        return ""
    s = re.sub(r"\s+", " ", t).strip()
    return (s[: limit - 1] + "…") if len(s) > limit else s


@router.post("/catalog-chat")
async def catalog_chat(body: CatalogChatRequest, db: AsyncSession = Depends(get_db)):
    try:
        project_id = UUID(body.site_project_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid site_project_id")

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

    catalog_table = (project.catalog_prompt_table or "").strip()
    system_prompt = (
        "Ты консультант по ассортименту и услугам. Единственный надёжный источник — переданные ниже данные: полный перечень товаров "
        "(названия, категории, цены, описания, характеристики) и блок услуг/информационных разделов.\n"
        "Не опирайся на URL, slug и «перейдите по ссылке»; отвечай по сути из каталога своими словами.\n"
        "Если позиции нет в переданных списках — скажи, что в текущих данных её нет.\n"
        "Если вопрос про оформление: можно предложить корзину и контакт менеджера.\n"
        "Формат: кратко и по делу."
    )
    if catalog_table:
        system_prompt = f"{system_prompt}\n\n{catalog_table}"
    user_prompt = (
        f"Проект: {project.name}\n\n"
        f"Товары (полный список, до {_MAX_PRODUCTS} позиций):\n"
        f"{chr(10).join(product_context) if product_context else '- нет'}\n\n"
        f"Услуги и информационные страницы:\n"
        f"{chr(10).join(page_context) if page_context else '- нет'}\n\n"
        f"Вопрос клиента: {body.question}"
    )
    try:
        answer = await complete_chat(messages=[{"role": "user", "content": user_prompt}], system_prompt=system_prompt)
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=503 if e.response.status_code >= 500 else 502,
            detail="Ошибка LLM при генерации ответа",
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Временная ошибка catalog-chat")
    return {"answer": answer}
