from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlmodel.ext.asyncio.session import AsyncSession

from app.agent.answerer import AnswerComposer
from app.agent.intake import QueryIntake
from app.agent.planner import ResearchPlanner
from app.agent.verification import Verifier
from app.browser.worker import BrowserWorker
from app.core.config import get_settings
from app.extraction.web import WebEvidenceExtractor
from app.extraction.youtube import YouTubeExtractor
from app.models.schemas import (
    BrowserAction,
    BrowserActionType,
    ErrorEvent,
    SourceItem,
    SourceType,
    TaskStatus,
)
from app.ranking.source_ranker import SourceRanker
from app.storage.repositories import TaskRepository
from app.utils.text import guess_domain

logger = logging.getLogger(__name__)


class ResearchOrchestrator:
    def __init__(self) -> None:
        self.intake = QueryIntake()
        self.planner = ResearchPlanner()
        self.extractor = WebEvidenceExtractor()
        self.youtube_extractor = YouTubeExtractor()
        self.ranker = SourceRanker()
        self.verifier = Verifier()
        self.answerer = AnswerComposer()
        self.settings = get_settings()

    async def run(self, session: AsyncSession, task_id: str) -> None:
        repo = TaskRepository(session)
        task = await repo.get_task(task_id)
        if not task:
            return

        try:
            await repo.update_task(task, status=TaskStatus.planning, current_step="Analyzing query")
            query, intent = await self.intake.analyze(task.query_text)
            plan = await self.planner.build_plan(query, intent)
            await repo.update_task(task, status=TaskStatus.planning, current_step="Plan generated", plan=plan)
            await repo.add_action(
                BrowserAction(
                    task_id=task_id,
                    action_type=BrowserActionType.search,
                    description="Generated research plan",
                    created_at=_utcnow(),
                    metadata=plan.model_dump(),
                )
            )

            sources: list[SourceItem] = []
            evidence = []
            best_video = None

            await repo.update_task(task, status=TaskStatus.researching, current_step="Browsing sources")
            async with BrowserWorker() as browser:
                for search_query in plan.search_queries[:2]:
                    await repo.add_action(
                        BrowserAction(
                            task_id=task_id,
                            action_type=BrowserActionType.search,
                            description=f"Searching the web for: {search_query}",
                            created_at=_utcnow(),
                            metadata={},
                        )
                    )
                    results = await browser.web_search(search_query, limit=plan.source_limit)
                    for index, result in enumerate(results[: plan.source_limit]):
                        try:
                            screenshot_path = self.settings.screenshot_dir / f"{task_id}_{len(sources)}.png"
                            page = await browser.capture_page(result.url, screenshot_file=screenshot_path)
                            source = SourceItem(
                                task_id=task_id,
                                source_type=SourceType.web,
                                title=page.title or result.title,
                                url=page.url,
                                domain=guess_domain(page.url),
                                snippet=result.snippet or page.text[:240],
                                author=page.metadata.get("author"),
                                published_at=page.metadata.get("published_time"),
                                metadata=page.metadata,
                            )
                            sources.append(source)
                            evidence.extend(
                                self.extractor.extract(
                                    task_id=task_id,
                                    query=query.text,
                                    url=page.url,
                                    html=page.html,
                                    text=page.text,
                                )
                            )
                            await repo.add_action(
                                BrowserAction(
                                    task_id=task_id,
                                    action_type=BrowserActionType.navigate,
                                    description=f"Visited source {index + 1}: {source.title}",
                                    url=page.url,
                                    screenshot_path=page.screenshot_path,
                                    created_at=_utcnow(),
                                    metadata={"domain": source.domain},
                                )
                            )
                            await repo.update_task(
                                task,
                                current_step=f"Collected evidence from {source.domain}",
                                latest_screenshot=page.screenshot_path,
                            )
                        except Exception as page_exc:
                            await repo.add_error(
                                ErrorEvent(
                                    task_id=task_id,
                                    message=f"Failed to process {result.url}: {page_exc}",
                                    recoverable=True,
                                    context={"url": result.url},
                                    created_at=_utcnow(),
                                )
                            )
                        if len(sources) >= plan.source_limit:
                            break
                    if len(sources) >= plan.source_limit:
                        break

                if intent.requires_youtube:
                    await repo.add_action(
                        BrowserAction(
                            task_id=task_id,
                            action_type=BrowserActionType.search,
                            description="Searching YouTube",
                            created_at=_utcnow(),
                            metadata={},
                        )
                    )
                    video_candidates = []
                    for video_query in plan.video_queries[:2]:
                        for result in await browser.youtube_search(video_query, limit=self.settings.max_video_results):
                            item = self.youtube_extractor.enrich(task_id=task_id, title=result.title, url=result.url)
                            item = self.youtube_extractor.score(query.text, item)
                            video_candidates.append(item)
                    if video_candidates:
                        best_video = sorted(video_candidates, key=lambda item: item.rank_score, reverse=True)[0]

            await repo.update_task(task, status=TaskStatus.ranking, current_step="Ranking sources")
            ranked_sources = self.ranker.rank(query.text, sources)[: self.settings.max_web_sources]
            await repo.replace_sources(task_id, ranked_sources)
            await repo.replace_evidence(task_id, evidence)

            await repo.update_task(task, status=TaskStatus.verifying, current_step="Verifying evidence")
            confidence = self.verifier.verify(ranked_sources, evidence)
            await repo.add_action(
                BrowserAction(
                    task_id=task_id,
                    action_type=BrowserActionType.verify,
                    description="Calculated confidence score",
                    created_at=_utcnow(),
                    metadata=confidence.model_dump(),
                )
            )

            await repo.update_task(task, status=TaskStatus.composing, current_step="Composing final answer")
            answer = await self.answerer.compose(
                query=query.text,
                sources=ranked_sources,
                evidence=evidence,
                confidence=confidence,
                best_video=best_video,
            )
            await repo.update_task(task, status=TaskStatus.completed, current_step="Completed", answer=answer)
            await repo.add_action(
                BrowserAction(
                    task_id=task_id,
                    action_type=BrowserActionType.compose,
                    description="Final answer composed",
                    created_at=_utcnow(),
                    metadata={"citations": answer.citations},
                )
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("Task failed")
            await repo.update_task(task, status=TaskStatus.failed, current_step="Failed")
            await repo.add_error(
                ErrorEvent(
                    task_id=task_id,
                    message=str(exc),
                    recoverable=True,
                    context={},
                    created_at=_utcnow(),
                )
            )
        finally:
            await session.close()


class TaskRunner:
    def __init__(self) -> None:
        self._running: dict[str, asyncio.Task] = {}

    def track(self, task_id: str, task: asyncio.Task) -> None:
        self._running[task_id] = task
        task.add_done_callback(lambda _: self._running.pop(task_id, None))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


task_runner = TaskRunner()
