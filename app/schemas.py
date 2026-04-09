"""
Pydantic-схемы для API: запрос отправки сообщения.
"""
from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=255, description="Идентификатор пользователя")
    message: str = Field(..., min_length=1, max_length=1000, description="Текст сообщения (не более 1000 символов)")
    dialog_id: str = Field(default="default", max_length=255, description="Идентификатор диалога (опционально)")


class CatalogChatRequest(BaseModel):
    site_project_id: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1, max_length=1500)
