from __future__ import annotations

import re
from typing import Any

from app.core.config import get_settings
from app.core.llm import llm_client
from app.models.schemas import QueryIntent, ResearchPlan, UserQuery


class ResearchPlanner:
    async def build_plan(self, query: UserQuery, intent: QueryIntent) -> ResearchPlan:
        settings = get_settings()
        llm_result = await llm_client.json_completion(
            model=settings.llm_model_planner,
            system_prompt=(
                "You are an expert research strategist. Your job is to create a comprehensive, "
                "highly targeted web research plan that will gather the BEST and MOST RELEVANT "
                "information for the user's query.\n\n"
                "IMPORTANT GUIDELINES:\n"
                "- Generate 4-6 diverse, highly specific search queries that approach the topic from different angles.\n"
                "- Each search query should be a real search engine query (concise, keyword-rich, no filler words).\n"
                "- Include queries targeting: authoritative sources, recent developments, technical details, "
                "practical applications, and comparisons.\n"
                "- For academic/technical topics, include queries targeting research papers, official documentation, "
                "and expert blogs.\n"
                "- Generate 2-3 sub-questions that break the topic into specific aspects the user needs answered.\n"
                "- For video queries, create queries that would find educational explainer videos.\n\n"
                "Return JSON with keys: objective (string), search_queries (array of 4-6 strings), "
                "video_queries (array of 1-3 strings), subquestions (array of 2-4 strings), "
                "source_limit (integer 8-12), stopping_criteria (array of strings)."
            ),
            user_prompt=(
                f"User query: {query.text}\n"
                f"Intent topic: {intent.topic}\n"
                f"Mode: {intent.mode}\n"
                f"Subtopics identified: {', '.join(intent.subtopics) if intent.subtopics else 'none'}\n"
                f"Requires YouTube: {intent.requires_youtube}\n\n"
                "Create a research plan with diverse, targeted search queries that will find "
                "the most relevant and authoritative sources."
            ),
        )
        if llm_result:
            normalized_payload = self._normalize_plan_payload(llm_result)
            normalized_payload["search_queries"] = self._normalize_search_queries(
                normalized_payload.get("search_queries", []),
                query.text,
                intent.topic,
                intent.subtopics,
            )
            return ResearchPlan.model_validate(normalized_payload)

        search_queries = [query.text]
        search_queries.extend(intent.subtopics[:2])
        video_queries = [f"{intent.topic} explained"] if intent.requires_youtube else []
        return ResearchPlan(
            objective=f"Answer the user's research request about {intent.topic}",
            search_queries=self._normalize_search_queries(search_queries, query.text, intent.topic, intent.subtopics),
            video_queries=video_queries,
            subquestions=intent.subtopics[:4],
            source_limit=settings.max_web_sources,
            stopping_criteria=[
                "At least three relevant independent sources reviewed",
                "Key facts supported by more than one source",
                "Low-quality or duplicate pages excluded",
            ],
        )

    def _normalize_plan_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        normalized["search_queries"] = self._ensure_list(normalized.get("search_queries"))
        normalized["video_queries"] = self._ensure_list(normalized.get("video_queries"))
        normalized["subquestions"] = self._ensure_list(normalized.get("subquestions"))
        normalized["stopping_criteria"] = self._ensure_list(normalized.get("stopping_criteria"))
        normalized["source_limit"] = self._normalize_source_limit(normalized.get("source_limit"))

        if not normalized["search_queries"]:
            normalized["search_queries"] = [normalized.get("objective") or "general research"]
        if not normalized["source_limit"]:
            normalized["source_limit"] = get_settings().max_web_sources
        return normalized

    def _ensure_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            separators = ["\n", ";", "|"]
            for separator in separators:
                if separator in value:
                    return [part.strip(" -") for part in value.split(separator) if part.strip(" -")]
            return [value.strip()] if value.strip() else []
        return [str(value).strip()] if str(value).strip() else []

    def _normalize_source_limit(self, value: Any) -> int:
        default_limit = get_settings().max_web_sources
        try:
            limit = int(value)
        except (TypeError, ValueError):
            return default_limit
        return max(6, min(limit, 15))

    def _normalize_search_queries(
        self,
        search_queries: list[str],
        query_text: str,
        intent_topic: str,
        intent_subtopics: list[str],
    ) -> list[str]:
        seeds = [*search_queries, query_text, intent_topic, *intent_subtopics[:3]]
        cleaned: list[str] = []
        seen: set[str] = set()
        for seed in seeds:
            compact = self._compact_search_query(seed)
            if not compact:
                continue
            key = compact.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(compact)
            if len(cleaned) >= 8:
                break
        return cleaned if cleaned else [query_text]

    def _compact_search_query(self, text: str) -> str:
        lowered = re.sub(r"[^a-zA-Z0-9\s-]", " ", text.lower())
        words = [word for word in lowered.split() if len(word) > 2]
        stopwords = {
            "explain",
            "including",
            "compare",
            "least",
            "three",
            "identify",
            "show",
            "describe",
            "about",
            "with",
            "then",
            "give",
            "provide",
            "recommend",
            "useful",
            "youtube",
            "video",
            "work",
            "works",
            "their",
        }
        words = [word for word in words if word not in stopwords]
        if not words:
            return ""
        if "rag" in words and "retrieval" not in words:
            words = ["retrieval", "augmented", "generation", *words]
        return " ".join(words[:12])
