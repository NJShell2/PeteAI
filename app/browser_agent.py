"""Autonomous goal-driven browsing for Pete AI.

The existing browser tools answer "what is on this page?". This module answers
"find tomorrow's weather" by running a ReAct loop over the persistent session in
``app.browser_session``:

    goal -> observe -> model picks ONE action -> perform it -> observe again ...

Each step is yielded as an event so the chat UI can narrate progress and the
live view can follow along. The loop ends when the model calls ``finish`` with an
answer, when a human takes the wheel and keeps it, or when the step budget runs
out -- in which case it returns the best answer it can assemble from what it did
see.

Three details matter more than they look:

* **One action per step.** Asking for a single action keeps each decision
  auditable and means a wrong turn costs one step rather than derailing the run.
* **Loop detection.** Models happily click the same cookie banner forever, so
  repeating the same action+target three times ends the run early.
* **A human can take over, and hand back.** Taking the wheel parks the run
  instead of killing it, and ``ask_user`` puts a question on screen and waits for
  a reply. Either way the page is re-read afterwards, so the model resumes from
  what is really there rather than from a stale snapshot. Every wait ends -- an
  unanswered handoff times out and falls back to a salvaged answer.
"""
import json
import re
from typing import Any, AsyncGenerator, Dict, List, Optional

from app.browser_session import browser_session
from app.config import load_settings
from app.llm_client import llm_client
from app.agent_helpers import current_date_note as _current_date_note

# Enough for a search, a click into results, and a read. Long enough to survive a
# login wall, short enough that a confused model cannot burn the credit balance.
DEFAULT_MAX_STEPS = 25

# How many times the identical action on the identical target may repeat.
MAX_REPEATED_ACTION = 3

# Tool-call-free format: local models follow plain text turns far more reliably
# than native function calling for this kind of tight observe/act cycle.
AGENT_SYSTEM_PROMPT = """You are an autonomous web-browsing agent. You accomplish \
one goal by operating a real web browser, one action at a time.

GOAL:
{goal}

Each turn you receive the current page: its URL, title, its interactive elements \
numbered like [0], [1], [2], and its visible text.

HOW TO ACT
Reply with a single JSON object and nothing else -- no prose, no markdown fence.

To do something:
{{"action": "click", "ref": 7}}
{{"action": "type", "ref": 3, "text": "West Lafayette weather", "submit": true}}
{{"action": "goto", "url": "https://example.com"}}
{{"action": "select", "ref": 5, "value": "Celsius"}}
{{"action": "press", "key": "Enter"}}
{{"action": "scroll", "direction": "down", "amount": 800}}
{{"action": "extract"}}
{{"action": "back"}}
{{"action": "wait", "ms": 2000}}

To ask the human for help, then wait for their reply:
{{"action": "ask_user", "question": "A CAPTCHA is blocking this page. Please solve it, then choose Resume."}}

To report that you are done:
{{"action": "finish", "answer": "The complete answer, with the specifics you found."}}

RULES
- Use the ref numbers from the page you were just shown. Do not invent refs or \
guess CSS selectors; if a ref is missing, scroll or navigate and look again.
- Search engines are usually the fastest route: type a query into the search box \
and set "submit": true.
- Close cookie banners and consent dialogs early, since they block the page.
- When the answer is on screen, read it carefully and call finish. Do not keep \
browsing once you have it.
- If a step errors, read the error and try a different ref or approach.
- Put concrete values (temperatures, dates, names, numbers) in the "answer".
- Never claim you verified something you only assumed; if you could not find it, \
say so in the answer.
- If the page is marked HUMAN VERIFICATION REQUIRED, or you need credentials, a \
one-time code, or a judgement call, call ask_user and wait. Do not try to click \
through a CAPTCHA, and do not guess a password. After the user answers you will \
be shown the page as it is now, so re-read it before acting.
"""


def _parse_action(text: str) -> Optional[Dict[str, Any]]:
    """Extracts the action JSON from a model reply.

    Small models routinely wrap the object in prose or a ```json fence, and
    sometimes emit a couple of objects, so this tolerates all of that and takes
    the first object that actually carries an "action" key.
    """
    if not text:
        return None
    body = text.strip()

    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", body)
    if fenced:
        body = fenced.group(1).strip()

    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # Balanced-brace scan that ignores braces inside strings.
    start = -1
    depth = 0
    in_string = False
    escaped = False
    for i, char in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = i
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    try:
                        candidate = json.loads(body[start:i + 1])
                        if isinstance(candidate, dict) and candidate.get("action"):
                            return candidate
                    except Exception:
                        pass
                    start = -1
    return None



