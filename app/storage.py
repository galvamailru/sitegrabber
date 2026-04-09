from io import BytesIO
from uuid import uuid4

from minio import Minio
from minio.error import S3Error

from app.config import get_settings

settings = get_settings()


def _client() -> Minio:
    return Minio(
        settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=False,
    )


def ensure_bucket() -> None:
    client = _client()
    if not client.bucket_exists(settings.MINIO_BUCKET):
        client.make_bucket(settings.MINIO_BUCKET)


def upload_bytes(data: bytes, content_type: str = "image/png", prefix: str = "generated") -> tuple[str, str]:
    client = _client()
    ensure_bucket()
    obj = f"{prefix}/{uuid4()}.png"
    client.put_object(
        settings.MINIO_BUCKET,
        obj,
        data=BytesIO(data),
        length=len(data),
        content_type=content_type,
    )
    return obj, f"/assets/{obj}"


def get_object_bytes(object_name: str) -> bytes | None:
    client = _client()
    try:
        resp = client.get_object(settings.MINIO_BUCKET, object_name)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()
    except S3Error:
        return None
