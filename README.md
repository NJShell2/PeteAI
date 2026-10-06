# Pete AI

**An AI assistant with a memory and a browser, powered by Purdue's GenAI Studio models.**

Pete AI brings the agentic assistant experience, persistent storage and real browser use, to Purdue's own GenAI Studio infrastructure. Chat that remembers. Files that persist. An agent that can actually go read the web.

Project site: https://njshell2.github.io/PeteAI/

## Run it yourself

Pete AI runs on your own Windows machine. Your API key never leaves it.

**Prerequisites:** Windows, Python 3.12.

1. Clone or download this repo.
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
   The browser agent also needs a browser it can drive. If you have Chrome or Edge installed, Pete AI finds it automatically. Otherwise run `playwright install chromium`.
3. Launch it:
   ```
   run.bat
   ```
   then open http://127.0.0.1:8000 in your browser.
4. On first run, a setup modal walks you through getting your GenAI Studio API key at https://genai.rcac.purdue.edu/, paste it in, run the connection test, and you are done. Pete AI encrypts the key with Windows DPAPI for your user account and remembers it from then on.

See [docs/SECURITY.md](docs/SECURITY.md) for key handling and hygiene. No key ever ships with the app: builds are assembled with an empty key store by design.

## Modes

- **Pete Solo** (working): a single agent with conversation, a persistent file workspace, and browser tools. The daily driver.
- **Pete Squad** (roadmap): a coordinated multi-agent team. An orchestrator decomposes goals, a Researcher works the browser while a Workspace agent works the files, agents message each other, and a Critic verifies code and facts before the final answer. See [docs/ROADMAP.md](docs/ROADMAP.md).

## Design principles

- **Local-first.** Chats and workspaces live on your machine as the source of truth. The app keeps working during network outages.
- **You hold the wheel.** Focus mode, a Take control button, Esc to hand control back, and a Stop button that stops one run without breaking the next.
- **Verified, not vibes.** Seven automated test suites guard the browser launch, the agent loop, stop behavior, chat persistence, and the takeover UX, measured against a real browser.
- **One model provider.** Pete AI connects only to Purdue GenAI Studio.

## Tests

```
python -m pytest test_browser_launch.py test_browser_agent.py test_console.py test_history_replay.py test_parser.py test_tool_loop.py test_backend.py
```

`verify_ux.py` drives a real browser against the UI; it needs the app serving on 127.0.0.1:8000.

## Building the Windows exe

```
build_exe.bat
```

This runs PyInstaller from the project venv and produces `dist\PeteAI\` with `PeteAI.exe`, the `_internal` support folder (do not rename it, the name is baked into the exe), and an `Engine` folder holding the UI and an empty key store. The development `data/` directory is never copied into a build.

## Status

Prototype stage. Pete Solo (chat, workspace, browser agent) works; the human-in-the-loop browser UX is shipped and verified; per-user encrypted key storage is done; the packaged Windows build is in progress; Pete Squad is designed and next.

## Contents

- `app/` - the application (API, agent loop, browser session/tools, storage, web UI in `app/static/`)
- `main.py` - entry point; `run.bat` - launcher
- `docs/` - project site source plus `ROADMAP.md` and `SECURITY.md`
- `test_*.py`, `verify_ux.py` - test suites and UX verification
