import json
import re
import asyncio
from typing import AsyncGenerator, Dict, Any, List, Optional

from app.config import load_settings, AppSettings
from app.storage import storage, Message, Chat
from app.browser_tool import browser_tool
from app.console import safe_print
from app import fs_tool
from app.llm_client import llm_client
from app.agent_helpers import current_date_note

# The model is a locally-hosted checkpoint whose training data stops well before
# today, so asked "what time is it" it answers with whatever date it last saw --
# confidently, and wrong. current_date_note() closes that gap on every request.
# See app/agent_helpers.py for why it lives outside settings.system_prompt.
PETE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the internet for current information, technical documentation, Purdue resources, or research papers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search keywords or query."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browse_page",
            "description": "Visit a URL to extract and read its readable content and text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full HTTP/HTTPS URL of the page to visit."
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather forecast for a place. USE THIS for any weather question instead of searching the web or driving the browser: it answers directly from a forecast API with no CAPTCHA, no cookies, and no waiting on pages to load.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "Place name, e.g. 'West Lafayette, Indiana'."
                    },
                    "days_ahead": {
                        "type": "integer",
                        "description": "0 = today, 1 = tomorrow (default), up to 7."
                    }
                },
                "required": ["location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_email",
            "description": "Read the newest emails in the Outlook inbox. Requires the Microsoft connector (Settings).",
            "parameters": {"type": "object", "properties": {
                "count": {"type": "integer", "description": "How many messages, 1-25. Default 10."}
            }}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_email",
            "description": "Search Outlook mail by keywords. Requires the Microsoft connector (Settings).",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Keywords to search for."},
                "count": {"type": "integer", "description": "How many messages, 1-25. Default 10."}
            }, "required": ["query"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send an email from the connected Outlook account. Only when the user explicitly asks to send something.",
            "parameters": {"type": "object", "properties": {
                "to": {"type": "string", "description": "Recipient address(es), comma-separated."},
                "subject": {"type": "string", "description": "Subject line."},
                "body": {"type": "string", "description": "Plain-text body."}
            }, "required": ["to", "subject", "body"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_calendar_events",
            "description": "List upcoming Outlook calendar events. Requires the Microsoft connector (Settings).",
            "parameters": {"type": "object", "properties": {
                "days": {"type": "integer", "description": "How many days ahead, 1-60. Default 7."}
            }}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Create an Outlook calendar event. Times like 2026-10-08T14:00.",
            "parameters": {"type": "object", "properties": {
                "subject": {"type": "string"},
                "start": {"type": "string", "description": "Start, e.g. 2026-10-08T14:00."},
                "end": {"type": "string", "description": "End, e.g. 2026-10-08T15:00."},
                "attendees": {"type": "string", "description": "Email(s), comma-separated. Optional."},
                "location": {"type": "string", "description": "Optional."}
            }, "required": ["subject", "start", "end"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_teams_chats",
            "description": "List recent Microsoft Teams chats (1:1 and group). Requires the Microsoft connector (Settings).",
            "parameters": {"type": "object", "properties": {
                "count": {"type": "integer", "description": "How many chats, 1-30. Default 15."}
            }}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_teams_message",
            "description": "Send a message to a Teams chat. List chats first for the id. Only when the user explicitly asks.",
            "parameters": {"type": "object", "properties": {
                "chat_id": {"type": "string", "description": "The Teams chat id."},
                "message": {"type": "string", "description": "Message text."}
            }, "required": ["chat_id", "message"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Capture a visual screenshot of a webpage and save it directly into the chat workspace for the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The webpage URL to capture."
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional custom filename (e.g. 'purdue_home.png')."
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_workspace_files",
            "description": "List all files stored in the active workspace for this chat.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_workspace_file",
            "description": "Read the contents of a file stored in the active workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "The exact name of the file to read."
                    }
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_workspace_file",
            "description": "Create or overwrite a file in the workspace (code, markdown, scripts, CSV, notes, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "The target filename."
                    },
                    "content": {
                        "type": "string",
                        "description": "The full text or code to write."
                    }
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "extract_links",
            "description": (
                "List the clickable links on a page so you can follow them. Use this to navigate "
                "a site, find a specific section, or discover article URLs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The page to inspect."},
                    "same_domain_only": {
                        "type": "boolean",
                        "description": "Only return links on the same site. Defaults to true; set false to include external links."
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Optional substring filter applied to the href, e.g. 'pdf' or 'admission'."
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "interact_with_page",
            "description": (
                "Drive a webpage like a person: type into a search box, click buttons and links, "
                "press Enter, scroll, and wait. Use it for JS-heavy sites, site search boxes, and "
                "anything browse_page cannot reach because the content loads after a click. "
                "Returns the page text after your actions run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The page to open first."},
                    "actions": {
                        "type": "array",
                        "description": "Ordered list of actions to perform in sequence.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "description": "One of: fill, click, press, scroll, wait."
                                },
                                "selector": {
                                    "type": "string",
                                    "description": "CSS selector, required for fill and click."
                                },
                                "value": {"type": "string", "description": "Text to type, for fill."},
                                "key": {"type": "string", "description": "Key name, for press (e.g. Enter)."},
                                "amount": {"type": "integer", "description": "Pixels to scroll, for scroll."},
                                "ms": {"type": "integer", "description": "Milliseconds to wait, for wait."}
                            },
                            "required": ["action"]
                        }
                    },
                    "wait_selector": {
                        "type": "string",
                        "description": "Optional CSS selector to wait for before reading the page."
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_local_dir",
            "description": (
                "Browse a folder on this computer. Use it to explore real folders outside the "
                "chat workspace (Documents, Desktop, a project directory). Read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute folder path, e.g. C:\\Users\\you\\Documents. Use '~' for your home folder."
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Include files in subfolders. Defaults to false."
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Optional filename filter, e.g. '*.py'."
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_local_file",
            "description": (
                "Read any text file on this computer, including files outside the chat workspace. "
                "Read-only. Use list_local_dir or search_files to find the path first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file."},
                    "start_line": {"type": "integer", "description": "First line to return (1-based)."},
                    "end_line": {"type": "integer", "description": "Last line to return. Omit for the whole file."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Find files by name or grep their contents under a folder anywhere on this computer. "
                "Use this to locate code, notes or documents. Read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "root": {"type": "string", "description": "Folder to search under, e.g. C:\\Users\\you or '~'."},
                    "query": {"type": "string", "description": "Filename or text to look for."},
                    "file_pattern": {"type": "string", "description": "Filename filter, e.g. '*.py'. Defaults to '*'."},
                    "search_content": {"type": "boolean", "description": "Also grep file contents. Defaults to true."}
                },
                "required": ["root", "query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_local_file",
            "description": (
                "Create or overwrite a file on this computer. Only allowed inside the chat workspace "
                "or folders the user has enabled for writing; anywhere else it will be refused."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path of the file to write."},
                    "content": {"type": "string", "description": "The full text to write."},
                    "append": {"type": "boolean", "description": "Append instead of overwriting. Defaults to false."}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_local_dir",
            "description": "Create a folder on this computer, subject to the same write allow-list as write_local_file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Absolute path of the folder to create."}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "move_local_path",
            "description": "Move or rename a file/folder, subject to the write allow-list. Both ends must be allowed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Absolute path to move from."},
                    "destination": {"type": "string", "description": "Absolute path to move to."},
                    "overwrite": {"type": "boolean", "description": "Replace the destination if it exists."}
                },
                "required": ["source", "destination"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_local_path",
            "description": "Delete a file or folder, subject to the write allow-list. Deleting a non-empty folder needs recursive=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to delete."},
                    "recursive": {"type": "boolean", "description": "Required to delete a non-empty folder."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_task",
            "description": (
                "Autonomously drive a real web browser to accomplish a goal, stepping "
                "through pages on its own (searching, clicking, typing, selecting, "
                "scrolling) until it finds the answer. Use this INSTEAD of "
                "browse_page/search_web whenever the answer requires multi-step "
                "navigation or interaction -- clicking through a search engine to read "
                "a result, filling a form, using a date picker, or reading a dynamic "
                "page. The user can watch the browser live while this runs. It returns "
                "a written answer, so report that answer rather than browsing further."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": (
                            "The end goal in plain language, e.g. \"find tomorrow's "
                            "weather for West Lafayette\" or \"find Purdue's fall "
                            "enrollment figure on the public web\"."
                        )
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "Optional step budget (default 25).",
                    },
                },
                "required": ["goal"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_subchat",
            "description": "Branch off a new subchat to investigate a specific sub-topic or heavy research task without cluttering the main conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Descriptive title for the subchat."
                    },
                    "initial_question": {
                        "type": "string",
                        "description": "The prompt or goal to initialize the subchat with."
                    }
                },
                "required": ["title", "initial_question"]
            }
        }
    }
]

