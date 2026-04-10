import asyncio
import base64
import json
import logging
import re
from collections import deque
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import delete, select

from app.database import async_session_factory
from app.models import Asset, Page, Product, ProductSourceSnapshot, ProductSpec, SiteProject, SiteRelease
from app.config import get_settings
from app.storage import upload_bytes

settings = get_settings()
_worker_loop: asyncio.AbstractEventLoop | None = None
logger = logging.getLogger("sitegrabber.clone_pipeline")

# Краулер использует короткий timeout (20s) для HTML; DeepSeek часто отвечает дольше — иначе ReadTimeout при чтении body.
_LLM_HTTP_TIMEOUT = httpx.Timeout(connect=30.0, read=240.0, write=60.0, pool=10.0)


def _strip_tags(html: str) -> str:
    txt = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    txt = re.sub(r"<style[\s\S]*?</style>", " ", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()


def _extract_links(base_url: str, html: str) -> list[str]:
    links = re.findall(r'href\s*=\s*"([^"]+)"', html, flags=re.IGNORECASE)
    out: list[str] = []
    non_html_ext = (
        ".js",
        ".css",
        ".json",
        ".xml",
        ".txt",
        ".pdf",
        ".zip",
        ".rar",
        ".7z",
        ".tar",
        ".gz",
        ".mp4",
        ".webm",
        ".mp3",
        ".wav",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".svg",
        ".ico",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
    )
    for href in links:
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        full = urljoin(base_url, href)
        p = urlparse(full)
        low_path = (p.path or "").lower()
        if any(seg in low_path for seg in ["/ajax/", "/api/", "/bitrix/"]):
            continue
        if low_path.endswith(non_html_ext):
            continue
        out.append(full)
    return out


def _normalize_crawl_url(url: str) -> str:
    u = (url or "").split("#")[0].strip()
    if not u:
        return u
    p = urlparse(u)
    scheme = p.scheme or "https"
    netloc = p.netloc
    path = p.path or "/"
    if path != "/":
        path = path.rstrip("/")
    norm = f"{scheme}://{netloc}{path}"
    if p.query:
        norm += f"?{p.query}"
    return norm


def _is_product_like_path(path: str) -> bool:
    p = (path or "/").lower().rstrip("/") or "/"
    product_patterns = [
        r"/product",
        r"/products",
        r"/catalog",
        r"/item",
        r"/detail",
        r"/card",
        r"/shop",
        r"/goods",
    ]
    if any(re.search(pattern, p) for pattern in product_patterns):
        return True
    if re.search(r"/\d{4,}", p):
        return True
    return False


def _is_service_like_path(path: str) -> bool:
    p = (path or "/").lower()
    service_tokens = [
        "/service",
        "/services",
        "/about",
        "/company",
        "/contact",
        "/delivery",
        "/payment",
        "/warranty",
        "/faq",
        "/blog",
        "/news",
    ]
    return any(tok in p for tok in service_tokens)


def _classify_by_rules(url_path: str, title: str | None, text: str) -> str | None:
    p = (url_path or "/").lower()
    t = ((title or "") + " " + text[:2000]).lower()
    product_tokens = ["цена", "руб", "характерист", "модель", "артикул", "vin", "sku", "л.с.", "двигател"]
    content_tokens = ["о компании", "контакты", "доставка", "оплата", "новости", "статья", "блог", "услуги"]
    product_score = sum(1 for tok in product_tokens if tok in t) + (2 if _is_product_like_path(p) else 0)
    content_score = sum(1 for tok in content_tokens if tok in t) + (2 if _is_service_like_path(p) else 0)
    if product_score >= max(content_score + 1, 2):
        return "product"
    if content_score >= max(product_score + 1, 2):
        return "content"
    return None


async def _discover_site_strategy(client: httpx.AsyncClient, start_url: str, host: str) -> dict[str, Any]:
    strategy: dict[str, Any] = {
        "mode": "mixed",
        "product_url_patterns": [],
        "service_url_patterns": [],
        "exclude_path_tokens": ["/ajax/", "/api/", "/bitrix/"],
    }
    samples: list[tuple[str, str, str]] = []
    queue = deque([start_url])
    seen: set[str] = set()
    while queue and len(samples) < 20:
        u = queue.popleft()
        if u in seen:
            continue
        seen.add(u)
        try:
            r = await _request_with_retry(client, "GET", u)
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                continue
            html = r.text
            path = urlparse(u).path or "/"
            txt = _strip_tags(html)
            samples.append((path, _extract_title(html) or "", txt))
            for link in _extract_links(u, html):
                p = urlparse(link)
                if p.netloc != host:
                    continue
                if len(queue) >= 100:
                    break
                queue.append(f"{p.scheme}://{p.netloc}{p.path}")
        except Exception:
            continue

    product_like = 0
    service_like = 0
    detail_like = 0
    for path, title, txt in samples:
        if _is_product_like_path(path) or _classify_by_rules(path, title, txt) == "product":
            product_like += 1
        if _is_service_like_path(path) or _classify_by_rules(path, title, txt) == "content":
            service_like += 1
        if re.search(r"/detail/\d+/?$", path.lower().rstrip("/") + "/"):
            detail_like += 1

    if product_like >= max(service_like * 2, 4):
        strategy["mode"] = "catalog-first"
    elif service_like >= max(product_like * 2, 4):
        strategy["mode"] = "content-first"
    else:
        strategy["mode"] = "mixed"

    if detail_like >= 2:
        strategy["product_url_patterns"].append(r"^/.*/detail/\d+/?$")
    strategy["service_url_patterns"] = [r"^/(about|company|contacts?|service|services|delivery|payment|news|blog)(/.*)?$"]
    strategy["samples"] = {"total": len(samples), "product_like": product_like, "service_like": service_like}
    # Let LLM refine strategy when possible.
    if settings.DEEPSEEK_API_KEY and samples:
        prompt = (
            "Определи оптимальную стратегию краулинга сайта для извлечения каталога товаров и полезных инфо-страниц.\n"
            "Верни строго JSON вида:\n"
            '{'
            '"mode":"catalog-first|content-first|mixed",'
            '"product_url_patterns":["regex1"],'
            '"service_url_patterns":["regex1"],'
            '"exclude_path_tokens":["/ajax/","/api/"]'
            '}\n'
            "Не добавляй комментариев. Не добавляй markdown.\n\n"
            f"HOST: {host}\n"
            f"HEURISTIC_MODE: {strategy['mode']}\n"
            f"SAMPLES_STATS: {strategy['samples']}\n"
            f"SAMPLES_URLS:\n" + "\n".join([f"- {path}" for path, _, _ in samples[:20]])
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
                timeout=_LLM_HTTP_TIMEOUT,
            )
            parsed = _try_parse_json(r.json().get("choices", [{}])[0].get("message", {}).get("content", ""))
            if isinstance(parsed, dict):
                mode = str(parsed.get("mode", strategy["mode"]))
                if mode in {"catalog-first", "content-first", "mixed"}:
                    strategy["mode"] = mode
                if isinstance(parsed.get("product_url_patterns"), list):
                    strategy["product_url_patterns"] = [str(x) for x in parsed["product_url_patterns"][:30]]
                if isinstance(parsed.get("service_url_patterns"), list):
                    strategy["service_url_patterns"] = [str(x) for x in parsed["service_url_patterns"][:30]]
                if isinstance(parsed.get("exclude_path_tokens"), list):
                    strategy["exclude_path_tokens"] = [str(x).lower() for x in parsed["exclude_path_tokens"][:30]]
                strategy["selector"] = "llm"
        except Exception:
            strategy["selector"] = "heuristic"
    else:
        strategy["selector"] = "heuristic"
    return strategy


async def discover_project_strategy(project_id: str) -> dict[str, Any]:
    async with async_session_factory() as db:
        project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
        if not project:
            return {"mode": "mixed", "samples": {"total": 0, "product_like": 0, "service_like": 0}}
        start = project.source_url.rstrip("/")
        host = urlparse(start).netloc
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            strategy = await _discover_site_strategy(client, start, host)
        project.crawl_strategy_state = _json_copy(strategy)
        await db.commit()
        return strategy


def _match_any_regex(path: str, patterns: list[str]) -> bool:
    return any(re.search(p, path) for p in patterns)


def _should_enqueue_link(start_host: str, link_host: str, link_path: str, strategy: dict[str, Any] | None = None) -> bool:
    if link_host != start_host:
        return False
    path = (link_path or "/").rstrip("/") or "/"
    low_path = path.lower()
    if strategy:
        for token in strategy.get("exclude_path_tokens", []):
            if token in low_path:
                return False
        mode = strategy.get("mode", "mixed")
        product_patterns = strategy.get("product_url_patterns", [])
        service_patterns = strategy.get("service_url_patterns", [])
        if mode == "catalog-first":
            return (
                path == "/"
                or _match_any_regex(path, product_patterns)
                or _match_any_regex(path, service_patterns)
                or _is_product_like_path(path)
                or _is_service_like_path(path)
            )
        if mode == "content-first":
            return path == "/" or _match_any_regex(path, service_patterns) or _is_service_like_path(path)
    return True


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
    icon_noise_tokens = [
        "/favicon",
        "favicon",
        "apple-touch-icon",
        "android-chrome",
        "/logo",
        "logo.",
        "/icon",
        "icon-",
        "sprite",
        "/sprites",
        "/icons/",
        "/icon/",
        "badge",
        "payment",
        "visa",
        "mastercard",
        "mir",
        "paypal",
        "social",
        "facebook",
        "vk",
        "telegram",
        "whatsapp",
        "instagram",
        "youtube",
        "tiktok",
        "arrow",
        "chevron",
        "close",
        "menu",
        "search",
        "cart",
        "basket",
        "loader",
        "spinner",
        "placeholder",
    ]
    for href in links:
        href = href.strip(" '\"")
        href = href.replace("&amp;", "&")
        if href.startswith("data:"):
            continue
        low_raw = href.lower()
        if any(ext in low_raw for ext in [".svg", ".ico"]):
            continue
        if not any(ext in href.lower() for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
            continue
        full = urljoin(base_url, href)
        # Skip obvious non-product assets.
        low = full.lower()
        if "reviews_" in low:
            continue
        if any(tok in low for tok in icon_noise_tokens):
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


def _rank_product_images(page_url: str, url_path: str, candidates: list[str]) -> list[str]:
    """
    Heuristic ranking to prefer product photos over UI icons/badges.
    Works purely on URL patterns to avoid extra network requests.
    """
    if not candidates:
        return []
    parsed = urlparse(page_url)
    host = parsed.netloc.lower()
    slug = (url_path.strip("/").split("/")[-1] or "").lower()
    slug_tokens = [t for t in re.split(r"[^a-z0-9а-яё]+", slug) if len(t) >= 3]
    bad = [
        "favicon",
        "logo",
        "icon",
        "sprite",
        "badge",
        "payment",
        "visa",
        "mastercard",
        "mir",
        "social",
        "telegram",
        "whatsapp",
        "instagram",
        "facebook",
        "vk",
        "youtube",
        "tiktok",
        "arrow",
        "chevron",
        "close",
        "menu",
        "search",
        "cart",
        "basket",
        "loader",
        "spinner",
        "placeholder",
    ]
    good = ["product", "catalog", "item", "goods", "upload", "images", "img"]

    scored: list[tuple[int, str]] = []
    for u in candidates:
        low = u.lower()
        score = 0
        try:
            p = urlparse(u)
            if p.netloc.lower() == host:
                score += 2
        except Exception:
            pass
        if any(b in low for b in bad):
            score -= 5
        if any(g in low for g in good):
            score += 2
        for t in slug_tokens[:6]:
            if t and t in low:
                score += 3
        if any(x in low for x in ["-150x", "-300x", "_thumb", "thumb", "icon-"]):
            score -= 1
        scored.append((score, u))

    scored.sort(key=lambda x: x[0], reverse=True)
    # Keep only images with some positive signal, but fallback to top few if all are weak.
    strong = [u for s, u in scored if s >= 1]
    return (strong or [u for _, u in scored])[:12]


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
        logger.info(
            "ai_call_skipped purpose=extract_structured url=%s reason=no_deepseek_api_key",
            page_url,
        )
        return None
    logger.info(
        "ai_call_start purpose=extract_structured url=%s title=%s candidates=%d",
        page_url,
        (title or "")[:120],
        len(image_candidates),
    )
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
            timeout=_LLM_HTTP_TIMEOUT,
        )
        content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = _try_parse_json(content)
        if isinstance(parsed, dict):
            logger.info(
                "ai_call_done purpose=extract_structured url=%s response_valid=true page_type=%s has_product=%s",
                page_url,
                str(parsed.get("page_type", "")),
                "product" in parsed,
            )
            return parsed
        logger.warning(
            "ai_call_done purpose=extract_structured url=%s response_valid=false parse_error=invalid_json_snippet",
            page_url,
        )
    except Exception:
        logger.exception(
            "ai_call_error purpose=extract_structured url=%s response_valid=false",
            page_url,
        )
        return None
    return None


async def _classify_page(client: httpx.AsyncClient, project: SiteProject, title: str | None, text: str) -> str:
    sample = text[:3000]
    if settings.DEEPSEEK_API_KEY:
        logger.info(
            "ai_call_start purpose=classify_page title=%s sample_len=%d",
            (title or "")[:120],
            len(sample),
        )
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
                timeout=_LLM_HTTP_TIMEOUT,
            )
            content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").lower()
            if "product" in content:
                logger.info("ai_call_done purpose=classify_page response_valid=true result=product")
                return "product"
            logger.info("ai_call_done purpose=classify_page response_valid=true result=content")
            return "content"
        except Exception:
            logger.exception("ai_call_error purpose=classify_page response_valid=false")
            pass
    t = (title or "").lower() + " " + sample.lower()
    result = "product" if any(x in t for x in ["радиатор", "цена", "характеристик"]) else "content"
    logger.info("ai_call_skipped purpose=classify_page response_valid=n/a fallback_used=true result=%s", result)
    return result


