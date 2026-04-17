from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import base64
import re
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from bs4 import BeautifulSoup
import httpx
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from app.core.config import get_settings
from app.safety.guardrails import safety_guard
from app.utils.text import keyword_overlap_score


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
            if "NotImplementedError: Browser initialization failed" in message:
                raise RuntimeError(
                    "Browser worker startup failed: Playwright could not initialize the browser in this "
                    "runtime. On Windows, start the API without hot reload using "
                    "`python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --app-dir .`."
                ) from exc
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
        search_urls = [
            f"https://duckduckgo.com/html/?q={quote_plus(query)}",
            f"https://www.bing.com/search?cc=us&setlang=en&q={quote_plus(query)}",
        ]
        for search_url in search_urls:
            try:
                await self.page.goto(search_url, wait_until="domcontentloaded")
                await self.page.wait_for_timeout(1200)
                if await self._is_bot_block_page():
                    continue
                if "bing.com" in self.page.url:
                    results = await self._parse_bing_results(query, limit)
                else:
                    results = await self._parse_duckduckgo_results(query, limit)
                relevant = _filter_results_by_query(query, results)
                if relevant:
                    return relevant[:limit]
            except Exception:
                continue
        fallback_results = await self._api_search_fallback(query, limit * 2)
        relevant_fallback = _filter_results_by_query(query, fallback_results)
        if relevant_fallback:
            return relevant_fallback[:limit]
        return fallback_results[:limit]

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
        try:
            await self.page.evaluate("window.scrollTo(0, 600)")
        except Exception:
            # Some pages navigate immediately after load; proceed with available content.
            pass
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

    async def _parse_duckduckgo_results(self, query: str, limit: int) -> list[SearchResult]:
        results: list[SearchResult] = []
        soup = BeautifulSoup(await self.page.content(), "html.parser")
        for node in soup.select(".result"):
            link_node = node.select_one(".result__title a")
            if not link_node:
                continue
            title = link_node.get_text(" ", strip=True)
            href = link_node.get("href")
            snippet_node = node.select_one(".result__snippet")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
            resolved_url = _resolve_result_url(href)
            if resolved_url and title:
                results.append(SearchResult(title=title, url=resolved_url, snippet=snippet))
            if len(results) >= limit:
                break
        return results

    async def _parse_bing_results(self, query: str, limit: int) -> list[SearchResult]:
        results: list[SearchResult] = []
        soup = BeautifulSoup(await self.page.content(), "html.parser")
        for node in soup.select("li.b_algo"):
            link_node = node.select_one("h2 a")
            if not link_node:
                continue
            title = link_node.get_text(" ", strip=True)
            href = link_node.get("href")
            snippet_node = node.select_one(".b_caption p")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
            resolved_url = _resolve_result_url(href)
            if resolved_url and title:
                results.append(SearchResult(title=title, url=resolved_url, snippet=snippet))
            if len(results) >= limit:
                break
        return results

    async def _is_bot_block_page(self) -> bool:
        body_text = (await self.page.locator("body").inner_text()).lower()
        blocked_markers = [
            "unfortunately, bots use duckduckgo too",
            "please complete the following challenge",
            "verify you are human",
            "captcha",
        ]
        return any(marker in body_text for marker in blocked_markers)

    async def _api_search_fallback(self, query: str, limit: int) -> list[SearchResult]:
        results: list[SearchResult] = []
        seen_urls: set[str] = set()
        timeout = httpx.Timeout(12.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "GeneralBrowserAIAgent/0.1 (+https://localhost; research bot for local development)"
                )
            },
        ) as client:
            try:
                ddg = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_redirect": "1", "no_html": "1"},
                )
                data = ddg.json()
                if data.get("AbstractURL"):
                    url = str(data["AbstractURL"])
                    seen_urls.add(url)
                    results.append(
                        SearchResult(
                            title=data.get("Heading") or query,
                            url=url,
                            snippet=data.get("AbstractText") or "DuckDuckGo instant answer",
                        )
                    )
                for topic in data.get("RelatedTopics", []):
                    if "Topics" in topic:
                        items = topic.get("Topics", [])
                    else:
                        items = [topic]
                    for item in items:
                        url = str(item.get("FirstURL") or "")
                        text = str(item.get("Text") or "")
                        if not url or url in seen_urls:
                            continue
                        seen_urls.add(url)
                        results.append(SearchResult(title=text[:120] or query, url=url, snippet=text))
                        if len(results) >= limit:
                            return results[:limit]
            except Exception:
                pass

            if len(results) < limit:
                try:
                    wiki = await client.get(
                        "https://en.wikipedia.org/w/api.php",
                        params={
                            "action": "query",
                            "list": "search",
                            "srsearch": query,
                            "format": "json",
                            "srlimit": str(limit),
                        },
                    )
                    pages = wiki.json().get("query", {}).get("search", [])
                    for page in pages:
                        title = str(page.get("title") or "").strip()
                        if not title:
                            continue
                        url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"
                        if url in seen_urls:
                            continue
                        seen_urls.add(url)
                        snippet_html = str(page.get("snippet") or "")
                        snippet = BeautifulSoup(snippet_html, "html.parser").get_text(" ", strip=True)
                        results.append(SearchResult(title=title, url=url, snippet=snippet))
                        if len(results) >= limit:
                            break
                except Exception:
                    pass
        return results[:limit]


def _get_meta_content(soup: BeautifulSoup, name: str) -> str | None:
    node = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
    return node.attrs.get("content") if node else None


def _resolve_result_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    query = parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return unquote(query["uddg"][0])
    if parsed.netloc.endswith("bing.com") and "u" in query and query["u"]:
        decoded = _decode_bing_redirect(query["u"][0])
        if decoded:
            return decoded
    return url


def _decode_bing_redirect(value: str) -> str | None:
    token = value
    if token.startswith("a1"):
        token = token[2:]
    padding = "=" * ((4 - len(token) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(token + padding).decode("utf-8", errors="ignore")
    except Exception:
        return None
    parsed = urlparse(decoded)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return decoded
    return None


def _filter_results_by_query(query: str, results: list[SearchResult]) -> list[SearchResult]:
    filtered: list[SearchResult] = []
    query_terms = {term for term in re.findall(r"\w+", query.lower()) if len(term) > 2}
    blocked_domain_hints = {"dictionary", "wiktionary.org", "thesaurus.com", "vocabulary.com"}
    for result in results:
        domain = urlparse(result.url).netloc.lower()
        if any(hint in domain for hint in blocked_domain_hints):
            continue
        combined = f"{result.title} {result.snippet or ''}"
        text_terms = set(re.findall(r"\w+", combined.lower()))
        overlap_count = len(query_terms & text_terms)
        overlap_score = keyword_overlap_score(query, combined)
        if overlap_count >= 2 or overlap_score >= 0.45:
            filtered.append(result)
    return filtered


