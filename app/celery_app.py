from celery import Celery

from app.config import get_settings

settings = get_settings()

celery = Celery(
    "site_clone",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery.conf.update(
    task_track_started=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
)
