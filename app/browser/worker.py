from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from app.core.config import get_settings
from app.safety.guardrails import safety_guard


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


@dataclass
class PageCapture:
    url: str
    title: str
    html: str
    text: str
    screenshot_path: str | None
    metadata: dict


class BrowserWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def __aenter__(self) -> "BrowserWorker":
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=self.settings.browser_headless)
            self.context = await self.browser.new_context(viewport={"width": 1440, "height": 900})
            self.page = await self.context.new_page()
            self.page.set_default_timeout(self.settings.browser_timeout_ms)
            return self
        except Exception as exc:
            await self._safe_shutdown()
            message = str(exc)
            if "Executable doesn't exist" in message or "browserType.launch" in message:
                raise RuntimeError(
                    "Chromium could not be launched by Playwright. Run "
                    "`python -m playwright install chromium` inside the active virtual environment "
                    "and restart the server."
                ) from exc
            raise RuntimeError(f"Browser worker startup failed: {message}") from exc

    async def __aexit__(self, *_: object) -> None:
        await self._safe_shutdown()

    async def web_search(self, query: str, limit: int = 5) -> list[SearchResult]:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        await self.page.goto(url, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(1000)
        results = []
        for node in await self.page.query_selector_all(".result"):
            title_node = await node.query_selector(".result__title")
            link_node = await node.query_selector(".result__title a")
            snippet_node = await node.query_selector(".result__snippet")
            if not link_node or not title_node:
                continue
            href = await link_node.get_attribute("href")
            title = (await title_node.inner_text()).strip()
            snippet = (await snippet_node.inner_text()).strip() if snippet_node else ""
            if href and title:
                results.append(SearchResult(title=title, url=href, snippet=snippet))
            if len(results) >= limit:
                break
        return results

    async def youtube_search(self, query: str, limit: int = 5) -> list[SearchResult]:
        url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        await self.page.goto(url, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2500)
        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.35)")
        await self.page.wait_for_timeout(1000)
        anchors = await self.page.query_selector_all("a#video-title")
        results = []
        for anchor in anchors[: limit * 2]:
            href = await anchor.get_attribute("href")
            title = ((await anchor.get_attribute("title")) or (await anchor.inner_text()) or "").strip()
            if href and "/watch" in href and title:
                results.append(
                    SearchResult(
                        title=title,
                        url=f"https://www.youtube.com{href}",
                        snippet="YouTube search result",
                    )
                )
            if len(results) >= limit:
                break
        return results

    async def capture_page(self, url: str, screenshot_file: Path | None = None) -> PageCapture:
        if not safety_guard.is_safe_url(url):
            raise ValueError(safety_guard.reason_for_block(url))
        await self.page.goto(url, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(1500)
        await self.page.evaluate("window.scrollTo(0, 600)")
        await self.page.wait_for_timeout(500)
        html = await self.page.content()
        title = await self.page.title()
        text = await self.page.locator("body").inner_text()
        screenshot_path = None
        if screenshot_file:
            screenshot_file.parent.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=str(screenshot_file), full_page=False)
            screenshot_path = str(screenshot_file)
        soup = BeautifulSoup(html, "html.parser")
        metadata = {
            "description": _get_meta_content(soup, "description"),
            "author": _get_meta_content(soup, "author"),
            "published_time": _get_meta_content(soup, "article:published_time"),
        }
        return PageCapture(
            url=url,
            title=title,
            html=html,
            text=text,
            screenshot_path=screenshot_path,
            metadata=metadata,
        )

    async def _safe_shutdown(self) -> None:
        if self.context is not None:
            await self.context.close()
            self.context = None
        if self.browser is not None:
            await self.browser.close()
            self.browser = None
        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None


def _get_meta_content(soup: BeautifulSoup, name: str) -> str | None:
    node = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
    return node.attrs.get("content") if node else None
