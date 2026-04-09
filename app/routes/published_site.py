from uuid import uuid4
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Asset, CartSelection, Lead, Page, PageBlock, Product, ProductSpec, SiteProject, SiteRelease
from app.storage import get_object_bytes

router = APIRouter(tags=["published-site"])


@router.get("/sitemap.xml")
async def sitemap(db: AsyncSession = Depends(get_db)):
    active = await db.scalar(select(SiteRelease).where(SiteRelease.is_active.is_(True)).order_by(SiteRelease.created_at.desc()))
    if not active:
        return Response("<urlset/>", media_type="application/xml")
    pages = (await db.execute(select(Page).where(Page.site_project_id == active.site_project_id))).scalars().all()
    items = "".join([f"<url><loc>{p.url_path}</loc></url>" for p in pages])
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset>{items}</urlset>', media_type="application/xml")


@router.get("/robots.txt")
async def robots():
    return PlainTextResponse("User-agent: *\nAllow: /\nSitemap: /sitemap.xml\n")


@router.get("/assets/{object_path:path}")
async def asset_proxy(object_path: str):
    data = get_object_bytes(object_path)
    if data is None:
        return Response(status_code=404)
    return Response(data, media_type="image/png")


def _html_shell(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{title}</title><style>body{{font-family:Arial,sans-serif;background:#f5f7fa;margin:0}}"
        ".wrap{max-width:1200px;margin:0 auto;padding:20px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}"
        ".card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:14px}.btn{display:inline-block;padding:8px 10px;background:#2563eb;color:#fff;border-radius:8px;text-decoration:none;border:0}"
        ".muted{color:#6b7280;font-size:13px}.chat-widget{position:fixed;right:16px;bottom:16px;width:320px;background:#fff;border:1px solid #e5e7eb;border-radius:12px;box-shadow:0 10px 28px rgba(0,0,0,.15);padding:10px}"
        ".chat-log{height:160px;overflow:auto;border:1px solid #e5e7eb;border-radius:8px;padding:8px;margin-bottom:8px;background:#fafafa}</style></head><body><div class='wrap'>"
        f"{body}</div>"
        "<div class='chat-widget'><div><b>AI консультант</b></div><div id='chat-log' class='chat-log'></div><input id='chat-q' placeholder='Ваш вопрос' style='width:100%;padding:8px'><button id='chat-send' class='btn' style='margin-top:8px;width:100%'>Спросить</button></div>"
        "<script>"
        "window.__siteProjectId = window.__siteProjectId || '';"
        "const logEl=document.getElementById('chat-log');"
        "document.getElementById('chat-send').onclick=async()=>{"
        "const q=document.getElementById('chat-q').value.trim(); if(!q||!window.__siteProjectId)return;"
        "logEl.innerHTML += `<div><b>Вы:</b> ${q}</div>`;"
        "const r=await fetch('/api/catalog-chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({site_project_id:window.__siteProjectId,question:q})});"
        "const d=await r.json(); logEl.innerHTML += `<div><b>Бот:</b> ${d.answer||'Нет ответа'}</div>`; logEl.scrollTop=logEl.scrollHeight;"
        "};"
        "</script></body></html>"
    )


def _get_session_id(request: Request) -> str:
    return request.cookies.get("cart_session") or str(uuid4())


@router.post("/cart/add/{product_id}")
async def cart_add(product_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    active = await db.scalar(select(SiteRelease).where(SiteRelease.is_active.is_(True)).order_by(SiteRelease.created_at.desc()))
    if not active:
        return Response("No active release", status_code=400)
    session_id = _get_session_id(request)
    product = await db.scalar(select(Product).where(Product.id == product_id, Product.site_project_id == active.site_project_id))
    if product:
        exists = await db.scalar(
            select(CartSelection).where(
                CartSelection.session_id == session_id,
                CartSelection.product_id == product.id,
                CartSelection.site_project_id == active.site_project_id,
            )
        )
        if not exists:
            db.add(CartSelection(site_project_id=active.site_project_id, session_id=session_id, product_id=product.id))
            await db.commit()
    response = HTMLResponse("<script>history.back()</script>")
    response.set_cookie("cart_session", session_id, max_age=60 * 60 * 24 * 30)
    return response


@router.get("/cart", response_class=HTMLResponse)
async def cart_page(request: Request, db: AsyncSession = Depends(get_db)):
    active = await db.scalar(select(SiteRelease).where(SiteRelease.is_active.is_(True)).order_by(SiteRelease.created_at.desc()))
    if not active:
        return HTMLResponse(_html_shell("Корзина", "<h1>Нет опубликованного сайта</h1>"))
    session_id = _get_session_id(request)
    selections = (
        await db.execute(
            select(CartSelection, Product)
            .join(Product, Product.id == CartSelection.product_id)
            .where(CartSelection.session_id == session_id, CartSelection.site_project_id == active.site_project_id)
        )
    ).all()
    rows = "".join([f"<li>{p.name}</li>" for _, p in selections]) or "<li>Корзина пуста</li>"
    msg = "; ".join([p.name for _, p in selections]) or "пока без товаров"
    body = (
        "<h1>Корзина интересов</h1><p class='muted'>Количество не указывается, только список выбранных позиций.</p>"
        f"<ul>{rows}</ul><p><b>Сообщение менеджеру:</b><br>Здравствуйте, интересуют товары: {msg}.</p>"
        "<form method='post' action='/cart/lead'>"
        "<label>Имя</label><input name='name' required style='width:100%;padding:8px'>"
        "<label>Телефон/контакт</label><input name='phone' required style='width:100%;padding:8px'>"
        "<label>Комментарий</label><textarea name='comment' rows='3' style='width:100%;padding:8px'></textarea>"
        "<button class='btn' type='submit'>Отправить менеджеру</button></form>"
        "<p><a class='btn' href='/'>Вернуться в каталог</a></p>"
    )
    response = HTMLResponse(_html_shell("Корзина", body))
    response.set_cookie("cart_session", session_id, max_age=60 * 60 * 24 * 30)
    return response


@router.post("/cart/lead")
async def cart_send_lead(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    name = str(form.get("name") or "").strip()
    phone = str(form.get("phone") or "").strip()
    comment = str(form.get("comment") or "").strip()
    active = await db.scalar(select(SiteRelease).where(SiteRelease.is_active.is_(True)).order_by(SiteRelease.created_at.desc()))
    if not active:
        return HTMLResponse(_html_shell("Корзина", "<h1>Нет опубликованного сайта</h1>"), status_code=400)
    session_id = _get_session_id(request)
    selections = (
        await db.execute(
            select(CartSelection, Product)
            .join(Product, Product.id == CartSelection.product_id)
            .where(CartSelection.session_id == session_id, CartSelection.site_project_id == active.site_project_id)
        )
    ).all()
    products_text = ", ".join([p.name for _, p in selections]) or "без позиций"
    lead_text = f"Клиент: {name}; Телефон: {phone}; Товары: {products_text}; Комментарий: {comment}"
    db.add(Lead(user_id=f"cart:{session_id}", dialog_id=str(active.site_project_id), contact_text=lead_text))
    await db.commit()
    return HTMLResponse(_html_shell("Лид отправлен", "<h1>Спасибо!</h1><p>Менеджер свяжется с вами.</p><a class='btn' href='/'>На главную</a>"))


@router.get("/{full_path:path}", response_class=HTMLResponse)
async def published_page(full_path: str, request: Request, db: AsyncSession = Depends(get_db)):
    path = "/" + full_path.strip("/")
    if path == "/":
        path = "/"
    active = await db.scalar(select(SiteRelease).where(SiteRelease.is_active.is_(True)).order_by(SiteRelease.created_at.desc()))
    if not active:
        return HTMLResponse("<h1>No published site yet</h1><p>Open /admin to start cloning.</p>", status_code=200)
    project = await db.scalar(select(SiteProject).where(SiteProject.id == active.site_project_id))
    source_root = project.source_url.rstrip("/") if project else ""
    page = await db.scalar(
        select(Page).where(Page.site_project_id == active.site_project_id, Page.url_path == path).limit(1)
    )
    if path == "/":
        products = (await db.execute(select(Product).where(Product.site_project_id == active.site_project_id).limit(200))).scalars().all()
        content_pages = (
            await db.execute(select(Page).where(Page.site_project_id == active.site_project_id, Page.page_type != "product").limit(100))
        ).scalars().all()
        cards = []
        for p in products:
            img = await db.scalar(select(Asset).where(Asset.page_id == p.page_id, Asset.role == "main").limit(1))
            img_html = f"<img src='{img.local_url or img.source_url}' style='width:100%;height:170px;object-fit:cover;border-radius:8px'>" if img else ""
            cards.append(
                "<div class='card'>"
                f"{img_html}<h3>{p.name}</h3><div class='muted'>от {int(p.price_from) if p.price_from else 'n/a'} ₽</div>"
                f"<a class='btn' href='/{p.slug or ''}'>Открыть</a> "
                f"<form method='post' action='/cart/add/{p.id}' style='display:inline'><button class='btn' type='submit'>В корзину</button></form>"
                "</div>"
            )
        pages_links = "".join([f"<li><a href='{pg.url_path}'>{pg.title or pg.url_path}</a></li>" for pg in content_pages if pg.url_path != "/"])
        body = (
            f"<h1>{project.name if project else 'Каталог'}</h1>"
            "<p><a class='btn' href='/cart'>Корзина</a></p>"
            f"<div class='grid'>{''.join(cards) or '<div>Товары не найдены</div>'}</div>"
            "<h2>Инфо-страницы</h2>"
            f"<ul>{pages_links or '<li>Нет</li>'}</ul>"
        )
        html = _html_shell("Каталог", body).replace("window.__siteProjectId = window.__siteProjectId || '';", f"window.__siteProjectId = '{active.site_project_id}';")
        response = HTMLResponse(html)
        response.set_cookie("cart_session", _get_session_id(request), max_age=60 * 60 * 24 * 30)
        return response
    if page and page.page_type == "product":
        product = await db.scalar(select(Product).where(Product.page_id == page.id).limit(1))
        if product:
            specs = (
                await db.execute(select(ProductSpec).where(ProductSpec.product_id == product.id).order_by(ProductSpec.sort_order))
            ).scalars().all()
            imgs = (await db.execute(select(Asset).where(Asset.page_id == page.id).limit(12))).scalars().all()
            images = "".join(
                [f"<img src='{a.local_url or a.source_url}' style='width:220px;height:220px;object-fit:cover;border-radius:8px;margin:6px'>" for a in imgs]
            )
            spec_html = "".join([f"<li><b>{s.key}:</b> {s.value}</li>" for s in specs]) or "<li>Нет характеристик</li>"
            blocks = (
                await db.execute(select(PageBlock).where(PageBlock.page_id == page.id).order_by(PageBlock.sort_order))
            ).scalars().all()
            block_html = "".join(
                [f"<section class='card'><h3>{b.title or ''}</h3><div>{b.content}</div></section>" for b in blocks]
            )
            body = (
                f"<h1>{product.rewritten_name or product.name}</h1>"
                f"<p class='muted'>Цена от: {int(product.price_from) if product.price_from else 'n/a'} ₽</p>"
                f"<p>{product.rewritten_description or product.original_description or ''}</p>"
                f"<div>{images}</div><h3>Характеристики</h3><ul>{spec_html}</ul>{block_html}"
                f"<form method='post' action='/cart/add/{product.id}'><button class='btn' type='submit'>Добавить в корзину</button></form>"
                "<p><a class='btn' href='/'>Назад в каталог</a></p>"
            )
            html = _html_shell(product.name, body).replace("window.__siteProjectId = window.__siteProjectId || '';", f"window.__siteProjectId = '{active.site_project_id}';")
            response = HTMLResponse(html)
            response.set_cookie("cart_session", _get_session_id(request), max_age=60 * 60 * 24 * 30)
            return response
    if page and page.page_type != "product":
        body = f"<h1>{page.title or page.url_path}</h1><p>{(page.meta_description or '')}</p><p><a class='btn' href='/'>Назад</a></p>"
        response = HTMLResponse(_html_shell(page.title or "Страница", body))
        response.set_cookie("cart_session", _get_session_id(request), max_age=60 * 60 * 24 * 30)
        return response
    if path == "/":
        first_page = await db.scalar(
            select(Page).where(Page.site_project_id == active.site_project_id).order_by(Page.depth, Page.created_at).limit(1)
        )
        if first_page and first_page.transformed_html:
            return HTMLResponse(first_page.transformed_html)
    # Fallback proxy for missed static/resources to preserve layout.
    if source_root:
        try:
            upstream_url = urljoin(f"{source_root}/", full_path)
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                r = await client.get(upstream_url)
            content_type = r.headers.get("content-type", "text/plain")
            return Response(content=r.content, media_type=content_type, status_code=r.status_code)
        except Exception:
            pass
    return HTMLResponse(_html_shell("404", "<h1>404</h1>"), status_code=404)
