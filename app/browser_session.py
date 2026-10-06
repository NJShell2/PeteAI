"""Persistent, observable browser session for Pete AI.

Everything here is built around one long-lived Playwright page rather than the
fire-and-forget pages the older tools open per call. A session is what makes the
two new capabilities possible at all:

* Autonomous goal navigation -- ``BrowserAgent`` can observe a page, decide on a
  single action and perform it, because state survives between steps. A query
  typed into a search box is still sitting in that box on the next step.
* Live viewing -- the page is mirrored to the browser UI as a JPEG stream over
  the Chrome DevTools Protocol, so the user can watch the agent work.

The other key idea is how the model "sees" a page. Raw ``page.content()`` is far
too large and too noisy to reason about, so ``OBSERVE_JS`` harvests only the
interactive elements -- links, buttons, fields, checkboxes -- and gives each one
a stable numeric ref. The model clicks ``7`` instead of guessing a brittle CSS
selector, which is the single biggest reason these loops tend to work.

A third party can share the page too. The live view is not just a video feed:
``dispatch_user_input`` replays the viewer's mouse, wheel and keyboard events
into the same page, so a human can click, type, copy and paste exactly as they
would on a real browser, then hand control back and let the agent carry on. The
handoff registry below is what makes that safe -- only one of {agent, human} is
ever driving, and every mutation still passes through the one lock.
"""
import asyncio
import base64
import re
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

from app.config import load_settings, WORKSPACES_DIR
from app.browser_path import launch_kwargs_for_chrome
from app.console import safe_print

VIEWPORT = {"width": 1280, "height": 800}

# How long to wait for a page to settle after an action before observing.
SETTLE_TIMEOUT_MS = 8000

# Frames pushed to live viewers. Base64 JPEG in JSON, so keep them modest.
STREAM_QUALITY = 60

# The CDP screencast only emits on visual change, so a slow poll runs alongside
# it. This is the worst-case staleness a viewer can see on a static page.
POLL_INTERVAL_MS = 1200

# Refs beyond this are dropped: a model cannot reliably reason about 400 options,
# and the first N in document order are almost always the ones a human would see.
MAX_OBSERVED_ELEMENTS = 90
MAX_OBSERVED_TEXT = 4000

# How long a run stays parked waiting for a human before giving up and salvaging
# an answer. Long enough to fill in a login form or solve a CAPTCHA, short
# enough that an abandoned tab does not wedge the agent forever.
HANDOFF_TIMEOUT_SECONDS = 300.0

# Keyboard modifiers as reported by the browser: Alt=1, Ctrl=2, Meta/Cmd=4, Shift=8.
MOD_ALT, MOD_CTRL, MOD_META, MOD_SHIFT = 1, 2, 4, 8

OBSERVE_JS = r"""
(() => {
  const MAX = 90;
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','SVG','IFRAME','HEAD','META','LINK']);
  const SEL = ['a[href]','button','input','select','textarea','summary','[role=button]',
               '[role=link]','[role=checkbox]','[role=radio]','[role=tab]','[role=menuitem]',
               '[role=searchbox]','[contenteditable=true]','[onclick]','label'].join(',');

  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 && r.height < 2) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  }

  function cssPath(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 5) {
      if (node.id) { parts.unshift('#' + CSS.escape(node.id)); break; }
      let part = node.tagName.toLowerCase();
      const cls = (node.getAttribute('class') || '').trim().split(/\s+/)
        .filter(Boolean).filter(c => !/^(is-|has-|ng-|css-)/.test(c)).slice(0, 2);
      if (cls.length) part += '.' + cls.map(c => CSS.escape(c)).join('.');
      const parent = node.parentElement;
      if (parent) {
        const twins = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (twins.length > 1) part += ':nth-of-type(' + (twins.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      node = node.parentElement;
      if (!node || node.tagName === 'BODY' || node.tagName === 'HTML') break;
    }
    return parts.join(' > ');
  }

  const out = [];
  for (const el of document.querySelectorAll(SEL)) {
    if (out.length >= MAX) break;
    if (SKIP.has(el.tagName) || !visible(el)) continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden') continue;
    let text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') ||
                el.getAttribute('placeholder') || '').replace(/\s+/g, ' ').trim();
    if (text.length > 110) text = text.slice(0, 110) + '...';
    let options = null;
    if (tag === 'select') {
      options = Array.from(el.options).slice(0, 40)
        .map(o => ({ value: o.value, label: (o.textContent || '').trim() }));
    }
    out.push({
      ref: out.length,
      tag: tag,
      type: type || null,
      text: text || null,
      placeholder: el.getAttribute('placeholder') || null,
      name: el.getAttribute('name') || null,
      label: el.labels && el.labels[0] ? (el.labels[0].innerText || '').trim().slice(0, 80) : null,
      value: (tag === 'input' || tag === 'textarea') ? String(el.value || '').slice(0, 80) : null,
      disabled: !!el.disabled,
      checked: !!el.checked,
      href: tag === 'a' ? (el.href || null) : null,
      options: options,
      selector: cssPath(el)
    });
  }

  const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
    .map(h => (h.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120))
    .filter(Boolean).slice(0, 30);

  const raw = document.body ? (document.body.innerText || '') : '';
  const text = raw.replace(/\n{3,}/g, '\n\n').trim();

  return {
    title: document.title || '',
    url: location.href,
    headings: headings,
    elements: out,
    text_length: text.length,
    text: text.slice(0, 4000),
    page: { width: innerWidth, height: innerHeight,
            scrolled: Math.round(scrollY),
            total_height: Math.round(document.body ? document.body.scrollHeight : 0) }
  };
})()
"""


