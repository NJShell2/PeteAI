"""Offline regression tests for the Pete Squad multi-agent pipeline.

The LLM is replaced with a scripted fake and `browser_tool.search_web` with canned
results, so the whole Orchestrator -> Browser -> Workspace -> Critic flow can be
asserted deterministically without network access or API credits.
"""
import asyncio
import shutil
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from app import agent as agent_module
from app.agent import (
    SQUAD_BROWSER,
    SQUAD_CRITIC,
    SQUAD_ORCHESTRATOR,
    SQUAD_WORKSPACE,
    extract_json_object,
    pete_agent,
)
from app.storage import storage


# ── Fakes ─────────────────────────────────────────────────────────────────────────

class _Delta:
    def __init__(self, content: Optional[str] = None, tool_calls: Optional[list] = None):
        self.content = content
        self.tool_calls = tool_calls


class _Function:
    def __init__(self, name: Optional[str] = None, arguments: Optional[str] = None):
        self.name = name
        self.arguments = arguments


class _ToolCallDelta:
    def __init__(self, index: int, call_id: Optional[str] = None, name: Optional[str] = None, arguments: Optional[str] = None):
        self.index = index
        self.id = call_id
        self.function = _Function(name, arguments)


class _Chunk:
    def __init__(self, delta: _Delta):
        self.choices = [SimpleNamespace(delta=delta)]


class _Stream:
    def __init__(self, chunks: List[_Chunk]):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk
        return gen()


class _TextResponse:
    def __init__(self, text: str):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=text))]


