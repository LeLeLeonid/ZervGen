---
name: memory
description: "Store, recall and maintain long-term knowledge; track multi-step work"
tags: [memory, recall, knowledge, remember, remember-this, context, todo, todos, plan, task-list, persistent]
tools: ["add_memory", "promote_memory", "search_memory", "search_tgs", "manage_todo", "response"]
contract:
  pre: {}
  post: {}
procedure:
  - Search before storing to avoid duplicates
  - Store durable facts, not conversation noise
  - Promote proven facts to a longer tier
  - Track multi-step work as TODOs
dependencies: []
verification: checklist
---
# Memory

Memory has three tiers: short-term (this session), recent (distilled facts) and knowledge graph (durable facts with outcomes). Search is always cheaper than re-deriving.

## Recall

```python
hits = await search_memory(query="database schema", limit=5)

graph = await search_tgs(query="how does auth reach the database", limit=3)
```

- `search_memory` is keyword/vector recall. Use it first for anything the user says "before", "earlier", "we decided".
- `search_tgs` traces relationships across facts. Use it for multi-hop questions: "which module owns X", "what depends on Y".
- Results can be empty. An empty result is information: say "not in memory" instead of guessing.

## Store

```python
await add_memory(content="Project uses SQLite through aiosqlite; no ORM.", category="fact")
await add_memory(content="pytest must pass before any commit.", category="rule")
```

Rules:
- One fact per call. "X does Y because Z" is one fact; a whole transcript is not.
- Only store what will still be true next week: constraints, decisions, credentials locations, gotchas.
- Never store secrets, tokens or passwords. Store where they live instead.
- Search first. Duplicate facts dilute recall and get pruned.

## Maintain

```python
await promote_memory(memory_id="abc123", new_tier="knowledge")
```

Promote a fact once it has proven useful more than once. Facts that are recalled and then contradicted by a failed outcome decay automatically; do not fight the pruning.

## Track work

```python
await manage_todo(action="add", task="port PDF skill to single-file format")
await manage_todo(action="list")
await manage_todo(action="done", todo_id="3")
```

Use TODOs for any task with three or more steps. Update the list as steps complete; do not batch the updates at the end, because an interrupted run loses the progress.