# Pete Squad Multi-Agent Architecture

## Overview

Pete AI ships with **Pete Solo** (single-agent with subchats, file storage, and browser tools). **Pete Squad** is the planned collaborative multi-agent system for complex jobs.

## Pete Solo vs. Pete Squad

| Feature | Pete Solo | Pete Squad |
| :--- | :--- | :--- |
| Agent model | Single master agent with branching subchats | Specialized multi-agent team coordinated by an orchestrator |
| Context strategy | Subchats isolate deep-dives; synthesis returns to parent | Agents exchange structured message payloads and task status |
| Tool allocation | Master agent has all tools (browser, files) | Tools distributed by specialist domain (researcher, coder, critic) |
| Execution flow | Focused conversational branching with synthesis | Autonomous multi-turn agent coordination and handoffs |

## Specialist agent personas

1. **Orchestrator agent**: analyzes incoming goals, decomposes them into distinct research and coding tasks, delegates to specialists, and synthesizes the final answer.
2. **Web and research agent (browser specialist)**: Playwright-driven browser tools plus search; navigates portals, extracts papers and docs, takes screenshots, returns summarized findings with direct citations.
3. **Workspace and data agent (code specialist)**: manages the local workspace files; reads, writes, modifies, and organizes documents and data; executes scripts and transformations.
4. **Critic and verification agent**: inspects researcher and coder output; validates code syntax and factual accuracy before anything reaches the user.

## Architecture blueprint

```
                           User Request / UI
                                  |
                                  v
                       Orchestrator Agent (Pete Squad)
                            /              \
                           /                \
               Research Agent            Workspace Agent
           (browser specialist)        (code specialist)
                           \                /
                            \              /
                       Critic / Synthesis Agent
                                  |
                                  v
                         Streamed to Frontend
```

## Key design insight

The squad does not give each agent its own computer. It follows the proven pattern: one shared computer with shared files, browser sessions, and logins, but one screen per agent, and one agent runs one computer-use task on its screen at a time. Isolation is per screen, not per machine.

## Status

Designed and documented. Implementation is the next milestone after the portable Windows build.
