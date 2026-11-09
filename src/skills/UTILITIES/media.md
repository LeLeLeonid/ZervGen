---
name: media
description: "Generate images, take and analyze screenshots, drive the desktop"
tags: [image, images, picture, draw, illustration, screenshot, screen, vision, desktop, click, type, automation]
tools: ["generate_image", "take_screenshot", "analyze_screen", "mouse_click", "type_text", "response"]
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
path = await generate_image(prompt="isometric server room, teal light", width=1024, height=768)
```

- Returns a local path on success. Anything starting with `Error:` or `Image gen failed` is a failure, not a path.
- Image generation is rate-limited and provider-dependent. If it fails once, report the reason; do not retry in a loop.
- `download_and_open_image(url)` accepts only `http`/`https`. It refuses other schemes by design.

## Screen

```python
path = await take_screenshot(filename="screen.png")
answer = await analyze_screen(question="What error dialog is open?")
```

- `take_screenshot` captures every monitor into one image.
- `analyze_screen` needs a vision-capable provider. If it answers `Error: Current provider does not support Vision.`, say so instead of guessing at the screen.
- Name the file after the task, not `screen2.png`.

## Desktop automation

```python
await mouse_click(x=640, y=480)
await type_text("hello")
```

These are BUILD-only and require explicit user intent. Click coordinates are absolute screen pixels. Confirm the target is visible in a screenshot taken immediately before acting; a stale coordinate lands somewhere unexpected.

## Rules

- Never claim an image or screenshot exists without a real path from the tool.
- Prefer `analyze_screen` over describing a screenshot from memory.
- One capture per question. Re-capturing the same screen wastes the budget.