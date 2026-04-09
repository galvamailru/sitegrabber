# AI Chatbot + Site Clone Platform

Проект теперь включает две подсистемы:

- **AI Chatbot** (историческая часть): API чата `/api/chat`.
- **Site Clone Platform**: рекурсивный парсинг каталогов, сохранение структуры в БД, рерайтинг, генерация изображений и публикация копии сайта.

## Основные URL

- Админка: `http://localhost:8080/admin`
- Публичный опубликованный сайт: `http://localhost:8080/`
- Старый чат API: `http://localhost:8080/api/chat`

## Быстрый старт

1. Скопировать `.env.example` в `.env` и задать переменные (в т.ч. `LLM_API_KEY`, параметры PostgreSQL).
2. Запуск через Docker Compose:
   ```bash
   docker-compose up --build
   ```
   Внешний вход через Nginx: http://localhost:8080

3. Локально (без Docker):
   ```bash
   pip install -r requirements.txt
   # PostgreSQL должен быть запущен, переменные в .env
   alembic upgrade head
   uvicorn app.main:app --reload
   ```

## Тесты

```bash
pip install -r requirements.txt
pytest tests -v
```

## Site Clone: сценарий

1. Открыть `/admin`.
2. Создать проект клонирования (URL, глубина, промпты).
3. Запустить `Клонировать` (Celery task `crawl_site_task`).
4. Опционально запустить `Рерайтинг` и `Генерация изображений` (модель `gpt-image-1.5` в `.env`).
5. Нажать `Опубликовать` — релиз станет доступен по URL-структуре донора на `:8080`.
6. Для ускорения доступна кнопка `Run all` (crawl -> rewrite -> images -> publish).
7. На странице проекта доступна модерация проблемных изображений: повторить генерацию, оставить оригинал, удалить.

## Сервисы Docker

- `web` (FastAPI)
- `worker` (Celery)
- `postgres`
- `redis`
- `minio`
- `nginx` (порт `8080`)
