from __future__ import annotations

import re
from urllib.parse import urlparse


def normalize_query(text: str) -> str:
    return " ".join(text.strip().split())


def guess_domain(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.replace("www.", "")


def keyword_overlap_score(query: str, text: str) -> float:
    query_terms = {term for term in re.findall(r"\w+", query.lower()) if len(term) > 2}
    if not query_terms:
        return 0.0
    text_terms = set(re.findall(r"\w+", text.lower()))
    overlap = len(query_terms & text_terms)
    return min(overlap / max(len(query_terms), 1), 1.0)
