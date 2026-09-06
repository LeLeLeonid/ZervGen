# ZervGen Instructions 

## Project Structure

```
ZervGen/
├── AGENTS.md              # This file
├── config.json            # Application settings
├── src/
│   ├── cli.py             # Rich CLI, chat loop, menus
│   ├── config.py          # Settings loader + defaults
│   ├── skills_loader.py   # YAML skill contract loader + registry
│   ├── tools.py           # 27 tool functions (delegation, web, shell..)
│   ├── utils.py           # Token counting, context discovery,compression
│   ├── core/
│   │   ├── base_agent.py      # StemAgent execution engine
│   │   ├── mcp_manager.py     # Model Context Protocol client
│   │   ├── memory.py          # Three-tier memory (KG, ChromaDB, SessionDB)
│   │   ├── orchestrator.py    # Metacognitive supervisor, multi-wave delegation
│   │   ├── provider.py        # LLM provider wrappers base
│   │   └── runtime.py         # The Harness Core
│   ├── providers/
│   │   ├── anthropic.py       # Anthropic Claude provider
│   │   ├── base.py            # Abstract provider interface
│   │   ├── gemini.py          # Google Gemini provider
│   │   ├── openai.py          # OpenAI GPT provider
│   │   ├── openrouter.py      # OpenRouter aggregator
│   │   └── pollinations.py    # Pollinations
│   └── skills/
│       ├── AGENTS/
│       │   ├── architect.md   # Architecture & design agent
│       │   ├── code.md        # Code generation agent
│       │   ├── researcher.md  # Research & data gathering agent
│       │   └── system.md      # Orchestrator definition
│       ├── UTILITIES/
│       │   ├── file-ops.md    # File operations guide
│       │   ├── web.md         # Web search & fetch guide
│       │   └── weather.md     # Weather tool guide
│       └── INTEGRATION/
│           ├── github.md      # Git/GitHub operations
│           ├── mcp-installer.md  # MCP server management
│           └── skill-creator.md   # Skill creation
├── tmp/
│   ├── memory/             # Persistent stores (SQLite, ChromaDB, KG..)
│   ├── history.log         # Event log (append-only)
├── requirements.txt        # Python dependencies
└── main.py                # Entry point
```

## Programmatic Tool Calling

All tools are called directly in Python code blocks:

I'm starting to work on the task...
```python
# Read a file
content = await read_file("tmp/PLAN.md")

# Search memory
results = await search_memory("database schema", limit=5)

# Delegate to another agent 
result = await delegate_to(agent_name="code", task=f"Execute: {content}")
```

## Configuration

- **Root config:** `config.json` — application settings.
- **Environment:** `.zervgen/.env` — API keys and secrets.