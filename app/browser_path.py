"""Locates a Chromium-family browser for Playwright to drive.

Playwright's own Chromium build is downloaded separately by
``playwright install chromium``. On machines where that step was never run --
or where it silently failed -- ``launch()`` with no ``executable_path`` raises
"Executable doesn't exist at ...chrome-headless-shell.exe". Because every
launch site catches that error and returns ``None``, the symptom is not a crash
but a browser that is permanently "unavailable" while every other tool keeps
working. That failure is very hard to diagnose from the outside.

So: prefer the bundled build when it is actually present, and otherwise fall
back to a Chrome/Edge/Brave already installed on the machine. The fallback is
tried first-registered wins, and the result is cached because the filesystem
answer cannot change within a process without a restart.

Returning ``None`` means nothing usable was found; callers pass that through to
Playwright unchanged, which preserves its own (clearer) error message.
"""
import os
from pathlib import Path
from typing import List, Optional

from app.console import safe_print

# Overridable for unusual installs and for tests. Checked before any probing.
CHROME_PATH_ENV_VAR = "PETE_CHROME_PATH"

# %LOCALAPPDATA% first: that is where Chrome lands for a per-machine install and
# is the common case on a domain-joined machine where Program Files is locked
# down. Both 64- and 32-bit Program Files are listed for the same reason.
_CANDIDATE_TEMPLATES: List[str] = [
    r"{localappdata}\Google\Chrome\Application\chrome.exe",
    r"{program_files}\Google\Chrome\Application\chrome.exe",
    r"{program_files_x86}\Google\Chrome\Application\chrome.exe",
    r"{program_files_x86}\Microsoft\Edge\Application\msedge.exe",
    r"{program_files}\Microsoft\Edge\Application\msedge.exe",
    r"{localappdata}\Microsoft\Edge\Application\msedge.exe",
    r"{program_files}\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"{localappdata}\BraveSoftware\Brave-Browser\Application\brave.exe",
]

_cached_path: Optional[str] = None
_cache_resolved = False


def _candidate_paths() -> List[Path]:
    """Expands the candidate templates against this machine's environment."""
    roots = {
        "localappdata": os.environ.get("LOCALAPPDATA", ""),
        "program_files": os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        "program_files_x86": os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
    }
    paths = []
    for template in _CANDIDATE_TEMPLATES:
        try:
            expanded = template.format(**roots)
        except KeyError:
            continue
        if expanded:
            paths.append(Path(expanded))
    return paths


def bundled_chromium_exists() -> bool:
    """True when Playwright's own Chromium download is present.

    Checked by looking for the browsers directory rather than by launching:
    launching to find out would be slow and would still throw. The directory
    layout is ``ms-playwright/chromium*``, and the headless shell is nested one
    level deeper, so match on the parent directory name.
    """
    override = os.environ.get(CHROME_PATH_ENV_VAR)
    if override:
        # An explicit override means the operator has made a decision; trust it
        # rather than second-guessing it against the bundled build.
        return False
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        return False
    ms_playwright = Path(root) / "ms-playwright"
    try:
        if not ms_playwright.is_dir():
            return False
        return any(
            child.is_dir() and child.name.startswith("chromium")
            for child in ms_playwright.iterdir()
        )
    except OSError:
        return False


def find_chrome_path() -> Optional[str]:
    """Returns a browser executable to hand Playwright, or None if none exists.

    Cached after the first call: this is on the hot path of every browser
    launch, and probing the filesystem each time buys nothing.
    """
    global _cached_path, _cache_resolved
    if _cache_resolved:
        return _cached_path

    _cache_resolved = True
    _cached_path = None

    override = os.environ.get(CHROME_PATH_ENV_VAR)
    if override:
        if Path(override).is_file():
            _cached_path = override
        else:
            safe_print(
                f"{CHROME_PATH_ENV_VAR} points at '{override}', which is not a file. Ignoring it."
            )
        return _cached_path

    for candidate in _candidate_paths():
        try:
            if candidate.is_file():
                _cached_path = str(candidate)
                return _cached_path
        except OSError:
            continue

    return _cached_path


def launch_kwargs_for_chrome(headless: bool) -> dict:
    """Builds the launch kwargs, pointing Playwright at a real browser if needed.

    When the bundled Chromium is missing, ``executable_path`` is added so the
    launch succeeds instead of raising. This is the fix for the whole family of
    "Browser session is unavailable" errors.
    """
    kwargs = {"headless": headless}
    if not bundled_chromium_exists():
        chrome = find_chrome_path()
        if chrome:
            kwargs["executable_path"] = chrome
    return kwargs


def reset_cache() -> None:
    """Clears the memoised lookup. For tests that change the environment."""
    global _cached_path, _cache_resolved
    _cached_path = None
    _cache_resolved = False