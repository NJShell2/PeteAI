import asyncio
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote
from bs4 import BeautifulSoup
import httpx

from app.config import WORKSPACES_DIR
from app.browser_path import launch_kwargs_for_chrome
from app.console import safe_print

# Try importing duckduckgo search. The package was renamed to `ddgs`; the legacy
# `duckduckgo_search` name is still supported as a fallback for existing installs.
try:
    from ddgs import DDGS
    HAS_DDGS = True
except ImportError:
    try:
        from duckduckgo_search import DDGS
        HAS_DDGS = True
    except ImportError:
        HAS_DDGS = False

# Try importing playwright
try:
    from playwright.async_api import async_playwright, Playwright, Browser
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

class BrowserTool:
    # DuckDuckGo/Bing ad-click wrappers and sponsored placements that must never
    # reach the agent as "research findings".
    AD_URL_MARKERS = (
        "duckduckgo.com/y.js", "bing.com/aclick", "ad_provider=", "ad_domain=",
        "/aclk?", "adclick", "googleadservices", "/pagead/",
    )
    AD_TITLE_MARKERS = ("adviewing ads", "sponsored", "ad·", "viewing ads is privacy protected")

    def __init__(self):
        self._playwright: Optional[Any] = None
        self._browser: Optional[Any] = None
        self._lock = asyncio.Lock()

    def _clean_result_url(self, url: str) -> str:
        """Unwraps search-engine redirect links and strips tracking parameters."""
        if not url:
            return ""
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        try:
            parsed = urlparse(url)
            # DDG wraps outbound links as //duckduckgo.com/l/?uddg=<encoded target>
            if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
                target = parse_qs(parsed.query).get("uddg", [""])[0]
                if target:
                    url = unquote(target)
                    parsed = urlparse(url)
            junk = ("utm_", "msclkid", "gclid", "fbclid", "ref_", "sponsored")
            query = {k: v for k, v in parse_qs(parsed.query).items() if not k.lower().startswith(junk)}
            return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
        except Exception:
            return url

    def _is_ad(self, url: str, title: str) -> bool:
        low_url = url.lower()
        if any(marker in low_url for marker in self.AD_URL_MARKERS):
            return True
        low_title = title.lower()
        return any(marker in low_title for marker in self.AD_TITLE_MARKERS)

    def _normalize_results(self, raw: List[Dict[str, Any]], max_results: int) -> List[Dict[str, str]]:
        """Filters ads, unwraps redirects, de-duplicates and truncates to max_results."""
        cleaned: List[Dict[str, str]] = []
        seen = set()
        for r in raw or []:
            url = self._clean_result_url(str(r.get("url") or r.get("href") or r.get("link") or ""))
            title = str(r.get("title") or "").strip()
            snippet = str(r.get("snippet") or r.get("body") or "").strip()
            if not url.startswith("http"):
                continue
            if self._is_ad(url, title):
                continue
            key = url.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append({"title": title or url, "url": url, "snippet": snippet})
            if len(cleaned) >= max_results:
                break
        return cleaned

    async def _get_browser(self):
        if not HAS_PLAYWRIGHT:
            return None
        async with self._lock:
            if not self._browser:
                try:
                    self._playwright = await async_playwright().start()
                    self._browser = await self._playwright.chromium.launch(
                        **launch_kwargs_for_chrome(headless=True),
                        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
                    )
                except Exception as e:
                    safe_print(f"Playwright chromium launch failed (may need 'playwright install chromium'): {e}")
                    self._browser = None
            return self._browser

    async def search_web(self, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        """Search the web using DuckDuckGo (ddgs library, HTML endpoint fallback).

        Ad placements, redirect wrappers, tracking parameters and duplicates are
        filtered out so the agent only ever sees real, citable results.
        """
        results: List[Dict[str, str]] = []
        if HAS_DDGS:
            try:
                # DDGS is synchronous, so run it in a worker thread.
                def _do_ddg_search():
                    with DDGS() as ddgs:
                        return list(ddgs.text(query, max_results=max_results * 3))
                # Hard timeout so a hung search can never stall the agent loop.
                raw_results = await asyncio.wait_for(asyncio.to_thread(_do_ddg_search), timeout=20)
                results = self._normalize_results(raw_results, max_results)
                if results:
                    return results
            except Exception as e:
                safe_print(f"DuckDuckGo search error: {e}")

        # Fallback using httpx directly
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            }) as client:
                resp = await client.get("https://html.duckduckgo.com/html/", params={"q": query})
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    raw: List[Dict[str, str]] = []
                    for r in soup.select(".result"):
                        title_el = r.select_one(".result__title")
                        snippet_el = r.select_one(".result__snippet")
                        url_el = r.select_one(".result__url")
                        link_el = r.select_one(".result__a")
                        if not title_el:
                            continue
                        url = link_el.get("href", "") if link_el else ""
                        if not url and url_el:
                            url = "https://" + url_el.get_text(strip=True)
                        raw.append({
                            "title": title_el.get_text(strip=True),
                            "url": url,
                            "snippet": snippet_el.get_text(strip=True) if snippet_el else ""
                        })
                    results = self._normalize_results(raw, max_results)
        except Exception as e:
            safe_print(f"Fallback search error: {e}")

        return results

    async def browse_page(self, url: str) -> Dict[str, Any]:
        """Navigate to a URL and extract text/markdown content."""
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url

        browser = await self._get_browser()
        if browser:
            try:
                page = await browser.new_page()
                await page.set_extra_http_headers({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                })
                # Wait for domcontentloaded with 15s timeout
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                title = await page.title()
                html = await page.content()
                await page.close()
                clean_text = self._clean_html(html)
                return {
                    "url": url,
                    "title": title,
                    "content": clean_text[:8000], # Keep within reasonable context limit
                    "method": "playwright"
                }
            except Exception as e:
                safe_print(f"Playwright navigation failed, falling back to httpx: {e}")

        # Fallback to HTTP request
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            }) as client:
                resp = await client.get(url)
                soup = BeautifulSoup(resp.text, "html.parser")
                title = soup.title.string.strip() if soup.title and soup.title.string else url
                clean_text = self._clean_html(resp.text)
                return {
                    "url": url,
                    "title": title,
                    "content": clean_text[:8000],
                    "method": "httpx"
                }
        except Exception as e:
            return {
                "url": url,
                "error": f"Failed to fetch {url}: {str(e)}",
                "content": ""
            }

    async def extract_links(self, url: str, same_domain_only: bool = True,
                            pattern: str = "", max_links: int = 60) -> Dict[str, Any]:
        """Returns the clickable links on a page so the agent can follow them.

        This is what turns read-only fetching into real browsing: the agent sees
        a site's navigation the way a person does and can then visit a link.
        """
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url

        browser = await self._get_browser()
        html = None
        if browser:
            page = None
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 800})
                await page.goto(url, wait_until="networkidle", timeout=20000)
                html = await page.content()
            except Exception as e:
                safe_print(f"Playwright link extraction failed, falling back to httpx: {e}")
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass

        if html is None:
            try:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                }) as client:
                    html = (await client.get(url)).text
            except Exception as e:
                return {"url": url, "error": f"Failed to fetch {url}: {e}", "links": []}

        soup = BeautifulSoup(html, "html.parser")
        base_netloc = urlparse(url).netloc.lower().lstrip("www.")
        needle = (pattern or "").lower()
        links, seen = [], set()

        for anchor in soup.find_all("a", href=True):
            href = self._clean_result_url(anchor["href"])
            if not href.startswith("http"):
                continue
            netloc = urlparse(href).netloc.lower()
            if same_domain_only and not (netloc == base_netloc
                                         or netloc.lstrip("www.") == base_netloc
                                         or base_netloc in netloc):
                continue
            if needle and needle not in href.lower():
                continue
            text = anchor.get_text(strip=True)[:120] or href
            key = href.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            links.append({"text": text, "url": href})
            if len(links) >= max_links:
                break

        return {
            "url": url,
            "title": soup.title.string.strip() if soup.title and soup.title.string else url,
            "link_count": len(links),
            "links": links,
        }

    async def interact_with_page(self, url: str, actions: List[Dict[str, Any]],
                                 wait_selector: str = "",
                                 final_content_chars: int = 8000) -> Dict[str, Any]:
        """Drives a page like a user: fill inputs, press keys, click, scroll, wait.

        Each action is {"action": "fill"|"click"|"press"|"scroll"|"wait", ...}:
          fill   {"selector": css, "value": text}
          click  {"selector": css}
          press  {"key": "Enter"}
          scroll {"amount": 800}
          wait   {"ms": 1000}
        Returns the page text after the actions run, so the agent can read the
        result of a search or a click-through without a second round trip.
        """
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url

        browser = await self._get_browser()
        if not browser:
            return {"url": url, "error": "Playwright is not installed. Run "
                                          "'playwright install chromium' to enable interaction."}

        page = None
        log: List[str] = []
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # slow single-page apps never go idle; carry on anyway

            for action in actions or []:
                kind = (action.get("action") or "").lower()
                try:
                    if kind == "fill":
                        await page.fill(action["selector"], action.get("value", ""))
                        log.append(f"filled {action['selector']}")
                    elif kind == "click":
                        await page.click(action["selector"], timeout=10000)
                        log.append(f"clicked {action['selector']}")
                        try:
                            await page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass
                    elif kind == "press":
                        await page.keyboard.press(action.get("key", "Enter"))
                        log.append(f"pressed {action.get('key', 'Enter')}")
                    elif kind == "scroll":
                        await page.mouse.wheel(0, int(action.get("amount", 800)))
                        await page.wait_for_timeout(400)
                        log.append(f"scrolled {action.get('amount', 800)}px")
                    elif kind == "wait":
                        await page.wait_for_timeout(min(int(action.get("ms", 1000)), 15000))
                        log.append("waited")
                    else:
                        log.append(f"skipped unknown action '{kind}'")
                except Exception as action_error:
                    # A stale selector should not kill the run: report it and let
                    # the agent retry with a different selector.
                    log.append(f"action '{kind}' failed: {action_error}")

            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=10000)
                    log.append(f"waited for selector {wait_selector}")
                except Exception as wait_error:
                    log.append(f"wait for {wait_selector} timed out: {wait_error}")

            return {
                "url": page.url,
                "title": await page.title(),
                "actions_performed": log,
                "content": self._clean_html(await page.content())[:final_content_chars],
                "method": "playwright",
            }
        except Exception as e:
            return {"url": url, "error": f"Page interaction failed: {e}",
                    "actions_performed": log}
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    async def take_screenshot(self, url: str, workspace_id: str, filename: Optional[str] = None) -> Dict[str, Any]:
        """Capture screenshot and store it in the workspace folder."""
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url

        browser = await self._get_browser()
        if not browser:
            return {"error": "Playwright browser is not initialized. Run 'playwright install chromium' to enable screenshots."}

        ws_dir = WORKSPACES_DIR / workspace_id
        ws_dir.mkdir(parents=True, exist_ok=True)
        
        if not filename:
            clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", url.replace("https://", "").replace("http://", ""))[:30]
            filename = f"screenshot_{clean_name}.png"
            
        target_path = ws_dir / filename
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.goto(url, wait_until="networkidle", timeout=20000)
            await page.screenshot(path=str(target_path), full_page=False)
            await page.close()
            return {
                "success": True,
                "filename": filename,
                "path": str(target_path),
                "url": url
            }
        except Exception as e:
            return {"error": f"Screenshot failed: {str(e)}"}

    def _clean_html(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "iframe"]):
            tag.decompose()
        
        text = soup.get_text(separator="\n")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

browser_tool = BrowserTool()
