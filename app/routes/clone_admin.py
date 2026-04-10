from uuid import UUID
import re
from collections import defaultdict

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery
from app.database import get_db
from app.models import Asset, CatalogFilterConfig, Page, PageBlock, Product, ProductSourceSnapshot, ProductSpec, SiteProject, SiteRelease
from app.tasks import (
    check_prices_task,
    crawl_site_task,
    crawl_site_resume_task,
    full_clone_pipeline_task,
    generate_images_task,
    generate_single_image_task,
    publish_site_task,
    rewrite_texts_task,
)

router = APIRouter(tags=["clone-admin"])
templates = Jinja2Templates(directory="templates")


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ]+", "_", value.strip().lower(), flags=re.UNICODE)
    return s.strip("_")[:100] or "param"


@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    projects = (await db.execute(select(SiteProject).order_by(desc(SiteProject.created_at)))).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="admin/dashboard.html",
        context={"request": request, "projects": projects},
    )


@router.post("/admin/projects")
async def create_project(
    source_url: str = Form(...),
    name: str = Form(...),
    crawl_depth: int = Form(2),
    design_prompt: str = Form(""),
    image_prompt_global: str = Form(""),
    tone_of_voice: str = Form(""),
    with_cart: bool = Form(False),
    db: AsyncSession = Depends(get_db),
):
    p = SiteProject(
        source_url=source_url.strip(),
        name=name.strip(),
        crawl_depth=crawl_depth,
        design_prompt=design_prompt.strip() or None,
        image_prompt_global=image_prompt_global.strip() or None,
        tone_of_voice=tone_of_voice.strip() or None,
        with_cart=with_cart,
    )
    db.add(p)
    await db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/projects/{project_id}/clone")
async def run_clone(project_id: UUID, db: AsyncSession = Depends(get_db)):
    project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
    depth = project.crawl_depth if project else 2
    task = crawl_site_task.delay(str(project_id), depth)
    return JSONResponse({"task_id": task.id, "status": "queued"})


@router.post("/admin/projects/{project_id}/clone/resume")
async def run_clone_resume(project_id: UUID, db: AsyncSession = Depends(get_db)):
    project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
    depth = project.crawl_depth if project else 2
    task = crawl_site_resume_task.delay(str(project_id), depth)
    return JSONResponse({"task_id": task.id, "status": "queued_resume"})


@router.post("/admin/projects/{project_id}/clone/stop")
async def stop_clone(project_id: UUID, publish_partial: bool = Form(False), db: AsyncSession = Depends(get_db)):
    project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
    if not project:
        return JSONResponse({"status": "not_found"}, status_code=404)
    project.crawl_stop_requested = True
    project.crawl_publish_on_stop = publish_partial
    await db.commit()
    if publish_partial and project.crawl_status != "running":
        task = publish_site_task.delay(str(project_id))
        return JSONResponse({"status": "published_partial_immediately", "task_id": task.id})
    return JSONResponse({"status": "stop_requested", "publish_partial": publish_partial})


@router.post("/admin/projects/{project_id}/rewrite")
async def run_rewrite(project_id: UUID):
    task = rewrite_texts_task.delay(str(project_id))
    return JSONResponse({"task_id": task.id, "status": "queued"})


@router.post("/admin/projects/{project_id}/images")
async def run_images(project_id: UUID):
    task = generate_images_task.delay(str(project_id))
    return JSONResponse({"task_id": task.id, "status": "queued"})


@router.post("/admin/projects/{project_id}/publish")
async def run_publish(project_id: UUID):
    task = publish_site_task.delay(str(project_id))
    return JSONResponse({"task_id": task.id, "status": "queued"})


@router.post("/admin/projects/{project_id}/price-check")
async def run_price_check(project_id: UUID):
    task = check_prices_task.delay(str(project_id))
    return JSONResponse({"task_id": task.id, "status": "queued"})


@router.post("/admin/projects/{project_id}/run-all")
async def run_all(project_id: UUID, db: AsyncSession = Depends(get_db)):
    project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
    depth = project.crawl_depth if project else 2
    task = full_clone_pipeline_task.delay(str(project_id), depth)
    return JSONResponse({"task_id": task.id, "status": "queued"})


