"""Console output that cannot crash the code doing the logging.

Why this module exists
----------------------
Every ``print()`` in this package sits inside an ``except`` block or on a
best-effort code path. That is exactly where a logging failure is most
expensive: if the log line itself raises, the original exception is replaced by
the logging failure, the recovery path never runs, and the user is shown a
message about the *logger* instead of the thing that actually broke.

That is not hypothetical. Running the app from ``run.bat`` leaves the console on
the OEM/ANSI code page (cp437/cp1252), whose codec cannot represent ordinary
characters. A Playwright error string quoting page text containing an em dash or
a CJK character made ``print(f"Failed to start browser session: {e}")`` raise::

    UnicodeEncodeError: 'charmap' codec can't encode character '\\u5e74' ...
                       character maps to <undefined>

That ``UnicodeEncodeError`` propagated out of ``browser_session._ensure_page``,
through the streaming generator, and reached the UI as the task's failure --
concealing the genuine browser error that was being reported. The user saw a
codec complaint in place of a diagnosis.

Two independent guards, so that neither failure mode is load-bearing:

1. :func:`configure_stdio` re-points ``stdout``/``stderr`` at UTF-8 with
   ``errors="replace"``, so unencodable characters degrade to ``?`` instead of
   raising. The console font on modern Windows renders UTF-8 fine, and the app
   is explicitly English-language, so this is a safe default.
2. :func:`safe_print` swallows any residual logging error. Even if the stream is
   a broken pipe, a closed handle, or something exotic, a diagnostic write must
   never escalate into a user-visible failure.

Guard 2 is the one that actually protects correctness; guard 1 only makes the
console readable.
"""

import sys
from typing import Any, TextIO

_CONFIGURED = False


def configure_stdio() -> None:
    """Make ``stdout``/``stderr`` UTF-8 and non-fatal for unencodable text.

    Idempotent, and safe to call from anywhere including import time. Silently
    does nothing if the streams are not real :class:`io.TextIOWrapper` objects
    (a test harness may have replaced them with something exotic).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # A stream we cannot reconfigure is still usable through safe_print;
            # failing here would be the very bug this module prevents.
            pass


def safe_print(*args: Any, **kwargs: Any) -> None:
    """``print`` that never raises.

    Used for every diagnostic in this package. Never let a message about a
    problem become a second, more confusing problem.
    """
    try:
        print(*args, **kwargs)
    except Exception:
        # Last resort: drop the unencodable parts and try once more. If even
        # that fails, the stream itself is unusable and there is nothing useful
        # left to do -- swallowing is correct, because raising here would
        # replace the caller's real error with a logging error.
        try:
            text = " ".join(str(a) for a in args)
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            print(text.encode(encoding, "replace").decode(encoding))
        except Exception:
            pass
