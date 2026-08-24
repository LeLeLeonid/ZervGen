---
name: file-ops
description: "Read, write, edit, search files and codebases"
tags: [file, read, write, edit, explore, codebase, directory, glob, grep, search, find, skeleton, structure]
---
# File Operations

All tools return strings. Check for "Error:" prefix.

## Explore → Analyze → Respond

Scanning Codebase...
```python
# 1. Explore structure without reading full files
listing = await list_files("src/core", recursive=True)

skeleton = await get_code_skeleton("src/core/base_agent.py")

matches = await grep_files("class.*Agent", path="src", use_regex=True)

# 2. Read what matters
content = await read_file("src/core/base_agent.py")

# 3. Do your analysis in code
lines = content.split("\n")
classes = [l.strip() for l in lines if l.strip().startswith("class ")]
```

## Quick reference

```python
# Explore
await list_files("src", recursive=False, ignore_dir=True)
await glob_files("**/*.py", path=".")
await grep_files("pattern", path="src", file_type=".py", use_regex=False)
await get_code_skeleton("path/to/file.py")

# Read
await read_file(path="path/to/file.py", offset=0, max_chars=8000)

# Write (BUILD mode only)
await write_file("path/to/file.py", content="...")
await append_file("log.txt", content="new line\n")
await edit_file("path.py", find="old_text", replace="new_text")
```

## Rules
- Never call response() after every tool. Scan everything first, analyze, then respond ONCE.
- edit_file uses EXACT string match, not regex. Read first, copy exact text.
- read_file returns full file. Slice in Python: content[1000:3000].
- list_files excludes .git, __pycache__, venv, node_modules by default (ignore_dir=True).
- grep_files default is literal search. Pass use_regex=True for regex.