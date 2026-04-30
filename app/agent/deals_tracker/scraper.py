from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup

from app.agent.task_automation.fetcher import fetch_html
from app.browser.worker import BrowserWorker
from app.safety.guardrails import safety_guard

logger = logging.getLogger(__name__)

TRUSTED_DEAL_DOMAINS = [
    "amazon.in",
    "flipkart.com",
    "croma.com",
    "reliancedigital.in",
    "vijaysales.com",
    "tatacliq.com",
]

SEARCH_TEMPLATES = [
    "https://www.amazon.in/s?k={q}",
    "https://www.flipkart.com/search?q={q}",
]


@dataclass
class ScrapedProduct:
    name: str
    price: float
    rating: float | None
    link: str
    source: str


async def search_products(query: str, *, task_id: str = "") -> list[ScrapedProduct]:
    """Use the existing BrowserWorker to search + extract product data."""
    products: list[ScrapedProduct] = []

    # 1. Direct site search via constructed URLs
    for tmpl in SEARCH_TEMPLATES:
        url = tmpl.format(q=query.replace(" ", "+"))
        try:
            items = await _scrape_url(url, task_id=task_id)
            products.extend(items)
        except Exception as exc:
            logger.warning("deals_scraper: failed %s — %s", url, exc)

    # 2. Bing fallback for breadth
    try:
        async with BrowserWorker(task_id=task_id) as bw:
            results = await bw.web_search(f"{query} price buy", limit=8)
            for r in results:
                domain = _domain(r.url)
                if not any(t in domain for t in TRUSTED_DEAL_DOMAINS):
                    continue
                if _is_junk_url(r.url):
                    continue
                try:
                    items = await _scrape_url(r.url, task_id=task_id)
                    products.extend(items)
                except Exception:
                    continue
    except Exception as exc:
        logger.warning("deals_scraper: bing fallback failed — %s", exc)

    # Deduplicate
    return _deduplicate(products)


async def scrape_url(url: str, *, task_id: str = "") -> list[ScrapedProduct]:
    """Public wrapper — scrape a single URL."""
    return await _scrape_url(url, task_id=task_id)


# ── Internal helpers ────────────────────────────────────────────────────


async def _scrape_url(url: str, *, task_id: str = "") -> list[ScrapedProduct]:
    if not safety_guard.is_safe_url(url):
        return []

    html: str | None = None

    # Try headless browser first
    try:
        async with BrowserWorker(task_id=task_id) as bw:
            page = bw.page
            await page.goto(url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)
            html = await page.content()
    except Exception:
        pass

    # Fallback to lightweight fetch
    if not html or len(html) < 2000:
        fetched = await fetch_html(url)
        if fetched and len(fetched) > len(html or ""):
            html = fetched

    if not html:
        return []

    return extract_product_data(html, base_url=url)


def extract_product_data(html: str, *, base_url: str = "") -> list[ScrapedProduct]:
    """Parse product cards from HTML and return structured data."""
    soup = BeautifulSoup(html, "html.parser")
    domain = _domain(base_url)
    items: list[ScrapedProduct] = []

    # Generic card selectors used by major Indian e-commerce sites
    card_selectors = [
        "[data-component-type='s-search-result']",   # Amazon
        "div._1AtVbE",                                 # Flipkart
        "div._75nlfW",                                 # Flipkart v2
        "div.product-card",
        "div.product-item",
        "article",
        "li[data-pid]",                                # Flipkart list items
    ]

    candidates = []
    for sel in card_selectors:
        nodes = soup.select(sel)
        if nodes:
            candidates = nodes
            break

    if not candidates:
        candidates = soup.find_all("div", recursive=True)

    for node in candidates:
        text = node.get_text(" ", strip=True)
        if not text or len(text) < 30:
            continue

        price = _parse_price(text)
        if price is None:
            continue

        name = _guess_name(node, text)
        if not name or len(name) < 5:
            continue

        # Skip image/blog links
        link = _extract_link(node, base_url)
        if link and _is_junk_url(link):
            continue

        rating = _parse_rating(text)

        items.append(ScrapedProduct(
            name=name[:120],
            price=price,
            rating=rating,
            link=link or base_url,
            source=domain,
        ))

        if len(items) >= 15:
            break

    return items


def _parse_price(text: str) -> float | None:
    m = re.search(r"(?:₹|rs\.?|inr)\s*([\d,]{3,})", text, flags=re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


def _parse_rating(text: str) -> float | None:
    m = re.search(r"(\d(?:\.\d)?)\s*(?:out of 5|/5|★|stars?)", text, flags=re.IGNORECASE)
    if m:
        try:
            v = float(m.group(1))
            return v if 0 < v <= 5 else None
        except ValueError:
            return None
    return None


def _guess_name(node: Any, text: str) -> str:
    for tag in ["h2", "h3", "h4", "a[class*='title']", "span[class*='title']", "a[class*='name']"]:
        el = node.select_one(tag)
        if el:
            t = el.get_text(strip=True)
            if 5 < len(t) < 200:
                return t

    # Fallback: first 80 chars before the price
    m = re.search(r"(?:₹|rs\.?|inr)", text, flags=re.IGNORECASE)
    if m:
        chunk = text[:m.start()].strip()
        if len(chunk) > 5:
            return chunk[:100]

    return text[:80]


def _extract_link(node: Any, base_url: str) -> str | None:
    a = node.find("a", href=True)
    if not a:
        return None
    href = a["href"]
    if href.startswith("/"):
        from urllib.parse import urlparse
        p = urlparse(base_url)
        return f"{p.scheme}://{p.netloc}{href}"
    if href.startswith("http"):
        return href
    return None


def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def _is_junk_url(url: str) -> bool:
    low = (url or "").lower()
    junk = [".jpg", ".png", ".gif", ".svg", "/blog/", "/article/", "/review/", "captcha", "/login"]
    return any(j in low for j in junk)


def _deduplicate(items: list[ScrapedProduct]) -> list[ScrapedProduct]:
    seen: set[str] = set()
    out: list[ScrapedProduct] = []
    for it in items:
        key = f"{it.name.lower()[:40]}|{int(it.price)}"
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out
