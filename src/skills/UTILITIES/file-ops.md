---
name: file-ops
description: "Read, write, edit, search files and codebases"
when_not: "the file is binary or larger than you can read"
tags: [file, read, write, edit, explore, codebase, directory, glob, grep, search, find, skeleton, structure]
---
# File Operations

All tools return strings. Check for "Error:" prefix.

## Explore → Analyze → Respond

Scanning Codebase...
```python
# 1. Explore structure without reading full files
listing = await ls("src/core", recursive=True)

skeleton = await outline("src/core/base_agent.py")

matches = await grep("class.*Agent", path="src", use_regex=True)

# 2. Read what matters
content = await read("src/core/base_agent.py")

# 3. Do your analysis in code
lines = content.split("\n")
classes = [l.strip() for l in lines if l.strip().startswith("class ")]
```

## Quick reference

```python
# Explore
await ls("src", recursive=False, ignore_dir=True)
await glob("**/*.py", path=".")
await grep("pattern", path="src", file_type=".py", use_regex=False)
await outline("path/to/file.py")

# Read
await read(path="path/to/file.py", offset=0, max_chars=8000)

# Write (BUILD mode only)
await write("path/to/file.py", content="...")
await append("log.txt", content="new line\n")
await edit("path.py", find="old_text", replace="new_text")
```

## Rules
- Never call response() after every tool. Scan everything first, analyze, then respond ONCE.
- edit uses EXACT string match, not regex. Read first, copy exact text.
- read returns full file. Slice in Python: content[1000:3000].
- ls excludes .git, __pycache__, venv, node_modules by default (ignore_dir=True).
- grep default is literal search. Pass use_regex=True for regex.
