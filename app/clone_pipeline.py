import asyncio
import base64
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
    links = re.findall(r'(?:src|data-src)\s*=\s*"([^"]+)"', html, flags=re.IGNORECASE)
    out = []
    for href in links:
        if href.startswith("data:"):
            continue
        if any(href.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]) or "creatium.io" in href:
            out.append(urljoin(base_url, href))
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
    for m in re.finditer(r"([А-ЯA-ZЁ][А-ЯA-ZЁа-яa-z0-9\-\s]{2,40})\s*:\s*([^:]{2,120})", text):
        k = re.sub(r"\s+", " ", m.group(1)).strip()
        v = re.sub(r"\s+", " ", m.group(2)).strip()
        if k.lower() in {"контакты", "каталог", "отзывы"}:
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


def _slug_from_path(path: str) -> str:
    s = path.strip("/").replace("/", "-")
    return s or "home"


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
                page_type = await _classify_page(client, project, _extract_title(html), text)
                transformed_html = _rewrite_html_for_local(start, html)
                page = Page(
                    site_project_id=project.id,
                    parent_id=parent_id,
                    url_path=path,
                    full_url=normalized,
                    depth=depth,
                    title=_extract_title(html),
                    meta_description=_extract_description(html),
                    original_html=html,
                    transformed_html=transformed_html,
                    original_texts={"text": text[:6000]},
                    page_type=page_type,
                )
                db.add(page)
                await db.flush()
                created += 1

                og_image = _extract_og_image(html)
                if og_image:
                    db.add(Asset(site_project_id=project.id, page_id=page.id, role="main", source_url=og_image))
                for i, image_url in enumerate(_extract_images(normalized, html)):
                    db.add(
                        Asset(
                            site_project_id=project.id,
                            page_id=page.id,
                            role="main" if i == 0 else "gallery",
                            source_url=image_url,
                        )
                    )

                if page.page_type == "product":
                    price = None
                    pm = re.search(r"(?:от|От)\s+([0-9\s]+)\s*(?:₽|руб)", text)
                    if pm:
                        try:
                            price = float(pm.group(1).replace(" ", ""))
                        except ValueError:
                            price = None
                    product = Product(
                        site_project_id=project.id,
                        page_id=page.id,
                        name=page.title or path,
                        slug=_slug_from_path(path),
                        price_from=price,
                        currency="RUB",
                        original_description=(page.meta_description or "")[:2000],
                        rewritten_description=(page.meta_description or "")[:2000],
                    )
                    db.add(product)
                    await db.flush()
                    for idx, (k, v) in enumerate(_extract_specs(text)):
                        db.add(ProductSpec(product_id=product.id, key=k, value=v, sort_order=idx))

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