# A human-verification wall is the one thing an agent genuinely cannot solve, so
# it is worth detecting explicitly rather than letting the model burn its step
# budget clicking an invisible checkbox. Detection is deliberately fuzzy (markers
# and phrases, not selectors) because these widgets are served from third-party
# iframes and are deliberately hostile to automation.
CHALLENGE_JS = r"""
(() => {
  const MARKERS = [
    'g-recaptcha', 'recaptcha', 'grecaptcha', 'hcaptcha', 'h-captcha',
    'cf-challenge', 'cf_chl', 'challenges.cloudflare.com', 'turnstile',
    'px-captcha', 'funcaptcha', 'arkoselabs', 'datadome', 'perimeterx'
  ];
  const PHRASES = [
    'verify you are human', "verify you're human", 'verify that you are human',
    'i am not a robot', "i'm not a robot", 'confirm you are not a robot',
    'press and hold', 'unusual traffic', 'checking your browser',
    'just a moment', 'enable javascript and cookies to continue',
    'complete the security check', 'verify your identity',
    'are you a robot', 'human verification', 'security verification'
  ];

  function found(kind, detail) {
    return { kind: kind, detail: String(detail || '').slice(0, 160) };
  }

  // Third-party widget iframes are the strongest signal: a normal page rarely
  // embeds one of these hosts at all.
  const iframes = Array.from(document.querySelectorAll('iframe[src]'));
  for (const frame of iframes) {
    const src = (frame.getAttribute('src') || '').toLowerCase();
    for (const marker of MARKERS) {
      if (src.includes(marker)) return found('widget', marker);
    }
  }

  // Widget containers sometimes load their script late and leave only the
  // class names behind.
  if (document.querySelector('.g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey]')) {
    return found('widget', 'captcha container');
  }

  const haystack = ((document.title || '') + '\n' + (document.body ? document.body.innerText : ''))
    .toLowerCase();
  for (const phrase of PHRASES) {
    if (haystack.includes(phrase)) return found('message', phrase);
  }
  return null;
})()
"""


class Handoff:
    """Who is driving one chat's page, plus any help request waiting on a human.

    ``generation`` increments on every control change. The agent captures it
    before it parks and compares it after it wakes, so a control toggle that
    arrived from a stale tab (or a run that has already finished) cannot be
    mistaken for a fresh hand-back.
    """

    __slots__ = ("human_control", "generation", "question", "answer_event",
                 "answer", "release", "stopped")

    def __init__(self) -> None:
        self.human_control: bool = False
        self.generation: int = 0
        # Set by the Stop button. Distinct from human_control: a stopped run is
        # over, whereas a take-over means the human is driving right now.
        self.stopped: bool = False
        # Set while the agent is parked waiting for the human to hand back.
        self.question: Optional[str] = None
        self.answer_event: Optional[asyncio.Event] = None
        self.answer: Optional[str] = None
        # Why the park ended: "answer", "release" (control came back) or
        # "cancel" (the user stopped it). Recorded explicitly rather than
        # inferred from the flags, because a user can answer and hand back at
        # the same moment and the two must not be confused.
        self.release: Optional[str] = None


