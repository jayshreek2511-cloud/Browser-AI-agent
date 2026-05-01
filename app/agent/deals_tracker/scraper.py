from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.agent.task_automation.fetcher import fetch_html
from app.browser.worker import BrowserWorker
from app.safety.guardrails import safety_guard

logger = logging.getLogger(__name__)

TRUSTED_DEAL_DOMAINS = [
    "amazon.in",
    "amazon.com",
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

# Markers that indicate a blocked / unusable page
_BLOCK_MARKERS = [
    "sign in", "log in", "captcha", "verify you are human",
    "robot or human", "access denied", "403 forbidden",
    "solve the challenge", "checking your browser",
    "unusual traffic", "bot detection",
]


@dataclass
class ScrapedProduct:
    name: str
    price: float
    rating: float | None
    link: str
    source: str


# ════════════════════════════════════════════════════════════════════════
#  PUBLIC API  — DO NOT modify search_products (search flow unchanged)
# ════════════════════════════════════════════════════════════════════════

async def search_products(query: str, *, task_id: str = "") -> list[ScrapedProduct]:
    """Use the existing BrowserWorker to search + extract product data."""
    products: list[ScrapedProduct] = []

    # 1. Direct site search via constructed URLs
    for tmpl in SEARCH_TEMPLATES:
        url = tmpl.format(q=query.replace(" ", "+"))
        try:
            items = await _scrape_search_page(url, task_id=task_id)
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
                    items = await _scrape_search_page(r.url, task_id=task_id)
                    products.extend(items)
                except Exception:
                    continue
    except Exception as exc:
        logger.warning("deals_scraper: bing fallback failed — %s", exc)

    # Deduplicate
    return _deduplicate(products)


async def scrape_url(url: str, *, task_id: str = "") -> list[ScrapedProduct]:
    """Public entry point for a single direct product URL.

    Detects whether the URL is a product page or a search/listing page
    and routes to the appropriate extractor.
    """
    if not safety_guard.is_safe_url(url):
        return []

    html = await _fetch_page_html(url, task_id=task_id)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True).lower()

    # ── Reject blocked / captcha pages ──────────────────────────────
    if _is_blocked_page(page_text):
        logger.warning("deals_scraper: blocked page detected for %s", url)
        return []

    domain = _domain(url)

    # ── Route to the right extractor ────────────────────────────────
    if _is_product_page_url(url):
        product = _extract_single_product(soup, url, domain)
        if product:
            return [product]
        # Retry with generic fallback
        product = _extract_generic_product(soup, url, domain)
        if product:
            return [product]
        logger.warning("deals_scraper: could not extract product from %s", url)
        return []

    # Fallback: treat as search/listing page
    return extract_product_data(html, base_url=url)


# ════════════════════════════════════════════════════════════════════════
#  SINGLE PRODUCT PAGE EXTRACTORS
# ════════════════════════════════════════════════════════════════════════

def _is_product_page_url(url: str) -> bool:
    """Heuristic: detect if a URL points to a single product page (not search)."""
    low = url.lower()
    # Amazon product pages contain /dp/ or /gp/
    if "amazon" in low and ("/dp/" in low or "/gp/" in low):
        return True
    # Flipkart product pages contain /p/ with item ID
    if "flipkart" in low and "/p/" in low:
        return True
    # Croma, Reliance Digital, etc. product pages
    if any(d in low for d in ["croma.com", "reliancedigital.in", "vijaysales.com"]):
        if "/p/" in low or "/product/" in low or re.search(r"/\d{5,}", low):
            return True
    # Generic: URLs with known product path patterns
    if re.search(r"/(product|item|p|dp|buy)/", low):
        return True
    return False


def _extract_single_product(soup: BeautifulSoup, url: str, domain: str) -> ScrapedProduct | None:
    """Route to site-specific extractor based on domain."""
    low_domain = domain.lower()

    if "amazon" in low_domain:
        return _extract_amazon_product(soup, url, domain)
    elif "flipkart" in low_domain:
        return _extract_flipkart_product(soup, url, domain)
    elif "croma" in low_domain:
        return _extract_croma_product(soup, url, domain)
    else:
        return _extract_generic_product(soup, url, domain)


# ── Amazon ──────────────────────────────────────────────────────────────