def _extract_price_from_text(text: str) -> float | None:
    pm = re.search(r"(?:от|От)\s+([0-9\s]+)\s*(?:₽|руб)", text)
    if not pm:
        pm = re.search(r"([0-9\s]{3,})\s*(?:₽|руб)", text)
    if not pm:
        return None
    try:
        return float(pm.group(1).replace(" ", ""))
    except ValueError:
        return None


def _append_tree_node(tree: dict[str, Any], *, url: str, depth: int, parent: str | None, state: str) -> None:
    nodes = tree.setdefault("nodes", [])
    if len(nodes) >= 3000:
        return
    nodes.append({"url": url, "depth": depth, "parent": parent, "state": state})


def _json_copy(obj: dict[str, Any]) -> dict[str, Any]:
    # Force a fresh JSON object so SQLAlchemy reliably persists JSONB changes.
    return json.loads(json.dumps(obj, ensure_ascii=False))


async def crawl_project(project_id: str, depth_limit: int = 2, resume: bool = False) -> int:
    async with async_session_factory() as db:
        project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
        if not project:
            return 0

        start = _normalize_crawl_url(project.source_url)
        host = urlparse(start).netloc
        q: deque[tuple[str, int, str | None]]
        visited: set[str]
        enqueued: set[str]
        tree_state: dict[str, Any]
        strategy_state: dict[str, Any]
        created = project.crawl_processed or 0

        can_resume = (
            resume
            and isinstance(project.crawl_queue_state, dict)
            and isinstance(project.crawl_visited_state, dict)
            and (project.crawl_queue_state.get("items") or [])
        )
        if can_resume:
            q = deque(
                (
                    str(i.get("url", "")),
                    int(i.get("depth", 0)),
                    str(i.get("parent_id")) if i.get("parent_id") else None,
                )
                for i in project.crawl_queue_state.get("items", [])
                if i.get("url")
            )
            visited = set(project.crawl_visited_state.get("items", []))
            enqueued = set(_normalize_crawl_url(u) for u, _, _ in q)
            tree_state = project.crawl_tree_state if isinstance(project.crawl_tree_state, dict) else {"nodes": []}
            strategy_state = project.crawl_strategy_state if isinstance(project.crawl_strategy_state, dict) else {"mode": "mixed"}
            project.crawl_status = "running"
        else:
            await db.execute(delete(Asset).where(Asset.site_project_id == project.id))
            await db.execute(
                delete(ProductSpec).where(ProductSpec.product_id.in_(select(Product.id).where(Product.site_project_id == project.id)))
            )
            await db.execute(delete(Product).where(Product.site_project_id == project.id))
            await db.execute(delete(Page).where(Page.site_project_id == project.id))
            await db.execute(delete(SiteRelease).where(SiteRelease.site_project_id == project.id))
            project.crawl_processed = 0
            project.crawl_discovered = 1
            project.crawl_last_url = None
            q = deque([(start, 0, None)])
            visited = set()
            enqueued = {start}
            tree_state = {"nodes": [{"url": start, "depth": 0, "parent": None, "state": "queued"}]}
            strategy_state = {"mode": "mixed"}
            created = 0
        project.crawl_stop_requested = False
        await db.commit()

        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            if not can_resume:
                try:
                    strategy_state = await _discover_site_strategy(client, start, host)
                except Exception:
                    strategy_state = {"mode": "mixed"}
                project.crawl_strategy_state = _json_copy(strategy_state)
                await db.commit()
            while q:
                project.crawl_discovered = max(project.crawl_discovered, len(visited) + len(q))
                project.crawl_queue_state = {
                    "items": [{"url": u, "depth": d, "parent_id": pid} for u, d, pid in list(q)[:1000]]
                }
                project.crawl_visited_state = {"items": list(visited)[:1000]}
                project.crawl_tree_state = _json_copy(tree_state)
                project.crawl_strategy_state = _json_copy(strategy_state)
                await db.commit()
                await db.refresh(project, attribute_names=["crawl_stop_requested", "crawl_publish_on_stop"])
                if project.crawl_stop_requested:
                    project.crawl_status = "stopped"
                    await db.commit()
                    if project.crawl_publish_on_stop:
                        project.publish_status = "running"
                        await db.commit()
                        await publish_project(project_id)
                        project.publish_status = "done"
                        project.crawl_publish_on_stop = False
                        await db.commit()
                    return created

                url, depth, parent_id = q.popleft()
                normalized = _normalize_crawl_url(url)
                pnorm = urlparse(normalized)
                low_path = (pnorm.path or "").lower()
                if any(seg in low_path for seg in ["/ajax/", "/api/", "/bitrix/"]):
                    _append_tree_node(tree_state, url=normalized, depth=depth, parent=parent_id, state="skipped")
                    continue
                if normalized in visited or depth > depth_limit:
                    continue
                visited.add(normalized)

                try:
                    resp = await client.get(normalized)
                    if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type", ""):
                        _append_tree_node(tree_state, url=normalized, depth=depth, parent=parent_id, state="skipped")
                        continue
                except Exception:
                    _append_tree_node(tree_state, url=normalized, depth=depth, parent=parent_id, state="failed")
                    continue
                project.crawl_last_url = normalized
                _append_tree_node(
                    tree_state,
                    url=normalized,
                    depth=depth,
                    parent=parent_id,
                    state="processing",
                )

                html = resp.text
                path = urlparse(normalized).path or "/"
                text = _strip_tags(html)
                title = _extract_title(html)
                image_candidates = _extract_images(normalized, html)
                ai_struct = None
                # Do not spend LLM budget on utility/technical endpoints.
                if not any(seg in low_path for seg in ["/ajax/", "/api/", "/bitrix/"]):
                    ai_struct = await _extract_structured_with_llm(
                        client=client,
                        project=project,
                        page_url=normalized,
                        title=title,
                        text=text,
                        image_candidates=image_candidates,
                    )
                page_type = (
                    ai_struct.get("page_type", "").lower() if isinstance(ai_struct, dict) else None
                )
                if not page_type:
                    page_type = _classify_by_rules(path, title, text) or await _classify_page(client, project, title, text)
                extraction_source = "ai" if isinstance(ai_struct, dict) else "fallback"
                if page_type not in {"product", "content"}:
                    logger.warning(
                        "page_type_invalid url=%s source=%s value=%s fallback_to_classify=true",
                        normalized,
                        extraction_source,
                        page_type,
                    )
                    page_type = await _classify_page(client, project, title, text)
                    extraction_source = "fallback"
                logger.info(
                    "page_extraction_result url=%s page_type=%s source=%s ai_valid=%s",
                    normalized,
                    page_type,
                    extraction_source,
                    str(isinstance(ai_struct, dict)).lower(),
                )
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
                project.crawl_processed = created
                _append_tree_node(tree_state, url=normalized, depth=depth, parent=parent_id, state="processed")
                # Persist progress incrementally so UI polling sees movement.
                await db.commit()

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
                image_list = gallery_from_ai or _rank_product_images(normalized, path, image_candidates)
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
                        price = _extract_price_from_text(text)
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
                        source_url=normalized,
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
                    db.add(
                        ProductSourceSnapshot(
                            site_project_id=project.id,
                            source_url=normalized,
                            product_name=product.name,
                            price_from=price,
                            currency=product.currency,
                            extraction_source=extraction_source,
                        )
                    )
                    ai_specs: list[tuple[str, str]] = []
                    if isinstance(ai_product, dict) and isinstance(ai_product.get("specs"), list):
                        for s in ai_product.get("specs"):
                            if isinstance(s, dict) and s.get("key") and s.get("value"):
                                ai_specs.append((str(s.get("key")).strip(), str(s.get("value")).strip()))
                    specs_pairs = ai_specs or _extract_specs(specs_block or text)
                    logger.info(
                        "product_extraction_result url=%s source=%s specs_count=%d gallery_count=%d desc_len=%d",
                        normalized,
                        extraction_source,
                        len(specs_pairs),
                        len(image_list),
                        len((description_block or "")),
                    )
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
                    if not _should_enqueue_link(host, p.netloc, p.path, strategy_state):
                        continue
                    child_url = _normalize_crawl_url(f"{p.scheme}://{p.netloc}{p.path}")
                    if child_url in visited or child_url in enqueued:
                        continue
                    enqueued.add(child_url)
                    q.append((child_url, depth + 1, str(page.id)))
                    _append_tree_node(tree_state, url=child_url, depth=depth + 1, parent=str(page.id), state="queued")
                project.crawl_discovered = max(project.crawl_discovered, len(visited) + len(q))
                project.crawl_tree_state = _json_copy(tree_state)
                await db.commit()

            release = SiteRelease(site_project_id=project.id, status="draft", is_active=False)
            db.add(release)
            project.crawl_queue_state = None
            project.crawl_visited_state = None
            project.crawl_publish_on_stop = False
            project.crawl_stop_requested = False
            project.crawl_status = "done"
            project.crawl_tree_state = _json_copy(tree_state)
            await db.commit()
        return created


