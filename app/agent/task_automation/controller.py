from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .composer import ComposedOutput, ResultComposer
from .executor import BrowserExecutor, ExecutionResult
from .extractor import ResultExtractor
from .planner import TaskPlanner
from .schema import ActionPlan

logger = logging.getLogger(__name__)


@dataclass
class AutomationRunResult:
    plan: ActionPlan
    execution: ExecutionResult
    items: list[dict[str, Any]]
    output: ComposedOutput


class TaskAutomationController:
    """Orchestrates: planner -> executor -> extractor -> composer."""

    def __init__(self, *, task_id: str = "") -> None:
        self.task_id = task_id
        self.planner = TaskPlanner()
        self.executor = BrowserExecutor(task_id=task_id)
        self.extractor = ResultExtractor()
        self.composer = ResultComposer()

    async def run(self, user_query: str) -> AutomationRunResult:
        logger.info("TaskAutomation: query=%s", user_query)

        plan = await self.planner.plan(user_query)
        self._validate_plan(plan)
        logger.info("TaskAutomation: plan objective=%s steps=%d", plan.objective, len(plan.steps))

        execution = await self.executor.run(plan)

        normalized_items = []
        for blob in execution.collected_html:
            url = str(blob.get("url") or "")
            html = str(blob.get("html") or "")
            items = self.extractor.extract_items(
                html=html,
                base_url=url,
                max_items=30,
                user_query=user_query,
                intent=plan.intent.value if hasattr(plan.intent, "value") else str(plan.intent),
            )
            normalized_items.extend(items)

            # Stop early if we already have enough candidates
            min_results = int((plan.stop_when or {}).get("min_results", 5))
            if len(normalized_items) >= min_results:
                break

        composed = self.composer.compose(user_query=user_query, items=normalized_items, top_k=5)

        return AutomationRunResult(
            plan=plan,
            execution=execution,
            items=[it.__dict__ for it in normalized_items],
            output=composed,
        )

    def _validate_plan(self, plan: ActionPlan) -> None:
        steps = plan.ordered_steps()
        if not steps:
            raise ValueError("ActionPlan has no steps")
        # Ensure sequential step numbering.
        expected = list(range(1, len(steps) + 1))
        got = [s.step for s in steps]
        if got != expected:
            raise ValueError(f"ActionPlan step numbering must be sequential: expected {expected}, got {got}")
        # Ensure at least one extraction action exists.
        if not any(s.action.value in {"extract_list", "extract_detail"} for s in steps):
            raise ValueError("ActionPlan must include at least one extraction step")