def _extract_amazon_product(soup: BeautifulSoup, url: str, domain: str) -> ScrapedProduct | None:
    """Extract product details from an Amazon product page."""
    # Title — primary selectors
    title = None
    for sel in ["#productTitle", "#title span", "span#productTitle"]:
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            if title:
                break

    # Price — multiple possible locations
    price = None
    price_selectors = [
        "span.a-price-whole",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "span.a-price .a-offscreen",
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "div.a-section.a-spacing-none span.a-price-whole",
        "#apex_offerDisplay_desktop span.a-price-whole",
    ]
    for sel in price_selectors:
        el = soup.select_one(sel)
        if el:
            raw = el.get_text(strip=True)
            price = _clean_price(raw)
            if price:
                break

    # If still no price, scan the page text for ₹ patterns
    if not price:
        page_text = soup.get_text(" ", strip=True)
        price = _parse_price(page_text)

    # Rating
    rating = None
    for sel in ["#acrPopover .a-icon-alt", "span.a-icon-alt", "#averageCustomerReviews .a-icon-alt"]:
        el = soup.select_one(sel)
        if el:
            raw = el.get_text(strip=True)
            m = re.search(r"(\d(?:\.\d)?)", raw)
            if m:
                v = float(m.group(1))
                if 0 < v <= 5:
                    rating = v
                    break

    # Validate: must have at least name and price
    if not title or not price:
        return None

    return ScrapedProduct(
        name=title[:200],
        price=price,
        rating=rating,
        link=url,
        source=domain,
    )


# ── Flipkart ───────────────────────────────────────────────────────────

def _extract_flipkart_product(soup: BeautifulSoup, url: str, domain: str) -> ScrapedProduct | None:
    """Extract product details from a Flipkart product page."""
    # Title
    title = None
    title_selectors = [
        "span.VU-ZEz",          # 2024+ layout
        "span.B_NuCI",          # Older layout
        "h1._9E25nV",           # Alternate
        "h1.yhB1nd",            # Newer
        "h1 span",              # Generic h1
    ]
    for sel in title_selectors:
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            if title:
                break

    # Price
    price = None
    price_selectors = [
        "div.Nx9bqj.CxhGGd",   # 2024+ main price
        "div._30jeq3._16Jk6d",  # Older
        "div.Nx9bqj",           # General price tag
        "div._30jeq3",          # General price tag old
        "div.CEmiEU div.Nx9bqj",
    ]
    for sel in price_selectors:
        el = soup.select_one(sel)
        if el:
            raw = el.get_text(strip=True)
            price = _clean_price(raw)
            if price:
                break

    if not price:
        page_text = soup.get_text(" ", strip=True)
        price = _parse_price(page_text)

    # Rating
    rating = None
    rating_selectors = [
        "div.XQDdHH",           # 2024+
        "div._3LWZlK",          # Older
        "span._1lRcqv div._3LWZlK",
    ]
    for sel in rating_selectors:
        el = soup.select_one(sel)
        if el:
            raw = el.get_text(strip=True)
            m = re.search(r"(\d(?:\.\d)?)", raw)
            if m:
                v = float(m.group(1))
                if 0 < v <= 5:
                    rating = v
                    break

    if not title or not price:
        return None

    return ScrapedProduct(
        name=title[:200],
        price=price,
        rating=rating,
        link=url,
        source=domain,
    )


# ── Croma ───────────────────────────────────────────────────────────────

def _extract_croma_product(soup: BeautifulSoup, url: str, domain: str) -> ScrapedProduct | None:
    """Extract product details from a Croma product page."""
    title = None
    for sel in ["h1.pd-title", "h1", "div.pd-title"]:
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            if title:
                break

    price = None
    for sel in ["span.amount", "span.new-price", "span.pdp-price"]:
        el = soup.select_one(sel)
        if el:
            price = _clean_price(el.get_text(strip=True))
            if price:
                break

    if not price:
        page_text = soup.get_text(" ", strip=True)
        price = _parse_price(page_text)

    rating = _parse_rating(soup.get_text(" ", strip=True))

    if not title or not price:
        return None

    return ScrapedProduct(name=title[:200], price=price, rating=rating, link=url, source=domain)


# ── Generic single-product fallback ────────────────────────────────────

def _extract_generic_product(soup: BeautifulSoup, url: str, domain: str) -> ScrapedProduct | None:
    """Last-resort extractor for any single product page."""
    # Try to get title from common tags
    title = None
    for sel in ["h1", "h1 span", "[itemprop='name']", "meta[property='og:title']"]:
        el = soup.select_one(sel)
        if el:
            if el.name == "meta":
                title = el.get("content", "")
            else:
                title = el.get_text(strip=True)
            if title and len(title) > 3:
                break
            title = None

    # Try og:title as fallback
    if not title:
        og = soup.find("meta", property="og:title")
        if og:
            title = og.get("content", "")

    # Extract price from page text
    page_text = soup.get_text(" ", strip=True)
    price = _parse_price(page_text)

    # Rating
    rating = _parse_rating(page_text)

    if not title or not price:
        return None

    # Clean the title — remove site name suffixes
    title = re.sub(r"\s*[-|:]\s*(Amazon|Flipkart|Croma|Buy Online).*$", "", title, flags=re.IGNORECASE)

    return ScrapedProduct(
        name=title.strip()[:200],
        price=price,
        rating=rating,
        link=url,
        source=domain,
    )


# ════════════════════════════════════════════════════════════════════════
#  PAGE FETCHING
# ════════════════════════════════════════════════════════════════════════

