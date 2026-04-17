from __future__ import annotations

from app.models.schemas import SourceItem
from app.utils.text import keyword_overlap_score


HIGH_AUTHORITY_HINTS = [
    ".gov",
    ".edu",
    "wikipedia.org",
    "github.com",
    "arxiv.org",
    "who.int",
    "nature.com",
    "reuters.com",
]


class SourceRanker:
    def rank(self, query: str, sources: list[SourceItem]) -> list[SourceItem]:
        ranked = []
        seen_urls: set[str] = set()
        for source in sources:
            if str(source.url) in seen_urls:
                continue
            seen_urls.add(str(source.url))
            combined_text = " ".join(filter(None, [source.title, source.snippet or ""]))
            source.relevance_score = keyword_overlap_score(query, combined_text)
            source.authority_score = self._authority_score(source.domain)
            source.freshness_score = 0.6 if source.published_at else 0.3
            source.completeness_score = min(len((source.snippet or "")) / 300, 1.0)
            source.rank_score = (
                (0.45 * source.relevance_score)
                + (0.25 * source.authority_score)
                + (0.15 * source.freshness_score)
                + (0.15 * source.completeness_score)
            )
            ranked.append(source)
        return sorted(ranked, key=lambda item: item.rank_score, reverse=True)

    def _authority_score(self, domain: str) -> float:
        lowered = domain.lower()
        if any(hint in lowered for hint in HIGH_AUTHORITY_HINTS):
            return 0.9
        if lowered.count(".") >= 1:
            return 0.55
        return 0.35
