---
name: web
description: "Search the web and fetch URLs for real-time information"
tags: ["web", "search", "internet", "news", "url", "fetch", "browse", "google", "online", "docs"]
---

# WEB SEARCH

I'm starting a search for you..
```python
# web_search returns a Python list directly. NO json.loads() needed.
hits = await web_search(
    query="python asyncio tutorial",
    limit=5,
    recency="w"
)

# fetch_url returns a Python dict directly. NO json.loads() needed.
page = await fetch_url(
    url=hits[0]["url"],
    parse_html=True,
    timeout=15
)
body = page["text"]
# respond with summary or better use print() to analyze
result = await response(f"Summary: {body[:500]}")
```

RULES:
- web_search returns JSON ARRAY directly. Iterate it. No wrapper object.
- ddgs raw key is "href"; the tool normalizes it to "url". Trust "url".
- For "latest / this week / today" pass recency ("d"|"w"|"m"|"y"); do not bake dates into the query.
- fetch_url default parse_html=True returns cleaned text. Use parse_html=False only when you need raw source.
- Snippets are teasers. Real content is in fetch_url. Don't answer from snippets alone.
- Chain: search → pick the best → fetch → analyze → respond + citate.
