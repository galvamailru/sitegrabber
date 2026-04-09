"""
FastAPI-приложение: API чата (POST /api/chat → SSE) и раздача статики для iframe.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import init_db
from app.routes.clone_admin import router as clone_admin_router
from app.routes.admin import router as admin_router
from app.routes.chat import router as chat_router
from app.routes.published_site import router as published_site_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="AI Chatbot",
    description="Чат с LLM DeepSeek, встраиваемый в iframe. REST + SSE, PostgreSQL.",
    lifespan=lifespan,
)

app.include_router(chat_router)
app.include_router(admin_router)
app.include_router(clone_admin_router)

# Статика для страницы iframe (форма + приём SSE)
static_path = Path(__file__).resolve().parent.parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path), html=True), name="static")


app.include_router(published_site_router)
