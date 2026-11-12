---
name: mcp
description: "Connect, inspect and call Model Context Protocol servers"
when_not: "a built-in tool already does the job"
tags: [mcp, model-context-protocol, server, servers, tools, integration, connect, context7]
tools: ["mcp_add", "mcps", "mcp", "tools", "response"]
contract:
  pre: {}
  post: {}
procedure:
  - Inspect configured servers and their state
  - Resolve or install the server before calling its tools
  - Call one tool with exact arguments
  - Read the error instead of retrying blindly
dependencies: []
verification: checklist
---
# MCP

An MCP server exposes extra tools. Tool names are namespaced `mcp__<server>__<tool>`. The `internal` server exposes ZervGen's own registry and needs no process.

## Inspect

```python
await mcps()
```

Server states: `idle` (configured, not started), `starting`, `ready`, `degraded` (last-known tools cached, transport failing), `dead` (backoff exhausted). A degraded server still lists cached tool names but will fail on call.

## Discover tool arguments

```python
await tools("search")
```

`tools` returns the full schema for matching tools. MCP argument names are defined by the server, not by ZervGen. Never guess them: scan first.

## Call

```python
await mcp(server="internal", tool="todo", arguments={"action": "ls"})
```

- `arguments` is a mapping or a JSON string. One tool at a time: a batch call that half-fails tells you nothing about which half.
- Under `ASK`/`CALL` modes, MCP calls are denied. They are BUILD/DEBUG tools.

## Install

```python
await mcp_add(
    name="context7",
    command="npx",
    args='["-y", "@upstash/context7-mcp"]',
    transport="stdio",
)
```

- `args` and `env` are JSON **strings**, not Python lists. `env='{"API_KEY": "sk-..."}'` is moved into `.zervgen/.env`; config keeps only `${API_KEY}`.
- Give either `command` (local) or `url` (remote), never both.
- Prefer a curated preset over a hand-written command; presets already carry the right args.
- `npx`-based servers need Node on the PATH. If `node` is missing, the server will sit in `degraded` no matter how many times you retry — report that instead of looping.

## Troubleshoot in order

1. `mcps` — is the state `ready`?
2. `tools` — does the tool name exist at all?
3. The error text — `not found` is a naming problem, `timed out` is a transport problem.
4. Only then reinstall.

## Context7

Context7 serves current library documentation, so answers about an API's exact signature do not come from the model's memory. It is an ordinary MCP server: install it, then `mcp(server="context7", tool="get-library-docs", arguments={...})`. Nothing about it is special-cased in ZervGen.
