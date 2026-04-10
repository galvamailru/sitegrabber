"""
Подключение к PostgreSQL. Async SQLAlchemy + asyncpg.
"""
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Base

_settings = get_settings()
engine = create_async_engine(
    _settings.database_url,
    echo=False,
    poolclass=NullPool,
)
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncSession:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Backward-compatible schema upgrades for existing volumes
        # (create_all does not add new columns to existing tables).
        await conn.execute(text("ALTER TABLE site_projects ADD COLUMN IF NOT EXISTS crawl_stop_requested BOOLEAN NOT NULL DEFAULT FALSE"))
        await conn.execute(text("ALTER TABLE site_projects ADD COLUMN IF NOT EXISTS crawl_publish_on_stop BOOLEAN NOT NULL DEFAULT FALSE"))
        await conn.execute(text("ALTER TABLE site_projects ADD COLUMN IF NOT EXISTS crawl_queue_state JSONB"))
        await conn.execute(text("ALTER TABLE site_projects ADD COLUMN IF NOT EXISTS crawl_visited_state JSONB"))
        await conn.execute(text("ALTER TABLE site_projects ADD COLUMN IF NOT EXISTS crawl_tree_state JSONB"))
        await conn.execute(text("ALTER TABLE site_projects ADD COLUMN IF NOT EXISTS crawl_strategy_state JSONB"))
        await conn.execute(text("ALTER TABLE site_projects ADD COLUMN IF NOT EXISTS crawl_last_url TEXT"))
        await conn.execute(text("ALTER TABLE site_projects ADD COLUMN IF NOT EXISTS price_check_status VARCHAR(32) NOT NULL DEFAULT 'idle'"))
        await conn.execute(text("ALTER TABLE site_projects ADD COLUMN IF NOT EXISTS catalog_prompt_table TEXT"))
        await conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS source_url TEXT"))
