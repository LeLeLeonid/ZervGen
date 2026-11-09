---
name: delegation
description: "Route work to the right role agent and synthesize what comes back"
tags: [delegate, delegation, agent, subagent, orchestrate, route, parallel, swarm, handoff, worker]
tools: ["delegate_to", "list_skills", "find_skill", "load_skill", "manage_todo", "response"]
contract:
  pre: {}
  post: {}
procedure:
  - Decide whether the task is actually separable
  - Pick the role that owns the work
  - Write a self-contained task with the constraints
  - Collect results and verify before reporting
dependencies: []
verification: checklist
---
# Delegation

Delegation pays when the work is separable and the role has a better toolset than you. It loses when you hand off something you could have done in one tool call.

## Decide

- One read, one file, one answer: do it yourself.
- Three independent investigations: delegate to three agents in the same script.
- A long build inside a larger plan: delegate, then keep planning.

## Choose a role

```python
names = await list_skills()
```

Roles are the agents under `src/skills/AGENTS`. Current set: `system` (routing), `code` (implementation), `architect` (design), `researcher` (external facts). Pick by ownership of the *output*, not by topic.

## Write the task

The delegate starts with none of your conversation. A task must carry:

1. the goal, in one sentence;
2. the constraints that would otherwise cause a wrong answer;
3. the exact paths, commands or URLs involved;
4. the shape of the expected reply.

```python
result = await delegate_to(
    agent_name="code",
    task="Add an `--json` flag to src/cli.py's parse_args. Must not change existing flags. "
         "Run python -m pytest tests/test_cli.py. Reply with the diff and the test result.",
)
```

## Parallel work

```python
results = await asyncio.gather(
    delegate_to(agent_name="researcher", task="..."),
    delegate_to(agent_name="code", task="..."),
)
```

- Only parallelize work with no shared files. Two agents editing one file in parallel lose one of the edits.
- `results` may contain an error string. Check each one before using it.

## Verify

A delegated answer is a claim, not a fact. Read the files it says it changed. Run the command it says it ran. If the delegate reports success without evidence, verify yourself or say what is unverified.

## Rules

- Never delegate the final answer. You own what you send the user.
- Never delegate with "do the task from the conversation above"; there is no conversation above.
- Do not chain delegations deeper than two: the context loss compounds.