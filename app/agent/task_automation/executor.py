from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.browser.worker import BrowserWorker, SearchResult
from app.safety.guardrails import safety_guard

from .schema import ActionPlan, ActionStep, ActionType

logger = logging.getLogger(__name__)


@dataclass
class ExecutionLog:
    step: int
    action: str
    ok: bool
    message: str
    data: dict[str, Any]


@dataclass
class ExecutionResult:
    logs: list[ExecutionLog]
    opened_urls: list[str]
    collected_html: list[dict[str, Any]]  # {url, title, html}
    search_results: list[SearchResult]


class BrowserExecutor:
    """Execute an ActionPlan using the existing BrowserWorker (Playwright wrapper)."""

    def __init__(self, *, task_id: str = "") -> None:
        self.task_id = task_id
        self._browser: BrowserWorker | None = None

        self._search_results: list[SearchResult] = []
        self._opened_urls: list[str] = []
        self._collected_html: list[dict[str, Any]] = []
        self._logs: list[ExecutionLog] = []

    async def run(self, plan: ActionPlan) -> ExecutionResult:
        self._browser = BrowserWorker(task_id=self.task_id)
        await self._browser.__aenter__()
        try:
            for step in plan.ordered_steps():
                if await self._execute_step(plan, step) is False:
                    break
            return ExecutionResult(
                logs=self._logs,
                opened_urls=self._opened_urls,
                collected_html=self._collected_html,
                search_results=self._search_results,
            )
        finally:
            await self._browser.__aexit__(None, None, None)

    async def _execute_step(self, plan: ActionPlan, step: ActionStep) -> bool:
        assert self._browser is not None
        action = step.action
        params = step.params or {}

        try:
            if action == ActionType.search:
                q_index = int(params.get("query_index", 0))
                limit = int(params.get("limit", 8))
                query_text = plan.search_queries[q_index].text if plan.search_queries else plan.objective
                self._search_results = await self._browser.web_search(query_text, limit=limit)
                self._log(step, True, f"Search returned {len(self._search_results)} results", {"query": query_text})
                return True

            if action == ActionType.open_result:
                direct_url = params.get("url")
                if direct_url:
                    url = str(direct_url)
                else:
                    idx = int(params.get("result_index", 0))
                    if not self._search_results or idx >= len(self._search_results):
                        self._log(step, False, "No search results to open", {})
                        return True
                    url = self._search_results[idx].url
                if not safety_guard.is_safe_url(url):
                    self._log(step, False, "Blocked unsafe URL", {"url": url})
                    return True
                await self._safe_goto(url)
                self._opened_urls.append(url)
                self._log(step, True, "Opened result", {"url": url})
                return True

            if action == ActionType.click:
                selector = str(params.get("selector") or "")
                if not selector:
                    self._log(step, False, "Missing selector", {})
                    return True
                await self.safe_click(selector)
                self._log(step, True, "Clicked", {"selector": selector})
                return True

            if action == ActionType.type:
                selector = str(params.get("selector") or "")
                text = str(params.get("text") or "")
                if not selector or not text:
                    self._log(step, False, "Missing selector/text", {})
                    return True
                await self.safe_type(selector, text)
                self._log(step, True, "Typed", {"selector": selector})
                return True

            if action == ActionType.navigate_back:
                await self._browser.page.go_back(wait_until="domcontentloaded")
                self._log(step, True, "Navigated back", {})
                return True

            if action in {ActionType.extract_list, ActionType.extract_detail}:
                html = await self.safe_extract(mode="html")
                title = await self._browser.page.title()
                url = self._browser.page.url
                self._collected_html.append({"url": url, "title": title, "html": html})
                self._log(step, True, "Extracted page HTML", {"url": url, "title": title})
                # Stop early if sufficient results heuristic
                return True

            if action == ActionType.rank:
                self._log(step, True, "Ranking delegated to controller/composer", {})
                return True

            if action == ActionType.stop:
                self._log(step, True, "Stopped", {})
                return False

            # apply_filter is intentionally treated like click/type combos; step params can define selectors.
            if action == ActionType.apply_filter:
                # Optional: click selector, then type selector.
                click_sel = params.get("click_selector")
                type_sel = params.get("type_selector")
                type_text = params.get("text")
                if click_sel:
                    await self.safe_click(str(click_sel))
                if type_sel and type_text:
                    await self.safe_type(str(type_sel), str(type_text))
                self._log(step, True, "Applied filter", {"params": params})
                return True

            self._log(step, False, "Unsupported action", {"action": str(action)})
            return True

        except Exception as exc:
            self._log(step, False, f"Step failed: {exc}", {"params": params})
            return True

    async def _safe_goto(self, url: str) -> None:
        assert self._browser is not None
        page = self._browser.page
        await page.goto(url, wait_until="domcontentloaded")
        # JS-heavy sites: give the page time to render dynamic content.
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)

    async def safe_click(self, selector: str, *, timeout_ms: int = 8000) -> None:
        assert self._browser is not None
        page = self._browser.page
        try:
            await page.locator(selector).first.click(timeout=timeout_ms)
        except PlaywrightTimeoutError:
            # Retry once after a small wait
            await page.wait_for_timeout(800)
            await page.locator(selector).first.click(timeout=timeout_ms)

    async def safe_type(self, selector: str, text: str, *, timeout_ms: int = 8000) -> None:
        assert self._browser is not None
        page = self._browser.page
        try:
            loc = page.locator(selector).first
            await loc.click(timeout=timeout_ms)
            await loc.fill(text, timeout=timeout_ms)
        except PlaywrightTimeoutError:
            await page.wait_for_timeout(800)
            loc = page.locator(selector).first
            await loc.click(timeout=timeout_ms)
            await loc.fill(text, timeout=timeout_ms)

    async def safe_extract(self, *, mode: str = "text") -> str:
        assert self._browser is not None
        page = self._browser.page
        # Avoid long hangs on very heavy pages.
        for _ in range(2):
            try:
                # give dynamic sites a short settle time before extraction
                await page.wait_for_timeout(900)
                if mode == "html":
                    return await asyncio.wait_for(page.content(), timeout=12)
                return await asyncio.wait_for(page.locator("body").inner_text(), timeout=12)
            except Exception:
                await page.wait_for_timeout(600)
        # last attempt without wait_for so we get real exception if it fails
        if mode == "html":
            return await page.content()
        return await page.locator("body").inner_text()

    def _log(self, step: ActionStep, ok: bool, message: str, data: dict[str, Any]) -> None:
        self._logs.append(
            ExecutionLog(step=step.step, action=str(step.action.value), ok=ok, message=message, data=data)
        )

