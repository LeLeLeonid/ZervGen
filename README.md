# ZervGen

**Agent harness — one loop, four modes, tools as Python, skills as Markdown.**

[![Version](https://img.shields.io/badge/Version-1.6.5a-purple?style=for-the-badge)](https://github.com/LeLeLeonid/ZervGen)
[![License](https://img.shields.io/badge/License-MIT-000000?style=for-the-badge)](LICENSE)

ZervGen runs a supervisor loop that decomposes tasks and spawns specialized agents (coder, researcher, architect, etc.) working in parallel with persistent memory, tool execution, model tiers, and MCP integration.

## Install

```bash
git clone https://github.com/LeLeLeonid/ZervGen.git
cd ZervGen
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Configure

Put keys in `.zervgen/.env` (never in config — it's git-ignored):

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
OPENROUTER_API_KEY=sk-or-...
GEMINI_API_KEY=...
```

On first run, `config.json` is created. Edit it directly, or use the in-app `/set` and `/get` commands. Set per-tier models with `/set model_tiers.<TIER>.<provider> <model>`, then activate with `/tier`.

## Use

Run `python main.py`, then chat or type slash commands:

- `/role` — switch agent (code, architect, researcher…)
- `/mode` — ASK, BUILD, CALL, DEBUG · TAB on an empty prompt cycles
- `/provider [name]`, `/model [name]`, `/tier [name]` — model selection
- `/memory`, `/compact`, `/load`, `/history` — memory
- `/help` — everything

## Layout

```
ZervGen/
├── main.py                # entry point
├── src/
│   ├── cli.py             # TUI, chat loop, commands
│   ├── config.py          # settings, presets, secret redaction
│   ├── skills_loader.py   # skill validation + registry
│   ├── tools.py           # tool functions + registry
│   ├── utils.py           # tokens, truncation, trace, compressor
│   ├── core/
│   │   ├── base_agent.py  # StemAgent: prompt, loop, PTC, sensors
│   │   ├── orchestrator.py# routing, delegation waves, critic gate
│   │   ├── runtime.py     # PermissionPolicy, ToolRuntime, Run
│   │   ├── mcp_manager.py # MCP clients, health, namespacing
│   │   ├── memory.py      # KG + vector + sessions, Dreamer
│   │   └── provider.py    # provider base, payloads, native tools, cost
│   ├── providers/         # anthropic, openai, openrouter, gemini, pollinations
│   └── skills/            # AGENTS, INTEGRATION, UTILITIES
├── tests/                 # pytest, one file per subsystem
└── tmp/                   # runtime data (git-ignored)
```

## Verify

```bash
python -m compileall -q src main.py
python -m pytest -q
```

## License

MIT
