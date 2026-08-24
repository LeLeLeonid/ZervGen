---
name: github
description: "Git and GitHub operations - commits, pushes, PRs, issues"
tags: [git, github, commit, push, pull, branch, merge, pr, issue, clone, repo, repository]
---
# Git/GitHub Context

For git operations, use `shell()` with git commands or MCP git tools.

## Common Git Operations

```python
result = await shell("git status")
return await response(text=result)
```

```python
result = await shell("git log --oneline -10")
return await response(text=result)
```

```python
await shell("git add -A")
result = await shell('git commit -m "your message"')
return await response(text=result)
```

```python
result = await shell("git push")
return await response(text=result)
```

```python
result = await shell("git checkout -b feature/my-feature")
return await response(text=result)
```

## GitHub CLI

```python
result = await shell("gh issue list")
return await response(text=result)
```

```python
result = await shell('gh issue create -t "Title" -b "Body"')
return await response(text=result)
```

```python
result = await run_shell("gh pr list")
return await response(text=result)
```

```python
result = await run_shell('gh pr create -t "Title" -b "Body"')
return await response(text=result)
```

```python
result = await run_shell("gh repo view")
return await response(text=result)
```

## Best Practices
- Always check git status before operations
- Use conventional commit messages (feat:, fix:, docs:, etc.)
- Warn before destructive operations (force push, reset, delete branch)
- Use descriptive branch names (feature/xxx, fix/xxx, chore/xxx)
