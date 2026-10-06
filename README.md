# Pete AI

**An AI assistant with a memory and a browser, powered by Purdue's GenAI Studio models.**

Pete AI brings the agentic assistant experience, persistent storage and real browser use, to Purdue's own GenAI Studio infrastructure. Chat that remembers. Files that persist. An agent that can actually go read the web.

Live project site: https://njshell2.github.io/PeteAI/

## Modes

- **Pete Solo** (working): a single agent with conversation, a persistent file workspace, and browser tools. The daily driver.
- **Pete Squad** (roadmap): a coordinated multi-agent team. An orchestrator decomposes goals, a Researcher works the browser while a Workspace agent works the files, agents message each other, and a Critic verifies code and facts before the final answer. See [docs/ROADMAP.md](docs/ROADMAP.md).

## Getting started: your API key

Pete AI talks to Purdue's GenAI Studio, and Studio needs to know who is asking. You get a key once, paste it once, and Pete AI remembers it from then on.

1. Go to [genai.rcac.purdue.edu](https://genai.rcac.purdue.edu/) and sign in with your Purdue account.
2. Generate an API key and copy it. Treat it like a password.
3. Launch Pete AI. A setup modal appears on first run with these instructions and a link back to Studio.
4. Paste the key and run the built-in connection test, which verifies the key against Studio live.
5. Done. Pete AI encrypts the key with Windows DPAPI (tied to your logged-in Windows account) and stores it in your per-user app data at `%LOCALAPPDATA%\PurduePeteAI`. You never reenter it.

Keys are per person: on a shared machine, each user enters their own key once. See [docs/SECURITY.md](docs/SECURITY.md) for key hygiene.

## Design principles

- **Local-first.** Chats and workspaces live on your machine as the source of truth. The app keeps working during network outages.
- **You hold the wheel.** Focus mode, a Take control button, Esc to hand control back, and a Stop button that stops one run without breaking the next.
- **Verified, not vibes.** Seven automated test suites guard the browser launch, the agent loop, stop behavior, chat persistence, and the takeover UX, measured against a real browser.
- **One model provider.** Pete AI connects only to Purdue GenAI Studio.

## Status

Prototype stage. Pete Solo (chat, workspace, browser agent) works; the human-in-the-loop browser UX is shipped and verified; per-user encrypted key storage is done; the packaged Windows build is in progress; Pete Squad is designed and next.

## Contents

- `index.html` - the project site (published via GitHub Pages)
- `docs/ROADMAP.md` - Pete Squad multi-agent architecture
- `docs/SECURITY.md` - API key handling and hygiene
