import asyncio
import base64
import json
import re
from collections import deque
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import delete, select

from app.database import async_session_factory
from app.models import Asset, Page, Product, ProductSpec, SiteProject, SiteRelease
from app.config import get_settings
from app.storage import upload_bytes

settings = get_settings()
_worker_loop: asyncio.AbstractEventLoop | None = None


def _strip_tags(html: str) -> str:
    txt = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    txt = re.sub(r"<style[\s\S]*?</style>", " ", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()


def _extract_links(base_url: str, html: str) -> list[str]:
    links = re.findall(r'href\s*=\s*"([^"]+)"', html, flags=re.IGNORECASE)
    out: list[str] = []
    for href in links:
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        out.append(urljoin(base_url, href))
    return out


def _extract_title(html: str) -> str | None:
    m = re.search(r"<title>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip()


def _extract_description(html: str) -> str | None:
    m = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', html, flags=re.IGNORECASE)
    return m.group(1).strip() if m else None


def _extract_og_image(html: str) -> str | None:
    m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html, flags=re.IGNORECASE)
    return m.group(1).strip() if m else None


def _extract_images(base_url: str, html: str) -> list[str]:
    out = []
    attr_links = re.findall(r'(?:src|data-src|data-lazy|data-original|href)\s*=\s*"([^"]+)"', html, flags=re.IGNORECASE)
    css_links = re.findall(r'url\(([^)]+)\)', html, flags=re.IGNORECASE)
    raw_links = re.findall(r'https?://[^\s"\'<>]+', html, flags=re.IGNORECASE)
    links = [*attr_links, *css_links, *raw_links]
    for href in links:
        href = href.strip(" '\"")
        href = href.replace("&amp;", "&")
        if href.startswith("data:"):
            continue
        if not any(ext in href.lower() for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
            continue
        full = urljoin(base_url, href)
        # Skip obvious non-product assets.
        low = full.lower()
        if any(skip in low for skip in ["/favicon", "reviews_", "/logo", "/icon", "sprite"]):
            continue
        out.append(full)
    seen = set()
    uniq = []
    for i in out:
        if i in seen:
            continue
        seen.add(i)
        uniq.append(i)
    return uniq


def _rewrite_html_for_local(source_url: str, html: str) -> str:
    parsed = urlparse(source_url)
    host = parsed.netloc

    def _rewrite_attr(match: re.Match) -> str:
        prefix = match.group(1)
        quote = match.group(2)
        value = match.group(3)
        suffix = match.group(4)

        if value.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            return match.group(0)

        if value.startswith("//"):
            return f"{prefix}{quote}https:{value}{quote}{suffix}"

        if value.startswith("http://") or value.startswith("https://"):
            p = urlparse(value)
            if p.netloc == host:
                local = p.path or "/"
                if p.query:
                    local += f"?{p.query}"
                if p.fragment:
                    local += f"#{p.fragment}"
                return f"{prefix}{quote}{local}{quote}{suffix}"
            return match.group(0)

        if value.startswith("/"):
            return match.group(0)

        return f"{prefix}{quote}/{value}{quote}{suffix}"

    rewritten = re.sub(
        r'((?:href|src|action)\s*=\s*)(["\'])(.*?)(\2)',
        _rewrite_attr,
        html,
        flags=re.IGNORECASE,
    )
    rewritten = rewritten.replace(source_url.rstrip("/"), "")
    rewritten = rewritten.replace(f"https://{host}", "")
    rewritten = rewritten.replace(f"http://{host}", "")
    return rewritten


async def _request_with_retry(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    delay = 1.0
    last_exc: Exception | None = None
    for _ in range(3):
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code < 500:
                return resp
        except Exception as exc:
            last_exc = exc
        await asyncio.sleep(delay)
        delay *= 2
    if last_exc:
        raise last_exc
    return await client.request(method, url, **kwargs)


def _extract_specs(text: str) -> list[tuple[str, str]]:
    specs = []
    for m in re.finditer(r"([А-ЯA-ZЁ][А-ЯA-ZЁа-яa-z0-9\-\s]{2,40})\s*:\s*([^:\n]{2,180})", text):
        k = re.sub(r"\s+", " ", m.group(1)).strip()
        v = re.sub(r"\s+", " ", m.group(2)).strip()
        if k.lower() in {"контакты", "каталог", "отзывы", "ваш номер"}:
            continue
        if any(noise in v.lower() for noise in ["каталог о компании", "перезвоните мне", "код подтверждения"]):
            continue
        specs.append((k, v))
    seen = set()
    uniq = []
    for item in specs:
        if item in seen:
            continue
        seen.add(item)
        uniq.append(item)
    return uniq


def _extract_description_block(text: str) -> str:
    m = re.search(
        r"Описание товара\s*(.*?)\s*(?:Характеристики товара|ОСТАЛИСЬ ВОПРОСЫ|КОНТАКТЫ|Copyright|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return ""
    val = re.sub(r"\s+", " ", m.group(1)).strip()
    return val[:4000]


def _extract_specs_block(text: str) -> str:
    m = re.search(
        r"Характеристики\s*:?\s*(.*?)\s*(?:От\s+[0-9\s]+\s+руб|Рассчитать стоимость|Заказать в 1 клик|Описание товара|ОСТАЛИСЬ ВОПРОСЫ|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        # fallback
        m = re.search(
            r"(Ширина\s*:.*?)(?:ОСТАЛИСЬ ВОПРОСЫ|КОНТАКТЫ|$)",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return ""
    val = re.sub(r"\s+", " ", m.group(1)).strip()
    return val[:2500]


def _slug_from_path(path: str) -> str:
    s = path.strip("/").replace("/", "-")
    return s or "home"


def _try_parse_json(text: str) -> dict | None:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def _extract_structured_with_llm(
    client: httpx.AsyncClient,
    project: SiteProject,
    page_url: str,
    title: str | None,
    text: str,
    image_candidates: list[str],
) -> dict | None:
    if not settings.DEEPSEEK_API_KEY:
        return None
    prompt = (
        "Извлеки структуру страницы в JSON. Ответ строго JSON без пояснений.\n"
        "Схема:\n"
        "{\n"
        '  "page_type":"product|content",\n'
        '  "page_title":"...",\n'
        '  "meta_description":"...",\n'
        '  "product":{\n'
        '    "name":"...",\n'
        '    "price_from":7410,\n'
        '    "currency":"RUB",\n'
        '    "description":"полное описание товара",\n'
        '    "specs":[{"key":"Ширина","value":"от 80 мм до 2500 мм"}],\n'
        '    "gallery":["url1","url2"]\n'
        "  },\n"
        '  "content_page":{"category":"contacts|news|about|other","summary":"..."}\n'
        "}\n"
        "Правила:\n"
        "- Если страница товарная, page_type=product и заполни product.\n"
        "- Если страница контентная, page_type=content и заполни content_page.\n"
        "- specs только реальные технические параметры, без навигации/контактов.\n"
        "- gallery заполни только ссылками на изображения товара.\n\n"
        f"URL: {page_url}\nTITLE: {title or ''}\n"
        f"IMAGE_CANDIDATES: {image_candidates[:40]}\n"
        f"TEXT:\n{text[:10000]}"
    )
    try:
        r = await _request_with_retry(
            client,
            "POST",
            f"{settings.LLM_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}"},
            json={
                "model": settings.DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
        )
        content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = _try_parse_json(content)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
    return None


async def _classify_page(client: httpx.AsyncClient, project: SiteProject, title: str | None, text: str) -> str:
    sample = text[:3000]
    if settings.DEEPSEEK_API_KEY:
        try:
            prompt = (
                "Классифицируй страницу как product или content. "
                "product = карточка/описание товара с характеристиками/ценой. "
                "content = новости, контакты, о компании, статья и т.п. "
                "Ответ только одним словом: product или content.\n\n"
                f"TITLE: {title or ''}\nTEXT: {sample}"
            )
            r = await _request_with_retry(
                client,
                "POST",
                f"{settings.LLM_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}"},
                json={
                    "model": settings.DEEPSEEK_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                },
            )
            content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").lower()
            if "product" in content:
                return "product"
            return "content"
        except Exception:
            pass
    t = (title or "").lower() + " " + sample.lower()
    return "product" if any(x in t for x in ["радиатор", "цена", "характеристик"]) else "content"


async def crawl_project(project_id: str, depth_limit: int = 2) -> int:
    async with async_session_factory() as db:
        project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
        if not project:
            return 0

        await db.execute(delete(Asset).where(Asset.site_project_id == project.id))
        await db.execute(delete(ProductSpec).where(ProductSpec.product_id.in_(select(Product.id).where(Product.site_project_id == project.id))))
        await db.execute(delete(Product).where(Product.site_project_id == project.id))
        await db.execute(delete(Page).where(Page.site_project_id == project.id))
        await db.execute(delete(SiteRelease).where(SiteRelease.site_project_id == project.id))
        await db.commit()

        start = project.source_url.rstrip("/")
        host = urlparse(start).netloc
        q = deque([(start, 0, None)])
        visited = set()
        created = 0

        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            while q:
                url, depth, parent_id = q.popleft()
                normalized = url.split("#")[0].rstrip("/")
                if normalized in visited or depth > depth_limit:
                    continue
                visited.add(normalized)

                try:
                    resp = await client.get(normalized)
                    if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type", ""):
                        continue
                except Exception:
                    continue

                html = resp.text
                path = urlparse(normalized).path or "/"
                text = _strip_tags(html)
                title = _extract_title(html)
                image_candidates = _extract_images(normalized, html)
                ai_struct = await _extract_structured_with_llm(
                    client=client,
                    project=project,
                    page_url=normalized,
                    title=title,
                    text=text,
                    image_candidates=image_candidates,
                )
                page_type = (
                    ai_struct.get("page_type", "").lower()
                    if isinstance(ai_struct, dict)
                    else await _classify_page(client, project, title, text)
                )
                extraction_source = "ai" if isinstance(ai_struct, dict) else "fallback"
                if page_type not in {"product", "content"}:
                    page_type = await _classify_page(client, project, title, text)
                    extraction_source = "fallback"
                transformed_html = _rewrite_html_for_local(start, html)
                page = Page(
                    site_project_id=project.id,
                    parent_id=parent_id,
                    url_path=path,
                    full_url=normalized,
                    depth=depth,
                    title=title,
                    meta_description=_extract_description(html),
                    original_html=html,
                    transformed_html=transformed_html,
                    original_texts={"text": text[:6000]},
                    page_type=page_type,
                    extraction_source=extraction_source,
                    raw_ai_json=ai_struct if isinstance(ai_struct, dict) else None,
                )
                db.add(page)
                await db.flush()
                created += 1

                og_image = _extract_og_image(html)
                if og_image:
                    db.add(Asset(site_project_id=project.id, page_id=page.id, role="main", source_url=og_image))
                gallery_from_ai = []
                if isinstance(ai_struct, dict):
                    gallery_from_ai = (
                        (ai_struct.get("product") or {}).get("gallery") or []
                        if isinstance(ai_struct.get("product"), dict)
                        else []
                    )
                image_list = gallery_from_ai or image_candidates
                for i, image_url in enumerate(image_list):
                    db.add(
                        Asset(
                            site_project_id=project.id,
                            page_id=page.id,
                            role="main" if i == 0 else "gallery",
                            source_url=image_url,
                        )
                    )

                if page.page_type == "product":
                    ai_product = (ai_struct.get("product") or {}) if isinstance(ai_struct, dict) else {}
                    price = None
                    if isinstance(ai_product, dict) and ai_product.get("price_from") is not None:
                        try:
                            price = float(str(ai_product.get("price_from")).replace(" ", ""))
                        except ValueError:
                            price = None
                    if price is None:
                        pm = re.search(r"(?:от|От)\s+([0-9\s]+)\s*(?:₽|руб)", text)
                        if pm:
                            try:
                                price = float(pm.group(1).replace(" ", ""))
                            except ValueError:
                                price = None
                    description_block = (
                        ai_product.get("description")
                        if isinstance(ai_product, dict) and ai_product.get("description")
                        else _extract_description_block(text)
                    )
                    specs_block = _extract_specs_block(text)
                    product = Product(
                        site_project_id=project.id,
                        page_id=page.id,
                        name=(
                            ai_product.get("name")
                            if isinstance(ai_product, dict) and ai_product.get("name")
                            else (page.title or path)
                        ),
                        slug=_slug_from_path(path),
                        price_from=price,
                        currency=(
                            ai_product.get("currency")
                            if isinstance(ai_product, dict) and ai_product.get("currency")
                            else "RUB"
                        ),
                        original_description=(description_block or page.meta_description or "")[:4000],
                        rewritten_description=(description_block or page.meta_description or "")[:4000],
                    )
                    db.add(product)
                    await db.flush()
                    ai_specs: list[tuple[str, str]] = []
                    if isinstance(ai_product, dict) and isinstance(ai_product.get("specs"), list):
                        for s in ai_product.get("specs"):
                            if isinstance(s, dict) and s.get("key") and s.get("value"):
                                ai_specs.append((str(s.get("key")).strip(), str(s.get("value")).strip()))
                    specs_pairs = ai_specs or _extract_specs(specs_block or text)
                    for idx, (k, v) in enumerate(specs_pairs):
                        db.add(ProductSpec(product_id=product.id, key=k, value=v, sort_order=idx))
                    # Link already collected page assets to this product.
                    page_assets = (
                        await db.execute(select(Asset).where(Asset.page_id == page.id).order_by(Asset.created_at))
                    ).scalars().all()
                    for a in page_assets:
                        a.product_id = product.id

                for link in _extract_links(normalized, html):
                    p = urlparse(link)
                    if p.netloc != host:
                        continue
                    q.append((f"{p.scheme}://{p.netloc}{p.path}", depth + 1, page.id))

            release = SiteRelease(site_project_id=project.id, status="draft", is_active=False)
            db.add(release)
            await db.commit()
        return created


async def rewrite_project(project_id: str) -> int:
    async with async_session_factory() as db:
        project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
        products = (await db.execute(select(Product).where(Product.site_project_id == project_id))).scalars().all()
        client = httpx.AsyncClient(timeout=30)
        for p in products:
            if settings.DEEPSEEK_API_KEY and p.original_description:
                try:
                    prompt = (
                        f"Перепиши описание товара в тоне '{project.tone_of_voice or 'профессиональный'}'. "
                        f"Сохрани технический смысл и факты.\n\nТекст:\n{p.original_description}"
                    )
                    resp = await _request_with_retry(
                        client,
                        "POST",
                        f"{settings.LLM_URL.rstrip('/')}/chat/completions",
                        headers={"Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}"},
                        json={
                            "model": settings.DEEPSEEK_MODEL,
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.5,
                        },
                    )
                    data = resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", p.original_description)
                    p.rewritten_description = content.strip()
                except Exception:
                    p.rewritten_description = p.original_description
            else:
                p.rewritten_description = p.original_description
            p.rewritten_name = p.name
        await client.aclose()
        await db.commit()
        return len(products)


async def regenerate_images(project_id: str) -> int:
    async with async_session_factory() as db:
        project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
        assets = (await db.execute(select(Asset).where(Asset.site_project_id == project_id))).scalars().all()
        client = httpx.AsyncClient(timeout=60)
        for a in assets:
            if settings.OPENAI_API_KEY:
                try:
                    prompt = (
                        f"{project.image_prompt_global or 'Same product, modern interior, minimal style'}. "
                        f"Keep product identity."
                    )
                    r = await client.post(
                        "https://api.openai.com/v1/images/generations",
                        headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                        json={"model": settings.OPENAI_IMAGE_MODEL, "prompt": prompt, "size": "1024x1024"},
                    )
                    payload = r.json()
                    item = payload.get("data", [{}])[0]
                    b64 = item.get("b64_json")
                    image_url = item.get("url")
                    if b64:
                        img_bytes = base64.b64decode(b64)
                        storage_key, local_url = upload_bytes(img_bytes, "image/png", "generated")
                        a.storage_key = storage_key
                        a.local_url = local_url
                        a.generated = True
                        a.generation_failed = False
                    elif image_url:
                        img_resp = await _request_with_retry(client, "GET", image_url)
                        storage_key, local_url = upload_bytes(img_resp.content, "image/png", "generated")
                        a.storage_key = storage_key
                        a.local_url = local_url
                        a.generated = True
                        a.generation_failed = False
                    else:
                        a.generated = True
                        a.local_url = a.source_url
                except Exception as e:
                    a.generation_failed = True
                    a.failure_reason = str(e)[:500]
                    a.generated = False
            else:
                a.generated = True
                if a.source_url and not a.local_url:
                    try:
                        img_resp = await _request_with_retry(client, "GET", a.source_url)
                        storage_key, local_url = upload_bytes(img_resp.content, "image/png", "original")
                        a.storage_key = storage_key
                        a.local_url = local_url
                    except Exception:
                        a.local_url = a.source_url
        await db.commit()
        await client.aclose()
        return len(assets)


async def regenerate_single_asset(asset_id: str) -> bool:
    async with async_session_factory() as db:
        asset = await db.scalar(select(Asset).where(Asset.id == asset_id))
        if not asset:
            return False
        project = await db.scalar(select(SiteProject).where(SiteProject.id == asset.site_project_id))
        client = httpx.AsyncClient(timeout=60)
        try:
            if settings.OPENAI_API_KEY:
                prompt = (
                    f"{project.image_prompt_global or 'Same product, modern interior, minimal style'}. "
                    f"Keep product identity."
                )
                r = await _request_with_retry(
                    client,
                    "POST",
                    "https://api.openai.com/v1/images/generations",
                    headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                    json={"model": settings.OPENAI_IMAGE_MODEL, "prompt": prompt, "size": "1024x1024"},
                )
                payload = r.json()
                item = payload.get("data", [{}])[0]
                b64 = item.get("b64_json")
                image_url = item.get("url")
                if b64:
                    img_bytes = base64.b64decode(b64)
                    storage_key, local_url = upload_bytes(img_bytes, "image/png", "generated")
                    asset.storage_key = storage_key
                    asset.local_url = local_url
                    asset.generated = True
                    asset.generation_failed = False
                    asset.failure_reason = None
                elif image_url:
                    img_resp = await _request_with_retry(client, "GET", image_url)
                    storage_key, local_url = upload_bytes(img_resp.content, "image/png", "generated")
                    asset.storage_key = storage_key
                    asset.local_url = local_url
                    asset.generated = True
                    asset.generation_failed = False
                    asset.failure_reason = None
                else:
                    asset.generation_failed = True
                    asset.failure_reason = "empty_response"
                    asset.generated = False
            await db.commit()
            return True
        except Exception as e:
            asset.generation_failed = True
            asset.failure_reason = str(e)[:500]
            asset.generated = False
            await db.commit()
            return False
        finally:
            await client.aclose()


async def publish_project(project_id: str) -> None:
    async with async_session_factory() as db:
        releases = (await db.execute(select(SiteRelease).where(SiteRelease.site_project_id == project_id))).scalars().all()
        for r in releases:
            r.is_active = False
        rel = SiteRelease(site_project_id=project_id, status="published", is_active=True)
        db.add(rel)
        await db.commit()


def run_async(coro):
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_worker_loop)
    return _worker_loop.run_until_complete(coro)