@router.get("/admin/projects/{project_id}/filters", response_class=HTMLResponse)
async def filters_editor(project_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
    if not project:
        return HTMLResponse("Project not found", status_code=404)
    keys_rows = (
        await db.execute(
            select(ProductSpec.key, func.count(ProductSpec.id).label("cnt"))
            .join(Product, Product.id == ProductSpec.product_id)
            .where(Product.site_project_id == project_id)
            .group_by(ProductSpec.key)
            .order_by(func.count(ProductSpec.id).desc(), ProductSpec.key)
        )
    ).all()
    existing = (
        await db.execute(
            select(CatalogFilterConfig).where(CatalogFilterConfig.site_project_id == project_id).order_by(CatalogFilterConfig.sort_order)
        )
    ).scalars().all()
    by_key = {x.spec_key: x for x in existing}
    key_rows = []
    for idx, r in enumerate(keys_rows):
        cfg = by_key.get(r.key)
        key_rows.append(
            {
                "key": r.key,
                "count": r.cnt,
                "enabled": cfg.enabled if cfg else False,
                "display_name": cfg.display_name if cfg else r.key,
                "param_name": cfg.param_name if cfg else _slugify(r.key),
                "sort_order": cfg.sort_order if cfg else idx,
            }
        )
    return templates.TemplateResponse(
        request=request,
        name="admin/filters.html",
        context={"request": request, "project": project, "keys": key_rows},
    )


@router.post("/admin/projects/{project_id}/filters/save")
async def save_filters(project_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    keys = form.getlist("key")
    enabled_keys = set(form.getlist("enabled"))
    existing = (
        await db.execute(select(CatalogFilterConfig).where(CatalogFilterConfig.site_project_id == project_id))
    ).scalars().all()
    existing_by_key = {e.spec_key: e for e in existing}

    order = 0
    for k in keys:
        display_name = str(form.get(f"display_name__{k}") or k).strip()
        param_name = _slugify(str(form.get(f"param_name__{k}") or k))
        enabled = k in enabled_keys
        cfg = existing_by_key.get(k)
        if not cfg:
            cfg = CatalogFilterConfig(
                site_project_id=project_id,
                spec_key=k,
                param_name=param_name,
                display_name=display_name,
                enabled=enabled,
                sort_order=order,
            )
            db.add(cfg)
        else:
            cfg.param_name = param_name
            cfg.display_name = display_name
            cfg.enabled = enabled
            cfg.sort_order = order
        order += 1

    await db.commit()
    return RedirectResponse(f"/admin/projects/{project_id}/filters", status_code=303)


@router.get("/admin/tasks/{task_id}")
async def task_status(task_id: str):
    result = celery.AsyncResult(task_id)
    payload = {"task_id": task_id, "state": result.state}
    if result.ready():
        payload["result"] = result.result
    return JSONResponse(payload)


@router.get("/admin/projects/{project_id}/progress")
async def project_progress(project_id: UUID, db: AsyncSession = Depends(get_db)):
    project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
    pages = await db.scalar(select(func.count(Page.id)).where(Page.site_project_id == project_id)) or 0
    products = await db.scalar(select(func.count(Product.id)).where(Product.site_project_id == project_id)) or 0
    rewritten = await db.scalar(
        select(func.count(Product.id)).where(Product.site_project_id == project_id, Product.rewritten_description.is_not(None))
    ) or 0
    assets = await db.scalar(select(func.count(Asset.id)).where(Asset.site_project_id == project_id)) or 0
    generated = await db.scalar(
        select(func.count(Asset.id)).where(Asset.site_project_id == project_id, Asset.generated.is_(True))
    ) or 0
    failed = await db.scalar(
        select(func.count(Asset.id)).where(Asset.site_project_id == project_id, Asset.generation_failed.is_(True))
    ) or 0
    return JSONResponse(
        {
            "crawl_status": project.crawl_status if project else "idle",
            "crawl_processed": project.crawl_processed if project else 0,
            "crawl_discovered": project.crawl_discovered if project else 0,
            "crawl_stop_requested": project.crawl_stop_requested if project else False,
            "crawl_last_url": project.crawl_last_url if project else None,
            "crawl_tree_nodes": (project.crawl_tree_state or {}).get("nodes", [])[-60:] if project else [],
            "rewrite_status": project.rewrite_status if project else "idle",
            "image_status": project.image_status if project else "idle",
            "publish_status": project.publish_status if project else "idle",
            "price_check_status": project.price_check_status if project else "idle",
            "last_error": project.last_error if project else None,
            "pages_total": pages,
            "products_total": products,
            "rewritten_total": rewritten,
            "assets_total": assets,
            "assets_generated": generated,
            "assets_failed": failed,
            "crawl_progress_pct": int(((project.crawl_processed if project else 0) / max((project.crawl_discovered if project else 1), 1)) * 100),
            "rewrite_progress_pct": int((rewritten / products) * 100) if products else 0,
            "images_progress_pct": int((generated / assets) * 100) if assets else 0,
        }
    )


@router.get("/admin/projects/{project_id}/crawl-tree", response_class=HTMLResponse)
async def crawl_tree_page(project_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
    if not project:
        return HTMLResponse("Project not found", status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="admin/crawl_tree.html",
        context={"request": request, "project": project},
    )


@router.get("/admin/projects/{project_id}/price-monitor", response_class=HTMLResponse)
async def price_monitor_page(project_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
    if not project:
        return HTMLResponse("Project not found", status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="admin/price_monitor.html",
        context={"request": request, "project": project},
    )


@router.get("/admin/projects/{project_id}/price-monitor/data")
async def price_monitor_data(project_id: UUID, db: AsyncSession = Depends(get_db)):
    project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
    if not project:
        return JSONResponse({"status": "not_found"}, status_code=404)
    snapshots = (
        await db.execute(
            select(ProductSourceSnapshot)
            .where(ProductSourceSnapshot.site_project_id == project_id)
            .order_by(desc(ProductSourceSnapshot.captured_at))
            .limit(6000)
        )
    ).scalars().all()
    by_url: dict[str, list[ProductSourceSnapshot]] = defaultdict(list)
    for snap in snapshots:
        if not snap.source_url:
            continue
        if len(by_url[snap.source_url]) < 2:
            by_url[snap.source_url].append(snap)
    rows: list[dict] = []
    for source_url, pair in by_url.items():
        latest = pair[0]
        prev = pair[1] if len(pair) > 1 else None
        delta = None
        delta_pct = None
        if prev and latest.price_from is not None and prev.price_from is not None:
            delta = round(latest.price_from - prev.price_from, 2)
            if prev.price_from:
                delta_pct = round((delta / prev.price_from) * 100, 2)
        rows.append(
            {
                "source_url": source_url,
                "product_name": latest.product_name,
                "latest_price": latest.price_from,
                "latest_currency": latest.currency or "RUB",
                "latest_at": latest.captured_at.isoformat() if latest.captured_at else None,
                "prev_price": prev.price_from if prev else None,
                "prev_at": prev.captured_at.isoformat() if prev and prev.captured_at else None,
                "delta": delta,
                "delta_pct": delta_pct,
            }
        )
    rows.sort(key=lambda x: (x["delta"] is None, -(abs(x["delta"] or 0))))
    return JSONResponse(
        {
            "project_id": str(project.id),
            "price_check_status": project.price_check_status,
            "rows": rows,
            "rows_total": len(rows),
        }
    )


@router.get("/admin/projects/{project_id}/crawl-tree/data")
async def crawl_tree_data(project_id: UUID, db: AsyncSession = Depends(get_db)):
    project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
    if not project:
        return JSONResponse({"status": "not_found"}, status_code=404)
    nodes = (project.crawl_tree_state or {}).get("nodes", [])
    return JSONResponse(
        {
            "project_id": str(project.id),
            "crawl_status": project.crawl_status,
            "crawl_processed": project.crawl_processed,
            "crawl_discovered": project.crawl_discovered,
            "crawl_last_url": project.crawl_last_url,
            "crawl_stop_requested": project.crawl_stop_requested,
            "nodes_total": len(nodes),
            "nodes": nodes,
        }
    )


@router.get("/admin/projects/{project_id}", response_class=HTMLResponse)
async def project_details(project_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
    if not project:
        return HTMLResponse("Project not found", status_code=404)
    pages_count = await db.scalar(select(func.count(Page.id)).where(Page.site_project_id == project_id))
    products_count = await db.scalar(select(func.count(Product.id)).where(Product.site_project_id == project_id))
    releases = (await db.execute(select(SiteRelease).where(SiteRelease.site_project_id == project_id).order_by(desc(SiteRelease.created_at)))).scalars().all()
    failed_assets = (
        await db.execute(
            select(Asset).where(Asset.site_project_id == project_id, Asset.generation_failed.is_(True)).order_by(desc(Asset.created_at))
        )
    ).scalars().all()
    ai_pages = await db.scalar(
        select(func.count(Page.id)).where(Page.site_project_id == project_id, Page.extraction_source == "ai")
    )
    fallback_pages = await db.scalar(
        select(func.count(Page.id)).where(Page.site_project_id == project_id, Page.extraction_source != "ai")
    )
    recent_pages = (
        await db.execute(
            select(Page)
            .where(Page.site_project_id == project_id)
            .order_by(desc(Page.created_at))
            .limit(20)
        )
    ).scalars().all()
    snapshots = (
        await db.execute(
            select(ProductSourceSnapshot)
            .where(ProductSourceSnapshot.site_project_id == project_id)
            .order_by(desc(ProductSourceSnapshot.captured_at))
            .limit(4000)
        )
    ).scalars().all()
    by_url: dict[str, list[ProductSourceSnapshot]] = defaultdict(list)
    for snap in snapshots:
        if not snap.source_url:
            continue
        if len(by_url[snap.source_url]) < 2:
            by_url[snap.source_url].append(snap)
    price_changes: list[dict] = []
    for source_url, pair in by_url.items():
        latest = pair[0]
        prev = pair[1] if len(pair) > 1 else None
        delta = None
        delta_pct = None
        if prev and latest.price_from is not None and prev.price_from is not None:
            delta = round(latest.price_from - prev.price_from, 2)
            if prev.price_from:
                delta_pct = round((delta / prev.price_from) * 100, 2)
        price_changes.append(
            {
                "source_url": source_url,
                "product_name": latest.product_name,
                "latest_price": latest.price_from,
                "latest_currency": latest.currency or "RUB",
                "latest_at": latest.captured_at,
                "prev_price": prev.price_from if prev else None,
                "prev_at": prev.captured_at if prev else None,
                "delta": delta,
                "delta_pct": delta_pct,
            }
        )
    price_changes.sort(
        key=lambda x: (x["delta"] is None, -(abs(x["delta"] or 0))),
    )
    price_changes = price_changes[:100]
    return templates.TemplateResponse(
        request=request,
        name="admin/project.html",
        context={
            "request": request,
            "project": project,
            "pages_count": pages_count or 0,
            "products_count": products_count or 0,
            "releases": releases,
            "failed_assets": failed_assets,
            "ai_pages": ai_pages or 0,
            "fallback_pages": fallback_pages or 0,
            "recent_pages": recent_pages,
            "price_changes": price_changes,
        },
    )


@router.post("/admin/assets/{asset_id}/retry")
async def retry_asset(asset_id: UUID, project_id: UUID = Form(...)):
    task = generate_single_image_task.delay(str(asset_id), str(project_id))
    return RedirectResponse(f"/admin/projects/{project_id}", status_code=303)


@router.post("/admin/assets/{asset_id}/use-original")
async def use_original_asset(asset_id: UUID, project_id: UUID = Form(...), db: AsyncSession = Depends(get_db)):
    asset = await db.scalar(select(Asset).where(Asset.id == asset_id))
    if asset:
        asset.local_url = asset.source_url
        asset.generation_failed = False
        asset.generated = True
        asset.failure_reason = None
        await db.commit()
    return RedirectResponse(f"/admin/projects/{project_id}", status_code=303)


@router.post("/admin/assets/{asset_id}/delete")
async def delete_asset(asset_id: UUID, project_id: UUID = Form(...), db: AsyncSession = Depends(get_db)):
    asset = await db.scalar(select(Asset).where(Asset.id == asset_id))
    if asset:
        await db.delete(asset)
        await db.commit()
    return RedirectResponse(f"/admin/projects/{project_id}", status_code=303)


@router.get("/admin/projects/{project_id}/cms", response_class=HTMLResponse)
async def cms_editor(project_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
    if not project:
        return HTMLResponse("Project not found", status_code=404)
    pages = (
        await db.execute(select(Page).where(Page.site_project_id == project_id).order_by(Page.page_type, Page.url_path))
    ).scalars().all()
    products = (
        await db.execute(select(Product).where(Product.site_project_id == project_id).order_by(Product.created_at.desc()).limit(200))
    ).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="admin/cms.html",
        context={"request": request, "project": project, "pages": pages, "products": products},
    )


@router.post("/admin/products/{product_id}/update")
async def update_product(
    product_id: UUID,
    project_id: UUID = Form(...),
    name: str = Form(...),
    rewritten_name: str = Form(""),
    price_from: float | None = Form(None),
    rewritten_description: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    product = await db.scalar(select(Product).where(Product.id == product_id))
    if product:
        product.name = name
        product.rewritten_name = rewritten_name or None
        product.price_from = price_from
        product.rewritten_description = rewritten_description
        await db.commit()
    return RedirectResponse(f"/admin/projects/{project_id}/cms", status_code=303)


@router.post("/admin/pages/{page_id}/update")
async def update_page(
    page_id: UUID,
    project_id: UUID = Form(...),
    title: str = Form(""),
    meta_description: str = Form(""),
    page_type: str = Form("content"),
    db: AsyncSession = Depends(get_db),
):
    page = await db.scalar(select(Page).where(Page.id == page_id))
    if page:
        page.title = title or page.title
        page.meta_description = meta_description
        page.page_type = page_type
        await db.commit()
    return RedirectResponse(f"/admin/projects/{project_id}/cms", status_code=303)


@router.get("/admin/pages/{page_id}/blocks", response_class=HTMLResponse)
async def page_blocks_editor(page_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    page = await db.scalar(select(Page).where(Page.id == page_id))
    if not page:
        return HTMLResponse("Page not found", status_code=404)
    blocks = (
        await db.execute(select(PageBlock).where(PageBlock.page_id == page_id).order_by(PageBlock.sort_order, PageBlock.created_at))
    ).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="admin/page_blocks.html",
        context={"request": request, "page": page, "blocks": blocks},
    )


@router.post("/admin/pages/{page_id}/blocks/add")
async def add_page_block(
    page_id: UUID,
    block_type: str = Form("text"),
    title: str = Form(""),
    content: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    order = await db.scalar(select(func.count(PageBlock.id)).where(PageBlock.page_id == page_id)) or 0
    db.add(PageBlock(page_id=page_id, block_type=block_type, title=title or None, content=content, sort_order=order))
    await db.commit()
    return RedirectResponse(f"/admin/pages/{page_id}/blocks", status_code=303)


@router.post("/admin/blocks/{block_id}/update")
async def update_page_block(
    block_id: UUID,
    title: str = Form(""),
    content: str = Form(""),
    block_type: str = Form("text"),
    db: AsyncSession = Depends(get_db),
):
    block = await db.scalar(select(PageBlock).where(PageBlock.id == block_id))
    if not block:
        return HTMLResponse("Block not found", status_code=404)
    block.title = title or None
    block.content = content
    block.block_type = block_type
    await db.commit()
    return RedirectResponse(f"/admin/pages/{block.page_id}/blocks", status_code=303)


@router.post("/admin/blocks/{block_id}/delete")
async def delete_page_block(block_id: UUID, db: AsyncSession = Depends(get_db)):
    block = await db.scalar(select(PageBlock).where(PageBlock.id == block_id))
    if not block:
        return HTMLResponse("Block not found", status_code=404)
    page_id = block.page_id
    await db.delete(block)
    await db.commit()
    return RedirectResponse(f"/admin/pages/{page_id}/blocks", status_code=303)


@router.post("/admin/pages/{page_id}/blocks/reorder")
async def reorder_page_blocks(page_id: UUID, order: str = Form(...), db: AsyncSession = Depends(get_db)):
    ids = [x.strip() for x in order.split(",") if x.strip()]
    blocks = (
        await db.execute(select(PageBlock).where(PageBlock.page_id == page_id))
    ).scalars().all()
    by_id = {str(b.id): b for b in blocks}
    for idx, bid in enumerate(ids):
        if bid in by_id:
            by_id[bid].sort_order = idx
    await db.commit()
    return JSONResponse({"status": "ok"})
