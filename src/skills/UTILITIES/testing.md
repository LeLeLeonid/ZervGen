---
name: testing
description: "Run tests, read failures, and verify a change before claiming it works"
tags: [test, tests, pytest, testing, verify, verification, failure, failing, regression, coverage, check, validate]
tools: ["shell", "read_file", "write_file", "edit_file", "grep_files", "glob_files", "list_files", "response"]
contract:
  pre: {}
  post: {}
procedure:
  - Run the narrowest test that covers the change
  - Read the actual failure, not just the summary line
  - Fix the cause, then rerun the same test
  - Widen to the full suite only after the narrow test passes
dependencies: []
verification: checklist
---
# Testing

A change is unverified until a command has run and its exit status is zero.

## Loop

```python
result = await shell("python -m pytest tests/test_auth.py -x -q")
print(result)
```

- Exit code is truth. Output that merely *starts* with "Error" is not a failure if the exit code is 0.
- `-x` stops at the first failure. Use it while iterating; drop it for the final confirmation run.
- `-q` keeps the output small enough to read in full.
- Never pipe a test run into `tail` or `head` if you still need the traceback; read the file or rerun with less output.

## Reading a failure

1. The failing test name and file:line are the address. Read that test before reading the implementation.
2. The assertion line shows expected vs actual. Compare them, do not skim.
3. The last frame from your project is the suspect. Frames from `site-packages` are usually victims, not causes.
4. If the failure moves when you change unrelated code, suspect shared state: a fixture, a singleton, a file under `tmp/`.

## Verify

```python
await shell("python -m compileall -q src")
await shell("python -m pytest -q")
```

- `compileall` catches syntax and import-time errors across the whole tree in one pass; it is cheaper than running the tests when the change is structural.
- Run the full suite once before reporting completion. Report the exact counts: `159 passed`, or `158 passed, 1 failed` with the failure.
- If a test fails because the *test* is stale (it encodes a design you intentionally changed), update the test and say so. Never delete an assertion to make a run green.

## Rules

- Do not claim "tests pass" from memory or from an earlier run in the session.
- Do not add a new test file for a one-line fix; put the assertion next to the code it covers.
- When mocking a network or a model, make the fake deterministic. A test that passes half the time is worse than no test.