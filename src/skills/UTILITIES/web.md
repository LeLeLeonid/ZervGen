---
name: web
description: "Search the web and fetch URLs for real-time information"
when_not: "the answer is stable knowledge or already on disk"
tags: ["web", "search", "internet", "news", "url", "fetch", "browse", "google", "online", "docs"]
---

# WEB SEARCH

I'm starting a search for you..
```python
# web returns a Python ls directly. NO json.loads() needed.
hits = await web(
    query="python asyncio tutorial",
    limit=5,
    recency="w"
)

# fetch returns a Python dict directly. NO json.loads() needed.
page = await fetch(
    url=hits[0]["url"],
    parse_html=True,
    timeout=15
)
body = page["text"]
# respond with summary or better use print() to analyze
result = await response(f"Summary: {body[:500]}")
```

RULES:
- web returns JSON ARRAY directly. Iterate it. No wrapper object.
- ddgs raw key is "href"; the tool normalizes it to "url". Trust "url".
- For "latest / this week / today" pass recency ("d"|"w"|"m"|"y"); do not bake dates into the query.
- fetch default parse_html=True returns cleaned text. Use parse_html=False only when you need raw source.
- Snippets are teasers. Real content is in fetch. Don't answer from snippets alone.
- Chain: search → pick the best → fetch → analyze → respond + citate.