KNOWN_TOOL_NAMES = {
    "search_web", "browse_page", "get_weather", "take_screenshot",
    "extract_links", "interact_with_page", "browser_task",
    "list_workspace_files", "read_workspace_file",
    "write_workspace_file", "spawn_subchat",
    "list_local_dir", "read_local_file", "search_files",
    "write_local_file", "create_local_dir", "move_local_path", "delete_local_path",
    "read_email", "search_email", "send_email",
    "list_calendar_events", "create_calendar_event",
    "list_teams_chats", "send_teams_message",
}

# What the chat shows while a tool runs. Plain words, never raw tool names --
# the user asked for output, not a tour of the machinery.
TOOL_STATUS_LABELS = {
    "search_web": "Searching the web",
    "get_weather": "Checking the forecast",
    "browse_page": "Reading that page",
    "browser_task": "Browsing for that",
    "take_screenshot": "Capturing that page",
    "extract_links": "Collecting links",
    "interact_with_page": "Working with that page",
    "list_workspace_files": "Looking through workspace files",
    "read_workspace_file": "Reading that file",
    "write_workspace_file": "Writing that file",
    "spawn_subchat": "Starting a focused subchat",
    "list_local_dir": "Looking through that folder",
    "read_local_file": "Reading that file",
    "search_files": "Searching your files",
    "write_local_file": "Writing that file",
    "create_local_dir": "Creating that folder",
    "move_local_path": "Moving that",
    "delete_local_path": "Deleting that",
    "read_email": "Reading email",
    "search_email": "Searching email",
    "send_email": "Sending email",
    "list_calendar_events": "Checking the calendar",
    "create_calendar_event": "Creating a calendar event",
    "list_teams_chats": "Listing Teams chats",
    "send_teams_message": "Sending a Teams message",
}

# Research tools cost live web round-trips: cap them per turn. Every workspace/file
# tool stays available for the whole turn so the agent can still save its findings.
# browser_task is excluded from the cap on purpose: it already has its own step
# budget, and it is a single tool call that does many navigations internally.
RESEARCH_TOOL_NAMES = {"search_web", "browse_page", "extract_links", "interact_with_page"}
MAX_RESEARCH_CALLS = 3
MAX_ITERATIONS = 8

# ── Pete Squad ────────────────────────────────────────────────────────────────────
# Specialists get a restricted slice of the toolbox so hand-offs stay clean.
SQUAD_ORCHESTRATOR = "🧠 Orchestrator Agent"
SQUAD_BROWSER = "🌐 Browser Specialist"
SQUAD_WORKSPACE = "💻 Workspace Specialist"
SQUAD_CRITIC = "🧐 Critic & Synthesizer"

SQUAD_RESEARCH_TOOL_NAMES = {"search_web", "browse_page", "take_screenshot", "browser_task"}
SQUAD_WORKSPACE_TOOL_NAMES = {"list_workspace_files", "read_workspace_file", "write_workspace_file"}

SQUAD_RESEARCH_TOOLS = [t for t in PETE_TOOLS if t["function"]["name"] in SQUAD_RESEARCH_TOOL_NAMES]
SQUAD_WORKSPACE_TOOLS = [t for t in PETE_TOOLS if t["function"]["name"] in SQUAD_WORKSPACE_TOOL_NAMES]

SQUAD_MAX_RESEARCH_CALLS = 4
SQUAD_MAX_ITERATIONS = 4
SQUAD_FINDINGS_FILE = "squad_findings.md"

SQUAD_RESEARCH_PROMPT = (
    "You are the Pete Squad Browser Specialist for Purdue University research.\n"
    "Gather PRIMARY evidence for the assigned research tasks using your tools.\n"
    "RULES:\n"
    "- Search first; open a page when the snippets are not enough detail.\n"
    "- Never invent a URL, quote, statistic or date. Report only what the tools returned.\n"
    "- Prefer .edu/.gov, journal, and official Purdue sources.\n"
    "- Finish with a compact findings digest: 3-8 markdown bullets, each a claim followed by its source URL in parentheses.\n"
    "- Mark anything you could not confirm as 'UNVERIFIED: ...'."
)

SQUAD_WORKSPACE_PROMPT = (
    "You are the Pete Squad Workspace Specialist for Purdue University research.\n"
    "The chat workspace is the shared sandbox; files from earlier turns are available.\n"
    "RULES:\n"
    "- Inspect the workspace first (list_workspace_files, then read the files that matter).\n"
    "- Write the consolidated findings for this investigation into a markdown file in the workspace.\n"
    f"- Use '{SQUAD_FINDINGS_FILE}' unless an existing workspace file is clearly a better fit.\n"
    "- The file must contain: title, objective, key findings, evidence with source URLs, and open questions.\n"
    "- Base every line on the research digest or on files you actually read; never invent content.\n"
    "- Finish by listing the files you read and the file you wrote."
)

SQUAD_CRITIC_PROMPT = (
    "You are the Pete Squad Critic and Verifier. You audit the evidence dossier before the user sees an answer.\n"
    "Check for: (1) claims unsupported by the research digest or the workspace artifact, "
    "(2) external claims missing source URLs, (3) files the specialists claim to have written that do not exist, "
    "(4) contradictions between the digest and the artifact, (5) the plan's success criteria.\n"
    "Return ONLY a JSON object:\n"
    '{"verdict": "pass" or "revise", "confidence": 0.0-1.0, "issues": ["..."], "required_fixes": ["..."]}\n'
    "Use 'revise' whenever something is unsupported, uncited, contradictory or missing. Never invent problems."
)

SQUAD_SYNTHESIS_PROMPT = (
    "You are Pete Squad, the autonomous research team of Purdue AI. Write the final answer for the user.\n"
    "RULES:\n"
    "- Answer the user's goal directly and authoritatively with markdown structure.\n"
    "- Ground every external fact in the evidence dossier; never invent sources, numbers or quotes.\n"
    "- End with a 'Sources' section listing the URLs the Browser Specialist found (markdown links).\n"
    "- Address every required fix from the Critic; if something could not be verified, say so explicitly.\n"
    "- Mention any workspace artifact that was written and what it contains.\n"
    "- Boiler Up!"
)

