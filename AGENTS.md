# ZervGen

Agent harness: one loop, four modes, tools as Python, skills as Markdown.

## Layout

```
ZervGen/
├── config.json            # settings (git-ignored)
├── .zervgen/.env          # secrets (git-ignored)
├── main.py                # entry point
├── src/
│   ├── cli.py             # Rich TUI: chat loop, /commands, pickers, TAB modes
│   ├── config.py          # settings, presets, provider/model catalog wiring
│   ├── skills_loader.py   # skill frontmatter contract, registry, role loader
│   ├── tools.py           # tool functions + registry + schema renderer
│   ├── utils.py           # tokens, truncation, TraceBus, compressor, git snapshots
│   ├── core/
│   │   ├── base_agent.py  # StemAgent: prompt build, loop, PTC, sensors
│   │   ├── orchestrator.py# routing, delegation waves, critic gate
│   │   ├── runtime.py     # PermissionPolicy, ToolRuntime, Run, Session, coordinator
│   │   ├── mcp_manager.py # MCP clients, health, namespacing
│   │   ├── memory.py      # KG + ChromaDB + sessions, Dreamer heartbeat
│   │   └── provider.py    # provider base, payloads, native tools, cost
│   ├── providers/         # anthropic, openai, openrouter, gemini, pollinations
│   └── skills/
│       ├── AGENTS/        # delegate roles (system, code, architect, researcher, reviewer)
│       ├── INTEGRATION/   # github, mcp, provider, skill-creator
│       └── UTILITIES/     # compute, delegation, file-ops, llm-wiki, media, memory, testing, weather, web
├── tests/                 # pytest, one file per subsystem
└── tmp/                   # runtime data: memory, state, trace, logs (git-ignored)
```

## Loop

```
user input
  → StemAgent.run
      build system prompt (header, run, protocol, tools, skills, memory, peers, rules)
      provider.generate_text
      parse turn: ```python``` PTC | ::tool shorthand | JSON/XML call | native call | answer
      every call → ToolRuntime.execute (policy → ask → budget → timeout → one result)
      every turn end → sensors → critic gate
  → one answer, rendered once
```

## Modes

| mode | may | may not |
|---|---|---|
| `ASK` | read, search, answer | write, shell, MCP |
| `BUILD` | everything | — |
| `CALL` | read, search, plan | write, shell, MCP |
| `DEBUG` | everything, focused on reproduction | — |

TAB in an empty prompt cycles modes. `/mode` opens the menu.

## Tools

Called as Python inside a ```` ```python ```` block, or as shorthand:

```python
content = await read("tmp/PLAN.md")
hits = await recall("database schema", limit=5)
result = await delegate(agent_name="code", task=f"Execute: {content}")
```

```text
::read("src/cli.py", 0, 4000)
::shell("pytest -q")
```

Shorthand binds positionally to the real signature. Use `tools(name)` for an exact schema.

## Skills

One Markdown file per skill. Frontmatter is enforced on load:

```yaml
---
name: kebab-case            # must equal the file stem
description: "one line"     # shown in the index
when_not: "the adjacent case this skill is wrong for"   # optional, shown as (not when: ...)
tags: [trigger, words]      # drive auto-match
tools: [tool_name]          # optional
contract: {pre: {}, post: {}}
procedure: [step, step]
dependencies: [tool_or_skill]
verification: checklist     # or heavy
---
```

A skill that fails validation is logged and dropped. `load_skill(name)` pulls the body on demand; the index is always shown.

## Configuration

- `config.json` — provider, model, mode, permissions, budgets. Ignored by git.
- `.zervgen/.env` — keys, referenced as `${VAR}` from config. Never commit either file.
- `tmp/state/` — trace JSONL, incidents, dream heartbeat state. Runtime scratch, safe to delete.

## Verify

```bash
python -m compileall -q src main.py
python -m pytest -q
```