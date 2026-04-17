from __future__ import annotations

from collections import Counter

from app.models.schemas import ConfidenceScore, EvidenceItem, SourceItem


class Verifier:
    def verify(self, sources: list[SourceItem], evidence: list[EvidenceItem]) -> ConfidenceScore:
        excerpts = [item.excerpt for item in evidence if item.excerpt]
        token_counter = Counter()
        for excerpt in excerpts:
            token_counter.update(word.lower() for word in excerpt.split()[:60])
        top_tokens = [token for token, count in token_counter.items() if count >= 3 and len(token) > 5]
        conflicts: list[str] = []
        overall = min(0.35 + (0.1 * len(sources)) + (0.08 * len(evidence)), 0.95)
        if len(sources) < 2:
            conflicts.append("Fewer than two independent sources were collected.")
            overall = min(overall, 0.5)
        if len(evidence) < 2:
            conflicts.append("Evidence is thin, so confidence is reduced.")
            overall = min(overall, 0.45)
        rationale = (
            f"Confidence based on {len(sources)} ranked sources, {len(evidence)} evidence items, "
            f"and recurring terms such as {', '.join(top_tokens[:5]) or 'limited overlap'}."
        )
        return ConfidenceScore(
            overall=overall,
            evidence_count=len(evidence),
            source_count=len(sources),
            conflicts=conflicts,
            rationale=rationale,
        )
