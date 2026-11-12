---
name: github
description: "Git and GitHub operations: branch, commit, diff, PR, issues"
when_not: "the repo has no git remote or the user asked for local-only work"
tags: [git, github, commit, branch, merge, rebase, diff, pull-request, pr, issue, remote, clone, push, blame, log]
tools: ["shell", "read", "grep", "response"]
contract:
  pre: {}
  post: {}
procedure:
  - Inspect state before changing it
  - Work on a branch, never directly on the default
  - Commit one logical change with a real message
  - Verify the result with git log and git status
dependencies: []
verification: checklist
---
# Git and GitHub

Shell commands are the interface. `gh` is available for anything that needs the API.

## Inspect first

```python
await shell("git status --short --branch")
await shell("git log --oneline -10")
await shell("git diff --stat")
```

Run these before any write. A dirty tree changes what a commit or merge will do.

## Branch and commit

```python
await shell("git switch -c fix/auth-timeout")
await shell("git add -A")
await shell("git commit -m 'fix(auth): bound token refresh to 5s'")
```

- Never commit on `main`/`master` unless the user asked for it.
- One logical change per commit. Ten commits named `wip` are worse than one honest commit.
- Message shape: `type(scope): what changed`. Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`.
- Only commit when the user asks. Stage and show the diff otherwise.

## Diff

```python
await shell("git diff")
await shell("git diff --cached")
await shell("git show --stat HEAD")
```

Read the diff before pushing. A diff you did not read is a change you did not review.

## Pull requests

```python
await shell("gh pr create --title 'fix(auth): bound token refresh' --body 'Closes #42'")
await shell("gh pr view 42 --json state,mergeable,reviews")
await shell("gh pr checks 42")
```

- The base branch is `main` unless the repo says otherwise. Check with `git symbolic-ref refs/remotes/origin/HEAD`.
- Wait for checks before claiming a PR is ready.
- Never force-push a shared branch. Never `--no-verify`.

## Recovery

```python
await shell("git reflog -20")
await shell("git restore --source=HEAD~1 path/to/file")
```

`reflog` is the safe undo. `reset --hard` and `clean -fd` destroy uncommitted work — confirm with the user before either.

## Rules

- Never delete a branch or rewrite history without explicit permission.
- Never commit secrets. Check `git diff --cached` for keys before every commit.
- If a repo has moved under you (`git status` shows unexpected files), stop and report rather than committing over it.
