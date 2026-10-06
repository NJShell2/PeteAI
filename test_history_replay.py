"""Regression tests for chat-history replay and tool-turn persistence.

Covers the fixes for:
  * orphaned assistant tool_calls, which make the *next* request in a chat invalid;
  * legacy Muse-era turns that stored a raw tool-call JSON blob as the visible answer;
  * native tool turns now being persisted as assistant(tool_calls) -> tool(reply),
    which is the only shape the OpenAI tool schema accepts.

These tests never touch the network: the LLM is replaced with a scripted fake.
"""
import asyncio
from typing import Any, Dict, List

from app import agent as agent_module
from app.agent import pete_agent
from app.storage import Message, storage


def make_call(call_id: str, name: str = "search_web", args: str = '{"query": "purdue"}') -> Dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


# --- History rebuild -----------------------------------------------------------------

def test_orphaned_tool_calls_are_dropped():
    history = [
        Message(role="user", content="What year was Purdue founded?"),
        Message(role="assistant", content="", tool_calls=[make_call("call_1")]),
        Message(role="user", content="thanks"),
    ]
    msgs = pete_agent._build_api_messages(history)
    assert [m["role"] for m in msgs] == ["user", "user"], msgs
    assert all("tool_calls" not in m for m in msgs)


def test_paired_tool_calls_survive():
    history = [
        Message(role="user", content="search it"),
        Message(role="assistant", content="", tool_calls=[make_call("call_1")]),
        Message(role="tool", content='[{"title": "Purdue"}]', tool_call_id="call_1", name="search_web"),
        Message(role="assistant", content="Purdue was founded in 1869."),
    ]
    msgs = pete_agent._build_api_messages(history)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
    assert msgs[1]["tool_calls"][0]["id"] == "call_1"
    assert msgs[2]["tool_call_id"] == "call_1"
    assert msgs[2]["name"] == "search_web"
    assert msgs[3]["content"] == "Purdue was founded in 1869."


def test_unanswered_call_is_trimmed_from_a_multi_call_turn():
    history = [
        Message(role="user", content="do two things"),
        Message(role="assistant", content="", tool_calls=[
            make_call("call_1"),
            make_call("call_2", name="browse_page"),
        ]),
        Message(role="tool", content="ok", tool_call_id="call_1", name="search_web"),
    ]
    msgs = pete_agent._build_api_messages(history)
    assert len(msgs[1]["tool_calls"]) == 1
    assert msgs[1]["tool_calls"][0]["id"] == "call_1"
    assert msgs[2]["role"] == "tool"


def test_orphan_tool_reply_is_dropped():
    history = [
        Message(role="user", content="hi"),
        Message(role="tool", content="stray output", tool_call_id="ghost", name="search_web"),
        Message(role="assistant", content="Hello!"),
    ]
    msgs = pete_agent._build_api_messages(history)
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_duplicate_tool_replies_are_collapsed():
    history = [
        Message(role="user", content="q"),
        Message(role="assistant", content="", tool_calls=[make_call("call_1")]),
        Message(role="tool", content="first", tool_call_id="call_1", name="search_web"),
        Message(role="tool", content="second", tool_call_id="call_1", name="search_web"),
    ]
    msgs = pete_agent._build_api_messages(history)
    assert sum(1 for m in msgs if m["role"] == "tool") == 1
    assert msgs[2]["content"] == "first"


def test_legacy_raw_tool_call_blob_is_skipped():
    legacy_blob = ('{"name": "browse_page", "arguments":{"url": '
                   '"https://www.google.com/search?q=buterflies&tbm=isch"}}')
    history = [
        Message(role="user", content="Search google images for butterflies"),
        Message(role="assistant", content=legacy_blob),
        Message(role="user", content="What year was Purdue established?"),
    ]
    msgs = pete_agent._build_api_messages(history)
    assert [m["role"] for m in msgs] == ["user", "user"]
    assert all("browse_page" not in m["content"] for m in msgs)


def test_normal_short_answers_are_kept():
    history = [
        Message(role="user", content="How do tool schemas work?"),
        Message(role="assistant", content="The model returns braces like {this} to describe arguments."),
    ]
    msgs = pete_agent._build_api_messages(history)
    assert [m["role"] for m in msgs] == ["user", "assistant"]


# --- End-to-end (offline) native tool round trip -------------------------------------

class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Function:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _ToolCallDelta:
    def __init__(self, index, call_id=None, name=None, arguments=None):
        self.index = index
        self.id = call_id
        self.function = _Function(name, arguments)


