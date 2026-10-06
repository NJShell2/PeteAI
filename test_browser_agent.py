"""Tests for the autonomous browser agent and the persistent session.

Five things are verified here:

* ``_parse_action`` -- the recovery paths that make the loop survive the way real
  local models reply (prose around JSON, fenced blocks, several objects, junk).
* ``render_observation`` -- the ref-annotated snapshot the model reasons over,
  including the CAPTCHA banner that must dominate it.
* the ReAct loop itself, driven by a scripted fake model and a fake session, so
  step accounting, failure handling, and the pause/resume and ``ask_user`` paths
  can be asserted without a browser.
* the handoff registry -- who is driving, why a wait ended, and the generation
  guard that stops a stale toggle from waking a parked agent.
* viewer input -- coordinate clamping and the dispatch calls each event makes.

An opt-in live test drives the real session against a real page; it is skipped
unless ``RUN_LIVE_BROWSER_TESTS=1``.
"""
import asyncio
import os
import sys
from typing import Any, Dict, List

from app.browser_agent import _parse_action
from app.browser_session import BrowserSession

# -- action parsing --------------------------------------------------------------


def test_parse_action_plain_json():
    assert _parse_action('{"action": "click", "ref": 7}') == {"action": "click", "ref": 7}


def test_parse_action_inside_prose():
    """Models routinely narrate around the JSON."""
    raw = 'Sure! Here you go: {"action": "finish", "answer": "42"} Hope that helps.'
    assert _parse_action(raw) == {"action": "finish", "answer": "42"}


def test_parse_action_from_code_fence():
    raw = 'Here you go:\n```json\n{"action": "type", "ref": 3, "text": "hi"}\n```\n'
    assert _parse_action(raw) == {"action": "type", "ref": 3, "text": "hi"}


def test_parse_action_picks_first_object_with_action_key():
    """A preamble object without "action" must not be mistaken for the command."""
    raw = '{"note": "thinking"} {"action": "click", "ref": 1}'
    assert _parse_action(raw) == {"action": "click", "ref": 1}


def test_parse_action_ignores_braces_in_strings():
    raw = '{"action": "type", "ref": 2, "text": "use {braces} here"}'
    assert _parse_action(raw)["text"] == "use {braces} here"


def test_parse_action_rejects_garbage():
    assert _parse_action("no json at all") is None
    assert _parse_action("") is None
    assert _parse_action(None) is None
    assert _parse_action("```json\n{ broken\n```") is None


# -- session observation rendering -----------------------------------------------

SAMPLE_SNAPSHOT: Dict[str, Any] = {
    "title": "Weather",
    "url": "https://weather.example/wl",
    "headings": ["West Lafayette"],
    "text": "Tomorrow: 78F, partly cloudy.",
    "text_length": 29,
    "refs_truncated": False,
    "elements": [
        {"ref": 0, "tag": "input", "type": "search", "text": None,
         "placeholder": "Search city", "name": "q", "label": None, "value": "",
         "disabled": False, "checked": False, "href": None, "options": None,
         "selector": "input[name=q]"},
        {"ref": 1, "tag": "button", "type": "submit", "text": "Go",
         "placeholder": None, "name": None, "label": None, "value": None,
         "disabled": False, "checked": False, "href": None, "options": None,
         "selector": "button.go"},
        {"ref": 2, "tag": "select", "type": None, "text": "Units",
         "placeholder": None, "name": "units", "label": None, "value": None,
         "disabled": False, "checked": False, "href": None,
         "options": [{"value": "f", "label": "Fahrenheit"}, {"value": "c", "label": "Celsius"}],
         "selector": "select[name=units]"},
        {"ref": 3, "tag": "input", "type": "checkbox", "text": "Daily",
         "placeholder": None, "name": "daily", "label": "Daily forecast", "value": None,
         "disabled": True, "checked": False, "href": None, "options": None,
         "selector": "input[name=daily]"},
    ],
}


