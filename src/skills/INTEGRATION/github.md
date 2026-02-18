---
description: "Git and GitHub operations - commits, pushes, PRs, issues"
tags: [git, github, commit, push, pull, branch, merge, pr, issue, clone, repo, repository]
---
# Git/GitHub Context

For git operations, use MCP git tools or run_shell with git commands.

## Available MCP Git Tools
- `git_status` - Check repository state
- `git_commit` - Commit changes
- `git_push` - Push to remote
- `git_pull` - Pull from remote
- `git_branch` - Branch operations

## GitHub CLI (via run_shell)
```bash
gh issue list           # List issues
gh issue create -t "Title" -b "Body"  # Create issue
gh pr list              # List pull requests
gh pr create -t "Title" -b "Body"     # Create PR
gh repo view            # View repo info
```

## Best Practices
- Always check git status before operations
- Use conventional commit messages (feat:, fix:, docs:, etc.)
- Warn before destructive operations (force push, reset, delete branch)
- Use descriptive branch names (feature/xxx, fix/xxx, chore/xxx)
