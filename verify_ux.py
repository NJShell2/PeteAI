"""Verifies the focus / takeover UX changes in a real browser.

Checks that the JS parses (a syntax error shows up as a page error), that the
exit control is on-screen in both layouts, that Escape leaves focus mode, and
that the cursor is no longer a crosshair. Read-only against the source.
"""
import asyncio
import os
import shutil
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from playwright.async_api import async_playwright

BASE = os.path.dirname(os.path.abspath(__file__))
# Browser binary for the harness. Override with PETE_TEST_CHROME; when unset,
# Playwright launches its own bundled Chromium (needs `playwright install`).
CHROME = os.environ.get("PETE_TEST_CHROME")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve():
    """Serve the real static tree over HTTP; index.html uses root-absolute paths."""
    tmp = tempfile.mkdtemp(prefix="peteux_")
    root = os.path.join(tmp, "static")
    shutil.copytree(os.path.join(BASE, "app", "static"), root, dirs_exist_ok=True)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=root))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


async def main():
    port = serve()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, **(
            {"executable_path": CHROME} if CHROME else {}))
        page = await browser.new_page(viewport={"width": 1600, "height": 900})

        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        await page.goto(f"http://127.0.0.1:{port}/index.html")
        await page.wait_for_timeout(600)

        # The stub server has no API, so POSTs bounce with 501 HTML. That is
        # harness noise, not a script error -- only real page errors count.
        def is_noise(text):
            return ("Failed to load resource" in text or "net::ERR" in text
                    or "404" in text or "501" in text
                    or "Error response" in text or "DOCTYPE" in text)

        real = [e for e in errors if not is_noise(e)]
        check("JS parses with no page errors", not real, "; ".join(real[:3])[:200])

        await page.click('[data-tab="tab-browser"]')
        await page.wait_for_timeout(200)

        vp = page.locator("#browserViewport")
        cur = await vp.evaluate("e => getComputedStyle(e).cursor")
        check("cursor is not crosshair (Pete driving)", cur == "default", cur)

        # Enter focus mode via the button.
        await page.click("#btnToggleFocusMode")
        await page.wait_for_timeout(300)

        info = await page.evaluate("""() => {
          const b = document.getElementById('btnToggleFocusMode');
          const r = b.getBoundingClientRect();
          return {
            focus: document.querySelector('.app-container').classList.contains('focus-mode'),
            pressed: b.getAttribute('aria-pressed'),
            label: document.getElementById('focusToggleLabel').textContent,
            active: b.classList.contains('is-active'),
            onScreen: r.left >= 0 && r.right <= window.innerWidth,
            right: Math.round(r.right), win: window.innerWidth,
          };
        }""")
        check("focus mode engaged", info["focus"])
        check("aria-pressed=true when active", info["pressed"] == "true", info["pressed"])
        check("button shows is-active styling", info["active"])
        check("label stays stable (not 'Exit')",
              info["label"] == "Focus browser", info["label"])
        check("EXIT BUTTON IS ON-SCREEN IN FOCUS MODE", info["onScreen"],
              f"right={info['right']} window={info['win']}")

        # The chat rail must no longer overflow now that it sheds controls.
        overflow = await page.evaluate(
            "() => { const b=document.querySelector('.chat-topbar');"
            " return b.scrollWidth - b.clientWidth; }")
        check("chat topbar no longer overflows", overflow <= 0, f"overflow={overflow}px")

        # Escape must leave focus mode.
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
        check("Escape leaves focus mode",
              not await page.evaluate("() => document.querySelector('.app-container')"
                                     ".classList.contains('focus-mode')"))
        check("aria-pressed=false after Escape",
              await page.get_attribute("#btnToggleFocusMode", "aria-pressed") == "false")

        # Simulate the user taking control, then check the states flip.
        await page.evaluate("() => applyBrowserControlState({ human_control: true })")
        await page.wait_for_timeout(150)
        cur = await vp.evaluate("e => getComputedStyle(e).cursor")
        check("cursor is pointer while user drives", cur == "pointer", cur)
        check("takeover aria-pressed=true",
              await page.get_attribute("#btnBrowserTakeover", "aria-pressed") == "true")
        check("Pete banner hidden while user drives",
              await page.evaluate("() => getComputedStyle("
                                  "document.getElementById('browserPeteBanner')).display") == "none")

        # A click on the letterbox must leave a visible marker.
        # setupBrowserInput() is normally called when the live socket opens,
        # which never happens against this static harness, so bind it directly.
        await page.evaluate("() => setupBrowserInput()")
        # The default 16:10 viewport matches the 1280x800 page exactly, so there
        # is no letterbox to click. Focus mode stretches the box and opens bars
        # down each side, which creates a genuine miss to test against.
        await page.click("#btnToggleFocusMode")
        await page.wait_for_timeout(300)
        box = await vp.bounding_box()
        await page.mouse.click(box["x"] + 3, box["y"] + box["height"] / 2)
        await page.wait_for_timeout(200)
        check("letterbox click shows a marker",
              await page.evaluate("() => document.querySelectorAll("
                                  "'.browser-miss-marker').length") > 0)
        check("marker is pointer-events: none",
              await page.evaluate("() => { const m = document.querySelector("
                                  "'.browser-miss-marker'); return !!m && "
                                  "getComputedStyle(m).pointerEvents === 'none'; }"))
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)

        # Escape from the driving state must not be forwarded to the page.
        await page.evaluate("() => applyBrowserControlState({ human_control: true })")
        before = len([e for e in errors if not is_noise(e)])
        await page.focus("#browserViewport")
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        new_errors = [e for e in errors if not is_noise(e)]
        check("Escape in driving state raises no script error",
              len(new_errors) == before,
              "; ".join(new_errors[before:])[:160])

        await browser.close()

    print("\n" + "=" * 56)
    failed = [n for n, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    print("=" * 56)


asyncio.run(main())

