"""Regression tests for the text-format tool-call parser.

Exercises the real implementation in app.agent (this file previously held a
throw-away copy of the parser, so it could pass while the app was broken).
"""
import json

from app.agent import extract_tool_calls_from_text, KNOWN_TOOL_NAMES, PETE_TOOLS

CASES = [
    (
        '{"name": "browse_page", "arguments": {"url": "https://www.purdue.edu"}}',
        "browse_page",
        {"url": "https://www.purdue.edu"},
    ),
    (
        'Sure!\n```json\n{"name": "search_web", "arguments": {"query": "boilermakers"}}\n```',
        "search_web",
        {"query": "boilermakers"},
    ),
    (
        '<tool_call>{"tool": "list_workspace_files", "parameters": {}}</tool_call>',
        "list_workspace_files",
        {},
    ),
    (
        'Working on it. {"function": "read_workspace_file", "args": {"filename": "notes.md"}} done.',
        "read_workspace_file",
        {"filename": "notes.md"},
    ),
    (
        "```tool_call\n"
        + json.dumps({
            "name": "write_workspace_file",
            "arguments": json.dumps({"filename": "report.md", "content": "# Report"}),
        })
        + "\n```",
        "write_workspace_file",
        {"filename": "report.md", "content": "# Report"},
    ),
]


def test_extracts_every_supported_format():
    for text, expected_name, expected_args in CASES:
        calls = extract_tool_calls_from_text(text)
        assert calls, f"no tool call parsed from: {text!r}"
        assert calls[0]["name"] == expected_name, f"{text!r} -> {calls}"
        assert calls[0]["arguments"] == expected_args, f"{text!r} -> {calls}"
        assert calls[0]["is_text_fallback"] is True


def test_plain_answer_is_not_a_tool_call():
    assert extract_tool_calls_from_text("Purdue was founded in 1869. Boiler Up!") == []
    assert extract_tool_calls_from_text("") == []


def test_unknown_tool_name_is_ignored():
    assert extract_tool_calls_from_text('{"name": "delete_everything", "arguments": {}}') == []


def test_known_tool_names_covers_every_tool_schema():
    schema_names = {t["function"]["name"] for t in PETE_TOOLS}
    assert schema_names == KNOWN_TOOL_NAMES


if __name__ == "__main__":
    tests = [
        test_extracts_every_supported_format,
        test_plain_answer_is_not_a_tool_call,
        test_unknown_tool_name_is_ignored,
        test_known_tool_names_covers_every_tool_schema,
    ]
    for t in tests:
        t()
        print(f"[OK] {t.__name__}")
    print("\nALL PARSER TESTS PASSED!")
