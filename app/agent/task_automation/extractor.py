from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .trusted_sources import detect_intent


@dataclass
class NormalizedItem:
    name: str
    price: float | None
    rating: float | None
    link: str | None
    source_domain: str | None
    raw: dict[str, Any]


class ResultExtractor:
    """Convert raw HTML/text into a normalized list of items (name/price/rating/link)."""

    def extract_items(
        self,
        *,
        html: str,
        base_url: str | None = None,
        max_items: int = 20,
        user_query: str | None = None,
        intent: str | None = None,
    ) -> list[NormalizedItem]:
        soup = BeautifulSoup(html or "", "html.parser")
        inferred_intent = intent or (detect_intent(user_query or "") if user_query else "general")

        # Broader but smarter selectors (higher recall on modern sites).
        selectors = [
            "article",
            "[class*='product']",
            "[class*='item']",
            "[class*='card']",
            "[class*='listing']",
            "[class*='result']",
            "li",
        ]

        candidates = []
        for selector in selectors:
            for node in soup.select(selector)[:400]:
                text = node.get_text(" ", strip=True)
                if not text:
                    continue
                if _looks_like_login_wall(text):
                    continue
                # Relax candidate filtering (CRITICAL FIX): collect first, filter later.
                if len(text) > 60:
                    candidates.append(node)

        items: list[NormalizedItem] = []
        domain = urlparse(base_url).netloc.replace("www.", "") if base_url else None

        for node in candidates:
            if len(items) >= max_items * 3:
                break

            text = node.get_text(" ", strip=True)
            print("CANDIDATE:", text[:120])

            name = _guess_name(node, text)
            if not name:
                continue

            price = _parse_price(text)
            rating = _parse_rating(text)
            link = _extract_link(node, base_url)

            # Junk filter (CRITICAL)
            if _is_junk_item(name, text):
                continue

            # Minimum quality rules by intent
            if inferred_intent in {"product", "flight"} and price is None:
                continue

            # Relevance scoring (filter out irrelevant UI labels)
            if user_query and _score_item(name, text, user_query) < 3:
                continue

            items.append(
                NormalizedItem(
                    name=_clean_name(name),
                    price=price,
                    rating=rating,
                    link=link,
                    source_domain=domain,
                    raw={"text": text[:1200]},
                )
            )

        # Post-extraction filtering + dedupe
        items = _dedupe_items(items)
        items = _prefer_structured(items, keep=max_items)

        # Mandatory fallback: never return empty.
        if not items:
            items.append(
                NormalizedItem(
                    name="Could not extract structured results. Showing best available page",
                    price=None,
                    rating=None,
                    link=base_url,
                    source_domain=domain,
                    raw={},
                )
            )

        return items[:max_items]


def _looks_like_login_wall(text: str) -> bool:
    lowered = text.lower()
    return any(k in lowered for k in ["sign in", "log in", "login", "create an account", "verify you are human"])


def _has_any_price(text: str) -> bool:
    return bool(re.search(r"(₹\s?[\d,]+|\$\s?[\d,]+|rs\.?\s?[\d,]+)", text, flags=re.IGNORECASE))


def _has_any_rating(text: str) -> bool:
    return bool(re.search(r"(\d\.\d)\s*(/5|out of 5|stars?)", text, flags=re.IGNORECASE))


def _parse_price(text: str) -> float | None:
    m = re.search(r"(₹|\$|rs\.?)\s*([\d,]{3,})", text, flags=re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(2).replace(",", ""))
    except ValueError:
        return None


def _parse_rating(text: str) -> float | None:
    m = re.search(r"(\d\.\d)\s*(/5|out of 5|stars?)", text, flags=re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _extract_link(node, base_url: str | None) -> str | None:
    a = node.find("a", href=True)
    if not a:
        return None
    href = str(a.get("href") or "").strip()
    if not href:
        return None
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if base_url and href.startswith("/"):
        parsed = urlparse(base_url)
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    return None


def _guess_name(node, text: str) -> str:
    for tag in ["h1", "h2", "h3"]:
        h = node.find(tag)
        if h:
            t = h.get_text(strip=True)
            if 10 <= len(t) <= 120:
                return t

    for tag in ["strong", "b"]:
        el = node.find(tag)
        if el:
            t = el.get_text(strip=True)
            if 10 <= len(t) <= 120:
                return t

    a = node.find("a")
    if a:
        t = a.get_text(strip=True)
        if 10 <= len(t) <= 140:
            return t

    return ""


def _clean_name(name: str) -> str:
    return re.sub(r"\s{2,}", " ", name).strip()


def _dedupe_items(items: list[NormalizedItem]) -> list[NormalizedItem]:
    seen: set[tuple[str, str, str]] = set()
    out: list[NormalizedItem] = []
    for it in items:
        key = (
            (it.name or "").strip().lower(),
            "" if it.price is None else str(int(it.price)),
            (it.link or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _prefer_structured(items: list[NormalizedItem], *, keep: int) -> list[NormalizedItem]:
    # Prefer items with a real name, and at least price or rating.
    good = [
        it
        for it in items
        if (it.name and len(it.name) >= 10) and (it.price is not None or it.rating is not None or it.link)
    ]
    if len(good) >= 3:
        items = good

    def score(it: NormalizedItem) -> float:
        s = 0.0
        if it.price is not None:
            s += 2.0
        if it.rating is not None:
            s += it.rating / 5.0
        if it.link:
            s += 0.2
        # penalize overly long names (usually noisy)
        if len(it.name) > 140:
            s -= 0.5
        return s

    # Sort final results (price first, then rating)
    ranked = sorted(items, key=lambda x: (x.price is None, -(x.rating or 0.0)))
    return ranked[:keep]


def _is_junk_item(name: str, text: str) -> bool:
    lowered = (f"{name} {text}").lower()
    junk_keywords = [
        "search",
        "filter",
        "sort",
        "login",
        "sign in",
        "economy class",
        "business class",
        "premium economy",
        "menu",
        "navigation",
        "home",
        "explore",
        "categories",
        "options",
    ]
    if any(k in lowered for k in junk_keywords):
        return True
    if len(name.split()) < 2:
        return True
    return False


def _score_item(name: str, text: str, query: str) -> int:
    score = 0
    q_words = (query or "").lower().split()
    name_low = (name or "").lower()
    text_low = (text or "").lower()
    for w in q_words:
        if not w or len(w) < 3:
            continue
        if w in name_low:
            score += 3
        if w in text_low:
            score += 1
    if _parse_price(text) is not None:
        score += 2
    if _parse_rating(text) is not None:
        score += 1
    return score