def test_render_observation_shows_refs_and_key_fields():
    rendered = BrowserSession().render_observation(SAMPLE_SNAPSHOT)
    assert "[0]<input type=search> Search city" in rendered
    assert "[1]<button type=submit> Go" in rendered
    # Select options must be visible, otherwise the model cannot pick a value.
    assert "Fahrenheit" in rendered and "Celsius" in rendered
    # Disabled state is load-bearing: clicking a disabled ref wastes a step.
    assert "[disabled]" in rendered
    assert "Tomorrow: 78F" in rendered


def test_render_observation_reports_error():
    rendered = BrowserSession().render_observation({"error": "boom"})
    assert rendered.startswith("ERROR") and "boom" in rendered


def test_render_observation_handles_empty_page():
    rendered = BrowserSession().render_observation(
        {"title": "", "url": "https://x", "headings": [], "elements": [], "text": ""}
    )
    assert "none detected" in rendered


def test_render_observation_truncates_element_list():
    snapshot = dict(SAMPLE_SNAPSHOT)
    snapshot["elements"] = [
        {"ref": i, "tag": "a", "text": f"link {i}", "href": f"/{i}", "selector": f"a[{i}]"}
        for i in range(60)
    ]
    rendered = BrowserSession().render_observation(snapshot, max_elements=10)
    assert "[9]<a> link 9" in rendered
    assert "[10]<a> link 10" not in rendered
    assert "50 more elements not shown" in rendered