class _FakeCompletions:
    def __init__(self, script: List[Any]):
        self.script = list(script)
        self.calls: List[Dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("fake LLM ran out of scripted turns")
        item = self.script.pop(0)
        if kwargs.get("stream"):
            assert isinstance(item, list), "stream=True expects a list of chunks"
            return _Stream(item)
        assert isinstance(item, str), "non-streaming calls expect a string reply"
        return _TextResponse(item)


class _FakeClient:
    def __init__(self, script: List[Any]):
        self.completions = _FakeCompletions(script)
        self.chat = SimpleNamespace(completions=self.completions)


def streamed_text(text: str) -> List[_Chunk]:
    return [_Chunk(_Delta(content=text[i:i + 24])) for i in range(0, len(text), 24)] or [_Chunk(_Delta(content=""))]


def streamed_tool_call(call_id: str, name: str, arguments: str) -> List[_Chunk]:
    return [
        _Chunk(_Delta(tool_calls=[_ToolCallDelta(0, call_id, name, "")])),
        _Chunk(_Delta(tool_calls=[_ToolCallDelta(0, None, None, arguments)])),
    ]


FAKE_SEARCH_RESULTS = [
    {"title": "Purdue University", "url": "https://www.purdue.edu/", "snippet": "Founded in 1869."},
    {"title": "Purdue History", "url": "https://en.wikipedia.org/wiki/Purdue_University", "snippet": "Land-grant university."},
]


def _patch(script: List[Any]):
    """Patches the LLM client and web search; returns a restore callable."""
    client = _FakeClient(script)
    original_client = agent_module.llm_client.get_client
    original_search = agent_module.browser_tool.search_web

    async def fake_search(query, max_results=5):
        return list(FAKE_SEARCH_RESULTS)

    agent_module.llm_client.get_client = lambda *a, **k: client
    agent_module.browser_tool.search_web = fake_search

    def restore():
        agent_module.llm_client.get_client = original_client
        agent_module.browser_tool.search_web = original_search

    return client, restore


def _cleanup_chat(chat):
    storage.delete_chat(chat.id)
    ws = storage.get_workspace_path(chat.workspace_id)
    if ws.exists() and ws.name == chat.workspace_id:
        shutil.rmtree(ws, ignore_errors=True)


def _agent_order(events):
    return [e["agent"] for e in events if e.get("type") == "agent_state"]


async def _run_squad_with_events(chat_id: str, question: str):
    events = []
    async for event in pete_agent.run_squad(chat_id, question):
        events.append(event)
    return events


def test_full_squad_pipeline_happy_path():
    chat = storage.create_chat("Squad Happy Path")
    plan_json = (
        '{"objective": "Find when Purdue was founded", '
        '"needs_research": true, '
        '"research_tasks": ["Search for the Purdue founding year"], '
        '"workspace_tasks": ["Save findings"], '
        '"success_criteria": ["Cite the founding year"]}'
    )
    search_args = '{"query": "Purdue University founded"}'
    write_args = '{"filename": "squad_findings.md", "content": "1869 (https://www.purdue.edu/)"}'
    script = [
        plan_json,
        streamed_tool_call("call_search_1", "search_web", search_args),
        streamed_text("- Purdue was founded in 1869 (https://www.purdue.edu/)."),
        streamed_tool_call("call_list_1", "list_workspace_files", "{}"),
        streamed_tool_call("call_write_1", "write_workspace_file", write_args),
        streamed_text("Read 0 files. Wrote squad_findings.md."),
        '{"verdict": "pass", "confidence": 0.95, "issues": [], "required_fixes": []}',
        streamed_text("Purdue was founded in 1869. Sources: https://www.purdue.edu/"),
    ]
    client, restore = _patch(script)
    try:
        events = asyncio.run(_run_squad_with_events(chat.id, "When was Purdue founded?"))
    finally:
        restore()
        _cleanup_chat(chat)
    assert _agent_order(events) == [
        SQUAD_ORCHESTRATOR, SQUAD_BROWSER, SQUAD_WORKSPACE, SQUAD_CRITIC, SQUAD_CRITIC,
    ]
    plans = [e for e in events if e.get("type") == "squad_plan"]
    assert len(plans) == 1 and plans[0]["plan"]["source"] == "orchestrator"
    tool_calls = [e for e in events if e.get("type") == "tool_call"]
    assert [c["name"] for c in tool_calls] == [
        "search_web", "list_workspace_files", "write_workspace_file"]
    verifications = [e for e in events if e.get("type") == "verification"]
    assert verifications[0]["verification"]["verdict"] == "pass"
    done = [e for e in events if e.get("type") == "done"]
    assert len(done) == 1 and "1869" in done[0]["content"]


def test_full_squad_pipeline_persists_answer_and_artifact():
    chat = storage.create_chat("Squad Persist Check")
    plan_json = (
        '{"objective": "Find when Purdue was founded", '
        '"needs_research": true, '
        '"research_tasks": ["Search for the Purdue founding year"], '
        '"workspace_tasks": ["Save findings"], '
        '"success_criteria": ["Cite the founding year"]}'
    )
    search_args = '{"query": "Purdue University founded"}'
    write_args = '{"filename": "squad_findings.md", "content": "1869 (https://www.purdue.edu/)"}'
    script = [
        plan_json,
        streamed_tool_call("call_search_1", "search_web", search_args),
        streamed_text("- Purdue was founded in 1869 (https://www.purdue.edu/)."),
        streamed_tool_call("call_list_1", "list_workspace_files", "{}"),
        streamed_tool_call("call_write_1", "write_workspace_file", write_args),
        streamed_text("Read 0 files. Wrote squad_findings.md."),
        '{"verdict": "pass", "confidence": 0.9, "issues": [], "required_fixes": []}',
        streamed_text("Purdue was founded in 1869. Sources: https://www.purdue.edu/"),
    ]
    client, restore = _patch(script)
    try:
        events = asyncio.run(_run_squad_with_events(chat.id, "When was Purdue founded?"))
        stored = storage.get_chat(chat.id)
        assert stored is not None
        assert [m.role for m in stored.messages] == ["user", "assistant"]
        final = stored.messages[-1]
        assert final.metadata.get("agent_mode") == "squad"
        assert final.metadata.get("verification", {}).get("verdict") == "pass"
        assert final.metadata.get("artifacts") == ["squad_findings.md"]
        assert "1869" in final.content
        artifact_text = storage.read_workspace_file(chat.workspace_id, "squad_findings.md")
        assert "1869" in artifact_text
    finally:
        restore()
        _cleanup_chat(chat)


def test_squad_skips_browser_when_no_research_needed():
    chat = storage.create_chat("Squad No Research")
    plan_json = (
        '{"objective": "Explain big-O notation", "needs_research": false, '
        '"research_tasks": [], "workspace_tasks": ["Write study notes"], '
        '"success_criteria": ["Clear explanation"]}'
    )
    write_args = '{"filename": "notes.md", "content": "# Big-O\\n\\nO(1), O(n)."}'
    script = [
        plan_json,
        streamed_tool_call("call_list_1", "list_workspace_files", "{}"),
        streamed_tool_call("call_write_1", "write_workspace_file", write_args),
        streamed_text("Wrote notes.md."),
        '{"verdict": "pass", "confidence": 0.8, "issues": [], "required_fixes": []}',
        streamed_text("Big-O describes growth: O(1), O(n)."),
    ]
    client, restore = _patch(script)
    try:
        events = asyncio.run(_run_squad_with_events(chat.id, "Explain big-O notation"))
    finally:
        restore()
        _cleanup_chat(chat)
    tool_calls = [e for e in events if e.get("type") == "tool_call"]
    assert [c["name"] for c in tool_calls] == ["list_workspace_files", "write_workspace_file"]
    done = [e for e in events if e.get("type") == "done"]
    assert len(done) == 1 and "Big-O" in done[0]["content"]

def test_squad_critic_revise_verdict_reaches_frontend():
    chat = storage.create_chat("Squad Critic Revise")
    plan_json = (
        '{"objective": "Find when Purdue was founded", '
        '"needs_research": true, '
        '"research_tasks": ["Search for the Purdue founding year"], '
        '"workspace_tasks": ["Save findings"], '
        '"success_criteria": ["Cite the founding year"]}'
    )
    script = [
        plan_json,
        streamed_tool_call("call_search_1", "search_web", '{"query": "Purdue founded"}'),
        streamed_text("- Purdue was founded in 1869 but I lost the URL."),
        streamed_tool_call("call_list_1", "list_workspace_files", "{}"),
        streamed_tool_call("call_write_1", "write_workspace_file",
                           '{"filename": "squad_findings.md", "content": "founded 1869"}'),
        streamed_text("Wrote squad_findings.md."),
        ('{"verdict": "revise", "confidence": 0.4, '
         '"issues": ["Claim lacks a source URL"], '
         '"required_fixes": ["Add the source URL or mark UNVERIFIED"]}'),
        streamed_text("Purdue was founded in 1869 (UNVERIFIED: source missing)."),
    ]
    client, restore = _patch(script)
    try:
        events = asyncio.run(_run_squad_with_events(chat.id, "When was Purdue founded?"))
    finally:
        restore()
        _cleanup_chat(chat)
    verifications = [e for e in events if e.get("type") == "verification"]
    assert verifications and verifications[0]["verification"]["verdict"] == "revise"
    done = [e for e in events if e.get("type") == "done"]
    assert done and "UNVERIFIED" in done[0]["content"]


# ── Pure unit tests ───────────────────────────────────────────────────────────

def test_extract_json_object_variants():
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('Here is the plan:\n{"a": {"b": "}"}}\nDone.') == {"a": {"b": "}"}}
    assert extract_json_object("no json here") is None
    assert extract_json_object("") is None
    assert extract_json_object("```\n{bad json}\n```") is None


def test_needs_research_heuristic():
    assert pete_agent._needs_research("search the web for Purdue enrollment")
    assert pete_agent._needs_research("what is the latest news about the CFP?")
    assert pete_agent._needs_research("summarize https://www.purdue.edu/about")
    assert not pete_agent._needs_research("explain recursion to me")


def test_fallback_plan_is_self_consistent():
    plan = pete_agent._fallback_squad_plan("What is the latest Purdue enrollment number?", [])
    assert plan["source"] == "fallback"
    assert plan["needs_research"] is True
    assert plan["research_tasks"]
    assert plan["objective"]
    assert plan["success_criteria"]

    quiet = pete_agent._fallback_squad_plan("explain big-O notation", [])
    assert quiet["needs_research"] is False
    assert quiet["research_tasks"] == []


if __name__ == "__main__":
    test_extract_json_object_variants()
    print("[OK] test_extract_json_object_variants")
    test_needs_research_heuristic()
    print("[OK] test_needs_research_heuristic")
    test_fallback_plan_is_self_consistent()
    print("[OK] test_fallback_plan_is_self_consistent")
    test_full_squad_pipeline_happy_path()
    print("[OK] test_full_squad_pipeline_happy_path")
    test_full_squad_pipeline_persists_answer_and_artifact()
    print("[OK] test_full_squad_pipeline_persists_answer_and_artifact")
    test_squad_skips_browser_when_no_research_needed()
    print("[OK] test_squad_skips_browser_when_no_research_needed")
    test_squad_critic_revise_verdict_reaches_frontend()
    print("[OK] test_squad_critic_revise_verdict_reaches_frontend")
    print("\nALL SQUAD PIPELINE TESTS PASSED!")
