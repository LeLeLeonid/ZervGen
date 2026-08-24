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

## Important Rule
**For standard skills, use:** `name`, `description`, `tags`.
Everything else lives in the Markdown body. The loader ignores extra fields unless explicitly wired.

## Advanced Fields
Reserved for orchestrated, multi-step, or safety-critical workflows. If implemented in the runtime, they function as:
- `contract.pre/post` → Python-level guards. Fails fast if required args are missing (`pre`) or output lacks expected keys/format (`post`). Bypasses critic on hard failures.
- `procedure` → Machine-readable step list. Used by the critic as a deterministic grading rubric instead of freeform evaluation.
- `dependencies` → Topological injection. Forces dependent skills/tools to load first. Prevents context race conditions in chained workflows.
- `verification` → Critic mode switch. `checklist` grades against `procedure`. Future modes: `heavy` (parallel reasoning), `code_test` (unit execution), `rubric` (custom scoring).

**Do not add these unless building state-chaining, auto-graded, or contract-bound agents.** They add zero overhead to standard skills.

## Directory Map
- `src/skills/AGENTS/` — Full agents. Requires `tools: [...]`. Spawned via `delegate_to()`.
- `src/skills/UTILITIES/` — Context hints, routing triggers, PTC patterns. No `tools` list.
- `src/skills/INTEGRATION/` — External APIs, webhooks, MCP bridges.

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