# -- the loop, driven by a scripted model and a fake session --------------------


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    """Replays a scripted list of replies, one per agent step."""

    def __init__(self, script: List[str]):
        self.script = list(script)
        self.calls: List[Dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("scripted model ran out of replies")
        return _FakeResponse(self.script.pop(0))


class _FakeClient:
    def __init__(self, script):
        self.completions = _FakeCompletions(script)
        self.chat = type("chat", (), {"completions": self.completions})()


class _FakeSession:
    """Stands in for BrowserSession so the loop is tested without a browser.

    It mirrors the real handoff contract closely enough to exercise the pause
    and resume paths: ``handoff_outcome`` scripts how ``wait_for_handoff``
    resolves, and ``ask_questions`` records what the agent asked.
    """

    def __init__(self, actions_result=None, handoff_outcome=None, answer="done"):
        self.actions: List[Dict[str, Any]] = []
        self.actions_result = actions_result or {"ok": True, "description": "ok"}
        self.human = False
        self.answer = answer
        # How each wait resolves. "abandoned" is the user keeping the wheel.
        self.handoff_outcome = handoff_outcome or "resumed"
        self.ask_questions: List[str] = []
        self.handoffs: List[str] = []
        self.observations = 0
        self.stopped = False

    async def observe(self, chat_id=None):
        self.observations += 1
        return dict(SAMPLE_SNAPSHOT)

    def render_observation(self, snapshot, max_elements=45):
        return "URL: " + snapshot.get("url", "")

    async def act(self, action, chat_id=None):
        self.actions.append(action)
        return self.actions_result

    def is_human_controlled(self, chat_id=""):
        return self.human

    def request_stop(self, chat_id=""):
        self.stopped = True

    def consume_stop(self, chat_id=""):
        was, self.stopped = self.stopped, False
        return was

    def is_stopped(self, chat_id=""):
        return self.stopped

    def begin_handoff(self, chat_id, question):
        self.handoffs.append(question)
        return None

    async def wait_for_handoff(self, chat_id, timeout=None):
        outcome = self.handoff_outcome
        if outcome == "resumed":
            # A real hand-back flips the flag; without this the loop would
            # re-park on every step and never get any work done.
            self.human = False
        return {"outcome": outcome, "answer": self.answer}


def _run(coro):
    """Runs a coroutine on a private loop, so tests never depend on a global one."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_LAST_FAKE_CLIENT: List[_FakeClient] = []


def _fake_client_refs() -> List[List[Dict[str, Any]]]:
    """The ``messages`` list of every model call made by the last run."""
    if not _LAST_FAKE_CLIENT:
        return []
    return [call.get("messages") or [] for call in _LAST_FAKE_CLIENT[0].completions.calls]


def _run_loop(script, actions_result=None, max_steps=25, human=False,
              handoff_outcome=None, answer="done"):
    """Runs browser_agent.run against fakes; returns (events, fake_session)."""
    import app.browser_agent as module

    fake_session = _FakeSession(actions_result, handoff_outcome, answer)
    fake_session.human = human
    fake_client = _FakeClient(script)
    _LAST_FAKE_CLIENT.clear()
    _LAST_FAKE_CLIENT.append(fake_client)

    original_session = module.browser_session
    original_get_client = module.llm_client.get_client
    module.browser_session = fake_session
    module.llm_client.get_client = lambda *a, **k: fake_client

    async def drive():
        events = []
        async for event in module.browser_agent.run(
            "tomorrow's weather", chat_id="c1", model_name="m", max_steps=max_steps
        ):
            events.append(event)
        return events

    try:
        events = _run(drive())
    finally:
        module.browser_session = original_session
        module.llm_client.get_client = original_get_client
    return events, fake_session


def _final(events):
    finals = [e["content"] for e in events if e.get("type") == "_final"]
    assert finals, f"no _final event in {events}"
    return finals[0]


def _logs(events):
    return [e.get("data", "") for e in events if e.get("type") == "log"]



def test_loop_finishes_and_returns_answer():
    events, session = _run_loop(
        script=['{"action": "click", "ref": 0}',
                '{"action": "finish", "answer": "Tomorrow: 78F and partly cloudy."}'],
        actions_result={"ok": True, "description": "clicked 'Go'",
                        "url": "https://weather.example/2"},
    )
    assert "78F" in _final(events)
    # It must actually have performed the click before finishing.
    assert session.actions == [{"action": "click", "ref": 0}]


def test_loop_yields_one_step_event_per_action():
    events, _ = _run_loop(
        script=['{"action": "goto", "url": "https://wttr.in"}',
                '{"action": "extract"}',
                '{"action": "finish", "answer": "sunny"}'],
    )
    steps = [e for e in events if e.get("type") == "_step"]
    assert len(steps) == 2
    assert _final(events) == "sunny"


def test_loop_recovers_from_invalid_json():
    events, _ = _run_loop(
        script=['I think I should click the search button',
                '{"action": "finish", "answer": "recovered"}'],
    )
    assert any("not valid JSON" in line for line in _logs(events))
    assert _final(events) == "recovered"


def test_loop_gives_up_after_repeated_parse_failures():
    events, _ = _run_loop(script=['garbage'] * 5)
    assert _final(events)
    assert any("cannot produce valid actions" in line for line in _logs(events))


def test_loop_detects_repeated_action_loop():
    """Clicking the same ref forever must end the run, not burn the whole budget."""
    events, session = _run_loop(
        script=['{"action": "click", "ref": 1}'] * 10,
        actions_result={"ok": True, "description": "clicked 'Go'"},
        max_steps=10,
    )
    assert any("without progress" in line for line in _logs(events))
    # MAX_REPEATED_ACTION is 3, plus the detecting attempt that breaks out.
    assert len(session.actions) <= 4


def test_loop_recovers_from_action_error():
    events, _ = _run_loop(
        script=['{"action": "click", "ref": 99}',
                '{"action": "finish", "answer": "worked around it"}'],
        actions_result={"error": "No element with ref 99 on this page."},
    )
    assert any("failed" in line for line in _logs(events))
    assert _final(events) == "worked around it"


def test_loop_gives_up_after_repeated_action_errors():
    """Distinct targets, so this exercises the error counter and not loop detection."""
    events, _ = _run_loop(
        script=['{"action": "click", "ref": 1}', '{"action": "click", "ref": 2}',
                '{"action": "click", "ref": 3}', '{"action": "click", "ref": 0}',
                '{"action": "finish", "answer": "never reached"}'],
        actions_result={"error": "nope"},
        max_steps=8,
    )
    assert any("repeated action failures" in line for line in _logs(events))
    # It must bail out rather than consume the whole script.
    assert "never reached" not in _final(events)


def test_loop_salvages_answer_at_step_limit():
    """Running out of steps must return an honest partial answer, not a fake one."""
    events, _ = _run_loop(
        script=['{"action": "click", "ref": 1}'] * 4,
        actions_result={"ok": True, "description": "clicked"},
        max_steps=2,
    )
    final = _final(events)
    assert "unconfirmed" in final
    assert "weather.example" in final


def test_loop_stops_when_human_takes_control():
    """A user who keeps the wheel ends the run, rather than resuming it."""
    events, session = _run_loop(
        script=['{"action": "finish", "answer": "never reached"}'],
        human=True,
        handoff_outcome="abandoned",
    )
    assert any("took control" in line for line in _logs(events))
    assert session.actions == []


def test_loop_resumes_after_the_user_hands_back():
    """Handing control back resumes the run instead of ending it."""
    events, session = _run_loop(
        script=['{"action": "click", "ref": 0}',
                '{"action": "finish", "answer": "Found it after the detour."}'],
        human=True,
        handoff_outcome="resumed",
    )
    assert any("handed back" in line for line in _logs(events))
    assert [a.get("action") for a in session.actions] == ["click"]
    assert "Found it after the detour." in _final(events)


def test_loop_asks_the_user_and_carries_the_reply_forward():
    """ask_user parks the run, then feeds the reply back into the model."""
    events, session = _run_loop(
        script=['{"action": "ask_user", "question": "A CAPTCHA is blocking this page."}',
                '{"action": "finish", "answer": "Read it after the CAPTCHA."}'],
        answer="solved it",
    )
    asks = [e for e in events if e.get("type") == "ask_user"]
    assert asks, f"no ask_user event in {events}"
    assert "CAPTCHA" in asks[0]["question"]
    assert session.handoffs == ["A CAPTCHA is blocking this page."]
    # The reply has to reach the model, or the resume is blind.
    second_call = _fake_client_refs()[-1]
    joined = " ".join(str(m.get("content")) for m in second_call)
    assert "solved it" in joined, f"the reply never reached the model: {joined[:400]}"
    assert "Read it after the CAPTCHA." in _final(events)


def test_loop_re_observes_after_the_user_answers():
    """The page may have changed while the user answered, so re-read it."""
    events, session = _run_loop(
        script=['{"action": "ask_user", "question": "Please solve it."}',
                '{"action": "finish", "answer": "ok"}'],
    )
    # One observation before the ask, one fresh one after the reply.
    assert session.observations >= 2


def test_loop_gives_up_when_nobody_answers():
    """A timeout must not stall the run: it carries on and still answers."""
    events, session = _run_loop(
        script=['{"action": "ask_user", "question": "Please help."}',
                '{"action": "finish", "answer": "Worked around it."}'],
        handoff_outcome="timeout",
    )
    assert any("no reply in time" in line for line in _logs(events))
    assert "Worked around it." in _final(events)


def test_loop_requires_a_goal():
    import app.browser_agent as module

    async def drive():
        return [event async for event in module.browser_agent.run("   ")]

    events = _run(drive())
    assert events and events[0]["type"] == "error"


def test_prompt_states_the_goal_and_action_format():
    from app.browser_agent import AGENT_SYSTEM_PROMPT
    prompt = AGENT_SYSTEM_PROMPT.format(goal="find tomorrow's weather")
    assert "find tomorrow's weather" in prompt
    assert '"action": "click"' in prompt
    assert '"action": "finish"' in prompt



def test_actions_that_take_their_own_lock_do_not_deadlock():
    """screenshot/extract acquire the session lock themselves.

    asyncio.Lock is not reentrant, so dispatching them from inside the locked
    section of act() would hang the session forever. They must be handled
    before the lock is taken.
    """
    from app.browser_session import BrowserSession as _Session

    session = _Session()

    class _FakePage:
        def is_closed(self):
            return False

    async def fake_ensure_page():
        return _FakePage()

    session._ensure_page = fake_ensure_page

    called = {}

    async def fake_screenshot(chat_id=None, **kwargs):
        called["screenshot"] = True
        return {"success": True}

    async def fake_read(max_chars=6000):
        called["read"] = True
        return {"text": "ok"}

    session.screenshot = fake_screenshot
    session.read_page_text = fake_read

    async def drive():
        # A hang here would fail on the timeout instead of blocking the run.
        await asyncio.wait_for(session.act({"action": "screenshot"}, "c1"), timeout=5)
        await asyncio.wait_for(session.act({"action": "extract"}, "c1"), timeout=5)

    _run(drive())
    assert called.get("screenshot") and called.get("read"), called


# -- human handoff and CAPTCHA detection -----------------------------------------


def test_render_observation_flags_a_human_verification_wall():
    """A CAPTCHA must be unmissable, or the model wastes steps clicking it."""
    snapshot = dict(SAMPLE_SNAPSHOT)
    snapshot["challenge"] = {"kind": "widget", "detail": "g-recaptcha"}
    rendered = BrowserSession().render_observation(snapshot)
    assert "HUMAN VERIFICATION REQUIRED" in rendered
    assert "g-recaptcha" in rendered
    assert "ask_user" in rendered
    # It belongs at the top, before the element list invites a click.
    assert rendered.index("HUMAN VERIFICATION REQUIRED") < rendered.index("INTERACTIVE")


def test_render_observation_is_quiet_without_a_challenge():
    rendered = BrowserSession().render_observation(dict(SAMPLE_SNAPSHOT))
    assert "HUMAN VERIFICATION" not in rendered


def test_control_state_tracks_generations():
    session = BrowserSession()
    assert session.control_state("c1")["human_control"] is False
    first = session.set_human_control("c1", True)
    assert first["human_control"] is True and first["generation"] == 1
    # Setting the same value again is a no-op, not a spurious generation bump.
    assert session.set_human_control("c1", True)["generation"] == 1
    assert session.set_human_control("c1", False)["generation"] == 2
    assert session.is_human_controlled("c1") is False


def test_handoff_reports_an_answer():
    session = BrowserSession()

    async def drive():
        session.begin_handoff("c1", "Please solve the CAPTCHA.")
        # Answer from another task, as the HTTP endpoint would.
        await asyncio.sleep(0)
        assert session.answer_handoff("c1", "done, it let me through")
        return await session.wait_for_handoff("c1", timeout=5)

    outcome = _run(drive())
    assert outcome["outcome"] == "answered"
    assert outcome["answer"] == "done, it let me through"


def test_handoff_reports_a_resume_when_control_comes_back():
    """Handing back without answering is a resume, not a hang."""
    session = BrowserSession()

    async def drive():
        # The real order: the user takes over, the agent parks, they hand back.
        session.set_human_control("c1", True)
        session.begin_handoff("c1", "")
        await asyncio.sleep(0)
        session.set_human_control("c1", False)
        return await session.wait_for_handoff("c1", timeout=5)

    outcome = _run(drive())
    assert outcome["outcome"] == "resumed"
    assert outcome["answer"] is None


def test_handoff_reports_abandoned_when_the_user_keeps_the_wheel():
    """Stopping while still holding the wheel must not resume the run."""
    session = BrowserSession()

    async def drive():
        session.set_human_control("c1", True)
        session.begin_handoff("c1", "")
        await asyncio.sleep(0)
        session.cancel_handoff("c1")  # e.g. the user pressed stop
        return await session.wait_for_handoff("c1", timeout=5)

    outcome = _run(drive())
    assert outcome["outcome"] == "abandoned"
    assert session.is_human_controlled("c1") is True


def test_the_first_release_reason_wins():
    """A reply and a hand-back at once resolves to whichever landed first.

    The answer is kept because it is the more specific signal, and the run
    resumes either way, so nothing is lost by the ordering.
    """
    session = BrowserSession()

    async def drive():
        session.set_human_control("c1", True)
        session.begin_handoff("c1", "done yet?")
        await asyncio.sleep(0)
        session.answer_handoff("c1", "yes")
        session.set_human_control("c1", False)
        return await session.wait_for_handoff("c1", timeout=5)

    outcome = _run(drive())
    assert outcome["outcome"] == "answered"
    assert outcome["answer"] == "yes"
    assert session.is_human_controlled("c1") is False


def test_handoff_times_out_rather_than_waiting_forever():
    session = BrowserSession()

    async def drive():
        session.begin_handoff("c1", "anyone there?")
        return await session.wait_for_handoff("c1", timeout=0.05)

    outcome = _run(drive())
    assert outcome["outcome"] == "timeout"
    # The question is cleared so the UI does not keep showing a dead prompt.
    assert session.control_state("c1")["awaiting_user"] is False


def test_answer_without_a_question_is_rejected():
    assert BrowserSession().answer_handoff("nobody", "hello") is False


def test_stale_resume_cannot_wake_a_parked_agent():
    """A toggle that lands after the park was released must not re-park it.

    The release reason is latched the moment it is recorded, so a control
    change arriving a moment later cannot be mistaken for the hand-back that
    already ended the wait.
    """
    session = BrowserSession()

    async def drive():
        session.set_human_control("c1", True)
        session.begin_handoff("c1", "first question")
        await asyncio.sleep(0)
        # The user hands back, then immediately takes over again.
        session.set_human_control("c1", False)
        session.set_human_control("c1", True)
        return await session.wait_for_handoff("c1", timeout=5)

    outcome = _run(drive())
    # The hand-back already released the park, so a later re-take-over cannot
    # retroactively turn that release into a stop.
    assert outcome["outcome"] == "resumed"


# -- stop ------------------------------------------------------------------------


def test_stop_ends_the_run_at_the_next_step():
    """Pressing Stop must end the loop, not just the stream.

    Aborting the SSE response on its own is not enough: the agent keeps working
    until its generator happens to unwind, so the run can outlive the button.
    """
    events, _ = _stop_mid_run()
    assert any(e.get("type") == "stopped" for e in events)
    # One step ran, then it stopped -- nowhere near the six-step budget.
    assert len([e for e in events if e.get("type") == "_step"]) == 1
    assert "Stopped after" in _final(events)


def test_an_unstopped_run_uses_its_full_budget():
    """Control: without a stop, the loop really would have kept going."""
    events, _ = _run_loop(
        script=['{"action": "scroll", "direction": "down"}'] * 3,
        max_steps=3,
    )
    assert len([e for e in events if e.get("type") == "_step"]) == 3
    assert not any(e.get("type") == "stopped" for e in events)


def _stop_mid_run(stale_stop=False):
    """Runs the loop and presses stop as soon as the first action is performed.

    ``stale_stop`` seeds the flag first, standing in for a stop left behind by an
    earlier abandoned run.
    """
    import app.browser_agent as module

    fake_session = _FakeSession()
    fake_session.stopped = bool(stale_stop)
    fake_client = _FakeClient(['{"action": "click", "ref": 0}'] * 6)
    original_session = module.browser_session
    original_get_client = module.llm_client.get_client
    module.browser_session = fake_session
    module.llm_client.get_client = lambda *a, **k: fake_client

    async def drive():
        events = []
        async for event in module.browser_agent.run(
            "tomorrow's weather", chat_id="c1", model_name="m", max_steps=6
        ):
            events.append(event)
            # The user sees Pete step once, then hits stop.
            if event.get("type") == "_step" and not fake_session.stopped:
                fake_session.request_stop("c1")
        return events

    try:
        events = _run(drive())
    finally:
        module.browser_session = original_session
        module.llm_client.get_client = original_get_client
    return events, fake_session


def test_stop_does_not_take_the_wheel():
    """Stop is not a take-over, and must not be allowed to become one.

    This is the bug that made Stop unusable: the old client-side handler issued a
    take-over to nudge the agent out of its loop, which left the session flagged as
    human-controlled, so the very next run parked on its first line and never
    started.
    """
    session = BrowserSession()
    session.request_stop("c1")
    assert session.is_stopped("c1") is True
    # The flag is set, but the wheel is untouched.
    assert session.is_human_controlled("c1") is False


def test_a_stale_stop_does_not_kill_the_next_run():
    """One press stops one run; the flag must not survive into the next goal."""
    session = BrowserSession()
    session.request_stop("c1")
    assert session.consume_stop("c1") is True
    assert session.consume_stop("c1") is False


def test_a_stale_stop_does_not_wedge_the_next_run():
    """A run that starts with a stop already flagged must still do work.

    The flag left behind by an abandoned run is consumed once at the top of
    run(), so it cannot kill the next goal. This is the regression guard for the
    original bug, where stopping left the session human-controlled and every
    later run parked on its first line.
    """
    # stale_stop seeds the flag the way a previous, abandoned run would.
    events, _ = _stop_mid_run(stale_stop=True)
    # Exactly one step happened before the mid-run stop, which is only possible
    # if the stale flag was cleared rather than read as this run's own stop.
    assert len([e for e in events if e.get("type") == "_step"]) == 1
    assert any(e.get("type") == "stopped" for e in events)


def test_stop_releases_an_agent_parked_on_a_question():
    """A stopped agent must not sit out the full handoff timeout."""
    session = BrowserSession()

    async def drive():
        session.begin_handoff("c1", "which city?")
        await asyncio.sleep(0)
        session.request_stop("c1")
        return await session.wait_for_handoff("c1", timeout=5)

    outcome = _run(drive())
    # "canceled" rather than "abandoned": no one took the wheel, the user pressed
    # stop.
    assert outcome["outcome"] == "canceled"
    assert session.is_stopped("c1") is True


def test_a_stopped_run_does_not_claim_to_have_hit_a_step_limit():
    """The closing message must not invent a failure that did not happen."""
    events, _ = _stop_mid_run()
    final = _final(events)
    assert "step limit" not in final
    assert "Stop" in final or "stopped" in final.lower()


def test_stop_endpoint_does_not_take_control():
    """The API contract mirrors the session contract."""
    import app.api as api

    api.browser_session.clear_human_control("stop-check")
    _ = _run(api.browser_stop("stop-check"))
    assert api.browser_session.is_human_controlled("stop-check") is False
    assert api.browser_session.is_stopped("stop-check") is True


# -- viewer input mapped into the page --------------------------------------------


class _StubMouse:
    """Playwright's mouse API is async, so these must be too."""

    def __init__(self):
        self.calls = []

    async def move(self, x, y):
        self.calls.append(("move", x, y))

    async def down(self, button="left"):
        self.calls.append(("down", button))

    async def up(self, button="left"):
        self.calls.append(("up", button))

    async def click(self, x, y, button="left", click_count=1):
        self.calls.append(("click", x, y, button, click_count))

    async def dblclick(self, x, y, button="left"):
        self.calls.append(("dblclick", x, y, button))

    async def wheel(self, dx, dy):
        self.calls.append(("wheel", dx, dy))


class _StubKeyboard:
    def __init__(self):
        self.calls = []

    async def down(self, key):
        self.calls.append(("down", key))

    async def up(self, key):
        self.calls.append(("up", key))

    async def press(self, key):
        self.calls.append(("press", key))

    async def insert_text(self, text):
        self.calls.append(("insert_text", text))


class _StubPage:
    """The sliver of Playwright's page API the input tests touch."""

    viewport_size = {"width": 100, "height": 50}

    def __init__(self):
        self.mouse = _StubMouse()
        self.keyboard = _StubKeyboard()


def _session_with_stub_page():
    session = BrowserSession()
    page = _StubPage()

    async def fake_ensure_page():
        return page

    session._ensure_page = fake_ensure_page
    return session, page


def test_dispatch_pointer_clamps_out_of_range_coordinates():
    """A stale frame or a resize mid-click must not send absurd offsets."""
    session, page = _session_with_stub_page()
    result = _run(session.dispatch_input(
        {"kind": "pointer", "action": "click", "x": 5000, "y": -40}))
    assert result.get("ok"), result
    assert page.mouse.calls == [("click", 99.0, 0.0, "left", 1)]


def test_dispatch_key_press_and_text_insert():
    session, page = _session_with_stub_page()
    _run(session.dispatch_input({"kind": "key", "action": "press", "key": "Enter"}))
    _run(session.dispatch_input({"kind": "text", "text": "hello world"}))
    assert page.keyboard.calls == [("press", "Enter"), ("insert_text", "hello world")]


def test_dispatch_wheel_forwards_both_axes():
    session, page = _session_with_stub_page()
    _run(session.dispatch_input({"kind": "wheel", "deltaX": 0, "deltaY": 400}))
    assert page.mouse.calls == [("wheel", 0.0, 400.0)]


def test_dispatch_rejects_unknown_and_incomplete_input():
    session, page = _session_with_stub_page()
    assert "Unknown input kind" in _run(
        session.dispatch_input({"kind": "telepathy"}))["error"]
    assert "missing a key" in _run(
        session.dispatch_input({"kind": "key", "action": "press"}))["error"]
    assert "missing text" in _run(session.dispatch_input({"kind": "text"}))["error"]


def test_dispatch_normalizes_an_impossible_button():
    """A bogus button must not reach Playwright, which would raise."""
    session, page = _session_with_stub_page()
    result = _run(session.dispatch_input(
        {"kind": "pointer", "action": "click", "x": 5, "y": 5, "button": "paw"}))
    assert result.get("ok"), result
    assert page.mouse.calls[0][3] == "left"


# -- opt-in live browser test ----------------------------------------------------


async def _live_smoke():
    """Drives a real page: navigate, observe refs, act, read back."""
    session = BrowserSession()
    try:
        await session.goto("https://example.com", chat_id="live")
        snapshot = await session.observe("live")
        assert snapshot.get("title"), "expected a page title"
        assert snapshot.get("elements"), "expected interactive elements on example.com"
        assert BrowserSession().render_observation(snapshot).startswith("URL:")

        # A real ref must round-trip through act().
        first_link = next(
            (e for e in snapshot["elements"] if e.get("tag") == "a" and e.get("href")),
            None,
        )
        if first_link:
            result = await session.act({"action": "click", "ref": first_link["ref"]}, "live")
            assert result.get("error") is None, result

        text = await session.read_page_text()
        assert text.get("text"), "expected page text"
        assert await session.frame_jpeg(), "expected a JPEG frame"
    finally:
        await session.close()


def test_live_browser_smoke():
    if os.environ.get("RUN_LIVE_BROWSER_TESTS") != "1":
        print("[SKIP] live browser test (set RUN_LIVE_BROWSER_TESTS=1 to enable)")
        return
    _run(_live_smoke())
    print("[OK] test_live_browser_smoke")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"[OK] {name}")
        except Exception as exc:  # noqa: BLE001 - top level test runner
            failures += 1
            print(f"[FAIL] {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)

