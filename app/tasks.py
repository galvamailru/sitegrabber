from app.celery_app import celery
from app.clone_pipeline import (
    check_prices_project,
    crawl_project,
    discover_project_strategy,
    publish_project,
    regenerate_images,
    regenerate_single_asset,
    rewrite_project,
    run_async,
)
from app.database import async_session_factory
from app.models import SiteProject
from sqlalchemy import select


async def _set_stage(project_id: str, field: str, value: str, error: str | None = None):
    async with async_session_factory() as db:
        project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
        if not project:
            return
        setattr(project, field, value)
        if error:
            project.last_error = error[:1000]
        await db.commit()


@celery.task(name="crawl_site_task")
def crawl_site_task(project_id: str, depth: int = 2):
    run_async(_set_stage(project_id, "crawl_status", "running"))
    try:
        result = run_async(crawl_project(project_id, depth))
        run_async(_finalize_crawl_stage(project_id))
        return result
    except Exception as e:
        run_async(_set_stage(project_id, "crawl_status", "failed", str(e)))
        raise


async def _finalize_crawl_stage(project_id: str):
    async with async_session_factory() as db:
        project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
        if not project:
            return
        if project.crawl_status == "stopped":
            return
        project.crawl_status = "done"
        await db.commit()


@celery.task(name="crawl_site_resume_task")
def crawl_site_resume_task(project_id: str, depth: int = 2):
    run_async(_set_stage(project_id, "crawl_status", "running"))
    try:
        result = run_async(crawl_project(project_id, depth, resume=True))
        run_async(_finalize_crawl_stage(project_id))
        return result
    except Exception as e:
        run_async(_set_stage(project_id, "crawl_status", "failed", str(e)))
        raise


@celery.task(name="rewrite_texts_task")
def rewrite_texts_task(project_id: str):
    run_async(_set_stage(project_id, "rewrite_status", "running"))
    try:
        result = run_async(rewrite_project(project_id))
        run_async(_set_stage(project_id, "rewrite_status", "done"))
        return result
    except Exception as e:
        run_async(_set_stage(project_id, "rewrite_status", "failed", str(e)))
        raise


@celery.task(name="generate_images_task")
def generate_images_task(project_id: str):
    run_async(_set_stage(project_id, "image_status", "running"))
    try:
        result = run_async(regenerate_images(project_id))
        run_async(_set_stage(project_id, "image_status", "done"))
        return result
    except Exception as e:
        run_async(_set_stage(project_id, "image_status", "failed", str(e)))
        raise


@celery.task(name="publish_site_task")
def publish_site_task(project_id: str):
    run_async(_set_stage(project_id, "publish_status", "running"))
    try:
        run_async(publish_project(project_id))
        run_async(_set_stage(project_id, "publish_status", "done"))
        return {"status": "published", "project_id": project_id}
    except Exception as e:
        run_async(_set_stage(project_id, "publish_status", "failed", str(e)))
        raise


@celery.task(name="full_clone_pipeline_task")
def full_clone_pipeline_task(project_id: str, depth: int = 2):
    crawl_site_task(project_id, depth)
    rewrite_texts_task(project_id)
    generate_images_task(project_id)
    publish_site_task(project_id)
    return {"status": "completed", "project_id": project_id}


@celery.task(name="generate_single_image_task")
def generate_single_image_task(asset_id: str, project_id: str):
    run_async(_set_stage(project_id, "image_status", "running"))
    ok = run_async(regenerate_single_asset(asset_id))
    run_async(_set_stage(project_id, "image_status", "done" if ok else "failed"))
    return {"asset_id": asset_id, "ok": ok}


@celery.task(name="check_prices_task")
def check_prices_task(project_id: str):
    run_async(_set_stage(project_id, "price_check_status", "running"))
    try:
        result = run_async(check_prices_project(project_id))
        run_async(_set_stage(project_id, "price_check_status", "done"))
        return result
    except Exception as e:
        run_async(_set_stage(project_id, "price_check_status", "failed", str(e)))
        raise


@celery.task(name="discover_strategy_task")
def discover_strategy_task(project_id: str):
    run_async(_set_stage(project_id, "crawl_status", "discovering"))
    try:
        result = run_async(discover_project_strategy(project_id))
        run_async(_set_stage(project_id, "crawl_status", "idle"))
        return result
    except Exception as e:
        run_async(_set_stage(project_id, "crawl_status", "failed", str(e)))
        raise
