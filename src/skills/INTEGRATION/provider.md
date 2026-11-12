---
name: provider
description: "Choose, configure and switch LLM providers without hardcoding prices"
when_not: "the provider and key are already working"
tags: [provider, providers, model, models, llm, openrouter, anthropic, openai, gemini, ollama, lmstudio, api-key, cost, pricing]
tools: ["read", "write", "shell", "response"]
contract:
  pre: {}
  post: {}
procedure:
  - Read the configured provider and model
  - Fetch live model metadata instead of assuming it
  - Set the key as an env reference
  - Verify with one real call
dependencies: []
verification: checklist
---
# Providers

Providers live in `src/providers/`. Settings live in `config.json` (`provider`, `model`, `providers`). Secrets live only in `.zervgen/.env`.

## Inspect

```python
config = await read("config.json")
```

Relevant keys:

```
provider          active provider id
model             model id for that provider
providers[name]   base_url, model, api_key (as ${VAR}), headers
```

## Model facts come from the catalog

Context window and price are fetched from `https://models.dev/api.json` at runtime and cached in `tmp/models_dev.json`. Do not write a price table into code, a skill, or an answer. If a price is needed, read it through the provider (`get_context`, `get_rates`, `get_cost`) and quote the catalog.

If the catalog has no entry, the model is treated as free-local. That is a fallback, not a claim.

## Add a provider

```python
await write(".zervgen/.env", content="DEEPSEEK_API_KEY=sk-...\n")
```

```json
{
  "provider": "deepseek",
  "model": "deepseek-chat",
  "providers": {
    "deepseek": {
      "base_url": "https://api.deepseek.com/v1",
      "api_key": "${DEEPSEEK_API_KEY}"
    }
  }
}
```

- The config holds `${VAR}`, never the literal key. The value belongs in `.zervgen/.env`, which is untracked.
- Never print a key, never paste one into a skill, never commit one.
- OpenAI-compatible endpoints need `base_url` ending in `/v1` and use the OpenAI provider class.

## Local endpoints

`ollama`, `lmstudio`, `koboldcpp` need no key and are always free. Point `base_url` at the local port (`http://localhost:11434/v1` for Ollama).

## Verify

```python
await shell("python -c \"import asyncio; from src.core.provider import get_provider; from src.config import load_config; p = get_provider(load_config().provider, load_config()); print(asyncio.run(p.generate_text([{'role': 'user', 'content': 'ping'}], 'reply pong')))\"")
```

One real call proves the key, the base URL and the model id. A config that looks right but has never made a call is unverified.

## Rules

- Do not remove fields from a provider payload; providers differ in what they accept.
- Do not swap the active provider without being asked. It changes cost and capability for the whole session.
- When a call fails with 401, the key is wrong or unset — check `.zervgen/.env`, not the model id.