async def _fetch_page_html(url: str, *, task_id: str = "") -> str | None:
    """Fetch a page via headless browser with lightweight HTTP fallback."""
    html: str | None = None

    # Try headless browser first (renders JS)
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
    except Exception as exc:
        logger.warning("deals_scraper: browser fetch failed for %s — %s", url, exc)

    # Fallback to lightweight HTTP fetch
    if not html or len(html) < 2000:
        fetched = await fetch_html(url)
        if fetched and len(fetched) > len(html or ""):
            html = fetched

    return html


# ════════════════════════════════════════════════════════════════════════
#  SEARCH/LISTING PAGE EXTRACTOR  (unchanged from original)
# ════════════════════════════════════════════════════════════════════════

async def _scrape_search_page(url: str, *, task_id: str = "") -> list[ScrapedProduct]:
    """Scrape a search/listing page. Used by search_products only."""
    if not safety_guard.is_safe_url(url):
        return []

    html = await _fetch_page_html(url, task_id=task_id)
    if not html:
        return []

    return extract_product_data(html, base_url=url)


def extract_product_data(html: str, *, base_url: str = "") -> list[ScrapedProduct]:
    """Parse product cards from a search/listing HTML page."""
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


# ════════════════════════════════════════════════════════════════════════
#  FILTERING & RELATED PRODUCT UTILITIES
# ════════════════════════════════════════════════════════════════════════

_ACCESSORY_WORDS = [
    "cover", "case", "tempered", "protector", "charger",
    "cable", "adapter", "earbuds", "headphones", "skin",
    "back cover", "screen guard", "glass", "pouch", "sleeve",
    "stand", "holder", "mount", "ring", "grip", "sticker",
    "film", "wrap", "bumper", "strap", "band", "dock",
    "keyboard", "mouse pad", "cleaning kit",
]

_QUERY_NOISE = {
    "under", "below", "above", "rs", "₹", "inr", "best", "cheapest",
    "deal", "buy", "online", "india", "price", "for", "in", "the",
    "a", "an", "of", "with", "to", "and", "or", "right", "now",
}


def is_primary_product(query: str, title: str) -> bool:
    """Return True only if *title* is a real product matching *query* (not an accessory)."""
    title_low = title.lower()

    # Block accessories
    if any(word in title_low for word in _ACCESSORY_WORDS):
        return False

    # Extract meaningful keywords from the query
    query_low = query.lower()
    core_keywords = [w for w in query_low.split() if w not in _QUERY_NOISE and len(w) > 1]

    if not core_keywords:
        return True  # Can't filter — allow everything

    # At least one core keyword must appear in the title
    return any(k in title_low for k in core_keywords)


def is_valid_product_link(url: str | None) -> bool:
    """Return True if the URL looks like a real product page (not an image or blog)."""
    if not url:
        return False
    low = url.lower()
    # Reject image/media links
    if low.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")):
        return False
    # Reject blog/article URLs
    if any(s in low for s in ["/blog/", "/article/", "/review/", "/news/", "captcha", "/login"]):
        return False
    # Prefer URLs with known product path patterns
    return True


def build_related_query(product_name: str) -> str:
    """Build a search query from a product name to find related listings."""
    # Take meaningful words, skip very short ones
    words = [w for w in product_name.split() if len(w) > 2]
    # Limit to first 6 meaningful words to keep the query focused
    core = " ".join(words[:6])
    return f"{core} buy online india price"


def filter_products(products: list[ScrapedProduct], query: str) -> list[ScrapedProduct]:
    """Apply accessory filter + valid link check to a product list."""
    filtered = []
    for p in products:
        if not is_primary_product(query, p.name):
            continue
        if not is_valid_product_link(p.link):
            continue
        if p.price <= 0:
            continue
        filtered.append(p)
    return filtered


# ════════════════════════════════════════════════════════════════════════
#  SHARED UTILITIES
# ════════════════════════════════════════════════════════════════════════

def _is_blocked_page(page_text_lower: str) -> bool:
    """Return True if the page appears to be blocked / captcha / login wall."""
    hits = sum(1 for m in _BLOCK_MARKERS if m in page_text_lower)
    return hits >= 2


def _clean_price(raw: str) -> float | None:
    """Parse a price string like '₹12,999' or '12999.00' into a float."""
    cleaned = raw.replace("₹", "").replace(",", "").replace(" ", "").strip()
    # Remove trailing dot (Amazon sometimes shows "12,999.")
    cleaned = cleaned.rstrip(".")
    try:
        val = float(cleaned)
        return val if val > 0 else None
    except ValueError:
        # Try regex as last resort
        m = re.search(r"([\d,]+(?:\.\d+)?)", raw.replace(",", ""))
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        return None


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
        p = urlparse(base_url)
        return f"{p.scheme}://{p.netloc}{href}"
    if href.startswith("http"):
        return href
    return None


def _domain(url: str) -> str:
    try:
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