class BrowserSession:
    """A single reusable browser+page that many callers can share.

    Concurrency is handled with one lock: the autonomous agent drives this page
    in a tight loop while a user may be watching or clicking at the same time, so
    every mutation is serialised to keep the page state coherent.
    """

    def __init__(self) -> None:
        self._playwright: Optional[Any] = None
        self._browser: Optional[Any] = None
        self._context: Optional[Any] = None
        self._page: Optional[Any] = None
        self._cdp: Optional[Any] = None
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()

        # Live viewers: chat_id -> set of frame queues.
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}
        # Last known URL per chat, so the UI can show where the agent is.
        self._current_urls: Dict[str, str] = {}
        # Per-chat handoff state: who holds the wheel, and any help request
        # parked in mid-air waiting for an answer.
        self._handoffs: Dict[str, "Handoff"] = {}
        self._streaming = False
        self._closed = False
        # Keepalive frame poller; only runs while a viewer is subscribed.
        self._poller_task: Optional[asyncio.Task] = None

    # -- lifecycle ---------------------------------------------------------------

    async def _ensure_page(self) -> Optional[Any]:
        """Starts Playwright if needed and returns the shared page, or None."""
        if self._closed:
            return None
        async with self._start_lock:
            if self._page is not None and not self._page.is_closed():
                return self._page
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                safe_print("Playwright is not installed; browser session is unavailable.")
                return None

            try:
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                if self._browser is None:
                    settings = load_settings()
                    self._browser = await self._playwright.chromium.launch(
                        **launch_kwargs_for_chrome(settings.browser_headless),
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                if self._context is None:
                    # A realistic UA matters: several sites serve a stripped-down
                    # page to the default automation string.
                    self._context = await self._browser.new_context(
                        viewport=VIEWPORT,
                        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
                        locale="en-US",
                    )
                    self._context.set_default_timeout(30000)
                self._page = await self._context.new_page()
                await self._attach_stream()
                return self._page
            except Exception as e:
                safe_print(f"Failed to start browser session: {e}")
                return None

    async def _attach_stream(self) -> None:
        """Begins a CDP screencast so viewers can watch the page live."""
        if self._streaming or self._page is None or self._cdp is not None:
            return
        try:
            self._cdp = await self._context.new_cdp_session(self._page)
            self._cdp.on("Page.screencastFrame", self._on_frame)
            await self._cdp.send("Page.startScreencast", {
                "format": "jpeg",
                "quality": STREAM_QUALITY,
                "maxWidth": VIEWPORT["width"],
                "maxHeight": VIEWPORT["height"],
                "everyNthFrame": 1,
            })
            self._streaming = True
        except Exception as e:
            # Live frames are a nice-to-have; polling screenshots remain as backup.
            safe_print(f"Live streaming unavailable (frames disabled): {e}")
            self._cdp = None

    def _on_frame(self, frame: Dict[str, Any]) -> None:
        """Drops a screencast frame into every viewer queue, newest wins.

        The ack is mandatory: Chrome only sends a new frame after the previous
        one is acknowledged, so skipping it silently stops the stream after the
        first frame.
        """
        data = frame.get("data")
        session_id = frame.get("sessionId")

        if self._cdp is not None and session_id:
            # The ack is fire-and-forget; the handler is sync so it cannot await.
            asyncio.ensure_future(self._ack_frame(session_id))

        if not data:
            return
        message = {"type": "frame", "data": data}
        for queues in self._subscribers.values():
            for queue in list(queues):
                try:
                    queue.put_nowait(message)
                except asyncio.QueueFull:
                    # A slow viewer must never stall the browser. Drop this frame;
                    # a newer one is already on its way.
                    pass

    async def _ack_frame(self, session_id: str) -> None:
        """Acknowledges one screencast frame so Chrome sends the next."""
        try:
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass  # the screencast may have been torn down mid-flight

    async def close(self) -> None:
        self._closed = True
        if self._poller_task is not None:
            self._poller_task.cancel()
            self._poller_task = None
        for chat_id in list(self._subscribers):
            self._subscribers.pop(chat_id, None)
        for target in (self._context, self._browser):
            try:
                if target:
                    await target.close()
            except Exception:
                pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._page = self._context = self._browser = self._playwright = None
        self._cdp = None
        self._streaming = False


    # -- live view plumbing ------------------------------------------------------

    def subscribe(self, chat_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=3)
        self._subscribers.setdefault(chat_id, set()).add(queue)
        # The CDP screencast only fires when pixels change, so a static page
        # would leave the panel frozen. A slow poller runs alongside it to keep
        # the view honest even when nothing is moving.
        self._ensure_poller()
        return queue

    def unsubscribe(self, chat_id: str, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(chat_id)
        if queues and queue in queues:
            queues.discard(queue)
            if not queues:
                self._subscribers.pop(chat_id, None)

    def _ensure_poller(self) -> None:
        """Starts the keepalive frame poller once, if viewers exist."""
        if self._poller_task is not None or self._closed:
            return
        try:
            self._poller_task = asyncio.get_event_loop().create_task(self._poll_frames())
        except RuntimeError:
            self._poller_task = None

    async def _poll_frames(self) -> None:
        """Emits a frame every POLL_INTERVAL_MS while anyone is watching.

        This is the floor under the live view: the screencast handles fast
        interaction, and this guarantees the panel still reflects the real page
        during a pause (a static result page, a slow form, a stalled load).
        """
        try:
            while not self._closed and self._subscribers:
                await asyncio.sleep(POLL_INTERVAL_MS / 1000)
                if self._closed or not self._subscribers:
                    break
                if self._page is None or self._page.is_closed():
                    continue
                data = await self.frame_jpeg()
                if not data:
                    continue
                message = {"type": "frame", "data": data, "polled": True}
                for queues in list(self._subscribers.values()):
                    for queue in list(queues):
                        try:
                            queue.put_nowait(message)
                        except asyncio.QueueFull:
                            pass
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            self._poller_task = None

    def _notify_status(self, chat_id: str, **payload: Any) -> None:
        """Pushes a non-frame message (URL change, step log) to viewers."""
        for queue in list(self._subscribers.get(chat_id, ())):
            try:
                queue.put_nowait({"type": "status", **payload})
            except asyncio.QueueFull:
                pass

    def set_current_url(self, chat_id: Optional[str], url: str) -> None:
        if chat_id and url:
            self._current_urls[chat_id] = url
            self._notify_status(chat_id, url=url)

    def get_current_url(self, chat_id: str) -> str:
        return self._current_urls.get(chat_id, "")

    def _handoff(self, chat_id: str) -> Handoff:
        state = self._handoffs.get(chat_id)
        if state is None:
            state = Handoff()
            self._handoffs[chat_id] = state
        return state

    def control_state(self, chat_id: str) -> Dict[str, Any]:
        """A snapshot of who is driving, for the UI and for the agent loop."""
        state = self._handoff(chat_id)
        return {
            "human_control": state.human_control,
            "generation": state.generation,
            "awaiting_user": state.question is not None,
            "question": state.question,
        }

    def set_human_control(self, chat_id: str, active: bool) -> Dict[str, Any]:
        """Hands the wheel to a human, or takes it back for the agent.

        Returns the resulting control state. Bumping the generation on every
        change is what makes the agent's park/wake handshake safe.
        """
        state = self._handoff(chat_id)
        active = bool(active)
        if state.human_control == active:
            return self.control_state(chat_id)

        state.human_control = active
        state.generation += 1
        if not active:
            # A hand-back releases an agent parked on a question nobody answered.
            self.cancel_handoff(chat_id, release="release")
        self._notify_status(chat_id, control=self.control_state(chat_id))
        return self.control_state(chat_id)

    def is_human_controlled(self, chat_id: str) -> bool:
        return self._handoff(chat_id).human_control

    # -- stop -------------------------------------------------------------------

    def request_stop(self, chat_id: str) -> None:
        """Asks any running agent to end its run at the next step boundary.

        This is deliberately *not* implemented as a take-over. Stop and "you are
        driving" are different intentions, and conflating them leaves the session
        flagged as human-controlled after the run is gone -- which silently parks
        the *next* run on its very first line. A stop flag is also the only thing
        that survives an abort of the SSE stream, since aborting the response
        cannot be relied on to unwind the generator promptly.

        Any agent parked on a question is released too, otherwise it would sit
        there until the handoff timeout.
        """
        self._handoff(chat_id).stopped = True
        self.cancel_handoff(chat_id)

    def consume_stop(self, chat_id: str) -> bool:
        """Reads and clears the stop flag, so one press stops one run.

        Clearing on read is what keeps a stop from poisoning the next goal: the
        flag must not still be set when ``run`` starts.
        """
        state = self._handoff(chat_id)
        was_stopped = state.stopped
        state.stopped = False
        return was_stopped

    def is_stopped(self, chat_id: str) -> bool:
        return self._handoff(chat_id).stopped

    def clear_human_control(self, chat_id: str) -> None:
        self._handoffs.pop(chat_id, None)

    # -- agent handoff -----------------------------------------------------------

    def begin_handoff(self, chat_id: str, question: str) -> Handoff:
        """Parks the agent and asks the human a question.

        Returns the handoff so the caller can await :meth:`wait_for_handoff`,
        which resolves as soon as the user answers, hands back, or the wait
        times out.
        """
        state = self._handoff(chat_id)
        state.question = question
        state.answer = None
        state.release = None
        state.answer_event = asyncio.Event()
        self._notify_status(chat_id, control=self.control_state(chat_id))
        return state

    def answer_handoff(self, chat_id: str, answer: str) -> bool:
        """Delivers the user's reply to a parked agent. False if nothing asked."""
        state = self._handoff(chat_id)
        if state.answer_event is None:
            return False
        state.answer = answer
        state.release = "answer"
        state.answer_event.set()
        return True

    def cancel_handoff(self, chat_id: str, release: str = "cancel") -> None:
        """Releases a parked agent without an answer (the user hit stop).

        ``release="release"`` marks a hand-back, which the agent reads as
        "carry on" rather than "the user took over".
        """
        state = self._handoff(chat_id)
        if state.answer_event is None or state.release is not None:
            return
        state.release = release
        state.answer_event.set()

    async def wait_for_handoff(self, chat_id: str,
                                timeout: float = HANDOFF_TIMEOUT_SECONDS) -> Dict[str, Any]:
        """Waits out a human handoff and reports how it ended.

        Returns ``{"outcome": ...}`` where outcome is one of ``"answered"``
        (the user replied), ``"resumed"`` (control came back to the agent),
        ``"abandoned"`` (the user took the wheel and is still driving), or
        ``"timeout"`` (nobody acted within ``timeout`` seconds).
        """
        state = self._handoff(chat_id)
        generation = state.generation
        event = state.answer_event
        if event is None:
            return {"outcome": "resumed", "answer": None}

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            state.question = None
            state.answer_event = None
            state.release = None
            return {"outcome": "timeout", "answer": None}

        answer = state.answer
        release = state.release
        state.question = None
        state.answer_event = None
        state.answer = None
        state.release = None

        if release == "answer":
            return {"outcome": "answered", "answer": answer}
        if release == "release":
            # An explicit hand-back, even one that raced a stale answer.
            return {"outcome": "resumed", "answer": None}
        # Stopped with control still held by the user: do not resume.
        if state.human_control:
            return {"outcome": "abandoned", "answer": answer}
        # Control toggled while parked with no explicit reason recorded: treat
        # the toggle as the hand-back rather than leaving the run parked.
        if state.generation != generation:
            return {"outcome": "resumed", "answer": None}
        return {"outcome": "canceled", "answer": answer}

    # -- observation -------------------------------------------------------------

    async def observe(self, chat_id: Optional[str] = None) -> Dict[str, Any]:
        """Returns the current page as a compact, ref-annotated snapshot."""
        page = await self._ensure_page()
        if page is None:
            return {"error": "Browser session is unavailable. Playwright could not "
                             "start a browser -- install one with 'playwright install "
                             "chromium', or install Chrome/Edge so app/browser_path.py "
                             "can find it."}
        async with self._lock:
            try:
                snapshot = await page.evaluate(OBSERVE_JS)
            except Exception as e:
                return {"error": f"Could not read the page: {e}"}
            # Detection is a separate evaluate so a page that throws inside the
            # main harvest still reports a challenge rather than looking clear.
            try:
                challenge = await page.evaluate(CHALLENGE_JS)
            except Exception:
                challenge = None
        snapshot = snapshot or {}
        snapshot["refs_truncated"] = len(snapshot.get("elements") or []) >= MAX_OBSERVED_ELEMENTS
        snapshot["challenge"] = challenge or None
        self.set_current_url(chat_id, snapshot.get("url", ""))
        return snapshot

    def viewport_size(self, chat_id: Optional[str] = None) -> Dict[str, int]:
        """The page's true pixel size, which the UI needs to map clicks."""
        page = self._page
        if page is None or page.is_closed():
            return dict(VIEWPORT)
        try:
            size = page.viewport_size or VIEWPORT
            return {"width": int(size["width"]), "height": int(size["height"])}
        except Exception:
            return dict(VIEWPORT)

    def render_observation(self, snapshot: Dict[str, Any], max_elements: int = 45) -> str:
        """Formats a snapshot as the compact text block the model reasons over."""
        if snapshot.get("error"):
            return f"ERROR: {snapshot['error']}"

        lines = [f"URL: {snapshot.get('url', '')}", f"TITLE: {snapshot.get('title', '')}"]

        # Put the block front and centre: a CAPTCHA is the one situation where
        # the correct move is to stop and ask, not to keep clicking.
        challenge = snapshot.get("challenge")
        if challenge:
            kind = (challenge or {}).get("kind", "unknown")
            detail = (challenge or {}).get("detail", "")
            lines.append(
                "\n*** HUMAN VERIFICATION REQUIRED ***\n"
                f"A {kind} challenge is blocking this page ({detail}). You cannot solve it. "
                "Use the ask_user action to ask the user to complete it, then continue "
                "from the page as it is once they answer."
            )

        headings = snapshot.get("headings") or []
        if headings:
            lines.append("\nHEADINGS: " + " | ".join(headings[:12]))

        lines.append("\nINTERACTIVE ELEMENTS (use these ref numbers in your action):")
        elements = snapshot.get("elements") or []
        if not elements:
            lines.append("  (none detected -- the page may be a canvas app or still loading)")
        for element in elements[:max_elements]:
            lines.append("  " + self._format_element(element))

        # Either the page itself was capped, or this render dropped the tail.
        hidden = max(len(elements) - max_elements, 0)
        if snapshot.get("refs_truncated") or hidden > 0:
            if hidden > 0:
                lines.append(f"  ... {hidden} more elements not shown")

        text = (snapshot.get("text") or "").strip()
        if text:
            truncated = snapshot.get("text_length", 0) > MAX_OBSERVED_TEXT
            lines.append("\nPAGE TEXT" + (" (truncated)" if truncated else "") + ":")
            lines.append(text[:MAX_OBSERVED_TEXT])

        return "\n".join(lines)

    def _format_element(self, element: Dict[str, Any]) -> str:
        bits = [f"[{element.get('ref')}]", f"<{element.get('tag', '?')}"]
        if element.get("type"):
            bits.append(f" type={element['type']}")
        bits.append(">")

        label = (element.get("text") or element.get("label")
                 or element.get("placeholder") or element.get("name"))
        if not label and element.get("href"):
            label = element["href"]
        rendered = f" {label}" if label else ""
        if element.get("text") and element.get("placeholder") and element["text"] != element["placeholder"]:
            rendered += f" (placeholder: {element['placeholder']})"

        if element.get("value"):
            rendered += f" value={element['value']!r}"
        if element.get("checked"):
            rendered += " [checked]"
        if element.get("disabled"):
            rendered += " [disabled]"
        if element.get("options"):
            rendered += " options=" + ", ".join(
                str(o.get("label") or o.get("value")) for o in element["options"][:8]
            )
        return "".join(bits) + rendered

    # -- human input -------------------------------------------------------------

    async def dispatch_input(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Replays one input event from a live viewer into the page.

        The message shape matches what the browser reports, so the client can
        forward events nearly verbatim. Coordinates arrive already mapped into
        page pixels by the client (see ``/api/browser/size``); they are clamped
        here to the viewport so a stale frame or a resize mid-drag cannot send a
        click to a nonsensical offset.
        """
        page = await self._ensure_page()
        if page is None:
            return {"error": "Browser session is unavailable."}

        kind = (message.get("kind") or "").lower()
        try:
            async with self._lock:
                if kind == "pointer":
                    return await self._dispatch_pointer(page, message)
                if kind == "wheel":
                    return await self._dispatch_wheel(page, message)
                if kind == "key":
                    return await self._dispatch_key(page, message)
                if kind == "text":
                    return await self._dispatch_text(page, message)
                if kind == "hotkey":
                    return await self._dispatch_hotkey(page, message)
        except Exception as e:
            return {"error": f"Could not send input to the page: {e}"}
        return {"error": f"Unknown input kind: {kind!r}"}

    def _clamp_point(self, page: Any, message: Dict[str, Any]) -> Dict[str, float]:
        try:
            x = float(message.get("x", 0))
            y = float(message.get("y", 0))
        except (TypeError, ValueError):
            x = y = 0.0
        try:
            size = page.viewport_size or VIEWPORT
            width, height = float(size["width"]), float(size["height"])
        except Exception:
            width, height = float(VIEWPORT["width"]), float(VIEWPORT["height"])
        return {
            "x": min(max(x, 0.0), max(width - 1.0, 0.0)),
            "y": min(max(y, 0.0), max(height - 1.0, 0.0)),
        }

    async def _dispatch_pointer(self, page: Any, message: Dict[str, Any]) -> Dict[str, Any]:
        action = (message.get("action") or "").lower()
        point = self._clamp_point(page, message)
        button = (message.get("button") or "left").lower()
        if button not in ("left", "right", "middle"):
            button = "left"
        if action == "move":
            await page.mouse.move(point["x"], point["y"])
        elif action == "down":
            await page.mouse.move(point["x"], point["y"])
            await page.mouse.down(button=button)
        elif action == "up":
            await page.mouse.up(button=button)
        elif action == "click":
            await page.mouse.click(point["x"], point["y"], button=button,
                                   click_count=int(message.get("clickCount") or 1))
        elif action == "dblclick":
            await page.mouse.dblclick(point["x"], point["y"], button=button)
        else:
            return {"error": f"Unknown pointer action: {action!r}"}
        return {"ok": True}

    async def _dispatch_wheel(self, page: Any, message: Dict[str, Any]) -> Dict[str, Any]:
        await page.mouse.wheel(float(message.get("deltaX") or 0), float(message.get("deltaY") or 0))
        return {"ok": True}

    async def _dispatch_key(self, page: Any, message: Dict[str, Any]) -> Dict[str, Any]:
        action = (message.get("action") or "").lower()
        key = message.get("key")
        if not key:
            return {"error": "Key event is missing a key."}
        if action == "down":
            await page.keyboard.down(key)
        elif action == "up":
            await page.keyboard.up(key)
        elif action == "press":
            await page.keyboard.press(key)
        else:
            return {"error": f"Unknown key action: {action!r}"}
        return {"ok": True}

    async def _dispatch_text(self, page: Any, message: Dict[str, Any]) -> Dict[str, Any]:
        text = message.get("text") or ""
        if not text:
            return {"error": "Text event is missing text."}
        # insert_text rather than per-key presses: it is how a paste behaves,
        # and it sidesteps the key-encoding differences between platforms.
        await page.keyboard.insert_text(str(text))
        return {"ok": True}

    async def _dispatch_hotkey(self, page: Any, message: Dict[str, Any]) -> Dict[str, Any]:
        combo = message.get("combo")
        if not combo:
            return {"error": "Hotkey event is missing a combo."}
        await page.keyboard.press(str(combo))
        return {"ok": True}

    async def read_selection(self) -> str:
        """Returns the page's current text selection, for copy in the viewer."""
        page = await self._ensure_page()
        if page is None:
            return ""
        try:
            async with self._lock:
                return await page.evaluate("() => String(window.getSelection() || '')")
        except Exception:
            return ""

    async def focus_page(self) -> Dict[str, Any]:
        """Brings keyboard focus into the page so typing lands somewhere real."""
        page = await self._ensure_page()
        if page is None:
            return {"error": "Browser session is unavailable."}
        try:
            async with self._lock:
                await page.bring_to_front()
                await page.evaluate("() => window.focus()")
        except Exception as e:
            return {"error": f"Could not focus the page: {e}"}
        return {"ok": True}

    # -- actions -----------------------------------------------------------------

    async def _resolve_ref(self, page: Any, ref: Any) -> Optional[Dict[str, Any]]:
        """Re-reads the element list and returns the entry for a ref number."""
        try:
            snapshot = await page.evaluate(OBSERVE_JS)
        except Exception:
            return None
        try:
            ref_num = int(ref)
        except (TypeError, ValueError):
            return None
        for element in (snapshot or {}).get("elements") or []:
            if element.get("ref") == ref_num:
                return element
        return None

    async def _settle(self, page: Any) -> None:
        """Best-effort wait for the page to finish loading after an action.

        ``networkidle`` is never treated as required: single-page apps poll in the
        background and would time out on every step, so a plain timeout is success.
        """
        try:
            await page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
            return
        except Exception:
            pass
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        try:
            await page.wait_for_timeout(350)
        except Exception:
            pass

    async def goto(self, url: str, chat_id: Optional[str] = None) -> Dict[str, Any]:
        """Navigates to a URL, resolving a plain search phrase to a result page."""
        page = await self._ensure_page()
        if page is None:
            return {"error": "Browser session is unavailable."}
        url = (url or "").strip()
        if not url:
            return {"error": "A URL is required."}
        if " " in url and "://" not in url:
            from app.browser_tool import browser_tool
            results = await browser_tool.search_web(url, 5)
            if not results:
                return {"error": f"Could not resolve '{url}' to a URL."}
            url = results[0]["url"]
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        async with self._lock:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await self._settle(page)
            except Exception as e:
                return {"error": f"Could not open {url}: {e}"}
            title, current = await page.title(), page.url
        self.set_current_url(chat_id, current)
        return {"ok": True, "url": current, "title": title}


    async def act(self, action: Dict[str, Any], chat_id: Optional[str] = None) -> Dict[str, Any]:
        """Performs one action against the shared page and reports what happened.

        Returns ``{"ok": True, ...}`` or ``{"error": ...}``; the loop feeds the
        message straight back to the model so it can correct itself.
        """
        page = await self._ensure_page()
        if page is None:
            return {"error": "Browser session is unavailable."}

        kind = str(action.get("action") or "").strip().lower()
        detail: Dict[str, Any] = {"ok": True, "action": kind}

        # Navigation takes its own lock, so it is dispatched before we take it.
        if kind in ("goto", "navigate", "open"):
            return await self.goto(action.get("url") or action.get("value", ""), chat_id)

        async with self._lock:
            try:
                if kind == "click":
                    target = await self._resolve_ref(page, action.get("ref", action.get("element")))
                    if not target:
                        return {"error": f"No element with ref {action.get('ref')!r} on this page. "
                                         "Re-read the element list and pick a ref that exists."}
                    selector = action.get("selector") or target.get("selector")
                    label = target.get("text") or target.get("label") or target.get("placeholder")
                    try:
                        await page.click(selector, timeout=12000)
                    except Exception:
                        # Overlays and moving targets can swallow a real click, so
                        # fall back to dispatching the DOM click directly.
                        await page.evaluate(
                            "sel => { const el = document.querySelector(sel); if (el) el.click(); }",
                            selector,
                        )
                    await self._settle(page)
                    detail.update(description=f"clicked '{label}'", url=page.url)
                    self.set_current_url(chat_id, page.url)
                    return detail

                if kind in ("type", "fill", "input"):
                    return await self._type(page, action, chat_id, detail)

                if kind == "press":
                    key = str(action.get("key", "Enter"))
                    await page.keyboard.press(key)
                    await self._settle(page)
                    detail.update(description=f"pressed {key}", url=page.url)
                    self.set_current_url(chat_id, page.url)
                    return detail

                if kind == "scroll":
                    amount = int(action.get("amount", 800))
                    direction = str(action.get("direction", "down")).lower()
                    dy = -abs(amount) if direction in ("up", "back") else abs(amount)
                    await page.mouse.wheel(0, dy)
                    await page.wait_for_timeout(500)
                    scrolled = await page.evaluate("Math.round(window.scrollY)")
                    detail.update(description=f"scrolled {direction} {abs(amount)}px",
                                  scroll_y=scrolled)
                    return detail

                if kind in ("back", "forward"):
                    await (page.go_back if kind == "back" else page.go_forward)(timeout=20000)
                    await self._settle(page)
                    detail.update(description=f"went {kind}", url=page.url)
                    self.set_current_url(chat_id, page.url)
                    return detail

                if kind in ("wait", "sleep"):
                    ms = min(int(action.get("ms", 1500)), 10000)
                    await page.wait_for_timeout(ms)
                    detail.update(description=f"waited {ms}ms")
                    return detail

                # These take the lock themselves, so they must be dispatched
                # before it is acquired: asyncio.Lock is not reentrant and
                # re-entering it here would deadlock the whole session.
                if kind == "screenshot":
                    return await self.screenshot(chat_id)

                if kind in ("extract", "read"):
                    return await self.read_page_text()

                return {"error": f"Unknown action '{kind}'. Valid actions: goto, click, type, "
                                 "select, press, scroll, back, forward, wait, extract, screenshot."}
            except Exception as e:
                return {"error": f"Action '{kind}' failed: {e}"}


    async def _type(self, page: Any, action: Dict[str, Any],
                    chat_id: Optional[str], detail: Dict[str, Any]) -> Dict[str, Any]:
        """Types into a field, resolving fuzzy option labels for <select>."""
        target = await self._resolve_ref(page, action.get("ref", action.get("element")))
        if not target:
            return {"error": f"No input with ref {action.get('ref')!r} on this page. "
                             "Use an <input> or <textarea> ref from the element list."}
        selector = action.get("selector") or target.get("selector")
        text = action.get("text")
        if text is None:
            text = action.get("value", "")
        text = str(text)

        if target.get("tag") == "select":
            options = [str(o.get("label") or o.get("value")) for o in target.get("options") or []]
            chosen = text
            if text not in options and text.lower() not in [o.lower() for o in options]:
                match = next((o for o in options if text.lower() in o.lower()), None)
                if not match:
                    return {"error": f"'{text}' is not an option here. Available: {options[:12]}"}
                chosen = match
            await page.select_option(selector, value=chosen, timeout=10000)
            detail.update(description=f"selected '{chosen}'", url=page.url)
            return detail

        if bool(action.get("clear", True)):
            await page.fill(selector, "", timeout=10000)
        await page.type(selector, text, delay=25, timeout=15000)
        if bool(action.get("submit")) or bool(action.get("press_enter")):
            await page.keyboard.press("Enter")
            await self._settle(page)
            detail["submitted"] = True
        label = target.get("placeholder") or target.get("name") or target.get("label")
        detail.update(description=f"typed {text!r} into '{label}'", url=page.url)
        self.set_current_url(chat_id, page.url)
        return detail

    async def read_page_text(self, max_chars: int = 6000) -> Dict[str, Any]:
        """Returns the visible text of the current page, for reading an answer."""
        page = await self._ensure_page()
        if page is None:
            return {"error": "Browser session is unavailable."}
        async with self._lock:
            try:
                text = await page.evaluate(
                    "() => (document.body ? document.body.innerText : '')"
                    ".replace(/\\n{3,}/g,'\\n\\n').trim()"
                )
                url, title = page.url, await page.title()
            except Exception as e:
                return {"error": f"Could not read the page: {e}"}
        total = len(text or "")
        return {"url": url, "title": title, "text": (text or "")[:max_chars],
                "text_length": total, "truncated": total > max_chars}

    async def screenshot(self, chat_id: Optional[str] = None,
                         filename: Optional[str] = None,
                         full_page: bool = False) -> Dict[str, Any]:
        """Saves a PNG into the chat workspace, like take_screenshot does."""
        page = await self._ensure_page()
        if page is None:
            return {"error": "Browser session is unavailable."}
        target_path = None
        if chat_id:
            ws_dir = WORKSPACES_DIR / chat_id
            ws_dir.mkdir(parents=True, exist_ok=True)
            if not filename:
                try:
                    stem = urlparse(page.url).netloc.replace("www.", "") or "page"
                    stem = re.sub(r"[^a-zA-Z0-9_-]", "_", stem)[:30]
                except Exception:
                    stem = "page"
                filename = f"screenshot_{stem}.png"
            target_path = ws_dir / filename

        async with self._lock:
            try:
                if target_path:
                    await page.screenshot(path=str(target_path), full_page=full_page)
                else:
                    await page.screenshot(full_page=full_page)
            except Exception as e:
                return {"error": f"Screenshot failed: {e}"}

        result: Dict[str, Any] = {"success": True, "url": page.url}
        if target_path:
            result.update(filename=filename, path=str(target_path))
        return result

    async def frame_jpeg(self) -> Optional[str]:
        """Returns one base64 JPEG of the current page, for the live view."""
        page = await self._ensure_page()
        if page is None:
            return None
        try:
            raw = await page.screenshot(type="jpeg", quality=STREAM_QUALITY)
        except Exception:
            return None
        return base64.b64encode(raw).decode("ascii")


browser_session = BrowserSession()

