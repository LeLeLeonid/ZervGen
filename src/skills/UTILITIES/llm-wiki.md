---
name: llm-wiki
description: "Build and maintain a persistent interlinked knowledge wiki (Karpathy-style)"
tags: [wiki, knowledge, rag, notes, research, persistent, interlinked]
---
# LLM Wiki

Persistent, interlinked Markdown knowledge base. Unlike RAG, the wiki is compiled once and read directly.

## Structure
wiki/
├── SCHEMA.md          # Rules, conventions, tag taxonomy, domain
├── index.md           # Sectioned catalog with one-line summaries
├── log.md             # Append-only action log
├── raw/               # Immutable source material
├── entities/          # People, orgs, products, models
├── concepts/          # Topics and ideas
├── comparisons/       # Side-by-side analyses
└── queries/           # Filed query results

## Frontmatter (every page)
---
title: Page Title
created: YYYY-MM-DD
updated: YYYY-MM-DD
type: entity | concept | comparison | query
tags: [tag1, tag2]
sources: [raw/source.md]
confidence: high | medium | low
contested: true (optional)
---

## Workflow
1. Orientation: read SCHEMA.md, index.md, last 50 lines of log.md. Check duplicates.
2. Ingestion: add raw → summarize into entity/concept → cross-link → log.
3. Linting: check broken links, valid frontmatter, rotate log >500 entries.

## Rules
- Never edit raw files.
- Flag contradictions with contested: true.
- Every page must link to another page.
