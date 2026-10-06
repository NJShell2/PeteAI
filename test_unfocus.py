"""Escape must release the browser from any focus position.

The Escape handler used to live only on the viewport and required
event.target === browserViewport, so once focus moved to the prompt box, a
sidebar button, or fell back to the body, Escape did nothing while the UI still
promised "Esc to give Pete back". These cases drive the real page against a real
live browser session and press Escape from each of those positions.

Run with the app already serving on 127.0.0.1:8000.
"""

import asyncio

from playwright.async_api import async_playwright

CHAT = "unfocus-regression"
UI_STATE = """() => ({
  pressed: document.getElementById('btnBrowserTakeover').getAttribute('aria-pressed'),
  banner: getComputedStyle(document.getElementById('browserUserBanner')).display,
})"""
UI_APPLY_FALSE = "() => applyBrowserControlState({human_control: false})"

# Each case steals focus somewhere other than the viewport before pressing Esc.
CASES = [
    ("viewport still focused", None),
    ("prompt input focused", "() => document.getElementById('promptInput').focus()"),
    ("sidebar button focused", "() => document.getElementById('btnNewChat').focus()"),
    ("focus blurred to body", "() => document.activeElement && document.activeElement.blur()"),
]


async def main():
    failures = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        page = await browser.new_page(viewport={"width": 1600, "height": 900})
        page.on("pageerror", lambda e: failures.append(f"page error: {e}"))
        await page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        await page.wait_for_timeout(2500)
        # The takeover controls are display:none until the browser tab is open.
        await page.click('[data-tab="tab-browser"]')
        await page.wait_for_timeout(1000)

        async def server_state(active):
            return await page.evaluate(
                """async (a) => {
                    const r = await fetch(
                        '/api/browser/takeover?chat_id=%s&active=' + a, {method: 'POST'});
                    return (await r.json()).human_control;
                }""" % CHAT, active)

        for label, steal in CASES:
            # Reset both sides unconditionally. A previous case that failed to
            # release can leave the UI showing "driving" while the server has
            # already handed back, and then the click below would toggle control
            # off instead of on -- a cascading failure that hides the real cause.
            await server_state("false")
            await page.evaluate(UI_APPLY_FALSE)
            await page.wait_for_timeout(250)
            # Use the real button so the UI is genuinely in the driving state.
            await page.click("#btnBrowserTakeover")
            await page.wait_for_timeout(700)
            start = await page.evaluate(UI_STATE)
            if start["pressed"] != "true":
                failures.append(f"{label}: never entered the driving state")
                continue

            if steal:
                await page.evaluate(steal)
                await page.wait_for_timeout(300)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(900)

            after = await page.evaluate(UI_STATE)
            released = after["pressed"] == "false" and after["banner"] == "none"
            print(f"  [{'PASS' if released else 'FAIL'}] Escape releases: {label}")
            if not released:
                failures.append(f"Escape did not release with {label}")
                # Do not leave the session stuck in the driving state.
                await server_state("false")

        await browser.close()

    print()
    if failures:
        for f in failures:
            print("FAIL:", f)
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("ALL UNFOCUS CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
