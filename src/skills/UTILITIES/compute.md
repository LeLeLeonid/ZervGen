---
name: compute
description: "Exact arithmetic, JSON formatting, hashing, encoding and UUIDs"
when_not: "the arithmetic fits in one expression you can evaluate with calc"
tags: [calc, calculate, math, arithmetic, compute, json, format, pretty, hash, md5, sha256, base64, encode, decode, uuid, random, string]
tools: ["calc", "pretty", "hash", "b64encode", "b64decode", "uuid", "random", "response"]
contract:
  pre: {}
  post: {}
procedure:
  - Use the dedicated tool instead of doing it by hand
  - Pass arguments in the tool's real types
  - Check the result for an Error prefix
dependencies: []
verification: checklist
---
# Compute

These tools exist so that exact work is exact. Do not estimate arithmetic in prose, and do not hand-encode base64.

## Arithmetic

```python
await calc(expression="(1200 * 1.08) ** 2")
```

- `calc` evaluates a numeric expression. It is not a Python sandbox: no variables, no imports, no strings.
- Order of operations applies. Parenthesize anything ambiguous.
- For money, round explicitly and say the unit.

## JSON

```python
await pretty(data='{"b":2,"a":[1,2]}')
await pretty(data='{"b":2,"a":[1,2]}', mode="minify")
```

- Accepts a dict or a JSON string. `pretty` (default) indents; `minify` compacts.
- If the input is not valid JSON, the result starts with `Error:`. Fix the input rather than regex-patching the output.

## Hashing and encoding

```python
await hash(text="hello", algorithm="sha256")
await b64encode(text="hello")
await b64decode(encoded="aGVsbG8=")
```

- `hash` supports `md5`, `sha1`, `sha256`, `sha512`. For anything security-relevant use `sha256` or better; md5 is for checksums, never for passwords.
- `b64encode` takes text and returns ASCII. `b64decode` returns text; invalid input starts with `Error:`.

## Identifiers

```python
await uuid()
await random(length=24)
```

- `uuid` returns a v4 UUID.
- `random` uses `secrets`. Never use it for a password you then log; never use a deterministic pattern for tokens.

## Rules

- Never do a hash, encoding or UUID "in your head". Use the tool.
- Never fabricate the output of a compute tool. If it errors, report the error.
- These are pure functions: calling one twice with the same input gives the same answer, except the random ones.
