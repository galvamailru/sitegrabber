import html
import json
from uuid import uuid4
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Asset, CartSelection, CatalogFilterConfig, Lead, Page, PageBlock, Product, ProductSpec, SiteProject, SiteRelease
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


def _html_shell(title: str, body: str, *, site_project_id: str | None = None) -> str:
    """Оболочка витрины + всплывающий чат (дизайн и «печать» ответа как в aichatbot)."""
    t = html.escape(title)
    sid_js = json.dumps(str(site_project_id) if site_project_id else "")
    chat_block = (
        '<button type="button" id="chatbot-toggler" aria-label="Открыть чат">'
        '<span class="material-symbols-rounded icon-chat">mode_comment</span>'
        '<span class="material-symbols-rounded icon-close">close</span></button>'
        '<div class="chatbot-popup">'
        '<div class="chat-header">'
        '<div class="header-info"><span class="logo-text">Консультант каталога</span></div>'
        '<div class="chat-header-actions">'
        '<a href="#" id="layout-switch" class="layout-switch" title="Переключить режим отображения">Полноэкранный</a>'
        '<button type="button" id="close-chatbot" class="material-symbols-rounded" aria-label="Свернуть">keyboard_arrow_down</button>'
        "</div></div>"
        '<div class="chat-body"></div>'
        '<div class="chat-footer">'
        '<form class="chat-form" id="chat-form">'
        '<div class="chat-form-wrapper">'
        '<textarea class="message-input" placeholder="Сообщение... (не более 1000 символов)" rows="1" required></textarea>'
        '<button type="submit" class="send-btn" aria-label="Отправить">'
        '<span class="material-symbols-rounded" style="font-size:1.2rem;">arrow_upward</span></button></div>'
        '<div class="error-msg" role="alert"></div></form></div></div>'
    )
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{t}</title>"
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" />'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@48,400,1,0" />'
        '<link rel="stylesheet" href="/static/catalog_chat_popup.css" />'
        "<style>body{font-family:Arial,sans-serif;background:#f5f7fa;margin:0}"
        ".wrap{max-width:1200px;margin:0 auto;padding:20px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}"
        ".card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:14px}.btn{display:inline-block;padding:8px 10px;background:#2563eb;color:#fff;border-radius:8px;text-decoration:none;border:0}"
        ".muted{color:#6b7280;font-size:13px}</style></head><body><div class='wrap'>"
        f"{body}</div>"
        f"{chat_block}"
        f"<script>window.__SITE_PROJECT_ID__={sid_js};</script>"
        '<script src="/static/catalog_chat_widget.js"></script></body></html>'
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
    response = HTMLResponse(_html_shell("Корзина", body, site_project_id=str(active.site_project_id)))
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
    return HTMLResponse(
        _html_shell(
            "Лид отправлен",
            "<h1>Спасибо!</h1><p>Менеджер свяжется с вами.</p><a class='btn' href='/'>На главную</a>",
            site_project_id=str(active.site_project_id),
        )
    )


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
        products = (await db.execute(select(Product).where(Product.site_project_id == active.site_project_id).limit(500))).scalars().all()
        enabled_filters = (
            await db.execute(
                select(CatalogFilterConfig)
                .where(CatalogFilterConfig.site_project_id == active.site_project_id, CatalogFilterConfig.enabled.is_(True))
                .order_by(CatalogFilterConfig.sort_order, CatalogFilterConfig.display_name)
            )
        ).scalars().all()
        query_map = dict(request.query_params)

        product_specs_map: dict[str, dict[str, list[str]]] = {}
        spec_rows = (
            await db.execute(
                select(ProductSpec.product_id, ProductSpec.key, ProductSpec.value)
                .join(Product, Product.id == ProductSpec.product_id)
                .where(Product.site_project_id == active.site_project_id)
            )
        ).all()
        for r in spec_rows:
            pid = str(r.product_id)
            bucket = product_specs_map.setdefault(pid, {})
            bucket.setdefault(r.key, []).append(r.value)

        # Build filter value dictionaries from parsed specs.
        filter_values: dict[str, list[str]] = {}
        for f in enabled_filters:
            vals = set()
            for specs in product_specs_map.values():
                for v in specs.get(f.spec_key, []):
                    vals.add(v)
            filter_values[f.param_name] = sorted(vals)[:500]

        # Apply filters from query params.
        filtered_products = []
        for p in products:
            specs = product_specs_map.get(str(p.id), {})
            ok = True
            for f in enabled_filters:
                selected = query_map.get(f.param_name)
                if not selected:
                    continue
                values = specs.get(f.spec_key, [])
                if selected not in values:
                    ok = False
                    break
            if ok:
                filtered_products.append(p)
        products = filtered_products
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
        filters_html = ""
        if enabled_filters:
            rows = []
            for f in enabled_filters:
                opts = "".join(
                    [f"<option value='{v}' {'selected' if query_map.get(f.param_name)==v else ''}>{v}</option>" for v in filter_values.get(f.param_name, [])]
                )
                rows.append(
                    f"<label>{f.display_name}</label><select name='{f.param_name}'><option value=''>Любое</option>{opts}</select>"
                )
            filters_html = (
                "<form method='get' class='card' style='margin-bottom:16px'>"
                + "".join(rows)
                + "<button class='btn' type='submit' style='margin-top:10px'>Применить фильтры</button> "
                + "<a class='btn' href='/' style='background:#6b7280'>Сбросить</a></form>"
            )
        body = (
            f"<h1>{project.name if project else 'Каталог'}</h1>"
            "<p><a class='btn' href='/cart'>Корзина</a></p>"
            f"{filters_html}"
            f"<div class='grid'>{''.join(cards) or '<div>Товары не найдены</div>'}</div>"
            "<h2>Инфо-страницы</h2>"
            f"<ul>{pages_links or '<li>Нет</li>'}</ul>"
        )
        html = _html_shell("Каталог", body, site_project_id=str(active.site_project_id))
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
            html = _html_shell(product.name, body, site_project_id=str(active.site_project_id))
            response = HTMLResponse(html)
            response.set_cookie("cart_session", _get_session_id(request), max_age=60 * 60 * 24 * 30)
            return response
    if page and page.page_type != "product":
        body = f"<h1>{page.title or page.url_path}</h1><p>{(page.meta_description or '')}</p><p><a class='btn' href='/'>Назад</a></p>"
        response = HTMLResponse(_html_shell(page.title or "Страница", body, site_project_id=str(active.site_project_id)))
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
    return HTMLResponse(_html_shell("404", "<h1>404</h1>", site_project_id=str(active.site_project_id)), status_code=404)
