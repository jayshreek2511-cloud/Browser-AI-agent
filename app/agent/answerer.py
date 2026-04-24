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
        videos: list[VideoItem] | None = None,
    ) -> FinalAnswer:
        videos = videos or []
        settings = get_settings()

        # Prioritize text evidence by confidence, then tables, then images
        text_evidence = sorted(
            [e for e in evidence if e.evidence_type.value == "text"],
            key=lambda item: -(item.confidence or 0.0),
        )
        table_evidence = [e for e in evidence if e.evidence_type.value == "table"]
        image_evidence = [e for e in evidence if e.evidence_type.value == "image"]

        # Send much more evidence to the LLM — up to 12 text items
        prioritized_text = text_evidence[:12]
        prioritized_tables = table_evidence[:4]
        prioritized_images = image_evidence[:6]

        source_lines = []
        for i, source in enumerate(sources[:10]):
            source_lines.append(
                f"[{i+1}] {source.title} ({source.url})\n"
                f"    Domain: {source.domain} | Relevance: {source.relevance_score:.2f} | "
                f"Authority: {source.authority_score:.2f}"
            )

        evidence_lines = []
        for item in prioritized_text:
            evidence_lines.append(
                f"--- Source: {item.source_url} (confidence: {item.confidence:.2f}) ---\n"
                f"{item.content[:12000]}"
            )

        table_lines = []
        for item in prioritized_tables:
            table_lines.append(
                f"--- Table from: {item.source_url} ---\n"
                f"{item.content[:4000]}"
            )

        image_lines = []
        for item in prioritized_images:
            image_lines.append(
                f"Image: {item.content} (alt: {item.excerpt})"
            )

        video_lines = []
        for v in videos[:3]:
            video_lines.append(f"- {v.title}: {v.url}")

        llm_payload = await llm_client.text_completion(
            model=settings.llm_model_final,
            system_prompt=(
                "You are a world-class research analyst. Your task is to provide a complete, deep, and structured answer "
                "to the user's query based on the collected evidence.\n\n"
                "CRITICAL REQUIREMENTS:\n"
                "1. DO NOT OUTPUT JSON. Output the raw HTML report directly.\n"
                "2. START WITH A CLEAR OUTLINE: Your first paragraph MUST explicitly state the topic being covered and list the structure/steps you will follow (e.g., 'To understand [Topic], we will break this down into: Step 1, Step 2...').\n"
                "3. Your response must be COMPREHENSIVE — explain the topic broadly, then dive into the specifics using the sources provided. Write at least 1000-2000 words.\n"
                "4. You must suggest and explain the sources in depth. Tell the user what information came from which source, e.g., 'According to [Source Name]...'.\n"
                "5. Use rich HTML formatting: <h3>, <h4>, <p>, <b>, <i>, <ul>, <ol>, <li>, <blockquote>.\n"
                "5. Include HTML tables (<table>, <thead>, <tbody>, <tr>, <th>, <td>) to present comparative data.\n"
                "6. Include relevant images using <img src='url' style='max-width:100%; border-radius:8px; margin:12px 0;'/> tags.\n"
                "7. Structure your answer with clear sections:\n"
                "   - Introduction & Overview\n"
                "   - In-Depth Analysis (explain sources and details)\n"
                "   - Key Takeaways (bullet points)\n"
                "8. Cite sources inline referencing the source numbers [1], [2], etc.\n\n"
                "Write the final report beautifully in HTML. Do not wrap in markdown ```html blocks."
            ),
            user_prompt=(
                f"RESEARCH QUERY: {query}\n\n"
                f"{'='*60}\n"
                f"RANKED SOURCES ({len(sources)} total):\n"
                f"{chr(10).join(source_lines)}\n\n"
                f"{'='*60}\n"
                f"EXTRACTED EVIDENCE ({len(prioritized_text)} text items):\n"
                f"{chr(10).join(evidence_lines)}\n\n"
                + (f"{'='*60}\nTABULAR DATA ({len(prioritized_tables)} tables):\n"
                   f"{chr(10).join(table_lines)}\n\n" if table_lines else "")
                + (f"{'='*60}\nAVAILABLE IMAGES:\n"
                   f"{chr(10).join(image_lines)}\n\n" if image_lines else "")
                + (f"{'='*60}\nRECOMMENDED VIDEOS:\n"
                   f"{chr(10).join(video_lines)}\n\n" if video_lines else "")
                + f"{'='*60}\n"
                f"CONFIDENCE: {confidence.overall:.2f} — {confidence.rationale}\n\n"
                "Synthesize ALL of the above evidence into a comprehensive, well-structured research report. "
                "Be thorough, explain each source in depth, and include specific data, statistics, and citations. "
                "DO NOT truncate your response — write the complete report in raw HTML."
            ),
        )

        direct_answer, supporting_points = self._build_structured_answer(llm_payload, prioritized_text)
        citations = [str(source.url) for source in sources[:10]]

        return FinalAnswer(
            direct_answer=direct_answer,
            supporting_points=supporting_points,
            citations=citations,
            videos=videos,
            confidence=confidence,
        )

    def _build_structured_answer(
        self,
        llm_payload: str | None,
        prioritized_evidence: list[EvidenceItem],
    ) -> tuple[str, list[str]]:
        if llm_payload:
            # The payload is now a direct HTML string, no JSON parsing needed
            direct = self._clean_answer_html(llm_payload)
            if direct:
                return direct, []

        fallback = self._summarize_from_evidence(prioritized_evidence)
        if fallback:
            return fallback
        return "The agent could not collect enough evidence.", []

    def _clean_answer_html(self, value: str) -> str:
        """Clean the answer while preserving HTML formatting."""
        # Remove markdown code fence artifacts
        value = re.sub(r"^```(?:html)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
        # Remove citation-style references like [0] but keep inline [1], [2] etc.
        value = re.sub(r"\[0\]", "", value)
        # Clean up excessive whitespace while preserving HTML structure
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    def _summarize_from_evidence(self, evidence: list[EvidenceItem]) -> tuple[str, list[str]] | None:
        text_items = [item for item in evidence if item.evidence_type.value == "text" and (item.content or item.excerpt)]
        if not text_items:
            return None
        corpus = " ".join((item.content or item.excerpt or "")[:2000] for item in text_items[:5])
        sentences = [chunk.strip() for chunk in re.split(r"(?<=[.!?])\s+", corpus) if chunk.strip()]
        sentences = [sentence for sentence in sentences if 25 <= len(sentence) <= 300]
        if not sentences:
            return None
        direct = sentences[0]
        points: list[str] = []
        for sentence in sentences[1:]:
            if sentence not in points:
                points.append(sentence)
            if len(points) >= 6:
                break
        if not points and len(sentences) > 1:
            points = sentences[1:4]
        return direct, points
