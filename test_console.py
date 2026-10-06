"""Regression tests for the console-encoding failure that masked a real error.

Run:  .venv\\Scripts\\python.exe test_console.py

Background
----------
Launched from ``run.bat``, the app's console sits on the OEM code page
(cp437/cp1252). Its codec cannot represent ordinary characters, so a diagnostic
``print`` of an exception string containing an em dash or a CJK character used
to raise::

    UnicodeEncodeError: 'charmap' codec can't encode character ...

That print lived *inside* an ``except`` block, so the UnicodeEncodeError
replaced the genuine browser error, the recovery path never ran, and the UI
displayed a complaint about the logger instead of the actual failure.

These tests pin both halves of the fix: stdout is made UTF-8, and a diagnostic
write can never raise even if that did not happen.
"""

import io
import sys

failures = 0
checks = 0


def check(name, ok, detail=""):
    global failures, checks
    checks += 1
    if not ok:
        failures += 1
    status = "OK" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))


# Characters a cp1252 console genuinely cannot encode. U+5E74 is CJK, U+2014 an
# em dash, U+201C a left curly quote, U+1F50E a magnifying glass.
HOSTILE = "latest CVE \u2014 2025\u5e74 \u201cquoted\u201d \U0001F50E"


def test_configure_stdio_forces_utf8():
    """Importing the app must leave stdout on UTF-8, not the locale codec."""
    import importlib
    from app import console as c

    # Fresh module state so configure_stdio() actually runs for this test.
    importlib.reload(c)
    c.configure_stdio()
    enc = (sys.stdout.encoding or "").lower().replace("-", "")
    check("stdout is UTF-8 after configure_stdio()", "utf8" in enc, f"encoding={sys.stdout.encoding}")


def test_hostile_text_does_not_raise():
    """The exact string that broke the app must now print cleanly."""
    from app.console import safe_print
    try:
        safe_print(HOSTILE)
        check("hostile unicode prints without raising", True)
    except Exception as e:
        check("hostile unicode prints without raising", False, repr(e))


def test_safe_print_survives_a_broken_stream():
    """Even with stdout unusable, a diagnostic write must not raise.

    This is the guard that actually protects correctness. It simulates a closed
    pipe / dead handle, the case where reconfiguring stdout cannot help.
    """
    from app.console import safe_print

    class Hostile:
        encoding = "cp1252"

        def write(self, _s):
            raise OSError(5, "Access is denied")

        def flush(self):
            raise OSError(5, "Access is denied")

    real = sys.stdout
    outcome = None
    sys.stdout = Hostile()
    try:
        safe_print(HOSTILE)
        safe_print("second call, also must not raise")
        outcome = (True, "")
    except Exception as e:
        outcome = (False, repr(e))
    finally:
        # Restore BEFORE reporting: the reporter prints too, and reporting
        # through the stub we are complaining about would be its own bug.
        sys.stdout = real

    check("safe_print survives a stream that always raises", *outcome)


def test_safe_print_replaces_rather_than_raises_on_charmap():
    """On a genuinely cp1252 stream, unencodable chars degrade instead of raising.

    This drives safe_print's *fallback* branch, which only runs when the stream
    itself cannot take the text. A plain StringIO cannot model that -- it
    accepts anything -- so the stub below encodes the way a real cp1252 console
    does and raises UnicodeEncodeError, exactly like the original bug.

    Note the two possible outcomes are both acceptable: preserve the text (if
    the stream turns out to be UTF-8 capable) or replace it. What must never
    happen is an exception escaping.
    """
    from app.console import safe_print

    class Cp1252Stream(io.StringIO):
        """Mimics a real Windows console: encodes on write, raises if it can't."""

        encoding = "cp1252"

        def write(self, s):
            s.encode("cp1252")  # raises UnicodeEncodeError, as the console would
            return super().write(s)

    real = sys.stdout
    outcome = (False, "")
    stream = Cp1252Stream()
    sys.stdout = stream
    try:
        safe_print(f"prefix {HOSTILE} suffix")
        out = stream.getvalue()
        # The message must survive intact around the unencodable characters.
        outcome = ("prefix" in out and "suffix" in out, repr(out))
    except Exception as e:
        outcome = (False, repr(e))
    finally:
        sys.stdout = real

    check("cp1252 stream degrades chars instead of raising", *outcome)


def test_regression_the_actual_bug_shape():
    """Reproduce the original shape: print inside an except block.

    Before the fix this raised UnicodeEncodeError and destroyed the recovery
    path. Now the handler must complete and the caller must see its real error.
    """
    from app.console import safe_print

    real_error = None
    try:
        raise RuntimeError(f"browser launch failed near \u2014 \u5e74 target")
    except Exception as e:
        real_error = e
        # This is the line that used to raise and mask `e`.
        try:
            safe_print(f"Failed to start browser session: {e}")
            check("except-block diagnostic does not mask the real error", True)
        except Exception as log_error:
            check("except-block diagnostic does not mask the real error", False,
                  f"logging raised {log_error!r}")

    check("original error object survives", isinstance(real_error, RuntimeError))


def test_browser_session_imports_cleanly():
    """The modules that were edited must still import and wire up safe_print."""
    from app import browser_session, browser_tool, agent, config, llm_client, storage
    for mod in (browser_session, browser_tool, agent, config, llm_client, storage):
        check(f"{mod.__name__} imports with safe_print",
              callable(getattr(mod, "safe_print", None)))


def test_api_still_loads():
    from app.api import app
    check("FastAPI app still builds", app is not None, f"routes={len(app.routes)}")


if __name__ == "__main__":
    test_configure_stdio_forces_utf8()
    test_hostile_text_does_not_raise()
    test_safe_print_survives_a_broken_stream()
    test_safe_print_replaces_rather_than_raises_on_charmap()
    test_regression_the_actual_bug_shape()
    test_browser_session_imports_cleanly()
    test_api_still_loads()

    print()
    if failures:
        print(f"{failures} of {checks} console checks FAILED")
        sys.exit(1)
    print(f"ALL {checks} CONSOLE CHECKS PASSED")
