from __future__ import annotations

from bs4 import BeautifulSoup
import trafilatura

from app.models.schemas import EvidenceItem, EvidenceType
from app.utils.text import keyword_overlap_score


class WebEvidenceExtractor:
    def extract(self, *, task_id: str, query: str, url: str, html: str, text: str) -> list[EvidenceItem]:
        extracted = trafilatura.extract(html, include_comments=False, include_tables=True) or text
        extracted = extracted.strip()
        soup = BeautifulSoup(html, "html.parser")

        evidence: list[EvidenceItem] = []
        if extracted:
            evidence.append(
                EvidenceItem(
                    task_id=task_id,
                    source_url=url,
                    evidence_type=EvidenceType.text,
                    content=extracted[:12000],
                    excerpt=extracted[:500],
                    confidence=0.5 + (0.5 * keyword_overlap_score(query, extracted[:2000])),
                    metadata={"length": len(extracted)},
                )
            )

        for table in soup.find_all("table")[:2]:
            table_text = " ".join(table.stripped_strings)
            if table_text:
                evidence.append(
                    EvidenceItem(
                        task_id=task_id,
                        source_url=url,
                        evidence_type=EvidenceType.table,
                        content=table_text[:4000],
                        excerpt=table_text[:350],
                        confidence=0.55,
                        metadata={},
                    )
                )
        return evidence
