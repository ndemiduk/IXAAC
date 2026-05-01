---
id: google-news
name: Google News (RSS)
description: Topic, region, and search-driven headlines via Google News' public RSS endpoint
categories: [news, headlines]
risk: low
auth_type: none
auth_env_vars: []
---

# Google News (RSS)

The Google News site exposes RSS feeds for every search/topic/region combination. No auth, no key, no published rate limit (be reasonable). Returns RSS XML — pipe through `xmllint` or a Python one-liner to parse.

## Usage

### Search by query

```bash
curl -s 'https://news.google.com/rss/search?q={QUERY}&hl=en-US&gl=US&ceid=US:en'
```

`{QUERY}` URL-encoded. Supports operators (`when:7d` last week, `site:bbc.com`, quoted phrases).

### Top headlines (no query)

```bash
curl -s 'https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en'
```

### Section-based (built-in topics)

```bash
curl -s 'https://news.google.com/rss/headlines/section/topic/{TOPIC}?hl=en-US&gl=US&ceid=US:en'
```

`{TOPIC}` ∈ `WORLD`, `NATION`, `BUSINESS`, `TECHNOLOGY`, `ENTERTAINMENT`, `SCIENCE`, `SPORTS`, `HEALTH`.

### Geographic location

```bash
curl -s 'https://news.google.com/rss/headlines/section/geo/{LOCATION}?hl=en-US&gl=US&ceid=US:en'
```

## Locale parameters

- `hl` — UI language (`en-US`, `en-GB`, `ja`, `de`, `fr`, …)
- `gl` — country code (`US`, `GB`, `JP`, `DE`, …)
- `ceid` — combined country/edition (`US:en`, `JP:ja`, `GB:en`)

## Response shape

RSS 2.0 XML. Each `<item>` has `<title>`, `<link>`, `<pubDate>`, `<source>`, `<description>` (HTML snippet).

Quick parse with Python:

```bash
curl -s '...' | python3 -c "import sys, xml.etree.ElementTree as ET; \
  tree = ET.fromstring(sys.stdin.read()); \
  [print(item.findtext('title'), '|', item.findtext('link')) for item in tree.iter('item')]"
```

## Notes

- `link` is a Google redirect URL, not the publisher direct link. Follow it (or extract the `url=` query param) for the canonical source.
- Some queries return zero items — Google's editorial filter is opaque. Try broader phrasing if a search comes back empty.
