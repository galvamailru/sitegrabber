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


@router.post("/catalog-chat")
async def catalog_chat(body: CatalogChatRequest, db: AsyncSession = Depends(get_db)):
    try:
        project_id = UUID(body.site_project_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid site_project_id")

    project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
    if not project:
        raise HTTPException(status_code=404, detail="site_project not found")

    products = (await db.execute(select(Product).where(Product.site_project_id == project.id).limit(30))).scalars().all()
    pages = (
        await db.execute(
            select(Page).where(Page.site_project_id == project.id, Page.page_type != "product").limit(20)
        )
    ).scalars().all()

    product_context = []
    for p in products:
        specs = (
            await db.execute(select(ProductSpec).where(ProductSpec.product_id == p.id).order_by(ProductSpec.sort_order).limit(8))
        ).scalars().all()
        specs_text = "; ".join([f"{s.key}: {s.value}" for s in specs])
        product_context.append(f"- {p.name} (slug: {p.slug or ''}, цена от: {p.price_from or 'n/a'}) {specs_text}")

    page_context = [f"- {pg.title or pg.url_path}: {(pg.meta_description or '')[:220]}" for pg in pages]

    catalog_table = (project.catalog_prompt_table or "").strip()
    system_prompt = (
        "Ты консультант сайта-каталога. Отвечай только по данным контекста, не выдумывай.\n"
        "Если вопрос о наличии/оформлении: предложи добавить товар в корзину и оставить контакты для менеджера.\n"
        "Формат ответа: кратко, по делу, с рекомендациями по товарам.\n"
        "Если товара нет в каталожной таблице — честно сообщи, что в текущем ассортименте его нет."
    )
    if catalog_table:
        system_prompt = f"{system_prompt}\n\n{catalog_table}"
    user_prompt = (
        f"Проект: {project.name}\n\n"
        f"Товары (оперативный контекст):\n{chr(10).join(product_context) if product_context else '- нет'}\n\n"
        f"Инфо-страницы:\n{chr(10).join(page_context) if page_context else '- нет'}\n\n"
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
