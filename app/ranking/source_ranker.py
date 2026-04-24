from __future__ import annotations

import logging

from app.core.config import get_settings
from app.core.llm import llm_client
from app.models.schemas import SourceItem
from app.utils.text import keyword_overlap_score

logger = logging.getLogger(__name__)


HIGH_AUTHORITY_HINTS = [
    ".gov",
    ".edu",
    "wikipedia.org",
    "github.com",
    "who.int",
    "nature.com",
    "reuters.com",
    "sciencedirect.com",
    "springer.com",
    "ieee.org",
    "acm.org",
    "medium.com",
    "towardsdatascience.com",
    "docs.python.org",
    "developer.mozilla.org",
    "huggingface.co",
    "openai.com",
    "deepmind.com",
    "microsoft.com/en-us/research",
]


class SourceRanker:
    async def llm_rank(self, query: str, sources: list[SourceItem]) -> list[SourceItem]:
        """Use the LLM to score source relevance, then combine with heuristic scores."""
        settings = get_settings()

        # Build a compact summary of sources for the LLM
        source_summaries = []
        for i, source in enumerate(sources):
            source_summaries.append(
                f"[{i}] Title: {source.title}\n"
                f"    Domain: {source.domain}\n"
                f"    Snippet: {(source.snippet or '')[:200]}"
            )

        llm_result = await llm_client.json_completion(
            model=settings.llm_model_worker,
            system_prompt=(
                "You are an expert research source evaluator. Given a research query and a list of sources, "
                "rate each source's relevance on a scale of 0.0 to 1.0.\n"
                "Consider:\n"
                "- How directly relevant the source is to answering the specific query\n"
                "- Whether the source likely contains in-depth, substantive information (not just a brief mention)\n"
                "- Whether the source is authoritative for this topic\n"
                "- Whether the source provides unique perspective or data\n\n"
                "Return JSON with key 'scores': an array of objects, each with 'index' (int) and 'relevance' (float 0-1)."
            ),
            user_prompt=(
                f"Research query: {query}\n\n"
                f"Sources to evaluate:\n" + "\n".join(source_summaries)
            ),
        )

        # Apply LLM relevance scores if available
        if llm_result and isinstance(llm_result.get("scores"), list):
            score_map = {}
            for entry in llm_result["scores"]:
                try:
                    idx = int(entry.get("index", -1))
                    rel = float(entry.get("relevance", 0.5))
                    score_map[idx] = rel
                except (TypeError, ValueError):
                    continue
            for i, source in enumerate(sources):
                if i in score_map:
                    source.relevance_score = score_map[i]

        # Fall back to / supplement with heuristic scoring
        return self.rank(query, sources)

    def rank(self, query: str, sources: list[SourceItem]) -> list[SourceItem]:
        ranked = []
        seen_urls: set[str] = set()
        for source in sources:
            if str(source.url) in seen_urls:
                continue
            seen_urls.add(str(source.url))
            combined_text = " ".join(filter(None, [source.title, source.snippet or ""]))

            # Only overwrite relevance_score if it hasn't been set by LLM
            if source.relevance_score == 0.0:
                source.relevance_score = keyword_overlap_score(query, combined_text)

            source.authority_score = self._authority_score(source.domain)
            source.freshness_score = 0.6 if source.published_at else 0.3
            source.completeness_score = min(len((source.snippet or "")) / 300, 1.0)
            source.rank_score = (
                (0.50 * source.relevance_score)
                + (0.22 * source.authority_score)
                + (0.13 * source.freshness_score)
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