class BrowserAgent:
    """Runs a single goal against the shared browser session."""

    def __init__(self) -> None:
        pass

    def _build_messages(self, goal: str, history: List[Dict[str, str]],
                        observation: str,
                        consecutive_errors: int) -> List[Dict[str, str]]:
        """Assembles the message list for one decision.

        History is plain user/assistant text rather than native tool calls,
        because the loop is driven by JSON replies and local models follow that
        far more reliably. Each past step ends with a fresh observation so the
        model never has to remember a page it has already navigated away from.
        """
        messages: List[Dict[str, str]] = [{
            "role": "system",
            # The date matters here more than anywhere else: this agent clicks
            # through real pages and has to recognise "today" on a calendar or a
            # weather widget, which a stale checkpoint cannot do unaided.
            "content": _current_date_note() + AGENT_SYSTEM_PROMPT.format(goal=goal),
        }]
        messages.extend(history)

        nudge = ""
        if consecutive_errors:
            nudge = ("\n\nYour last action failed. Read the error, pick a DIFFERENT ref "
                     "or approach, and do not repeat the failed action.")
        messages.append({"role": "user", "content": observation + nudge})
        return messages

    def _summarize_history(self, history: List[Dict[str, str]],
                           max_chars: int = 12000) -> List[Dict[str, str]]:
        """Trims the oldest step away when the transcript grows too long.

        A long run can push the transcript past a small model's context, so the
        first step is replaced by a one-line record of what it did.
        """
        if sum(len(m.get("content", "")) for m in history) <= max_chars:
            return history
        if len(history) < 2:
            return history
        dropped = history[0]
        return [{
            "role": "user",
            "content": f"(earlier step omitted: {dropped.get('content', '')[:200]})",
        }] + history[1:]


    async def run(self, goal: str, chat_id: Optional[str] = None,
                  model_name: Optional[str] = None,
                  max_steps: int = DEFAULT_MAX_STEPS
                  ) -> AsyncGenerator[Dict[str, Any], None]:
        """Drives the browser toward ``goal``, yielding progress events.

        Yields ``_step``/``url``/``log``/``error`` while running and a final
        ``_final`` event carrying the answer.
        """
        goal = (goal or "").strip()
        if not goal:
            yield {"type": "error", "error": "A goal is required."}
            return

        settings = load_settings()
        model = model_name or settings.default_model
        client = llm_client.get_client()

        history: List[Dict[str, str]] = []
        answer: Optional[str] = None
        consecutive_errors = 0
        last_page_text = ""
        last_url = ""
        # Consecutive observations showing a human-verification wall. Two in a
        # row means the page is genuinely stuck behind it, so the agent asks the
        # human directly instead of hoping the model remembers to.
        challenge_streak = 0
        # action+target fingerprints, used to break click-forever loops.
        repeats: Dict[str, int] = {}
        steps_taken = 0
        stopped = False

        yield {"type": "log", "data": f"Browser agent started: {goal}"}

        # A stop left over from an abandoned run must not kill this one.
        browser_session.consume_stop(chat_id or "")

        for step in range(1, max_steps + 1):
            # Stop is checked at the top of every step, before anything is acted
            # on. Checking after each await is what keeps Stop feeling immediate:
            # the loop still finishes the in-flight action, but never starts
            # another one.
            if browser_session.consume_stop(chat_id or ""):
                stopped = True
                yield {"type": "log", "data": "Stopped at your request."}
                yield {"type": "stopped", "step": step}
                break

            # A human on the wheel parks the run rather than killing it, so they
            # can step in to solve a CAPTCHA or type a password and then hand
            # back and let the agent continue from the page as it now is.
            if browser_session.is_human_controlled(chat_id or ""):
                yield {"type": "log", "data": "You took control of the browser; pausing."}
                yield {"type": "handoff", "state": "user", "step": step,
                       "data": "You took control. Pete is paused."}
                outcome = await self._wait_for_user(chat_id or "", step)
                if outcome["outcome"] == "abandoned":
                    # Still the user's turn when the wait ended: the run stops.
                    yield {"type": "log", "data": "Still your control; stopping."}
                    yield {"type": "handoff", "state": "user",
                           "data": "Still yours -- stopping."}
                    break
                yield {"type": "log", "data": "You handed back; Pete is continuing."}
                yield {"type": "handoff", "state": "agent",
                       "data": "You handed back. Pete is continuing."}
                if outcome.get("answer"):
                    # The reply and the fresh page below are one observation, so
                    # the model resumes from reality rather than from memory.
                    fresh = await browser_session.observe(chat_id)
                    if fresh.get("error"):
                        yield {"type": "error", "error": fresh["error"]}
                        return
                    if fresh.get("text"):
                        last_page_text = fresh["text"]
                    if fresh.get("url"):
                        last_url = fresh["url"]
                    history.append({
                        "role": "user",
                        "content": f"THE USER REPLIED: {outcome['answer']}\n"
                                   f"The page is now:\n"
                                   f"{browser_session.render_observation(fresh)[:6000]}",
                    })
                continue
            steps_taken = step

            snapshot = await browser_session.observe(chat_id)
            if snapshot.get("error"):
                yield {"type": "error", "error": snapshot["error"]}
                return

            if snapshot.get("challenge"):
                challenge_streak += 1
            else:
                challenge_streak = 0

            observation = browser_session.render_observation(snapshot)
            if snapshot.get("text"):
                last_page_text = snapshot["text"]
            if snapshot.get("url") and snapshot["url"] != last_url:
                last_url = snapshot["url"]
                yield {"type": "url", "url": last_url}

            history = self._summarize_history(history)
            messages = self._build_messages(goal, history, observation, consecutive_errors)
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.2,
                    max_tokens=600,
                )
                reply = response.choices[0].message.content or ""
            except Exception as e:
                yield {"type": "error", "error": f"Browser agent LLM error: {e}"}
                return

            action = _parse_action(reply)
            if not action:
                # Unparseable output is a failed step, not a crash: re-prompt with
                # a stricter nudge and let the model recover.
                consecutive_errors += 1
                history.append({"role": "assistant", "content": (reply or "(empty reply)")[:800]})
                history.append({
                    "role": "user",
                    "content": 'That was not a valid JSON action. Reply with ONLY the JSON '
                               'object, for example: {"action": "click", "ref": 3}',
                })
                yield {"type": "log", "data": f"Step {step}: model reply was not valid JSON."}
                if consecutive_errors >= 3:
                    yield {"type": "log",
                           "data": "Stopping: the model cannot produce valid actions."}
                    break
                continue

            kind = str(action.get("action", "")).strip().lower()

            if kind in ("ask_user", "ask", "handoff", "need_help"):
                challenge_streak = 0  # the model asked on its own; reset
            elif challenge_streak >= 2:
                # The page is still challenge-walled after the last step. Stop
                # hoping the model asks and ask the human directly, through the
                # same ask_user flow below.
                _detail = (snapshot.get("challenge") or {}).get("detail", "verification")
                _where = snapshot.get("url") or last_url or "this page"
                action = {"action": "ask_user", "question": (
                    f"A human-verification challenge ({_detail}) is blocking {_where}. "
                    "I cannot solve it myself. Please press Take control, complete the "
                    "check in the live view, then hand control back (Esc) and I will "
                    "continue from the page as it is.")}
                kind = "ask_user"
                challenge_streak = 0

            if kind in ("ask_user", "ask", "handoff", "need_help"):
                yield {"type": "_step", "step": step, "action": action}
                question = str(action.get("question") or action.get("text")
                               or action.get("reason") or "").strip()
                if not question:
                    question = "I need your help to continue on this page."
                yield {"type": "ask_user", "step": step, "question": question,
                       "url": last_url}
                yield {"type": "log", "data": f"Step {step}: asking you for help."}
                outcome = await self._ask(chat_id or "", step, question)
                if outcome["outcome"] in ("abandoned", "canceled"):
                    yield {"type": "log", "data": "Stopped while waiting for you."}
                    break
                if outcome["outcome"] == "timeout":
                    # Nobody answered in time. Salvage whatever is on screen
                    # rather than silently stalling the run forever.
                    yield {"type": "log", "data":
                           f"Step {step}: no reply in time; continuing without help."}
                    history.append({"role": "assistant", "content": json.dumps(action)[:600]})
                    history.append({"role": "user", "content":
                                    "The user did not reply. Continue on your own, or call "
                                    "finish and explain what is blocking you."})
                    continue
                yield {"type": "log", "data": f"Step {step}: the user replied."}
                history.append({"role": "assistant", "content": json.dumps(action)[:600]})
                # Re-observe: the user changed the page while answering, so the
                # old observation is stale exactly when it matters most.
                fresh = await browser_session.observe(chat_id)
                if fresh.get("error"):
                    yield {"type": "error", "error": fresh["error"]}
                    return
                if fresh.get("text"):
                    last_page_text = fresh["text"]
                if fresh.get("url") and fresh["url"] != last_url:
                    last_url = fresh["url"]
                    yield {"type": "url", "url": last_url}
                history.append({"role": "user", "content":
                                f"THE USER REPLIED: {outcome.get('answer') or '(done, no text)'}\n"
                                f"The page is now:\n"
                                f"{browser_session.render_observation(fresh)[:6000]}"})
                continue

            if kind in ("finish", "done", "complete", "answer"):
                answer = str(action.get("answer") or action.get("text") or "").strip()
                yield {"type": "log", "data": "Browser agent finished."}
                break

            # Loop detection before acting, so a doomed step costs nothing.
            fingerprint = f"{kind}:{action.get('ref', action.get('url', action.get('key', '')))}"
            repeats[fingerprint] = repeats.get(fingerprint, 0) + 1
            if repeats[fingerprint] > MAX_REPEATED_ACTION:
                yield {"type": "log", "data":
                       f"Step {step}: stopping, '{kind}' on the same target repeated "
                       f"{repeats[fingerprint]} times without progress."}
                break

            if browser_session.is_human_controlled(chat_id or ""):
                # Grabbed the wheel while the model was thinking: loop back so
                # the top-of-loop check parks the run instead of firing a step
                # into a page the user is driving.
                continue

            yield {"type": "_step", "step": step, "action": action}
            result = await browser_session.act(action, chat_id)

            if result.get("error"):
                consecutive_errors += 1
                history.append({"role": "assistant", "content": json.dumps(action)[:600]})
                history.append({"role": "user", "content": f"ERROR: {result['error']}"})
                yield {"type": "log", "data": f"Step {step} failed: {result['error']}"}
                if consecutive_errors >= 4:
                    yield {"type": "log", "data": "Stopping after repeated action failures."}
                    break
                continue

            consecutive_errors = 0
            description = result.get("description") or f"ran {kind}"
            after = await browser_session.observe(chat_id)
            history.append({"role": "assistant", "content": json.dumps(action)[:600]})
            history.append({"role": "user", "content":
                            f"OK: {description}. The page is now:\n"
                            f"{browser_session.render_observation(after)[:6000]}"})
            yield {"type": "log", "data": f"Step {step}: {description}"}
            if result.get("url"):
                yield {"type": "url", "url": result["url"]}

        if not answer:
            # Out of steps with no explicit finish: assemble the most honest
            # answer possible from the last thing the page actually showed.
            if stopped:
                # A stop is not a step limit. Saying "I hit my step limit" after the
                # user pressed Stop would invent a failure that never happened.
                answer = self._stopped_answer(goal, last_url, steps_taken)
            else:
                answer = self._salvage_answer(goal, last_page_text, last_url, steps_taken)

        yield {"type": "_final", "content": answer}


    async def _ask(self, chat_id: str, step: int, question: str) -> Dict[str, Any]:
        """Parks the agent on a question and waits for the user's reply.

        The handoff is released by a reply, by a control change, or by the
        timeout, so a user who walks away cannot wedge the run.
        """
        browser_session.begin_handoff(chat_id, question)
        return await browser_session.wait_for_handoff(chat_id)

    async def _wait_for_user(self, chat_id: str, step: int) -> Dict[str, Any]:
        """Waits out a human take-over, without putting a question on screen.

        There is nothing to ask here, so the handoff is opened with an empty
        question; the user ends it by handing control back or by answering the
        prompt in the chat box.
        """
        browser_session.begin_handoff(chat_id, "")
        return await browser_session.wait_for_handoff(chat_id)

    def _stopped_answer(self, goal: str, url: str, steps: int) -> str:
        """Says the run was stopped, without pretending it failed or succeeded."""
        where = f"\nThe browser was left on: {url}" if url else ""
        return (
            f"Stopped after {steps} step(s) -- you pressed Stop, so I stopped there."
            f"{where}\n\n"
            f"Nothing further is running. Send the request again and I will pick up "
            f"from the page as it is now."
        )

    def _salvage_answer(self, goal: str, page_text: str, url: str, steps: int) -> str:
        """Builds an honest partial answer when the loop ends without finishing."""
        if not page_text:
            return (f"I could not complete '{goal}' within {steps} step(s) -- the browser "
                    f"never reached a page I could read. Try a more specific goal, or give "
                    f"me a direct URL to start from.")
        excerpt = page_text.strip()[:1200]
        return (
            f"I made partial progress on '{goal}' and hit my step limit, so treat this as "
            f"unconfirmed rather than a final answer.\n\n"
            f"Last page visited: {url}\n"
            f"Page content:\n---\n{excerpt}\n---\n\n"
            f"Send the request again and the agent will continue from here."
        )


browser_agent = BrowserAgent()

