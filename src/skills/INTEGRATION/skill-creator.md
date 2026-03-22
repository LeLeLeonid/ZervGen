---
description: "Create and modify ZervGen skills/agents"
tags: [skill, create, agent, new, make, generate, template, skill-creator]
---
# Skill Creator Context

Use this context when creating new ZervGen skills/agents.

## Skill File Format

### For AGENTS (have tools, spawned via delegate_to):
```markdown
---
description: "Short description"
tools: ["tool1", "tool2", "response"]
---
# [Agent Name]

## IDENTITY
You are a [role description].

## BEHAVIOR
1. [Step 1]
2. [Step 2]

## OUTPUT FORMAT
[Expected output]
```

### For INTEGRATION/UTILITIES (just tags, found via find_skill):
```markdown
---
description: "Short description"
tags: [tag1, tag2, tag3]
---
# [Skill Name] Context

[Context/hints for using the tools]
```

## File Locations
- `src/skills/AGENTS/` - Full agents with tools
- `src/skills/INTEGRATION/` - External integrations
- `src/skills/UTILITIES/` - Simple utility skills

## Available Tools for Agents
- **File**: read_file, write_file, edit_file, list_files, glob_files, grep_files
- **Code**: get_code_skeleton, run_code, run_shell
- **Web**: web_search, fetch_url, get_weather
- **Memory**: add_memory, search_memory
- **Utility**: generate_uuid, hash_string, format_json, timer, calc
