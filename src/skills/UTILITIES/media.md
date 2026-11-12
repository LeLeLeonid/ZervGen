---
name: media
description: "Generate images, take and analyze screenshots, drive the desktop"
when_not: "the user asked for text output"
tags: [image, images, picture, draw, illustration, screenshot, screen, vision, desktop, click, type, automation]
tools: ["image", "screenshot", "vision", "click", "keys", "response"]
contract:
  pre: {}
  post: {}
procedure:
  - Pick the smallest tool that answers the request
  - Generate or capture the artifact
  - Inspect the result before reporting success
dependencies: []
verification: checklist
---
# Media

## Images

```python
path = await image(prompt="isometric server room, teal light", width=1024, height=768)
```

- Returns a local path on success. Anything starting with `Error:` or `Image gen failed` is a failure, not a path.
- Image generation is rate-limited and provider-dependent. If it fails once, report the reason; do not retry in a loop.
- `download_and_open_image(url)` accepts only `http`/`https`. It refuses other schemes by design.

## Screen

```python
path = await screenshot(filename="screen.png")
answer = await vision(question="What error dialog is open?")
```

- `screenshot` captures every monitor into one image.
- `vision` needs a vision-capable provider. If it answers `Error: Current provider does not support Vision.`, say so instead of guessing at the screen.
- Name the file after the task, not `screen2.png`.

## Desktop automation

```python
await click(x=640, y=480)
await keys("hello")
```

These are BUILD-only and require explicit user intent. Click coordinates are absolute screen pixels. Confirm the target is visible in a screenshot taken immediately before acting; a stale coordinate lands somewhere unexpected.

## Rules

- Never claim an image or screenshot exists without a real path from the tool.
- Prefer `vision` over describing a screenshot from memory.
- One capture per question. Re-capturing the same screen wastes the budget.