async def check_prices_project(project_id: str) -> dict:
    async with async_session_factory() as db:
        project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
        if not project:
            return {"checked": 0, "changed": 0}
        products = (
            await db.execute(
                select(Product)
                .where(Product.site_project_id == project.id, Product.source_url.is_not(None))
                .order_by(Product.updated_at.desc())
            )
        ).scalars().all()
        checked = 0
        changed = 0
        failed = 0
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for product in products:
                source_url = (product.source_url or "").strip()
                if not source_url:
                    continue
                try:
                    resp = await _request_with_retry(client, "GET", source_url)
                    if resp.status_code != 200:
                        failed += 1
                        continue
                    text = _strip_tags(resp.text)
                    new_price = _extract_price_from_text(text)
                    old_price = product.price_from
                    db.add(
                        ProductSourceSnapshot(
                            site_project_id=project.id,
                            source_url=source_url,
                            product_name=product.name,
                            price_from=new_price,
                            currency=product.currency or "RUB",
                            extraction_source="price-check",
                        )
                    )
                    checked += 1
                    if new_price is not None and old_price is not None and abs(new_price - old_price) > 0.01:
                        changed += 1
                        product.price_from = new_price
                    elif new_price is not None and old_price is None:
                        changed += 1
                        product.price_from = new_price
                except Exception:
                    failed += 1
                    continue
            await db.commit()
        return {"checked": checked, "changed": changed, "failed": failed}


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
                        timeout=_LLM_HTTP_TIMEOUT,
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
        project = await db.scalar(select(SiteProject).where(SiteProject.id == project_id))
        if project:
            product_rows = (
                await db.execute(
                    select(Product)
                    .where(Product.site_project_id == project_id)
                    .order_by(Product.created_at.desc())
                    .limit(500)
                )
            ).scalars().all()
            specs_rows = (
                await db.execute(
                    select(ProductSpec.product_id, ProductSpec.key, ProductSpec.value)
                    .join(Product, Product.id == ProductSpec.product_id)
                    .where(Product.site_project_id == project_id)
                    .order_by(ProductSpec.sort_order)
                )
            ).all()
            by_product_specs: dict[str, list[str]] = {}
            for r in specs_rows:
                pid = str(r.product_id)
                by_product_specs.setdefault(pid, [])
                if len(by_product_specs[pid]) < 12:
                    by_product_specs[pid].append(f"{r.key}: {r.value}")

            lines = [
                "Полный каталог товаров (после публикации). Опирайся на названия, цены, описания и характеристики, а не на URL.",
                "| Название | Категория | Цена от | Валюта | Краткое описание | Характеристики | На витрине |",
                "| --- | --- | ---:| --- | --- | --- | --- |",
            ]

            def _one_line_desc(t: str | None) -> str:
                if not t:
                    return "-"
                s = re.sub(r"\s+", " ", t).strip()
                return (s[:320] + "…") if len(s) > 320 else s

            for p in product_rows:
                specs = "; ".join(by_product_specs.get(str(p.id), [])) or "-"
                title = (p.rewritten_name or p.name or "-").replace("|", "/")
                desc = _one_line_desc(p.rewritten_description or p.original_description).replace("|", "/")
                vis = "да" if p.catalog_visible else "нет"
                lines.append(
                    f"| {title} "
                    f"| {(p.category or '-').replace('|', '/')} "
                    f"| {p.price_from if p.price_from is not None else '-'} "
                    f"| {(p.currency or 'RUB').replace('|', '/')} "
                    f"| {desc} "
                    f"| {specs.replace('|', '/')} "
                    f"| {vis} |"
                )
            if len(lines) == 3:
                lines.append("| - | - | - | - | - | - | Нет товаров |")

            svc_lines = ["", "---", "Услуги и информационные разделы (контентные страницы):", ""]
            content_pages = (
                await db.execute(
                    select(Page)
                    .where(Page.site_project_id == project_id, Page.page_type != "product")
                    .order_by(Page.url_path)
                    .limit(120)
                )
            ).scalars().all()
            for pg in content_pages:
                if (pg.url_path or "").strip() in ("/", ""):
                    continue
                snippet = (pg.meta_description or "").strip()
                if not snippet and isinstance(pg.original_texts, dict):
                    snippet = str(pg.original_texts.get("text") or "")[:600]
                snippet = re.sub(r"\s+", " ", snippet).strip()
                if len(snippet) > 400:
                    snippet = snippet[:400] + "…"
                if not snippet:
                    snippet = "-"
                head = (pg.title or pg.url_path or "раздел").replace("|", "/")
                svc_lines.append(f"- **{head}**: {snippet.replace('|', '/')}")
            if len(svc_lines) <= 4:
                svc_lines.append("- (нет отдельных контентных страниц в базе)")

            project.catalog_prompt_table = ("\n".join(lines + svc_lines))[:120000]

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
