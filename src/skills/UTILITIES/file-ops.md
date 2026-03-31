---
description: "File operations guide - reading, writing, exploring files and directories"
tags: [file, read, write, edit, explore, codebase, directory, glob, grep, search, find]
---
# File Operations Guide

## Exploring a Codebase
Always start by discovering what exists before reading.

```python
# List all files recursively
files = await list_files(".", recursive=True)

# Find specific file patterns
py_files = await glob_files("**/*.py", path="src")

# Search file contents
matches = await grep_files("class.*Agent", path="src", use_regex=True)
return await response(text=matches)
```

## Reading Files
```python
# Read a single file
content = await read_file("src/main.py")
return await response(text=content[:500])

# Read multiple files
results = []
for f in ["src/config.py", "src/main.py"]:
    content = await read_file(f)
    results.append(f"--- {f} ---\n{content[:200]}")
return await response(text="\n".join(results))
```

## Writing Files
```python
# Write new file
await write_file("output/result.txt", content="Hello World")

# Append to existing
await append_file("log.txt", content="New entry\n")

# Edit existing file (read first!)
original = await read_file("src/config.py")
result = await edit_file("src/config.py", find="old_line", replace="new_line")
return await response(text=result)
```

## File Structure
```python
# Search for class/function definitions
matches = await grep_files("class ", path="src", use_regex=False)
return await response(text=matches)
```

## Common Patterns
1. **Explore first**: `glob_files` → `read_file` (never read blindly)
2. **Edit safely**: `read_file` → `edit_file` (always read before edit)
3. **Search then read**: `grep_files` → `read_file` on matching files
4. **Verify changes**: `read_file` after `write_file` to confirm
