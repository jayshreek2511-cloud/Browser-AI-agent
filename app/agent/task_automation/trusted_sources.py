from __future__ import annotations

import re


TRUSTED_SOURCES: dict[str, list[str]] = {
    "flight": [
        "ixigo.com",
        "skyscanner.com",
        "kayak.com",
        "makemytrip.com",
    ],
    "product": [
        "91mobiles.com",
        "gsmarena.com",
        "amazon.in",
        "flipkart.com",
    ],
    "general": [],
}


def detect_intent(query: str) -> str:
    q = (query or "").lower()

    if "flight" in q or "travel" in q:
        return "flight"
    if "hotel" in q:
        return "hotel"
    if "buy" in q or "under" in q:
        return "product"

    return "general"


def extract_url(query: str) -> str | None:
    match = re.search(r"https?://\\S+", query or "")
    return match.group(0) if match else None

