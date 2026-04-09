from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery
from app.database import get_db
from app.models import Asset, Page, Product, SiteProject, SiteRelease
from app.tasks import (
    crawl_site_task,
    full_clone_pipeline_task,
    generate_images_task,
    generate_single_image_task,
    publish_site_task,
    rewrite_texts_task,
)

router = APIRouter(tags=["clone-admin"])
templates = Jinja2Templates(directory="templates")


@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    projects = (await db.execute(select(SiteProject).order_by(desc(SiteProject.created_at)))).scalars().all()
    return templates.TemplateResponse("admin/dashboard.html", {"request": request, "projects": projects})


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
async def run_clone(project_id: UUID):
    task = crawl_site_task.delay(str(project_id))
    return JSONResponse({"task_id": task.id, "status": "queued"})


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


@router.post("/admin/projects/{project_id}/run-all")
async def run_all(project_id: UUID, db: AsyncSession = Depends(get_db)):
    project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
    depth = project.crawl_depth if project else 2
    task = full_clone_pipeline_task.delay(str(project_id), depth)
    return JSONResponse({"task_id": task.id, "status": "queued"})


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
            "rewrite_status": project.rewrite_status if project else "idle",
            "image_status": project.image_status if project else "idle",
            "publish_status": project.publish_status if project else "idle",
            "last_error": project.last_error if project else None,
            "pages_total": pages,
            "products_total": products,
            "rewritten_total": rewritten,
            "assets_total": assets,
            "assets_generated": generated,
            "assets_failed": failed,
            "rewrite_progress_pct": int((rewritten / products) * 100) if products else 0,
            "images_progress_pct": int((generated / assets) * 100) if assets else 0,
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
    return templates.TemplateResponse(
        "admin/project.html",
        {
            "request": request,
            "project": project,
            "pages_count": pages_count or 0,
            "products_count": products_count or 0,
            "releases": releases,
            "failed_assets": failed_assets,
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