class _Chunk:
    def __init__(self, delta):
        self.choices = [type("_Choice", (), {"delta": delta})()]


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk
        return gen()


class _FakeClient:
    """Turn 1: native write_workspace_file call. Turn 2: plain final answer."""

    def __init__(self):
        self.turns = [
            [
                _Chunk(_Delta(tool_calls=[_ToolCallDelta(0, "call_native", "write_workspace_file", "")])),
                _Chunk(_Delta(tool_calls=[
                    _ToolCallDelta(0, None, None, '{"filename": "notes.md", "content": "# Notes"}')
                ])),
            ],
            [_Chunk(_Delta(content="I saved `notes.md` to your workspace."))],
        ]
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        if not self.turns:
            raise AssertionError(f"fake LLM ran out of scripted turns (kwargs={list(kwargs)})")
        return _Stream(self.turns.pop(0))


async def _collect_events(chat_id: str) -> List[Dict[str, Any]]:
    events = []
    async for event in pete_agent.run(chat_id, "Save my research notes to a file."):
        events.append(event)
    return events


def test_native_tool_round_trip_persists_replayable_history():
    chat = storage.create_chat("Tool Round Trip Test")
    original = agent_module.llm_client.get_client
    agent_module.llm_client.get_client = lambda *a, **k: _FakeClient()
    try:
        events = asyncio.run(_collect_events(chat.id))
        stored = storage.get_chat(chat.id)
    finally:
        agent_module.llm_client.get_client = original
        storage.delete_chat(chat.id)

    # Streamed event contract
    assert any(e.get("type") == "tool_call" and e.get("name") == "write_workspace_file" for e in events), events
    assert any(e.get("type") == "tool_result" for e in events)
    done = [e for e in events if e.get("type") == "done"]
    assert done and done[0]["content"] == "I saved `notes.md` to your workspace."

    # Stored history: user -> assistant(tool_calls) -> tool -> assistant
    roles = [m.role for m in stored.messages]
    assert roles == ["user", "assistant", "tool", "assistant"], roles

    tool_turn, tool_reply, final = stored.messages[1], stored.messages[2], stored.messages[3]
    assert tool_turn.metadata.get("hidden") is True
    assert tool_turn.tool_calls and tool_turn.tool_calls[0]["id"] == "call_native"
    assert tool_reply.tool_call_id == "call_native"
    assert tool_reply.name == "write_workspace_file"
    assert tool_reply.metadata.get("hidden") is True
    assert "notes.md" in tool_reply.content

    # The final answer must not carry tool_calls and must not leak raw JSON
    assert final.tool_calls is None
    assert final.metadata["tools_used"] == ["write_workspace_file"]
    assert "{" not in final.content

    # Replaying the stored history must produce a valid, paired message list
    api_messages = pete_agent._build_api_messages(stored.messages)
    assert [m["role"] for m in api_messages] == ["user", "assistant", "tool", "assistant"], api_messages
    assert api_messages[1]["tool_calls"][0]["id"] == api_messages[2]["tool_call_id"]


def test_second_turn_replays_cleanly():
    """A follow-up turn on the same chat rebuilds history without orphaned tool data."""
    chat = storage.create_chat("Follow Up Test")
    original = agent_module.llm_client.get_client
    agent_module.llm_client.get_client = lambda *a, **k: _FakeClient()
    try:
        asyncio.run(_collect_events(chat.id))
        stored = storage.get_chat(chat.id)
        api_messages = pete_agent._build_api_messages(stored.messages)
    finally:
        agent_module.llm_client.get_client = original
        storage.delete_chat(chat.id)

    # Every tool message must be preceded by a declared call with the same id
    declared = set()
    for msg in api_messages:
        if msg["role"] == "assistant":
            declared.update(tc["id"] for tc in msg.get("tool_calls", []))
        elif msg["role"] == "tool":
            assert msg["tool_call_id"] in declared, api_messages
    assert api_messages[-1]["role"] == "assistant"


if __name__ == "__main__":
    tests = [
        test_orphaned_tool_calls_are_dropped,
        test_paired_tool_calls_survive,
        test_unanswered_call_is_trimmed_from_a_multi_call_turn,
        test_orphan_tool_reply_is_dropped,
        test_duplicate_tool_replies_are_collapsed,
        test_legacy_raw_tool_call_blob_is_skipped,
        test_normal_short_answers_are_kept,
        test_native_tool_round_trip_persists_replayable_history,
        test_second_turn_replays_cleanly,
    ]
    for t in tests:
        t()
        print(f"[OK] {t.__name__}")
    print("\nALL HISTORY REPLAY TESTS PASSED!")
