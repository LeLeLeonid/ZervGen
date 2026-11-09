---
name: skill-creator
description: "Create and modify ZervGen skills/agents with minimal frontmatter"
tags: [skill, meta, skill-creator, template, agent]
contract:
  pre: {}
  post: {}
procedure:
  - Parse intent & skill type
  - Generate YAML frontmatter + Markdown body
  - Write to correct directory
  - Verify syntax & reload index
dependencies: [write_file, list_files, skill_index.reload]
verification: checklist
---
# Skill Creator

You create new ZervGen skills/agents by writing `.md` files.

## Frontmatter contract

Required, enforced by `validate_skill` on every load — a skill that fails is logged and dropped:

- `name` — must equal the file stem (or the directory name for `SKILL.md`), lowercase `[a-z0-9._-]`, max 64 chars.
- `description` — one line, max 1024 chars. This is what the index shows; write it as a trigger.
- `tags` — non-empty list. These drive `match_trigger`; include the words a user would actually type.

Optional, with defaults filled in by the loader:

- `tools` — list of tool names this skill needs. Use `["all"]` only for orchestrator roles.
- `contract.pre` / `contract.post` — mappings. `post` keys are checked against the result.
- `procedure` — ordered list. The critic grades against it when `critic_enabled`.
- `dependencies` — list of tools or skills that must load first.
- `verification` — `checklist` (default) or `heavy`.
- `related`, `version`, `license`, `category`, `metadata`.

Unknown top-level fields are rejected. Do not invent keys.

## Directory Map
- `src/skills/AGENTS/` — Full agents. Requires `tools: [...]`. Spawned via `delegate_to()`.
- `src/skills/UTILITIES/` — Context hints, routing triggers, PTC patterns.
- `src/skills/INTEGRATION/` — External APIs, webhooks, MCP bridges.

One skill per file. A directory with a `SKILL.md` (plus optional `references/`, `scripts/`, `templates/`, `assets/`) is the bundled form; a flat `<name>.md` is the simple form. Do not create both for the same skill.

## How to Create a Skill

Use `write_file()` to create the skill file:

```python
# Create a new agent (has tools, spawned via delegate_to)
result = await write_file(
    path="src/skills/AGENTS/my_agent.md",
    content='''---
name: my_agent
description: "Short description of what this agent does"
tags: [agent, domain]
tools: ["read_file", "write_file", "shell", "response"]
---
# My Agent

## IDENTITY
You are a specialized agent that does X.

## BEHAVIOR
1. Read the input task
2. Use tools to complete it
3. Return result via response()
'''
)
return await response(text=result)
```

```python
# Create a utility skill (no tools, just context hints)
result = await write_file(
    path="src/skills/UTILITIES/my_skill.md",
    content='''---
name: my_skill
description: "What this skill helps with"
tags: [tag1, tag2, tag3]
---
# My Skill

Use `tool_name(args)` to do X.
Always do Y before Z.
'''
)
return await response(text=result)
```

## Validation Checklist
- Frontmatter is valid YAML with ONLY `name`, `description`, `tags` (+ `tools` for agents)
- `name` matches filename (lowercase, hyphens)
- Markdown body contains triggers, steps, constraints
- Call `skill_index.reload()` after writing
- Test agents: `delegate_to(agent_name="name", task="test")`
- Test utilities: `find_skill(tags=["tag"])`