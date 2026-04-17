from __future__ import annotations

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
        source_lines = [f"- {source.title} ({source.url})" for source in sources[:5]]
        evidence_lines = [f"- {item.excerpt or item.content[:250]}" for item in evidence[:5]]
        llm_text = await llm_client.text_completion(
            model=settings.llm_model_final,
            system_prompt=(
                "Answer only from the provided evidence. Be concise, useful, and avoid unsupported claims."
            ),
            user_prompt=(
                f"User query: {query}\n"
                f"Sources:\n{chr(10).join(source_lines)}\n"
                f"Evidence:\n{chr(10).join(evidence_lines)}\n"
                f"Confidence: {confidence.rationale}\n"
                "Return a concise answer with 2-4 supporting bullets."
            ),
        )
        if llm_text:
            lines = [line.strip("- ").strip() for line in llm_text.splitlines() if line.strip()]
            direct_answer = lines[0]
            supporting_points = lines[1:5]
        else:
            direct_answer = evidence[0].excerpt if evidence else "The agent could not collect enough evidence."
            supporting_points = [item.excerpt or item.content[:180] for item in evidence[1:4]]
        citations = [str(source.url) for source in sources[:4]]
        return FinalAnswer(
            direct_answer=direct_answer,
            supporting_points=supporting_points,
            citations=citations,
            best_video=best_video,
            confidence=confidence,
        )
