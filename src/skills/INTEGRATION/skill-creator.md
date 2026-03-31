---
description: "Create and modify ZervGen skills/agents"
tags: [skill, agent, new, make, generate, template, skill-creator]
---
# Skill Creator

You create new ZervGen skills/agents by writing `.md` files to specific directories.

## How to Create a Skill

Use `write_file()` to create the skill file:

```python
# Create a new agent (has tools, spawned via delegate_to)
result = await write_file(
    path="src/skills/AGENTS/my_agent.md",
    content='''---
description: "Short description of what this agent does"
tools: ["read_file", "write_file", "run_shell", "response"]
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

## File Locations
- `src/skills/AGENTS/` — Full agents with tools list (spawned via delegate_to)
- `src/skills/INTEGRATION/` — External integrations
- `src/skills/UTILITIES/` — Simple utility skills (context hints only)

## YAML Frontmatter Rules
- `description` — One sentence, in quotes
- `tools` — Array of tool names the agent can use. MUST include "response".
- `tags` — Array of lowercase keywords for find_skill() matching
- Agent files have tools: [...], skill files have tags: [...]

## Available Tools for Agents
- **File**: read_file, write_file, append_file, edit_file, list_files, glob_files, grep_files, get_code_skeleton
- **Shell**: run_shell
- **Web**: web_search, fetch_url, get_weather
- **Memory**: add_memory, search_memory, promote_memory
- **Delegation**: delegate_to, response
- **Utility**: generate_uuid, hash_string, format_json, calc, manage_todo

## After Creating
- The skill/agent is available immediately (skills are loaded on demand)
- Test with delegate_to(agent_name="my_agent", task="test task") for agents
- Test with find_skill(tags=["tag1"]) for utility skills
- Use list_skills() to verify it appears
