from __future__ import annotations

import re

from app.core.config import get_settings
from app.core.llm import llm_client
from app.models.schemas import ConfidenceScore, EvidenceItem, FinalAnswer, SourceItem, VideoItem


class AnswerComposer:
    async def compose(
        self,
        *,
        query: str,
        sources: list[SourceItem],
        evidence: list[EvidenceItem],
        confidence: ConfidenceScore,
        best_video: VideoItem | None = None,
    ) -> FinalAnswer:
        settings = get_settings()
        prioritized_evidence = sorted(
            evidence,
            key=lambda item: (item.evidence_type.value != "text", -(item.confidence or 0.0)),
        )[:6]
        source_lines = [f"- {source.title} ({source.url})" for source in sources[:6]]
        evidence_lines = [f"- {self._clean_text(item.excerpt or item.content[:300])}" for item in prioritized_evidence]
        llm_payload = await llm_client.json_completion(
            model=settings.llm_model_final,
            system_prompt=(
                "You are an evidence-grounded research assistant. Use only provided evidence. "
                "Return strict JSON only."
            ),
            user_prompt=(
                f"User query: {query}\n"
                f"Sources:\n{chr(10).join(source_lines)}\n"
                f"Evidence:\n{chr(10).join(evidence_lines)}\n"
                f"Confidence: {confidence.rationale}\n"
                "Return ONLY JSON with keys: direct_answer (string), supporting_points (array of 3-6 strings), "
                "key_takeaways (array of 2-4 strings). "
                "Keep direct_answer under 120 words and do not include markdown/code fences."
            ),
        )
        direct_answer, supporting_points = self._build_structured_answer(llm_payload, prioritized_evidence)
        citations = [str(source.url) for source in sources[:6]]
        return FinalAnswer(
            direct_answer=direct_answer,
            supporting_points=supporting_points,
            citations=citations,
            best_video=best_video,
            confidence=confidence,
        )

    def _build_structured_answer(
        self,
        llm_payload: dict | None,
        prioritized_evidence: list[EvidenceItem],
    ) -> tuple[str, list[str]]:
        if llm_payload:
            direct = self._clean_text(str(llm_payload.get("direct_answer", "")))
            points = [self._clean_text(str(item)) for item in llm_payload.get("supporting_points", []) if str(item)]
            takeaways = [self._clean_text(str(item)) for item in llm_payload.get("key_takeaways", []) if str(item)]
            merged_points = [*points, *takeaways]
            if direct and merged_points:
                return direct, merged_points[:6]

        fallback = self._summarize_from_evidence(prioritized_evidence)
        if fallback:
            return fallback
        return "The agent could not collect enough evidence.", []

    def _clean_text(self, value: str) -> str:
        lines = [line.strip("-* \t") for line in value.splitlines() if line.strip()]
        compact = " ".join(lines)
        compact = " ".join(compact.split())
        compact = re.sub(r"\[[0-9]+\]", "", compact)
        return compact.strip()

    def _summarize_from_evidence(self, evidence: list[EvidenceItem]) -> tuple[str, list[str]] | None:
        text_items = [item for item in evidence if item.evidence_type.value == "text" and (item.content or item.excerpt)]
        if not text_items:
            return None
        corpus = " ".join((item.content or item.excerpt or "")[:1200] for item in text_items[:3])
        sentences = [self._clean_text(chunk) for chunk in re.split(r"(?<=[.!?])\s+", corpus) if chunk.strip()]
        sentences = [sentence for sentence in sentences if 25 <= len(sentence) <= 240]
        if not sentences:
            return None
        direct = sentences[0]
        points: list[str] = []
        for sentence in sentences[1:]:
            if sentence not in points:
                points.append(sentence)
            if len(points) >= 4:
                break
        if not points and len(sentences) > 1:
            points = sentences[1:3]
        return direct, points
