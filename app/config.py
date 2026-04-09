"""
Конфигурация приложения. Все переменные читаются только из .env.
"""
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_prompt_path(v: str) -> Path:
    p = Path(v)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    return p


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "aichatbot"
    POSTGRES_PASSWORD: str = ""
    POSTGRES_DB: str = "aichatbot"

    LLM_URL: str = "https://api.deepseek.com"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "deepseek-chat"
    LLM_TEMPERATURE: float = 0.7

    PROMPT_FILE_PATH: str = "prompts/system.txt"
    ADMIN_KEY: str = ""
    REDIS_URL: str = "redis://redis:6379/0"
    MINIO_ENDPOINT: str = "minio:9000"
    MINIO_ACCESS_KEY: str = "admin"
    MINIO_SECRET_KEY: str = "password"
    MINIO_BUCKET: str = "site-assets"

    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_MODEL: str = "deepseek-chat"
    OPENAI_API_KEY: str = ""
    OPENAI_IMAGE_MODEL: str = "gpt-image-1.5"

    MAX_CRAWL_DEPTH: int = 3
    CRAWL_RATE_LIMIT_RPS: float = 1.0
    MAX_CONCURRENT_IMAGE_GENERATIONS: int = 5

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def prompt_path(self) -> Path:
        return _resolve_prompt_path(self.PROMPT_FILE_PATH)


def get_settings() -> Settings:
    return Settings()
