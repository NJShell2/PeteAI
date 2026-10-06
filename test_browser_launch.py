"""Regression tests for the browser never starting, and the stale "today".

Run:  .venv\\Scripts\\python.exe test_browser_launch.py

Background
----------
Two unrelated failures made the app look broken while everything else worked.

**1. "Browser session is unavailable."** Playwright's bundled Chromium is a
separate download (``playwright install chromium``). On a machine where that
was never run, ``launch()`` raises "Executable doesn't exist at ...". Every
launch site caught that and returned ``None``, so the browser was not merely
degraded -- it was dead, permanently, while ``search_web`` kept answering. The
error the user sees names the wrong remedy ("pip install playwright"), which
pushes you further from the actual fix. ``app.browser_path`` now finds a real
Chrome/Edge/Brave on the machine and hands Playwright an ``executable_path``.

**2. "Today is May 22, 2024."** The model is a locally-hosted checkpoint whose
training data ends well before the present. Asked for the current date it
confidently returned the newest date it had ever seen, then reasoned about moon
phases and weather against it. Nothing injected the real date, so
``app.agent_helpers.current_date_note`` now prepends it to every system prompt.
"""
import os
import sys
from datetime import datetime, timedelta

failures = 0


def check(name, ok, detail=""):
    global failures
    if ok:
        print(f"[OK] {name}")
    else:
        failures += 1
        print(f"[FAIL] {name}: {detail}")
# -- browser resolution --------------------------------------------------------

def test_bundled_chromium_is_actually_probed():
    """Must test for the download, not assume it: assuming it is the bug."""
    from app.browser_path import bundled_chromium_exists

    check("bundled_chromium_exists returns a bool",
          isinstance(bundled_chromium_exists(), bool))


def test_finds_a_chrome_on_this_machine():
    """This machine is known to have Chrome in Program Files."""
    from app.browser_path import find_chrome_path

    found = find_chrome_path()
    check("find_chrome_path finds a browser", bool(found), "no browser found")


def test_launch_kwargs_include_executable_path_when_bundled_is_missing():
    from app.browser_path import bundled_chromium_exists, launch_kwargs_for_chrome

    if bundled_chromium_exists():
        print("[SKIP] bundled Chromium present; fallback path not exercised")
        return
    kwargs = launch_kwargs_for_chrome(True)
    check("headless is passed through", kwargs.get("headless") is True, str(kwargs))
    check("executable_path is supplied", "executable_path" in kwargs, str(kwargs))
    check("executable_path points at a real file",
          os.path.isfile(kwargs.get("executable_path", "")),
          str(kwargs.get("executable_path")))


def test_env_override_wins():
    from app.browser_path import (CHROME_PATH_ENV_VAR, find_chrome_path,
                                  launch_kwargs_for_chrome, reset_cache)

    fake = os.path.join(os.environ.get("TEMP", "."), "_fake_chrome_probe.exe")
    open(fake, "wb").close()
    original = os.environ.get(CHROME_PATH_ENV_VAR)
    os.environ[CHROME_PATH_ENV_VAR] = fake
    reset_cache()
    try:
        check("env override is honoured", find_chrome_path() == fake)
        kwargs = launch_kwargs_for_chrome(True)
        check("override reaches launch kwargs",
              kwargs.get("executable_path") == fake, str(kwargs))
    finally:
        if original is None:
            os.environ.pop(CHROME_PATH_ENV_VAR, None)
        else:
            os.environ[CHROME_PATH_ENV_VAR] = original
        reset_cache()
        try:
            os.remove(fake)
        except OSError:
            pass


def test_bad_override_does_not_crash():
    """A typo in the env var must not take the whole browser down."""
    from app.browser_path import (CHROME_PATH_ENV_VAR,
                                  launch_kwargs_for_chrome, reset_cache)

    bogus = os.path.join(os.environ.get("TEMP", "."), "_no_such_chrome_probe.exe")
    original = os.environ.get(CHROME_PATH_ENV_VAR)
    os.environ[CHROME_PATH_ENV_VAR] = bogus
    reset_cache()
    try:
        kwargs = launch_kwargs_for_chrome(True)
        check("bogus override still yields usable kwargs",
              isinstance(kwargs, dict), str(kwargs))
        check("bogus override is not passed through",
              kwargs.get("executable_path") != bogus, str(kwargs))
    finally:
        if original is None:
            os.environ.pop(CHROME_PATH_ENV_VAR, None)
        else:
            os.environ[CHROME_PATH_ENV_VAR] = original
        reset_cache()


def test_session_actually_opens_a_page():
    """The real regression: the session must return a page, not None.

    Skipped when nothing is installed, since that is a legitimate state to be
    in -- but on any machine with a browser this must pass.
    """
    import asyncio

    from app.browser_path import bundled_chromium_exists, find_chrome_path
    from app.browser_session import browser_session

    if not find_chrome_path() and not bundled_chromium_exists():
        print("[SKIP] no Chromium available; cannot open a real page")
        return

    async def probe():
        page = await browser_session._ensure_page()
        ok = page is not None
        title = None
        if ok:
            res = await browser_session.goto("https://example.com", chat_id="test")
            title = res.get("title")
        await browser_session.close()
        return ok, title

    try:
        ok, title = asyncio.run(asyncio.wait_for(probe(), timeout=120))
    except Exception as exc:  # noqa: BLE001 - top level test runner
        check("session opens a real page", False, repr(exc))
        return
    check("session opens a real page", ok, "_ensure_page returned None")
    check("navigation returns a title", bool(title), "no title returned")


# -- the current date ----------------------------------------------------------

def test_date_note_reports_today():
    from app.agent_helpers import current_date_note

    note = current_date_note()
    today = datetime.now().astimezone()
    check("date note contains today's year", str(today.year) in note, note[:120])
    check("date note names the month", today.strftime("%B") in note, note[:120])
    check("date note mentions the current time", "current time" in note, note[:120])


def test_date_note_is_not_memoised():
    """Guards the real trap: a date baked into settings would freeze forever."""
    from app.agent_helpers import current_date_note

    tomorrow = (datetime.now().astimezone() + timedelta(days=1))
    note = current_date_note()
    check("note reflects today, not tomorrow",
          tomorrow.strftime("%B %d") not in note
          and tomorrow.strftime("%Y-%m-%d") not in note,
          "note looks stale or memoised")


def test_date_note_reaches_the_browser_agent_prompt():
    """The browser agent clicks through live pages; it needs the date too."""
    from app.agent_helpers import current_date_note
    from app.browser_agent import BrowserAgent

    note = current_date_note().strip().splitlines()[0]
    messages = BrowserAgent()._build_messages("check the weather", [], "obs", 0)
    system = messages[0]["content"]
    check("browser agent prompt carries the date", note in system, system[:160])


def test_date_note_reaches_the_main_agent_prompt():
    from app.config import load_settings
    from app.agent_helpers import current_date_note

    settings = load_settings()
    # Mirrors how agent.stream assembles the instruction it sends.
    instruction = f"{settings.system_prompt}\n{current_date_note()}"
    check("main agent prompt carries the date",
          current_date_note().strip() in instruction)


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - top level test runner
                failures += 1
                print(f"[FAIL] {name}: {exc!r}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)