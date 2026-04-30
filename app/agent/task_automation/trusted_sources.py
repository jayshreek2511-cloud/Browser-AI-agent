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
        "amazon.in",
        "flipkart.com",
        "croma.com",
        "reliancedigital.in",
        "tatacliq.com",
        "vijaysales.com",
    ],
    "hotel": [
        "booking.com",
        "agoda.com",
        "tripadvisor.com",
    ],
    "general": [],
}


def detect_intent(query: str) -> str:
    q = (query or "").lower()

    if any(k in q for k in ["flight", "airline", "travel", "route"]):
        return "flight"
    if any(k in q for k in ["hotel", "stay"]):
        return "hotel"
    if any(k in q for k in ["under", "buy", "price", "budget", "compare"]):
        return "product"

    return "general"


def extract_url(query: str) -> str | None:
    match = re.search(r"https?://\\S+", query or "")
    return match.group(0) if match else None

