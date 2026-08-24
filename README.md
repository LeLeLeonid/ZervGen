# ZervGen

**Autonomous AI Agent Framework — v1.6.0 (alpha)**

[![Version](https://img.shields.io/badge/Version-1.6.0-purple?style=for-the-badge)](https://github.com/LeLeLeonid/ZervGen)
[![License](https://img.shields.io/badge/License-MIT-000000?style=for-the-badge)](LICENSE)

> ⚠️ **Alpha software.** v1.6.0 is a major rewrite and is **buggy**. Expect crashes, rough edges, and breaking changes. Use at your own risk.

ZervGen is a multi-agent AI framework. It spins up specialized agents (coder, researcher, architect) that work together on your tasks, with persistent memory, tool execution, and Model Context Protocol (MCP) integration.

## Features

- **Multi-agent orchestration** — a supervisor decomposes tasks and spawns agents to work on them in parallel
- **Provider-agnostic** — works with Anthropic, OpenAI, OpenRouter, Gemini, and Pollinations
- **Persistent memory** — three-tier storage (knowledge graph, vector DB, session log)
- **Tool execution** — 27 built-in tools: web search, file ops, shell, weather, git, and more
- **MCP support** — connect external MCP servers for additional tools
- **Skill system** — YAML-defined skills with pre/post validation contracts
- **Slash commands** — control the agent from the terminal (role, mode, provider, memory, etc.)

## Requirements

- Python 3.10+
- An API key for at least one provider (Anthropic, OpenAI, OpenRouter, Gemini, or Pollinations)

## Install

```bash
git clone https://github.com/LeLeLeonid/ZervGen.git
cd ZervGen
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

pip install httpx pydantic rich ddgs beautifulsoup4 fake_useragent aiofiles pyyaml dirtyjson chromadb tiktoken simpleeval prompt_toolkit
python main.py
```

## Configuration

Create a `.env` file in `~/.zervgen/` with your API keys:

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
OPENROUTER_API_KEY=sk-or-...
GEMINI_API_KEY=...
```

## Usage

Run `python main.py`, then chat directly or use slash commands:

- `/role` — Switch agent role (code, architect, researcher, etc.)
- `/mode` — Switch mode (ASK, PLAN, BUILD)
- `/provider [name]` — Switch AI provider
- `/memory` — View memory
- `/compact` — Compact memory
- `/load` — Load a previous session
- `/history` — View conversation history
- `/help` — Show all commands

## Project Layout

```
ZervGen/
├── main.py             # Entry point
├── src/
│   ├── cli.py          # Terminal UI, chat loop, commands
│   ├── config.py       # Settings + secret redaction
│   ├── skills_loader.py# Skill validation + registry
│   ├── tools.py        # 27 tool functions
│   ├── utils.py        # Token counting, context, retry logic
│   ├── core/
│   │   ├── base_agent.py   # StemAgent execution engine
│   │   ├── memory.py       # Three-tier memory (KG, vector, sessions)
│   │   ├── orchestrator.py # Metacognitive supervisor
│   │   ├── provider.py     # Provider wrappers
│   │   └── mcp_manager.py  # MCP client
│   ├── providers/       # Anthropic, OpenAI, OpenRouter, Gemini, Pollinations
│   └── skills/          # Skill definitions (AGENTS, UTILITIES, INTEGRATION)
└── requirements.txt
```

## License

MIT