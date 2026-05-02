---
id: hackernews
name: Hacker News (Algolia)
description: Full-text search over Hacker News stories and comments via the Algolia HN API
categories: [tech, news, search]
risk: low
effect: read-only
trust: subscription
auth_type: none
auth_env_vars: []
actions:
  - id: search
    description: Search HN stories by relevance
    method: GET
    url: https://hn.algolia.com/api/v1/search
    params:
      query: {required: true, description: "Search query"}
      tags: {default: "story", description: "Filter tags: story, comment, show_hn, ask_hn, front_page"}
      numericFilters: {description: "e.g. points>50, num_comments>10"}
    response_shape: ".hits[] → {objectID, title, url, author, points, num_comments, created_at_i}"
  - id: search_by_date
    description: Search HN stories/comments sorted by date (most recent first)
    method: GET
    url: https://hn.algolia.com/api/v1/search_by_date
    params:
      query: {required: true, description: "Search query"}
      tags: {default: "story"}
    response_shape: ".hits[] → {objectID, title, url, author, points, num_comments, created_at_i}"
  - id: front_page
    description: Current HN front page stories
    method: GET
    url: https://hn.algolia.com/api/v1/search
    params:
      tags: {const: "front_page"}
    response_shape: ".hits[] → {objectID, title, url, author, points, num_comments}"
  - id: get_item
    description: Get a specific HN item (story or comment) by ID
    method: GET
    url: https://hn.algolia.com/api/v1/items/{item_id}
    params:
      item_id: {required: true, description: "HN item ID (numeric)"}
---

# Hacker News (Algolia)

Algolia hosts Hacker News' public search index. No auth, no key, very generous rate limit. Two endpoints: relevance-ranked search and date-ranked search.

## Usage

### Relevance search (most useful for "any HN discussion of X?")

```bash
curl -s 'https://hn.algolia.com/api/v1/search?query={QUERY}&tags=story'
```

### Date-ranked (most recent first)

```bash
curl -s 'https://hn.algolia.com/api/v1/search_by_date?query={QUERY}&tags=story'
```

### Front page right now

```bash
curl -s 'https://hn.algolia.com/api/v1/search?tags=front_page'
```

### One specific item (story or comment) by id

```bash
curl -s 'https://hn.algolia.com/api/v1/items/{ITEM_ID}'
```

## Filter tags

- `story`, `comment`, `poll`, `pollopt`, `show_hn`, `ask_hn`, `front_page`
- Combine with comma (AND): `tags=story,author_pg`
- Or paren-form (OR): `tags=(story,comment)`

## Numeric filters (e.g. high-upvoted only)

```bash
curl -s 'https://hn.algolia.com/api/v1/search?query={QUERY}&tags=story&numericFilters=points>50'
```

Common fields: `points`, `num_comments`, `created_at_i` (unix epoch).

## Response shape

JSON. `hits[]` is the result set; each hit has `objectID`, `title`, `url`, `author`, `points`, `num_comments`, `created_at_i`. The `_highlightResult` field shows where matches landed in the title/URL.

## Notes

- `objectID` doubles as the HN item id — append to `https://news.ycombinator.com/item?id=` for the human URL.
- The Algolia index lags real-time HN by a few minutes — fine for research, not for live monitoring.
- Story bodies (Ask HN posts) are in the `story_text` field, HTML-encoded.
