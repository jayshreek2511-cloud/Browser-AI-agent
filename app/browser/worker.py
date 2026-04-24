from __future__ import annotations
import asyncio

from dataclasses import dataclass
from pathlib import Path
import base64
import re
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from bs4 import BeautifulSoup
import httpx
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from app.core.config import get_settings
from app.core.stream import screencast_stream
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
    def __init__(self, task_id: str = "") -> None:
        self.settings = get_settings()
        self.task_id = task_id
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._cdp_sessions = []

    async def _attach_screencast(self, page: Page):
        if not self.task_id:
            return
        try:
            page_id = str(id(page))
            cdp = await page.context.new_cdp_session(page)
            self._cdp_sessions.append(cdp)
            await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 60, "everyNthFrame": 1})
            
            def handle_screencast(event):
                session_id = event.get("sessionId")
                data = event.get("data")
                asyncio.create_task(cdp.send("Page.screencastFrameAck", {"sessionId": session_id}))
                asyncio.create_task(screencast_stream.broadcast(self.task_id, page_id, f"data:image/jpeg;base64,{data}"))
                
            cdp.on("Page.screencastFrame", handle_screencast)
        except Exception as e:
            print(f"Failed to attach screencast: {e}")

    async def __aenter__(self) -> "BrowserWorker":
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=self.settings.browser_headless,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                ],
            )
            self.context = await self.browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                },
            )
            self.page = await self.context.new_page()
            await self._attach_screencast(self.page)
            # Mask Playwright fingerprint
            await self.page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
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
        """Search via Playwright (stealth) + research APIs for diverse, non-Wikipedia sources."""
        results: list[SearchResult] = []
        seen: set[str] = set()

        # --- Primary: Try DuckDuckGo with stealth Playwright ---
        try:
            ddg_results = await self._playwright_ddg_search(query, limit * 2)
            for r in ddg_results:
                if r.url not in seen:
                    seen.add(r.url)
                    results.append(r)
        except Exception:
            pass

        # --- Secondary: Try Bing with stealth Playwright ---
        if len(results) < limit:
            try:
                bing_results = await self._playwright_bing_search(query, limit * 2)
                for r in bing_results:
                    if r.url not in seen:
                        seen.add(r.url)
                        results.append(r)
            except Exception:
                pass

        # --- Always: Supplement with research-grade APIs (arXiv, Semantic Scholar) ---
        try:
            research_results = await self._research_source_search(query, limit)
            for r in research_results:
                if r.url not in seen:
                    seen.add(r.url)
                    results.append(r)
        except Exception:
            pass

        # --- Last resort: DuckDuckGo Instant Answers API ---
        if not results:
            fallback = await self._api_search_fallback(query, limit * 2)
            for r in fallback:
                if r.url not in seen:
                    seen.add(r.url)
                    results.append(r)

        relevant = _filter_results_by_query(query, results)
        return relevant[:limit]

    async def _playwright_ddg_search(self, query: str, limit: int) -> list[SearchResult]:
        """DuckDuckGo search via Playwright stealth browser."""
        search_page = await self.context.new_page()
        await self._attach_screencast(search_page)
        await search_page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        try:
            await search_page.goto(
                f"https://html.duckduckgo.com/html/?q={quote_plus(query)}&kl=us-en",
                wait_until="domcontentloaded",
            )
            await search_page.wait_for_timeout(1500)
            body_text = (await search_page.locator("body").inner_text()).lower()
            if "bots use duckduckgo" in body_text or "captcha" in body_text or "verify" in body_text:
                return []
            soup = BeautifulSoup(await search_page.content(), "html.parser")
        finally:
            await search_page.close()
        results: list[SearchResult] = []
        for node in soup.select(".result"):
            link_node = node.select_one(".result__title a")
            if not link_node:
                continue
            title = link_node.get_text(" ", strip=True)
            href = str(link_node.get("href") or "")
            snippet_node = node.select_one(".result__snippet")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
            resolved = _resolve_result_url(href)
            if resolved and title:
                results.append(SearchResult(title=title, url=resolved, snippet=snippet))
            if len(results) >= limit:
                break
        return results

    async def _playwright_bing_search(self, query: str, limit: int) -> list[SearchResult]:
        """Bing search via Playwright stealth browser."""
        search_page = await self.context.new_page()
        await self._attach_screencast(search_page)
        await search_page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        try:
            await search_page.goto(
                f"https://www.bing.com/search?q={quote_plus(query)}&cc=us&setlang=en",
                wait_until="domcontentloaded",
            )
            await search_page.wait_for_timeout(1500)
            body_text = (await search_page.locator("body").inner_text()).lower()
            if "captcha" in body_text or "verify" in body_text or "solve the challenge" in body_text:
                return []
            soup = BeautifulSoup(await search_page.content(), "html.parser")
        finally:
            await search_page.close()
        results: list[SearchResult] = []
        for node in soup.select("li.b_algo"):
            link_node = node.select_one("h2 a")
            if not link_node:
                continue
            title = link_node.get_text(" ", strip=True)
            href = str(link_node.get("href") or "")
            snippet_node = node.select_one(".b_caption p")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
            resolved = _resolve_result_url(href)
            if resolved and title:
                results.append(SearchResult(title=title, url=resolved, snippet=snippet))
            if len(results) >= limit:
                break
        return results

    async def _research_source_search(self, query: str, limit: int) -> list[SearchResult]:
        """Search research-grade sources: arXiv, Semantic Scholar, and Wikipedia."""
        results: list[SearchResult] = []
        headers = {
            "User-Agent": "ResearchAgent/1.0 (bot@example.com)",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
            # Wikipedia API
            try:
                wiki_resp = await client.get(
                    "https://en.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "list": "search",
                        "srsearch": query,
                        "utf8": "",
                        "format": "json",
                        "srlimit": limit
                    }
                )
                if wiki_resp.status_code == 200:
                    for item in wiki_resp.json().get("query", {}).get("search", []):
                        title = item.get("title", "")
                        snippet = BeautifulSoup(item.get("snippet", ""), "html.parser").get_text()
                        if title:
                            url = f"https://en.wikipedia.org/wiki/{quote_plus(title.replace(' ', '_'))}"
                            results.append(SearchResult(title=f"[Wikipedia] {title}", url=url, snippet=snippet))
            except Exception:
                pass

            # arXiv API
            try:
                arxiv_resp = await client.get(
                    "https://export.arxiv.org/api/query",
                    params={"search_query": f"all:{query}", "max_results": str(limit), "sortBy": "relevance"},
                )
                if arxiv_resp.status_code == 200:
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(arxiv_resp.text)
                    ns = {"atom": "http://www.w3.org/2005/Atom"}
                    for entry in root.findall("atom:entry", ns)[:limit]:
                        title_el = entry.find("atom:title", ns)
                        summary_el = entry.find("atom:summary", ns)
                        id_el = entry.find("atom:id", ns)
                        title = (title_el.text or "").strip().replace("\n", " ") if title_el is not None else ""
                        summary = (summary_el.text or "").strip()[:300] if summary_el is not None else ""
                        url = (id_el.text or "").strip() if id_el is not None else ""
                        if title and url:
                            results.append(SearchResult(title=f"[arXiv] {title}", url=url, snippet=summary))
            except Exception:
                pass

            # Semantic Scholar API
            try:
                ss_resp = await client.get(
                    "https://api.semanticscholar.org/graph/v1/paper/search",
                    params={"query": query, "limit": str(limit), "fields": "title,url,abstract"},
                    headers={"User-Agent": "ResearchAgent/1.0"},
                )
                if ss_resp.status_code == 200:
                    for paper in ss_resp.json().get("data", [])[:limit]:
                        title = str(paper.get("title") or "")
                        url = str(paper.get("url") or "")
                        abstract = str(paper.get("abstract") or "")[:300]
                        if title and url:
                            results.append(SearchResult(title=f"[Paper] {title}", url=url, snippet=abstract))
            except Exception:
                pass

        return results[:limit]

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
        for cdp in self._cdp_sessions:
            try:
                await cdp.detach()
            except Exception:
                pass
        self._cdp_sessions = []
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
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
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
        return results[:limit]



def _get_meta_content(soup: BeautifulSoup, name: str) -> str | None:
    node = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
    return node.attrs.get("content") if node else None


def _resolve_result_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
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
    from app.utils.text import STOP_WORDS
    query_terms = {term for term in re.findall(r"\w+", query.lower()) if len(term) > 2 and term not in STOP_WORDS}
    blocked_domain_hints = {"dictionary", "wiktionary.org", "thesaurus.com", "vocabulary.com"}
    for result in results:
        domain = urlparse(result.url).netloc.lower()
        if any(hint in domain for hint in blocked_domain_hints):
            continue
        combined = f"{result.title} {result.snippet or ''}"
        text_terms = set(re.findall(r"\w+", combined.lower()))
        overlap_count = len(query_terms & text_terms)
        overlap_score = keyword_overlap_score(query, combined)
        if overlap_count >= 2 or overlap_score >= 0.30:
            filtered.append(result)
    return filtered


