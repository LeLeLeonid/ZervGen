# ZervGen

**Autonomous AI Agent Framework**

[![Version](https://img.shields.io/badge/Version-1.5.0-purple?style=for-the-badge)](https://github.com/LeLeLeonid/ZervGen)
[![License](https://img.shields.io/badge/License-MIT-000000?style=for-the-badge)](LICENSE)

ZervGen orchestrates multiple AI agents through a provider-agnostic layer with persistent memory, tool execution, and MCP integration. The Orchestrator delegates tasks to specialized sub-agents (coder, researcher, architect) with wave-based parallel execution.

## Providers

OpenAI, Anthropic, Gemini, Groq, OpenRouter, SiliconFlow, Pollinations (free, default), Local (Ollama/LM Studio)

## Install

```bash
git clone https://github.com/LeLeLeonid/ZervGen.git
cd ZervGen
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

pip install httpx pydantic rich ddgs beautifulsoup4 fake_useragent aiofiles pyyaml
python main.py
```

## Usage

Chat directly or use slash commands:

- `/role` — Check agent role (code, architect, researcher, etc.)
- `/mode` — Switch mode (ASK, PLAN, BUILD, DEBUG)
- `/auto` — Toggle autonomous execution
- `/provider [name]` — Switch AI provider
- `/memory` — View memory
- `/compact` — Compact memory
- `/load` — Load previous session
- `/history` — View conversation history
- `/help` — Show all commands

The orchestrator auto-delegates to specialized agents:
> **User:** "Research the latest Python web frameworks and build a comparison table"
> **ZervGen:** Spawns researcher → spawns coder → combines results

## Architecture

```
main.py                      → Entry point
src/
├── cli.py                   → Rich CLI, menus, chat loop
├── config.py                → Pydantic settings, config.json
├── tools.py                 → 40+ tool functions, auto-registry
├── utils.py                 → Token counting, retry, sanitization
├── skills_loader.py         → Markdown role/skill loading
├── core/
│   ├── base_agent.py        → Agent execution loop
│   ├── orchestrator.py      → Delegation, wave execution
│   ├── memory.py            → Three-tier memory, ChromaDB
│   ├── mcp_manager.py       → MCP server lifecycle
│   └── provider.py          → Provider abstraction, circuit breaker
├── providers/               → 8 provider implementations
└── skills/                  → Agent roles (markdown + YAML)
    ├── AGENTS/              → code, architect, researcher, game_dev, n8n
    ├── INTEGRATION/         → github, skill-creator
    └── UTILITIES/           → weather
tmp/
├── memory/
│   ├── sessions/            → Session logs (JSONL)
│   ├── knowledge_graph.json → Persistent facts
│   └── vector_store/        → ChromaDB data
```

## Configuration

Edit `config.json` or use the Settings menu (option 2 from main menu):

```json
{
  "provider": "openrouter",
  "max_steps": 50,
  "mode": "BUILD",
  "auto_mode": true,
  "memory_enabled": true,
  "mcp_enabled": true,
  "mcp_servers": {
    "filesystem": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"], "enabled": true }
  }
}
```

Provider settings are in the same file under provider name keys.

## Skills

Create agent roles by adding markdown files to `src/skills/AGENTS/`:

```markdown
---
description: "A security auditor"
tools: ["read_file", "grep_files", "web_search", "response"]
---
# Security Auditor
You analyze code for vulnerabilities...
```

## License

MIT
