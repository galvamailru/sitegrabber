from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Page, SiteRelease
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


@router.get("/{full_path:path}", response_class=HTMLResponse)
async def published_page(full_path: str, db: AsyncSession = Depends(get_db)):
    path = "/" + full_path.strip("/")
    if path == "/":
        path = "/"
    active = await db.scalar(select(SiteRelease).where(SiteRelease.is_active.is_(True)).order_by(SiteRelease.created_at.desc()))
    if not active:
        return HTMLResponse("<h1>No published site yet</h1><p>Open /admin to start cloning.</p>", status_code=200)
    page = await db.scalar(
        select(Page).where(Page.site_project_id == active.site_project_id, Page.url_path == path).limit(1)
    )
    if page and page.transformed_html:
        return HTMLResponse(page.transformed_html)
    if path == "/":
        first_page = await db.scalar(
            select(Page).where(Page.site_project_id == active.site_project_id).order_by(Page.depth, Page.created_at).limit(1)
        )
        if first_page and first_page.transformed_html:
            return HTMLResponse(first_page.transformed_html)
    return HTMLResponse("<h1>404</h1>", status_code=404)