def extract_tool_calls_from_text(text: str) -> List[Dict[str, Any]]:
    """Robustly extracts tool calls from raw text emitted by LLMs."""
    text = text.strip()
    calls = []

    # 1. Whole text is valid JSON
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            name = obj.get("name") or obj.get("tool") or obj.get("function")
            args = obj.get("arguments") or obj.get("parameters") or obj.get("args") or {}
            if name in KNOWN_TOOL_NAMES:
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        pass
                calls.append({"id": "text_call_0", "name": name, "arguments": args, "is_text_fallback": True})
                return calls
    except Exception:
        pass

    # 2. Markdown blocks ```json ... ``` or ```tool_call ... ```
    for m in re.finditer(r"```(?:json|tool_call)?\s*(\{[\s\S]*?\})\s*```", text):
        try:
            obj = json.loads(m.group(1))
            name = obj.get("name") or obj.get("tool") or obj.get("function")
            args = obj.get("arguments") or obj.get("parameters") or obj.get("args") or {}
            if name in KNOWN_TOOL_NAMES:
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        pass
                calls.append({"id": f"text_call_{len(calls)}", "name": name, "arguments": args, "is_text_fallback": True})
        except Exception:
            pass

    if calls:
        return calls

    # 3. Tagged <tool_call>...</tool_call>
    for m in re.finditer(r"<tool_call>\s*(\{[\s\S]*?\})\s*</tool_call>", text):
        try:
            obj = json.loads(m.group(1))
            name = obj.get("name") or obj.get("tool") or obj.get("function")
            args = obj.get("arguments") or obj.get("parameters") or obj.get("args") or {}
            if name in KNOWN_TOOL_NAMES:
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        pass
                calls.append({"id": f"text_call_{len(calls)}", "name": name, "arguments": args, "is_text_fallback": True})
        except Exception:
            pass

    if calls:
        return calls

    # 4. Search for balanced brace JSON substring
    start = -1
    brace_depth = 0
    for i, c in enumerate(text):
        if c == '{':
            if brace_depth == 0:
                start = i
            brace_depth += 1
        elif c == '}':
            if brace_depth > 0:
                brace_depth -= 1
                if brace_depth == 0 and start != -1:
                    chunk = text[start:i+1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            name = obj.get("name") or obj.get("tool") or obj.get("function")
                            args = obj.get("arguments") or obj.get("parameters") or obj.get("args") or {}
                            if name in KNOWN_TOOL_NAMES:
                                if isinstance(args, str):
                                    try:
                                        args = json.loads(args)
                                    except Exception:
                                        pass
                                calls.append({"id": f"text_call_{len(calls)}", "name": name, "arguments": args, "is_text_fallback": True})
                    except Exception:
                        pass
                    start = -1

    return calls

def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Extracts the first JSON object from an LLM reply (tolerates prose/markdown fences).

    Used by Pete Squad's Orchestrator and Critic, which must return structured plans
    and verdicts but frequently wrap them in explanation text.
    """
    if not text:
        return None
    body = text.strip()

    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", body)
    if fenced:
        body = fenced.group(1).strip()

    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # Balanced-brace scan that ignores braces inside strings
    start = -1
    depth = 0
    in_string = False
    escaped = False
    for i, c in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    try:
                        obj = json.loads(body[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        pass
                    start = -1
    return None

class PeteAgent:
    def __init__(self):
        pass

    def _is_raw_tool_call_blob(self, content: str) -> bool:
        """Detects legacy assistant turns that leaked a raw tool-call blob as content.

        The earlier Muse/GrokBot build stored messages such as
        `{"name": "browse_page", "arguments": {"url": "..."}}` as the visible answer
        with no tool_calls record and no follow-up. Replaying that history teaches the
        model to leak raw JSON again, so those turns are skipped when rebuilding history.
        """
        text = (content or "").strip()
        if len(text) < 12 or len(text) > 500 or "{" not in text:
            return False
        return bool(extract_tool_calls_from_text(text))

    def _build_api_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        """Rebuilds a valid OpenAI-style history from stored chat messages.

        Guarantees the pairing rules the OpenAI tool schema enforces:
          * an assistant turn keeps only the tool_calls that have a matching role="tool"
            reply later in the transcript;
          * role="tool" messages without a matching, previously declared call are dropped.
        This keeps chats written by older builds (orphaned tool_calls, leaked raw JSON)
        from producing 400s or truncated answers.
        """
        history = [m for m in messages if m.role in ("user", "assistant", "tool")]

        # Index of the first reply for every tool_call_id
        first_reply_index: Dict[str, int] = {}
        for idx, m in enumerate(history):
            if m.role == "tool" and m.tool_call_id:
                first_reply_index.setdefault(m.tool_call_id, idx)

        api_messages: List[Dict[str, Any]] = []
        declared_call_ids: set = set()
        answered_call_ids: set = set()

        for idx, msg in enumerate(history):
            content = msg.content or ""

            if msg.role == "assistant":
                if not msg.tool_calls and self._is_raw_tool_call_blob(content):
                    continue  # legacy leaked JSON — never replay it
                valid_calls = [
                    tc for tc in (msg.tool_calls or [])
                    if tc.get("id") and first_reply_index.get(tc["id"], -1) > idx
                ]
                entry: Dict[str, Any] = {"role": "assistant", "content": content}
                if valid_calls:
                    entry["tool_calls"] = valid_calls
                    declared_call_ids.update(tc["id"] for tc in valid_calls)
                if not content.strip() and not valid_calls:
                    continue
                api_messages.append(entry)
                continue

            if msg.role == "tool":
                call_id = msg.tool_call_id or ""
                if call_id in declared_call_ids and call_id not in answered_call_ids:
                    answered_call_ids.add(call_id)
                    api_messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": msg.name or "tool",
                        "content": content[:12000]
                    })
                continue

            if content.strip():
                api_messages.append({"role": "user", "content": content})

        return api_messages

    async def execute_tool(self, name: str, args: Dict[str, Any], chat: Chat) -> Any:
        ws_id = chat.workspace_id
        if name == "search_web":
            query = args.get("query", "")
            results = await browser_tool.search_web(query)
            return results if results else "No results found."

        elif name == "browse_page":
            url = args.get("url", "")
            return await browser_tool.browse_page(url)

        elif name == "get_weather":
            from app.weather_tool import get_weather as _get_weather
            return await _get_weather(
                args.get("location", ""),
                int(args.get("days_ahead") if args.get("days_ahead") is not None else 1),
            )

        elif name == "take_screenshot":
            url = args.get("url", "")
            filename = args.get("filename")
            return await browser_tool.take_screenshot(url, ws_id, filename)

        elif name == "extract_links":
            return await browser_tool.extract_links(
                args.get("url", ""),
                same_domain_only=bool(args.get("same_domain_only", True)),
                pattern=args.get("pattern", "") or "",
            )

        elif name == "interact_with_page":
            actions = args.get("actions") or []
            # Models sometimes send a JSON string instead of a real list.
            if isinstance(actions, str):
                try:
                    actions = json.loads(actions)
                except Exception:
                    actions = []
            return await browser_tool.interact_with_page(
                args.get("url", ""),
                actions if isinstance(actions, list) else [],
                wait_selector=args.get("wait_selector", "") or "",
            )

        elif name == "browser_task":
            # Drives the persistent session. Progress is echoed as status events so
            # the user can follow along in the live browser view.
            # NOTE: the run is keyed by chat.id, not the workspace id. The UI sends
            # takeover / answer requests with the chat id, and a subchat's
            # workspace_id is its parent's -- keying by workspace silently broke
            # takeovers and ask-card answers in subchats (the agent never parked).
            from app.browser_agent import browser_agent as _browser_agent
            goal = args.get("goal", "")
            max_steps = int(args.get("max_steps") or 0) or 25
            answer = ""
            async for event in _browser_agent.run(goal, chat_id=chat.id, max_steps=max_steps):
                if event.get("type") == "_final":
                    answer = event.get("content", "")
                elif event.get("type") == "error":
                    return {"error": event.get("error", "Browser agent failed."), "url": None}
            return answer or "The browser agent finished without producing an answer."

        elif name in ("read_email", "search_email", "send_email",
                         "list_calendar_events", "create_calendar_event",
                         "list_teams_chats", "send_teams_message"):
            from app import graph_tools as _gt
            from app.config import DATA_DIR
            _settings = load_settings()
            _cid, _tenant = _settings.graph_client_id, _settings.graph_tenant or "common"
            if name == "read_email":
                return await _gt.read_email(DATA_DIR, _cid, _tenant,
                                            int(args.get("count") or 10))
            if name == "search_email":
                return await _gt.search_email(DATA_DIR, _cid, _tenant,
                                              args.get("query", ""),
                                              int(args.get("count") or 10))
            if name == "send_email":
                return await _gt.send_email(DATA_DIR, _cid, _tenant,
                                            args.get("to", ""), args.get("subject", ""),
                                            args.get("body", ""))
            if name == "list_calendar_events":
                return await _gt.list_calendar_events(DATA_DIR, _cid, _tenant,
                                                      int(args.get("days") or 7))
            if name == "create_calendar_event":
                return await _gt.create_calendar_event(
                    DATA_DIR, _cid, _tenant, args.get("subject", ""),
                    args.get("start", ""), args.get("end", ""),
                    args.get("attendees", "") or "", args.get("location", "") or "")
            if name == "list_teams_chats":
                return await _gt.list_teams_chats(DATA_DIR, _cid, _tenant,
                                                  int(args.get("count") or 15))
            if name == "send_teams_message":
                return await _gt.send_teams_message(DATA_DIR, _cid, _tenant,
                                                    args.get("chat_id", ""),
                                                    args.get("message", ""))

        elif name == "list_workspace_files":
            files = storage.list_files(ws_id)
            return [{"name": f.name, "size": f.size, "extension": f.extension} for f in files]

        elif name == "read_workspace_file":
            filename = args.get("filename", "")
            return storage.read_workspace_file(ws_id, filename)

        elif name == "write_workspace_file":
            filename = args.get("filename", "")
            content = args.get("content", "")
            return storage.write_workspace_text_file(ws_id, filename, content)

        elif name == "list_local_dir":
            return fs_tool.list_directory(
                args.get("path", ""),
                recursive=bool(args.get("recursive", False)),
                pattern=args.get("pattern", "") or "",
            )

        elif name == "read_local_file":
            return fs_tool.read_file(
                args.get("path", ""),
                start_line=int(args.get("start_line") or 1),
                end_line=int(args.get("end_line") or 0),
            )

        elif name == "search_files":
            return fs_tool.search_files(
                args.get("root", ""),
                args.get("query", ""),
                file_pattern=args.get("file_pattern") or "*",
                search_content=bool(args.get("search_content", True)),
            )

        elif name == "write_local_file":
            return fs_tool.write_file(
                args.get("path", ""),
                args.get("content", ""),
                workspace_id=ws_id,
                append=bool(args.get("append", False)),
            )

        elif name == "create_local_dir":
            return fs_tool.make_directory(args.get("path", ""), workspace_id=ws_id)

        elif name == "move_local_path":
            return fs_tool.move_path(
                args.get("source", ""),
                args.get("destination", ""),
                workspace_id=ws_id,
                overwrite=bool(args.get("overwrite", False)),
            )

        elif name == "delete_local_path":
            return fs_tool.delete_path(
                args.get("path", ""),
                workspace_id=ws_id,
                recursive=bool(args.get("recursive", False)),
            )

        elif name == "spawn_subchat":
            title = args.get("title", "Subchat")
            initial_question = args.get("initial_question", "")
            subchat = storage.create_chat(
                title=title,
                parent_id=chat.id,
                workspace_id=chat.workspace_id,
                model=chat.model
            )
            user_msg = Message(role="user", content=initial_question)
            storage.add_message(subchat.id, user_msg)
            return {
                "subchat_id": subchat.id,
                "title": subchat.title,
                "message": f"Subchat '{subchat.title}' successfully created (ID: {subchat.id})."
            }

        return f"Unknown tool: {name}"

    async def synthesize_subchat(self, subchat_id: str, model_override: Optional[str] = None) -> Optional[Message]:
        """Synthesizes findings of a subchat into an executive summary and appends to parent chat."""
        subchat = storage.get_chat(subchat_id)
        if not subchat or not subchat.parent_id:
            return None

        parent_chat = storage.get_chat(subchat.parent_id)
        if not parent_chat:
            return None

        settings = load_settings()
        model_name = model_override or subchat.model or settings.default_model

        transcript = []
        for m in subchat.messages:
            if m.role in ["user", "assistant"]:
                transcript.append(f"{m.role.upper()}: {m.content}")
        transcript_text = "\n\n".join(transcript)

        synthesis_prompt = [
            {"role": "system", "content": "You are Pete AI. Summarize the following subchat investigation into a clear executive summary for the primary research thread. Highlight key findings, solutions, and any created files. Keep it concise, actionable, and structured."},
            {"role": "user", "content": f"Subchat Title: '{subchat.title}'\n\nFull Transcript:\n{transcript_text}\n\nProvide the synthesis summary now."}
        ]

        client = llm_client.get_client()
        try:
            resp = await client.chat.completions.create(
                model=model_name,
                messages=synthesis_prompt,
                temperature=0.5
            )
            summary_text = resp.choices[0].message.content
        except Exception as e:
            summary_text = f"Subchat '{subchat.title}' completed with {len(subchat.messages)} messages."

        formatted_content = f"### 🌿 Subchat Synthesis: {subchat.title}\n\n{summary_text}\n\n*(Branched from Subchat: `{subchat.id}`)*"

        msg = Message(
            role="assistant",
            content=formatted_content,
            metadata={"type": "subchat_synthesis", "subchat_id": subchat.id, "subchat_title": subchat.title}
        )
        storage.add_message(parent_chat.id, msg)
        return msg

    async def run(self, chat_id: str, user_content: str, model_override: Optional[str] = None, agent_mode: str = "solo") -> AsyncGenerator[Dict[str, Any], None]:
        # Support "squad" or legacy "grokbot"
        if agent_mode in ["squad", "grokbot"]:
            async for ev in self.run_squad(chat_id, user_content, model_override):
                yield ev
            return

        chat = storage.get_chat(chat_id)
        if not chat:
            yield {"type": "error", "error": f"Chat {chat_id} not found"}
            return

        settings = load_settings()
        model_name = model_override or chat.model or settings.default_model

        # Save user message
        user_message = Message(role="user", content=user_content)
        chat = storage.add_message(chat_id, user_message)

        # Build workspace summary context
        ws_files = storage.list_files(chat.workspace_id)
        file_summary = ""
        if ws_files:
            file_names = ", ".join([f.name for f in ws_files[:10]])
            file_summary = f"\nFiles in active workspace: {file_names}"

        system_instruction = (
            f"{settings.system_prompt}\n"
            f"{current_date_note()}"
            f"Active Workspace ID: {chat.workspace_id}{file_summary}\n"
            f"If this is a subchat, focus specifically on the subtask and prepare clear findings."
        )

        api_messages = [{"role": "system", "content": system_instruction}]
        api_messages.extend(self._build_api_messages(chat.messages))

        client = llm_client.get_client()

        max_iterations = MAX_ITERATIONS
        iteration = 0
        final_assistant_content = ""
        executed_tool_calls_record = []

        yield {"type": "status", "data": "Consulting Purdue GenAI Studio..."}

        while iteration < max_iterations:
            iteration += 1
            # Never disable the whole toolbox: research tools are the only expensive
            # ones, and they are capped per turn. Build/write tools stay enabled so the
            # agent can still save a report after its research pass.
            research_used = sum(
                1 for tc in executed_tool_calls_record
                if tc["function"]["name"] in RESEARCH_TOOL_NAMES
            )
            if research_used >= MAX_RESEARCH_CALLS:
                current_tools = [
                    t for t in PETE_TOOLS
                    if t["function"]["name"] not in RESEARCH_TOOL_NAMES
                ]
            else:
                current_tools = PETE_TOOLS

            try:
                if current_tools:
                    response = await client.chat.completions.create(
                        model=model_name,
                        messages=api_messages,
                        tools=current_tools,
                        tool_choice="auto",
                        temperature=settings.temperature,
                        stream=True
                    )
                else:
                    response = await client.chat.completions.create(
                        model=model_name,
                        messages=api_messages,
                        temperature=settings.temperature,
                        stream=True
                    )
            except Exception as e:
                err_str = str(e)
                if "tools" in err_str.lower() or "400" in err_str or "unsupported" in err_str:
                    # Model doesn't support tool_calls parameter — fall back to plain completion
                    yield {"type": "status", "data": "Model using standard completion (no native tools)..."}
                    current_tools = None
                    try:
                        response = await client.chat.completions.create(
                            model=model_name,
                            messages=api_messages,
                            temperature=settings.temperature,
                            stream=True
                        )
                    except Exception as ex2:
                        yield {"type": "error", "error": f"LLM error: {str(ex2)}"}
                        return
                else:
                    yield {"type": "error", "error": f"LLM error: {err_str}"}
                    return

            # ─── BUFFER SILENTLY ────────────────────────────────────────────────────
            # We NEVER stream content to the frontend during collection.
            # After the full response arrives we decide: real content → forward it;
            # text-format tool call → suppress the raw JSON and route through the
            # tool execution path instead.
            # ────────────────────────────────────────────────────────────────────────
            assistant_content_chunk = ""
            current_tool_calls = {}

            async for chunk in response:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                # Accumulate text content silently (DO NOT yield yet)
                if delta.content:
                    assistant_content_chunk += delta.content

                # Native structured tool_calls from the API
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in current_tool_calls:
                            current_tool_calls[idx] = {
                                "id": tc.id or f"call_{idx}",
                                "type": "function",
                                "function": {
                                    "name": tc.function.name or "" if tc.function else "",
                                    "arguments": tc.function.arguments or "" if tc.function else ""
                                }
                            }
                        else:
                            if tc.function:
                                if tc.function.name:
                                    current_tool_calls[idx]["function"]["name"] += tc.function.name
                                if tc.function.arguments:
                                    current_tool_calls[idx]["function"]["arguments"] += tc.function.arguments

            # ─── DECIDE: tool call or real content ──────────────────────────────────
            tool_calls_list = list(current_tool_calls.values())
            is_text_tool_call = False

            if not tool_calls_list and assistant_content_chunk.strip():
                # Check whether the model smuggled a tool call inside plain text
                extracted = extract_tool_calls_from_text(assistant_content_chunk)
                if extracted:
                    tool_calls_list = [
                        {
                            "id": ext["id"],
                            "type": "function",
                            "function": {
                                "name": ext["name"],
                                "arguments": (
                                    json.dumps(ext["arguments"])
                                    if isinstance(ext["arguments"], dict)
                                    else str(ext["arguments"])
                                )
                            }
                        }
                        for ext in extracted
                    ]
                    is_text_tool_call = True
                    # Raw JSON was already buffered and never sent — nothing to clear
                else:
                    # Confirmed: this is real answer content, not a tool call.
                    # Stream the full buffered text to the frontend now.
                    yield {"type": "content", "delta": assistant_content_chunk}

            elif not tool_calls_list:
                # Empty response (shouldn't happen, but be safe)
                pass
            else:
                # Native tool_calls branch — content (if any) was preamble text
                if assistant_content_chunk.strip():
                    yield {"type": "content", "delta": assistant_content_chunk}

            if not tool_calls_list:
                final_assistant_content += assistant_content_chunk
                break

            executed_tool_calls_record.extend(tool_calls_list)

            # Record assistant turn in the message history
            if not is_text_tool_call:
                api_messages.append({
                    "role": "assistant",
                    "content": assistant_content_chunk,
                    "tool_calls": tool_calls_list
                })
                # Persist the assistant tool-call turn BEFORE its results so a replayed
                # transcript keeps the assistant -> tool(s) -> assistant order the API
                # requires. Hidden: it is an internal turn; the final answer carries the
                # tool badges via metadata.tools_used.
                storage.add_message(chat.id, Message(
                    role="assistant",
                    content=assistant_content_chunk,
                    tool_calls=tool_calls_list,
                    metadata={"agent_mode": "solo", "type": "tool_turn", "hidden": True}
                ))
            else:
                api_messages.append({
                    "role": "assistant",
                    "content": assistant_content_chunk
                })

            for tc in tool_calls_list:
                func_name = tc["function"]["name"]
                raw_args = tc["function"]["arguments"]
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    args = {}

                yield {
                    "type": "tool_call",
                    "id": tc["id"],
                    "name": func_name,
                    "arguments": args
                }

                yield {"type": "status", "data": f"{TOOL_STATUS_LABELS.get(func_name, func_name)}..."}
                tool_output = await self.execute_tool(func_name, args, chat)

                if isinstance(tool_output, (dict, list)):
                    tool_output_str = json.dumps(tool_output, indent=2, ensure_ascii=False)
                else:
                    tool_output_str = str(tool_output)

                yield {
                    "type": "tool_result",
                    "id": tc["id"],
                    "name": func_name,
                    "result": tool_output
                }

                if not is_text_tool_call:
                    # Persist the tool output so the next turn replays a valid
                    # assistant(tool_calls) -> tool(reply) pair.
                    storage.add_message(chat.id, Message(
                        role="tool",
                        content=tool_output_str[:12000],
                        tool_call_id=tc["id"],
                        name=func_name,
                        metadata={"hidden": True}
                    ))
                    api_messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": func_name,
                        "content": tool_output_str[:12000]
                    })
                else:
                    # Models that emit text-format tool calls: feed result back as user message
                    api_messages.append({
                        "role": "user",
                        "content": (
                            f"[Tool Output for {func_name}]:\n{tool_output_str[:12000]}\n\n"
                            "Based on the tool output above, please provide your complete, "
                            "helpful answer to the user. Do NOT output another tool call — "
                            "just answer directly."
                        )
                    })

            yield {"type": "status", "data": "Synthesizing answer..."}

        assistant_message = Message(
            role="assistant",
            content=final_assistant_content or assistant_content_chunk,
            # tool_calls are recorded on the persisted tool turn instead; repeating them
            # here would save an orphaned assistant(tool_calls) turn with no replies.
            tool_calls=None,
            metadata={
                "agent_mode": "solo",
                "tools_used": [tc["function"]["name"] for tc in executed_tool_calls_record]
            }
        )
        storage.add_message(chat_id, assistant_message)

        yield {
            "type": "done",
            "message_id": assistant_message.id,
            "content": assistant_message.content
        }

    async def run_squad(self, chat_id: str, user_content: str, model_override: Optional[str] = None) -> AsyncGenerator[Dict[str, Any], None]:
        """Pete Squad Multi-Agent Execution Pipeline: Orchestrator -> Browser Specialist -> Code Specialist -> Critic."""
        chat = storage.get_chat(chat_id)
        if not chat:
            yield {"type": "error", "error": f"Chat {chat_id} not found"}
            return

        settings = load_settings()
        model_name = model_override or chat.model or settings.default_model
        client = llm_client.get_client()

        user_message = Message(role="user", content=user_content)
        chat = storage.add_message(chat_id, user_message)

        async for event in self._squad_pipeline(chat, user_content, model_name, settings):
            yield event

    # ── Pete Squad pipeline ───────────────────────────────────────────────────────

    async def _squad_pipeline(
        self,
        chat: Chat,
        user_content: str,
        model_name: str,
        settings: AppSettings,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Orchestrator -> Browser Specialist -> Workspace Specialist -> Critic & Synthesizer.

        Every specialist runs its own bounded tool loop and hands a structured payload
        to the next one. Only the final answer is persisted to the chat.
        """
        client = llm_client.get_client()
        workspace_before = [f.name for f in storage.list_files(chat.workspace_id)]

        # 1 ─ Orchestrator Agent: decompose the goal into specialist tasks
        yield {"type": "agent_state", "agent": SQUAD_ORCHESTRATOR, "action": "Decomposing the goal into specialist tasks..."}
        plan = await self._squad_plan(client, model_name, user_content, workspace_before, settings.temperature)
        yield {"type": "squad_plan", "plan": plan}
        yield {"type": "agent_log", "agent": SQUAD_ORCHESTRATOR, "text": self._format_squad_plan(plan)}
        yield {"type": "agent_done", "agent": SQUAD_ORCHESTRATOR, "status": "ok", "summary": plan["objective"][:240]}

        # 2 ─ Browser Specialist: live research with the headless browser
        findings, research_tools = "", []
        if plan["research_tasks"]:
            yield {"type": "agent_state", "agent": SQUAD_BROWSER, "action": f"Researching {len(plan['research_tasks'])} task(s) with the headless browser..."}
            async for event in self._squad_research_step(chat, plan, user_content, model_name, settings, workspace_before):
                if event.get("type") == "_step":
                    findings = event["findings"]
                    research_tools = event["tools"]
                    continue
                yield event
            first_line = findings.strip().splitlines()[0] if findings.strip() else "No usable sources were retrieved."
            yield {
                "type": "agent_done",
                "agent": SQUAD_BROWSER,
                "status": "ok" if findings.strip() else "warn",
                "summary": first_line[:240],
            }
        else:
            yield {"type": "agent_log", "agent": SQUAD_BROWSER, "text": "Orchestrator found no live-research task: browser pass skipped."}
            yield {"type": "agent_done", "agent": SQUAD_BROWSER, "status": "ok", "summary": "No external research required."}

        # 3 ─ Workspace Specialist: inspect files and write the findings artifact
        yield {"type": "agent_state", "agent": SQUAD_WORKSPACE, "action": "Inspecting the workspace and writing consolidated findings..."}
        artifact, artifact_content, workspace_tools = None, "", []
        async for event in self._squad_workspace_step(chat, plan, user_content, findings, model_name, settings, workspace_before):
            if event.get("type") == "_step":
                artifact = event["artifact"]
                artifact_content = event["content"]
                workspace_tools = event["tools"]
                continue
            yield event
        yield {
            "type": "agent_done",
            "agent": SQUAD_WORKSPACE,
            "status": "ok" if artifact else "warn",
            "summary": f"Saved `{artifact}` to the workspace." if artifact else "No artifact was written.",
        }

        # 4 ─ Critic & Synthesizer: verify the evidence, then write the answer
        yield {"type": "agent_state", "agent": SQUAD_CRITIC, "action": "Verifying evidence, citations and file claims..."}
        verification = await self._squad_verify(client, model_name, user_content, plan, findings, artifact, artifact_content)
        yield {"type": "verification", "verification": verification}
        yield {"type": "agent_log", "agent": SQUAD_CRITIC, "text": self._format_verification(verification)}
        yield {"type": "agent_state", "agent": SQUAD_CRITIC, "action": "Compiling the verified final answer..."}

        answer = ""
        async for event in self._squad_final_step(
            chat, user_content, plan, findings, artifact, artifact_content,
            verification, research_tools + workspace_tools, model_name, settings,
        ):
            if event.get("type") == "_final":
                answer = event["content"]
                continue
            yield event

        verdict = verification.get("verdict", "unverified")
        issue_count = len(verification.get("issues") or [])
        yield {
            "type": "agent_done",
            "agent": SQUAD_CRITIC,
            "status": "ok" if verdict == "pass" else "warn",
            "summary": f"Verifier verdict: {verdict} ({issue_count} issue(s) flagged).",
        }

        all_tools = list(dict.fromkeys(research_tools + workspace_tools))
        assistant_message = Message(
            role="assistant",
            content=answer or "Pete Squad finished without producing an answer.",
            metadata={
                "agent_mode": "squad",
                "plan": plan,
                "verification": verification,
                "artifacts": [artifact] if artifact else [],
                "tools_used": all_tools,
                "agents": [SQUAD_ORCHESTRATOR, SQUAD_BROWSER, SQUAD_WORKSPACE, SQUAD_CRITIC],
            },
        )
        storage.add_message(chat.id, assistant_message)
        yield {"type": "done", "message_id": assistant_message.id, "content": assistant_message.content}

    async def _squad_collect(self, response) -> "tuple[str, List[Dict[str, Any]], bool]":
        """Consumes a streamed completion into (text, tool_calls, came_from_text).

        Squad specialists may run on models that do not support the native `tools`
        parameter, so raw JSON tool calls emitted as plain text are parsed as a fallback.
        """
        content_chunk = ""
        pending: Dict[int, Dict[str, Any]] = {}
        async for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue
            if delta.content:
                content_chunk += delta.content
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in pending:
                        pending[idx] = {
                            "id": tc.id or f"squad_call_{idx}",
                            "type": "function",
                            "function": {
                                "name": (tc.function.name or "") if tc.function else "",
                                "arguments": (tc.function.arguments or "") if tc.function else "",
                            },
                        }
                    elif tc.function:
                        if tc.function.name:
                            pending[idx]["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            pending[idx]["function"]["arguments"] += tc.function.arguments

        calls = list(pending.values())
        if calls or not content_chunk.strip():
            return content_chunk, calls, False

        extracted = extract_tool_calls_from_text(content_chunk)
        if not extracted:
            return content_chunk, [], False

        from_text = [
            {
                "id": ext["id"],
                "type": "function",
                "function": {
                    "name": ext["name"],
                    "arguments": (
                        json.dumps(ext["arguments"])
                        if isinstance(ext["arguments"], dict)
                        else str(ext["arguments"])
                    ),
                },
            }
            for ext in extracted
        ]
        return content_chunk, from_text, True

    async def _squad_specialist(
        self,
        chat: Chat,
        *,
        agent_label: str,
        system_prompt: str,
        task_prompt: str,
        model_name: str,
        temperature: float,
        tools: List[Dict[str, Any]],
        max_iterations: int,
        research_cap: Optional[int] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Runs one Pete Squad specialist as a bounded tool loop.

        Emits the same UI events as the solo agent, tagged with `agent` so the frontend
        can nest them inside that specialist's card, then a terminal `_final` event with
        the specialist's written summary. Nothing is persisted: squad internals must stay
        out of the chat transcript.
        """
        client = llm_client.get_client()
        api_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task_prompt},
        ]
        native_tools = bool(tools)
        used_tools: List[str] = []
        research_used = 0
        final_text = ""

        for iteration in range(1, max_iterations + 1):
            capped = research_cap is not None and research_used >= research_cap
            available = [t for t in tools if t["function"]["name"] not in RESEARCH_TOOL_NAMES] if capped else tools

            try:
                kwargs: Dict[str, Any] = {
                    "model": model_name,
                    "messages": api_messages,
                    "temperature": temperature,
                    "stream": True,
                }
                if native_tools and available:
                    kwargs.update({"tools": available, "tool_choice": "auto"})
                response = await client.chat.completions.create(**kwargs)
            except Exception as e:
                err = str(e)
                if native_tools and ("tool" in err.lower() or "400" in err or "unsupported" in err.lower()):
                    native_tools = False  # model rejects the tools parameter: use text mode
                    continue
                yield {"type": "error", "agent": agent_label, "error": f"{agent_label} stopped: {err}"}
                yield {"type": "_final", "content": final_text, "tools_used": used_tools}
                return

            content_chunk, calls, from_text = await self._squad_collect(response)
            if from_text:
                for i, call in enumerate(calls):
                    call["id"] = f"{agent_label}_text_{iteration}_{i}"

            if not calls:
                yield {"type": "_final", "content": content_chunk.strip(), "tools_used": used_tools}
                return

            api_messages.append(
                {"role": "assistant", "content": content_chunk}
                if from_text
                else {"role": "assistant", "content": content_chunk, "tool_calls": calls}
            )

            for call in calls:
                func_name = call["function"]["name"]
                raw_args = call["function"]["arguments"]
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}

                yield {"type": "tool_call", "agent": agent_label, "id": call["id"], "name": func_name, "arguments": args}
                tool_output = await self.execute_tool(func_name, args, chat)
                used_tools.append(func_name)
                if func_name in RESEARCH_TOOL_NAMES:
                    research_used += 1
                yield {"type": "tool_result", "agent": agent_label, "id": call["id"], "name": func_name, "result": tool_output}

                output_str = (
                    json.dumps(tool_output, indent=2, ensure_ascii=False)
                    if isinstance(tool_output, (dict, list))
                    else str(tool_output)
                )
                if from_text:
                    api_messages.append({
                        "role": "user",
                        "content": (
                            f"[Tool Output for {func_name}]:\n{output_str[:12000]}\n\n"
                            "Use this output to continue the task. When you have enough evidence, "
                            "reply with your final summary and stop calling tools."
                        ),
                    })
                else:
                    api_messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": func_name,
                        "content": output_str[:12000],
                    })

        # Tool budget exhausted: force a written summary of whatever was gathered.
        yield {"type": "status", "agent": agent_label, "data": f"{agent_label}: wrapping up findings..."}
        try:
            api_messages.append({
                "role": "user",
                "content": "Stop using tools now and summarize your findings so far in a compact digest.",
            })
            response = await client.chat.completions.create(
                model=model_name, messages=api_messages, temperature=temperature, stream=True
            )
            async for chunk in response:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    final_text += delta.content
        except Exception:
            pass
        yield {"type": "_final", "content": final_text.strip(), "tools_used": used_tools}

    # ── Pete Squad steps ──────────────────────────────────────────────────────────

    async def _squad_research_step(
        self, chat: Chat, plan: Dict[str, Any], user_content: str,
        model_name: str, settings: AppSettings, workspace_before: List[str],
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Browser Specialist pass: live research driven by its own tool loop."""
        async for event in self._squad_specialist(
            chat,
            agent_label=SQUAD_BROWSER,
            system_prompt=SQUAD_RESEARCH_PROMPT,
            task_prompt=self._squad_research_task(user_content, plan, workspace_before),
            model_name=model_name,
            temperature=settings.temperature,
            tools=SQUAD_RESEARCH_TOOLS,
            max_iterations=SQUAD_MAX_ITERATIONS,
            research_cap=SQUAD_MAX_RESEARCH_CALLS,
        ):
            if event.get("type") == "_final":
                yield {"type": "_step", "findings": event["content"], "tools": event["tools_used"]}
                continue
            if event.get("type") == "status":
                # Specialist progress belongs in its card, not in the answer area.
                yield {"type": "agent_log", "agent": event.get("agent") or SQUAD_BROWSER, "text": event.get("data", "")}
                continue
            yield event

    async def _squad_workspace_step(
        self, chat: Chat, plan: Dict[str, Any], user_content: str, findings: str,
        model_name: str, settings: AppSettings, workspace_before: List[str],
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Workspace Specialist pass: inspect files, then write the findings artifact."""
        used_tools: List[str] = []
        async for event in self._squad_specialist(
            chat,
            agent_label=SQUAD_WORKSPACE,
            system_prompt=SQUAD_WORKSPACE_PROMPT,
            task_prompt=self._squad_workspace_task(user_content, plan, findings, workspace_before),
            model_name=model_name,
            temperature=settings.temperature,
            tools=SQUAD_WORKSPACE_TOOLS,
            max_iterations=SQUAD_MAX_ITERATIONS,
        ):
            if event.get("type") == "_final":
                used_tools = event["tools_used"]
                continue
            if event.get("type") == "status":
                # Specialist progress belongs in its card, not in the answer area.
                yield {"type": "agent_log", "agent": event.get("agent") or SQUAD_WORKSPACE, "text": event.get("data", "")}
                continue
            yield event

        artifact = self._squad_ensure_artifact(chat, workspace_before, findings, plan, user_content)
        content = storage.read_workspace_file(chat.workspace_id, artifact) if artifact else ""
        yield {
            "type": "agent_log",
            "agent": SQUAD_WORKSPACE,
            "text": f"Workspace artifact ready: {artifact}" if artifact else "No workspace artifact was produced.",
        }
        yield {"type": "_step", "artifact": artifact, "content": content[:8000], "tools": used_tools}

    def _squad_ensure_artifact(
        self, chat: Chat, workspace_before: List[str], findings: str,
        plan: Dict[str, Any], user_content: str,
    ) -> Optional[str]:
        """Returns the file the Workspace Specialist wrote, or writes a digest ourselves.

        The squad always leaves a durable artifact behind, even when the model answers
        without ever calling write_workspace_file.
        """
        after = [f.name for f in storage.list_files(chat.workspace_id)]
        new_files = sorted(n for n in after if n not in workspace_before)
        if new_files:
            return new_files[0]
        if not findings.strip():
            return None
        criteria = "\n".join(f"- {c}" for c in (plan.get("success_criteria") or [])) or "- (none stated)"
        digest = (
            f"# Pete Squad Findings\n\n"
            f"**Objective:** {plan.get('objective') or user_content}\n\n"
            f"*Compiled automatically because the Workspace Specialist answered without writing a file.*\n\n"
            f"## Browser Findings\n\n{findings}\n\n"
            f"## Success Criteria\n\n{criteria}\n"
        )
        storage.write_workspace_text_file(chat.workspace_id, SQUAD_FINDINGS_FILE, digest)
        return SQUAD_FINDINGS_FILE

    # ── Orchestrator helpers ──────────────────────────────────────────────────────

    def _needs_research(self, text: str) -> bool:
        """Heuristic guard so an empty orchestrator plan cannot skip obvious research."""
        lowered = (text or "").lower()
        if "http://" in lowered or "https://" in lowered or "www." in lowered:
            return True
        markers = (
            "search", "google", "look up", "lookup", "find out", "latest", "current", "today",
            "news", "online", "web", "browse", "cite", "citation", "source", "paper",
            "documentation", "release", "statistic", "data", "compare", "price", "weather", "who won",
        )
        return any(m in lowered for m in markers)

    def _fallback_squad_plan(self, user_content: str, workspace_before: List[str]) -> Dict[str, Any]:
        """Deterministic plan used when the Orchestrator returns unusable JSON."""
        needs_research = self._needs_research(user_content)
        return {
            "objective": user_content.strip(),
            "needs_research": needs_research,
            "research_tasks": [user_content.strip()] if needs_research else [],
            "workspace_tasks": [f"Save the consolidated findings to {SQUAD_FINDINGS_FILE}."],
            "success_criteria": [
                "Answer the user's goal directly and completely.",
                "Cite a source for every external claim.",
                "Leave the consolidated findings in the workspace.",
            ],
            "source": "fallback",
        }

    async def _squad_plan(
        self, client, model_name: str, user_content: str,
        workspace_before: List[str], temperature: float,
    ) -> Dict[str, Any]:
        """Orchestrator Agent: decomposes the goal into specialist tasks (JSON plan)."""
        prompt = [
            {"role": "system", "content": (
                "You are the Pete Squad Chief Orchestrator for Purdue University research.\n"
                "Decide what the specialist agents must do, then return ONLY a JSON object:\n"
                '{"objective": "...", "needs_research": true, "research_tasks": ["..."], '
                '"workspace_tasks": ["..."], "success_criteria": ["..."]}\n'
                "Rules: set needs_research=true whenever the goal needs current, external or citable "
                "information; otherwise leave research_tasks empty. workspace_tasks are file actions "
                "(inspect, consolidate, write). Keep each list to 1-3 concrete items."
            )},
            {"role": "user", "content": (
                f"User goal: {user_content}\n\n"
                f"Workspace files already available: "
                f"{', '.join(workspace_before) if workspace_before else '(none)'}"
            )},
        ]
        try:
            response = await client.chat.completions.create(
                model=model_name, messages=prompt, temperature=min(temperature, 0.3), max_tokens=500
            )
            parsed = extract_json_object(response.choices[0].message.content or "")
        except Exception as e:
            safe_print(f"Squad orchestrator failed, using fallback plan: {e}")
            parsed = None

        plan = self._fallback_squad_plan(user_content, workspace_before)
        if not parsed:
            return plan

        objective = str(parsed.get("objective") or "").strip()
        if objective:
            plan["objective"] = objective

        research_tasks = [str(t).strip() for t in (parsed.get("research_tasks") or []) if str(t).strip()]
        if not research_tasks and (parsed.get("needs_research") or self._needs_research(user_content)):
            research_tasks = [user_content.strip()]
        plan["needs_research"] = bool(research_tasks)
        plan["research_tasks"] = research_tasks[:3]

        workspace_tasks = [str(t).strip() for t in (parsed.get("workspace_tasks") or []) if str(t).strip()]
        if workspace_tasks:
            plan["workspace_tasks"] = workspace_tasks[:3]

        criteria = [str(c).strip() for c in (parsed.get("success_criteria") or []) if str(c).strip()]
        if criteria:
            plan["success_criteria"] = criteria[:5]

        plan["source"] = "orchestrator"
        return plan

    def _format_squad_plan(self, plan: Dict[str, Any]) -> str:
        research = plan.get("research_tasks") or []
        workspace = plan.get("workspace_tasks") or []
        criteria = plan.get("success_criteria") or []
        lines = [
            f"Objective: {plan.get('objective')}",
            "Research tasks: " + ("; ".join(research) if research else "none (live research skipped)"),
            "Workspace tasks: " + ("; ".join(workspace) if workspace else "consolidate findings"),
        ]
        if criteria:
            lines.append("Success criteria: " + "; ".join(criteria))
        return "\n".join(lines)

    # ── Task briefs handed to each specialist ─────────────────────────────────────

    def _squad_research_task(self, user_content: str, plan: Dict[str, Any], workspace_before: List[str]) -> str:
        tasks = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(plan.get("research_tasks") or []))
        criteria = "\n".join(f"- {c}" for c in (plan.get("success_criteria") or [])) or "- Answer the goal accurately."
        workspace = (
            f"Files already in the workspace (you may ignore them for now): {', '.join(workspace_before)}"
            if workspace_before
            else "The workspace is currently empty."
        )
        return (
            f"User goal: {user_content}\n\n"
            f"Objective: {plan.get('objective')}\n\n"
            f"Research tasks:\n{tasks}\n\n"
            f"Success criteria:\n{criteria}\n\n"
            f"{workspace}\n\n"
            "Gather the evidence now, then reply with your findings digest."
        )

    def _squad_workspace_task(
        self, user_content: str, plan: Dict[str, Any], findings: str, workspace_before: List[str],
    ) -> str:
        tasks = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(plan.get("workspace_tasks") or []))
        if not tasks:
            tasks = f"1. Consolidate the findings into {SQUAD_FINDINGS_FILE}."
        return (
            f"User goal: {user_content}\n\n"
            f"Objective: {plan.get('objective')}\n\n"
            f"Workspace tasks:\n{tasks}\n\n"
            f"Files already in the workspace: "
            f"{', '.join(workspace_before) if workspace_before else '(none)'}\n\n"
            f"Browser findings digest to consolidate:\n{findings or '(no live research was performed)'}\n\n"
            "Write the findings artifact now, then reply with the list of files you read and the file you wrote."
        )

    # ── Critic & Synthesizer ──────────────────────────────────────────────────────

    async def _squad_verify(
        self, client, model_name: str, user_content: str, plan: Dict[str, Any],
        findings: str, artifact: Optional[str], artifact_content: str,
    ) -> Dict[str, Any]:
        """Critic Agent: audits the evidence dossier and returns a structured verdict."""
        dossier = (
            f"User goal: {user_content}\n\n"
            f"Plan: {json.dumps(plan, ensure_ascii=False)}\n\n"
            f"Browser findings digest:\n{findings or '(no live research was performed)'}\n\n"
            f"Workspace artifact: {artifact or '(none written)'}\n"
            f"{('--- artifact content ---' + chr(10) + artifact_content) if artifact_content else ''}"
        )
        try:
            response = await client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": SQUAD_CRITIC_PROMPT},
                    {"role": "user", "content": dossier[:20000]},
                ],
                temperature=0.1,
                max_tokens=700,
            )
            parsed = extract_json_object(response.choices[0].message.content or "")
        except Exception as e:
            safe_print(f"Squad critic failed, continuing unverified: {e}")
            parsed = None

        if not parsed:
            return {
                "verdict": "unverified",
                "confidence": 0.0,
                "issues": ["The verifier could not be reached, so the evidence was not audited."],
                "required_fixes": ["State clearly that the evidence is unaudited."],
            }

        verdict = str(parsed.get("verdict") or "").strip().lower()
        if verdict not in ("pass", "revise"):
            verdict = "revise" if parsed.get("issues") else "unverified"

        try:
            confidence = float(parsed.get("confidence"))
        except Exception:
            confidence = 0.0

        return {
            "verdict": verdict,
            "confidence": max(0.0, min(1.0, confidence)),
            "issues": [str(i).strip() for i in (parsed.get("issues") or []) if str(i).strip()][:8],
            "required_fixes": [str(f).strip() for f in (parsed.get("required_fixes") or []) if str(f).strip()][:8],
        }

    def _format_verification(self, verification: Dict[str, Any]) -> str:
        issues = verification.get("issues") or []
        fixes = verification.get("required_fixes") or []
        lines = [
            f"Verdict: {verification.get('verdict')} "
            f"(confidence {float(verification.get('confidence') or 0):.2f})"
        ]
        lines += [f"Issue: {i}" for i in issues] or ["No unsupported claims or missing citations found."]
        lines += [f"Fix: {f}" for f in fixes]
        return "\n".join(lines)

    async def _squad_final_step(
        self, chat: Chat, user_content: str, plan: Dict[str, Any], findings: str,
        artifact: Optional[str], artifact_content: str, verification: Dict[str, Any],
        tools_used: List[str], model_name: str, settings: AppSettings,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Critic & Synthesizer: streams the verified final answer to the user."""
        client = llm_client.get_client()
        issues = verification.get("issues") or []
        fixes = verification.get("required_fixes") or []
        criteria = "\n".join(f"- {c}" for c in (plan.get("success_criteria") or []))
        issue_lines = "\n".join(f"- {i}" for i in issues) or "- none"
        fix_lines = "\n".join(f"- {f}" for f in fixes) or "- none"
        prompt = [
            {"role": "system", "content": SQUAD_SYNTHESIS_PROMPT},
            {"role": "user", "content": (
                f"User goal: {user_content}\n\n"
                f"Objective: {plan.get('objective')}\n\n"
                f"Success criteria:\n{criteria}\n\n"
                f"Browser findings digest:\n{findings or '(no live research was performed)'}\n\n"
                f"Workspace artifact: {artifact or '(none written)'}\n"
                f"{artifact_content[:6000] if artifact_content else ''}\n\n"
                f"Verifier verdict: {verification.get('verdict')} "
                f"(confidence {float(verification.get('confidence') or 0):.2f})\n"
                f"Verifier issues:\n{issue_lines}\n"
                f"Required fixes:\n{fix_lines}\n\n"
                f"Tools the squad used: {', '.join(tools_used) if tools_used else 'none'}\n\n"
                "Write the final answer for the user now."
            )},
        ]

        text = ""
        try:
            stream = await client.chat.completions.create(
                model=model_name, messages=prompt, temperature=settings.temperature, stream=True
            )
        except Exception as e:
            yield {"type": "error", "agent": SQUAD_CRITIC, "error": f"Synthesis error: {e}"}
            yield {"type": "_final", "content": text}
            return

        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                text += delta.content
                yield {"type": "content", "delta": delta.content}
        yield {"type": "_final", "content": text}

pete_agent = PeteAgent()
