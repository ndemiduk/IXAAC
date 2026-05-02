---
id: wikipedia
name: Wikipedia
description: Article search and summary lookup via the Wikipedia REST and Action APIs
categories: [reference, facts]
risk: low
effect: read-only
trust: subscription
auth_type: none
auth_env_vars: []
actions:
  - id: title_search
    description: Autocomplete-style search for Wikipedia article titles
    method: GET
    url: https://en.wikipedia.org/w/api.php
    params:
      action: {const: "opensearch"}
      search: {required: true, description: "Search query"}
      limit: {default: "10"}
      format: {const: "json"}
    response_shape: "[query, [titles], [descriptions], [urls]]"
  - id: fulltext_search
    description: Full-text search across Wikipedia articles
    method: GET
    url: https://en.wikipedia.org/w/api.php
    params:
      action: {const: "query"}
      list: {const: "search"}
      srsearch: {required: true, description: "Search query"}
      format: {const: "json"}
      srlimit: {default: "10"}
    response_shape: ".query.search[] → {title, snippet, timestamp, wordcount}"
  - id: page_summary
    description: Clean 3–4 sentence summary of a Wikipedia article
    method: GET
    url: https://en.wikipedia.org/api/rest_v1/page/summary/{title}
    params:
      title: {required: true, description: "Article title (underscores OK, e.g. Albert_Einstein)"}
    response_shape: "{title, extract, description, thumbnail}"
  - id: page_intro
    description: Plain-text intro section of a Wikipedia article (longer than summary)
    method: GET
    url: https://en.wikipedia.org/w/api.php
    params:
      action: {const: "query"}
      prop: {const: "extracts"}
      exintro: {const: "1"}
      explaintext: {const: "1"}
      titles: {required: true, description: "Article title"}
      format: {const: "json"}
      redirects: {const: "1"}
    response_shape: ".query.pages[id].extract → plain text"
---

# Wikipedia

Two complementary APIs: the modern REST API for clean per-page summaries/HTML, and the legacy Action API for everything else (search, full-text, links, etc.). No auth needed for reads; please send a descriptive `User-Agent` header on any non-trivial volume.

## Usage

### Title search (autocomplete-style)

```bash
curl -s 'https://en.wikipedia.org/w/api.php?action=opensearch&search={QUERY}&limit=10&format=json'
```

Returns `[query, [titles], [descriptions], [urls]]` — quick fuzzy match for "what's the article called".

### Full-text search

```bash
curl -s 'https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={QUERY}&format=json&srlimit=10'
```

### Page summary (clean, short)

```bash
curl -s 'https://en.wikipedia.org/api/rest_v1/page/summary/{TITLE}'
```

`{TITLE}` URL-encoded; underscores are fine (`Albert_Einstein` or `Albert%20Einstein`). Response includes `extract` (3-4 sentence summary), `description`, `thumbnail`.

### Full HTML

```bash
curl -s 'https://en.wikipedia.org/api/rest_v1/page/html/{TITLE}'
```

### Plain-text intro section only

```bash
curl -s 'https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1&explaintext=1&titles={TITLE}&format=json&redirects=1'
```

`redirects=1` follows Wikipedia's redirects (e.g. `JFK` → `John_F._Kennedy`).

## Other languages

Replace `en.wikipedia.org` with `<lang>.wikipedia.org` (`ja`, `de`, `fr`, `es`, …). Same URL shapes everywhere.

## Response shape

Action API returns `{ query: { ... } }` with the result list under varying keys (`search`, `pages`, etc.). REST API returns one flat object per page.

## Notes

- Best for `/get` use case: a quick summary the agent uses to ground a follow-up answer. Pair with `web_search` when Wikipedia stops short.
- For programmatic mass scraping, prefer Wikipedia's bulk dumps over the API.
