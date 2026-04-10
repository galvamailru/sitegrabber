"""
HTTP-клиент к LLM DeepSeek с поддержкой стриминга. URL и ключ только из конфигурации (.env).
"""
import json
from pathlib import Path
from typing import AsyncIterator

import httpx

from app.config import get_settings

# Длинные промпты (каталог в system) и DeepSeek могут отвечать >60s — как в clone_pipeline.
_LLM_HTTP_TIMEOUT = httpx.Timeout(connect=30.0, read=240.0, write=60.0, pool=10.0)


def _chat_api_key_model() -> tuple[str, str]:
    s = get_settings()
    key = (s.LLM_API_KEY or s.DEEPSEEK_API_KEY or "").strip()
    model = (s.LLM_MODEL or s.DEEPSEEK_MODEL or "deepseek-chat").strip()
    return key, model


def load_system_prompt(path: Path) -> str:
    """Читает системный промпт только из файла. Путь из конфигурации."""
    if not path.exists():
        raise FileNotFoundError(f"Файл промпта не найден: {path}")
    return path.read_text(encoding="utf-8").strip()


async def stream_chat(
    messages: list[dict[str, str]],
    *,
    system_prompt: str,
) -> AsyncIterator[str]:
    """
    Вызов DeepSeek chat/completions со stream=True.
    Yields фрагменты content из delta.
    При ошибке LLM пробрасывает httpx.HTTPStatusError (502/503).
    """
    settings = get_settings()
    api_key, model = _chat_api_key_model()
    if not api_key:
        raise RuntimeError("llm_api_key_missing")
    url = f"{settings.LLM_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}, *messages],
        "stream": True,
        "temperature": settings.LLM_TEMPERATURE,
    }
    async with httpx.AsyncClient(timeout=_LLM_HTTP_TIMEOUT) as client:
        async with client.stream("POST", url, json=body, headers=headers) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or line.strip() != line:
                    continue
                if line.startswith("data: "):
                    data = line[6:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield content


async def complete_chat(messages: list[dict[str, str]], *, system_prompt: str) -> str:
    settings = get_settings()
    api_key, model = _chat_api_key_model()
    if not api_key:
        raise RuntimeError("llm_api_key_missing")
    url = f"{settings.LLM_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}, *messages],
        "stream": False,
        "temperature": 0.4,
    }
    async with httpx.AsyncClient(timeout=_LLM_HTTP_TIMEOUT) as client:
        response = await client.post(url, json=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
