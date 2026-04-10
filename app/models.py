"""
Модели БД: сообщения с привязкой к user_id и dialog_id.
"""
from datetime import datetime
from uuid import UUID as PyUUID, uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    dialog_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True, default="default")
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class Lead(Base):
    """Лиды: контакты для обратной связи, извлечённые из диалогов. Один лид на сессию (user_id, dialog_id), обновляется при новом контакте."""
    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("user_id", "dialog_id", name="uq_leads_user_dialog"),)

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    dialog_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    contact_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SiteProject(Base):
    __tablename__ = "site_projects"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    source_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    design_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_prompt_global: Mapped[str | None] = mapped_column(Text, nullable=True)
    tone_of_voice: Mapped[str | None] = mapped_column(String(255), nullable=True)
    crawl_depth: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    with_cart: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    crawl_status: Mapped[str] = mapped_column(String(32), default="idle", nullable=False)
    crawl_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    crawl_discovered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    crawl_stop_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    crawl_publish_on_stop: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    crawl_queue_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    crawl_visited_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    crawl_tree_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    crawl_strategy_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    crawl_last_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    rewrite_status: Mapped[str] = mapped_column(String(32), default="idle", nullable=False)
    image_status: Mapped[str] = mapped_column(String(32), default="idle", nullable=False)
    publish_status: Mapped[str] = mapped_column(String(32), default="idle", nullable=False)
    price_check_status: Mapped[str] = mapped_column(String(32), default="idle", nullable=False)
    catalog_prompt_table: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SiteRelease(Base):
    __tablename__ = "site_releases"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    site_project_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), ForeignKey("site_projects.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("site_project_id", "url_path", name="uq_pages_project_path"),)

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    site_project_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), ForeignKey("site_projects.id"), nullable=False, index=True)
    parent_id: Mapped[PyUUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("pages.id"), nullable=True)
    url_path: Mapped[str] = mapped_column(Text, nullable=False)
    full_url: Mapped[str] = mapped_column(Text, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_type: Mapped[str] = mapped_column(String(32), nullable=False, default="generic")
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    transformed_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_texts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    rewritten_texts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    html_structure: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    extraction_source: Mapped[str | None] = mapped_column(String(32), nullable=True, default="fallback")
    raw_ai_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Product(Base):
    __tablename__ = "products"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    site_project_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), ForeignKey("site_projects.id"), nullable=False, index=True)
    page_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), ForeignKey("pages.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    rewritten_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    price_from: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    original_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    rewritten_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    generation_failed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    catalog_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ProductSpec(Base):
    __tablename__ = "product_specs"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    product_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ProductSourceSnapshot(Base):
    __tablename__ = "product_source_snapshots"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    site_project_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), ForeignKey("site_projects.id"), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    product_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_from: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    extraction_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    site_project_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), ForeignKey("site_projects.id"), nullable=False, index=True)
    page_id: Mapped[PyUUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("pages.id"), nullable=True)
    product_id: Mapped[PyUUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="gallery")
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    alt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    generation_failed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SiteDesign(Base):
    __tablename__ = "site_design"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    site_project_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), ForeignKey("site_projects.id"), nullable=False, unique=True, index=True)
    css_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    html_template: Mapped[str] = mapped_column(Text, nullable=False, default="")
    prompt_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CategoryPrompt(Base):
    __tablename__ = "category_prompts"
    __table_args__ = (UniqueConstraint("site_project_id", "name", name="uq_category_prompts"),)

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    site_project_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), ForeignKey("site_projects.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url_pattern: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)


class AIJob(Base):
    __tablename__ = "ai_jobs"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    site_project_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), ForeignKey("site_projects.id"), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    external_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CartSelection(Base):
    __tablename__ = "cart_selections"
    __table_args__ = (UniqueConstraint("session_id", "product_id", name="uq_cart_session_product"),)

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    site_project_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), ForeignKey("site_projects.id"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    product_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PageBlock(Base):
    __tablename__ = "page_blocks"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    page_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), ForeignKey("pages.id"), nullable=False, index=True)
    block_type: Mapped[str] = mapped_column(String(64), nullable=False, default="text")
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CatalogFilterConfig(Base):
    __tablename__ = "catalog_filter_configs"
    __table_args__ = (UniqueConstraint("site_project_id", "spec_key", name="uq_filter_site_spec_key"),)

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    site_project_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), ForeignKey("site_projects.id"), nullable=False, index=True)
    spec_key: Mapped[str] = mapped_column(String(255), nullable=False)
    param_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
