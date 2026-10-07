import os
import sys
import json
from pathlib import Path
from typing import List
from pydantic import BaseModel, ConfigDict, Field
from dotenv import load_dotenv

# Imported for its side effect: puts stdout/stderr into UTF-8 before anything can
# print a message the Windows console code page cannot represent. config.py is
# the first app module every entrypoint imports, so this is the earliest safe
# place to do it.
from app.console import configure_stdio, safe_print
from app.key_store import is_protected, protect_api_key, unprotect_api_key

configure_stdio()

load_dotenv()

def _base_dir() -> Path:
    """Where the app's own folders live, in both source and frozen (PeteAI.exe) form.

    Normally this is the project root, one level above this file. But when frozen
    by PyInstaller, ``__file__`` points into the temp folder the one-file build
    unpacks itself into (C:\\Users\\...\\AppData\\Local\\Temp\\_MEIxxxxxx), which is
    wiped on exit. Using that as the base would mean data/ and app/static/ were
    recreated in a throwaway temp dir on every launch: chats, workspaces and the
    saved API key would silently vanish between sessions, and the UI would serve
    an empty static folder.

    So when frozen, anchor to the directory holding the executable instead, and
    resolve it once at import time. ``sys.executable`` is the real PeteAI.exe
    path, not a symlink into the temp dir, so it is stable across runs.
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        # Current builds have ONE support folder beside the exe, named Engine:
        # PeteAI.spec sets COLLECT(contents_directory="Engine"), so PyInstaller
        # creates it under that name and bakes the name into the exe. Do not
        # rename it afterwards -- the bootloader resolves python312.dll against
        # that exact name and the exe will not start (PETE_UX_NOTES.md 8.6).
        #
        # _internal is kept only as a fallback for builds made before that
        # change, which shipped both folders side by side.
        #
        # The fallback is NOT silent, because api.py will happily create an empty
        # app\static in the exe folder and then serve a blank page with HTTP 200
        # while every API call works -- a genuinely confusing failure to debug
        # from the outside. Say so loudly instead.
        for candidate in (exe_dir / "Engine", exe_dir / "_internal"):
            if candidate.is_dir():
                return candidate
        safe_print(
            f"FATAL: neither the Engine nor the _internal folder is beside\n"
            f"       {exe_dir}\n"
            "       PeteAI.exe needs both, exactly as built. Restore the whole\n"
            "       dist\\PeteAI folder -- copying the .exe on its own will not work."
        )
        return exe_dir
    return Path(__file__).resolve().parent.parent

BASE_DIR = _base_dir()


def _data_dir() -> Path:
    """Writable folder for chats, workspaces and settings.

    In development this lives beside the source tree.  When frozen, use a
    per-user AppData folder so it works whether the EXE is on a local drive
    or a network share (the share itself may be read-only).
    """
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("LOCALAPPDATA", ".")) / "PeteAI" / "data"
    return BASE_DIR / "data"


DATA_DIR = _data_dir()
CHATS_DIR = DATA_DIR / "chats"
WORKSPACES_DIR = DATA_DIR / "workspaces"
SETTINGS_FILE = DATA_DIR / "settings.json"

# Bump when settings.json needs a one-time migration (legacy Muse/GrokBot builds wrote v1;
# v3 added the browser_task tool-choice guidance to the stock system prompt).
SETTINGS_SCHEMA_VERSION = 4

for directory in [DATA_DIR, CHATS_DIR, WORKSPACES_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

class AppSettings(BaseModel):
    # Ignore unknown keys so an old settings.json can never crash app startup.
    model_config = ConfigDict(extra="ignore")

    schema_version: int = SETTINGS_SCHEMA_VERSION
    purdue_api_url: str = "https://genai.rcac.purdue.edu/api/v1"
    purdue_api_key: str = ""
    default_model: str = "gemma4:26b-a4b"

    # Microsoft Graph connector (Outlook, Teams, Calendar). The client ID is the
    # Azure app registration's Application (client) ID; tokens live encrypted
    # beside the data dir, never in settings.json.
    graph_client_id: str = ""
    graph_tenant: str = "common"

    temperature: float = 0.7
    max_tokens: int = 4096
    browser_headless: bool = True

    # Folders the agent is allowed to WRITE to. Reads are unrestricted; only
    # writes are limited, to these folders plus the chat workspace.
    local_write_roots: List[str] = Field(default_factory=list)

    system_prompt: str = (
        "You are Pete AI, the official AI research and engineering assistant for Purdue University, "
        "powered by Purdue GenAI Studio.\n\n"
        "RESPONSE RULES:\n"
        "- For conversational messages, greetings, or general knowledge questions: answer directly from "
        "your training knowledge. Do NOT call any tools.\n"
        "- Only use tools (browser_task, search_web, browse_page, take_screenshot, file tools, "
        "spawn_subchat) when the user explicitly asks you to search the web, look something up "
        "online, visit a URL, read/write a file, or branch off a subtopic investigation.\n"
        "- Never call list_workspace_files or any tool unprompted.\n"
        "- When you do use a tool, wait for the tool result before answering.\n"
        "- Always respond in clear, well-structured markdown. Boiler Up!\n\n"
        "CHOOSING A WEB TOOL:\n"
        "- get_weather: for ANY weather question (\"tomorrow's weather\", \"will it rain this weekend\"). "
        "It answers from a forecast API with no key and no browser, so it never hits a CAPTCHA. "
        "Never drive the browser for weather.\n"
        "- browser_task: PREFER THIS whenever the answer needs more than one web step, or needs a page "
        "that only works with interaction. It drives a real browser by itself: it runs a search, clicks "
        "results, fills forms, picks dates from dropdowns, scrolls, and reads the answer. Examples: "
        "\"look up my flight status\", \"find Purdue's fall enrollment "
        "number on their site\", \"get the current price of X\". It returns a finished written answer, "
        "so report that answer back rather than browsing further yourself. The user can watch it work "
        "in the Browser panel.\n"
        "- browse_page: for a single, known, static URL where one fetch of the text is enough.\n"
        "- search_web: for a quick fact where a list of links and snippets will do.\n"
        "- take_screenshot: when the user explicitly wants an image of a page.\n"
        "MICROSOFT TOOLS (need the Microsoft connector in Settings; if a call says it is not "
        "connected, tell the user how to connect instead of retrying):\n"
        "- read_email / search_email: Outlook inbox. search_email takes keywords.\n"
        "- send_email: only when the user explicitly asks to send an email. Confirm the "
        "recipient and subject back to them first if either is ambiguous.\n"
        "- list_calendar_events / create_calendar_event: Outlook calendar.\n"
        "- list_teams_chats / send_teams_message: Teams chats; list first for the chat id, "
        "and only send when the user explicitly asks.\n"
        "Do not use browser_task AND browse_page/search_web for the same request; pick the best one.\n\n"
        "BROWSER_TASK TIPS:\n"
        "- Write the goal concretely, including the location, date, or units needed. \"Find tomorrow's "
        "weather for West Lafayette, Indiana in Fahrenheit\" beats \"find the weather\".\n"
        "- If it returns a partial result, it hit its step limit; re-issue a narrower, more specific "
        "goal rather than repeating the same vague one.\n\n"
        "LOCAL FILES:\n"
        "- list_local_dir / read_local_file / search_files: read anything on this machine.\n"
        "- write_local_file / create_local_dir / move_local_path / delete_local_path: writes are "
        "restricted to the chat workspace and to folders the user allowed in Settings. If a write is "
        "refused for being outside those, tell the user and suggest they add the folder in Settings."
    )

def _migrate_settings(data: dict) -> "tuple[dict, bool]":
    """Applies one-time migrations to a settings.json payload.

    Legacy Muse/GrokBot builds wrote a different system prompt and a stale default
    model. The migration only runs once (schema_version gate) so a model the user
    deliberately picks afterwards is never silently overwritten again.
    """
    defaults = AppSettings()
    changed = False

    if int(data.get("schema_version") or 0) < 2:
        sp = data.get("system_prompt") or ""
        if (not sp.strip()) or "Muse" in sp or "GrokBot" in sp or "Use your tools when you need to" in sp:
            data["system_prompt"] = defaults.system_prompt
        if data.get("default_model") in (None, "", "llama3.1:latest"):
            data["default_model"] = defaults.default_model
        data["schema_version"] = SETTINGS_SCHEMA_VERSION
        changed = True

    if int(data.get("schema_version") or 0) < 3:
        # v3: browser_task arrived, so the stock prompt needs the new tool-choice
        # guidance. Only rewrite prompts that are still the *stock* v2 text --
        # the tell-tale phrase is unique to the default -- so a prompt the user
        # has deliberately edited is never clobbered.
        sp = data.get("system_prompt") or ""
        if "workspace file tools, spawn_subchat" in sp:
            data["system_prompt"] = defaults.system_prompt
        data["schema_version"] = 3
        changed = True

    if int(data.get("schema_version") or 0) < 4:
        # v4: get_weather arrived, and browser_task is no longer the answer for
        # weather questions. Appending is safe for user-edited prompts: it adds
        # the new rule without touching anything they wrote.
        sp = data.get("system_prompt") or ""
        if "get_weather" not in sp:
            data["system_prompt"] = sp + (
                "\nWEATHER: for any weather question, call get_weather with the place name "
                "(days_ahead 0=today, 1=tomorrow). Never drive the browser for weather: "
                "it is slower, and weather sites show CAPTCHAs to automated browsers.\n"
            )
        data["schema_version"] = 4
        changed = True

    # Always backfill anything missing/blank so the file stays complete.
    for field, value in defaults.model_dump().items():
        if field not in data or data[field] in (None, ""):
            data[field] = value
            changed = True

    return data, changed

def load_settings() -> AppSettings:
    defaults = AppSettings()
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data, migrated = _migrate_settings(data)
            settings = AppSettings(**data)
            # One-time upgrade: a key saved as legacy plaintext gets sealed for
            # the current user/machine and re-saved. Never stored in the clear.
            blob = settings.purdue_api_key or ""
            if blob and not is_protected(blob):
                settings.purdue_api_key = protect_api_key(blob, DATA_DIR)
                save_settings(settings)
                migrated = True
            elif migrated:
                save_settings(settings)  # persist the upgrade so legacy values stop coming back
            return settings
        except Exception as e:
            safe_print(f"Error reading settings, using defaults: {e}")

    settings = AppSettings(
        purdue_api_url=os.getenv("PURDUE_API_URL", "https://genai.rcac.purdue.edu/api/v1"),
        purdue_api_key=os.getenv("PURDUE_API_KEY", ""),
        default_model=os.getenv("DEFAULT_MODEL", defaults.default_model)
    )
    if settings.purdue_api_key and not is_protected(settings.purdue_api_key):
        settings.purdue_api_key = protect_api_key(settings.purdue_api_key, DATA_DIR)
    save_settings(settings)
    return settings


def get_api_key(settings: "AppSettings | None" = None) -> str:
    """The usable API key, unsealed. Returns "" when none is configured.

    ``AppSettings.purdue_api_key`` on disk is always the sealed blob (or a
    legacy plaintext value awaiting migration); never use it directly.
    """
    s = settings or load_settings()
    blob = s.purdue_api_key or ""
    if not blob:
        return ""
    if is_protected(blob):
        return unprotect_api_key(blob, DATA_DIR)
    return blob


def save_settings(settings: AppSettings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings.model_dump(), f, indent=2)
