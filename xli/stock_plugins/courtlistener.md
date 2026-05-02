---
id: courtlistener
name: CourtListener
description: US federal and state court opinions, dockets, judges, and oral arguments (Free Law Project)
categories: [legal, research]
risk: low
effect: read-only
trust: subscription
auth_type: token
auth_env_vars:
  - COURTLISTENER_TOKEN
actions:
  - id: search_opinions
    description: Search US court opinions by query, optionally filtered by court and date
    method: GET
    url: https://www.courtlistener.com/api/rest/v3/search/
    params:
      q: {required: true, description: "Search query"}
      type: {const: "o"}
      court: {description: "Court code (scotus, ca1-ca11, cadc, cafc, nysd, cand, etc.)"}
      filed_after: {description: "Date filter YYYY-MM-DD"}
      filed_before: {description: "Date filter YYYY-MM-DD"}
    headers:
      Authorization: "Token ${COURTLISTENER_TOKEN}"
    response_shape: ".results[] → {id, caseName, court, dateFiled, citation, snippet}"
  - id: get_opinion
    description: Get full text of a specific court opinion by ID
    method: GET
    url: https://www.courtlistener.com/api/rest/v3/opinions/{opinion_id}/
    params:
      opinion_id: {required: true, description: "Opinion ID from search results"}
    response_shape: "{id, plain_text, html, date_created, cluster}"
  - id: search_dockets
    description: Search case dockets (case-level metadata)
    method: GET
    url: https://www.courtlistener.com/api/rest/v3/search/
    params:
      q: {required: true, description: "Search query"}
      type: {const: "r"}
    headers:
      Authorization: "Token ${COURTLISTENER_TOKEN}"
  - id: search_judges
    description: Look up a judge by name
    method: GET
    url: https://www.courtlistener.com/api/rest/v3/people/
    params:
      name_first: {description: "First name"}
      name_last: {description: "Last name"}
---

# CourtListener

Free Law Project's open database of US case law, dockets, judges, and oral argument audio. Reads work without a token at lower rate limits; a free account token raises limits substantially. Get one at https://www.courtlistener.com/sign-in/ → profile → API tokens.

## Auth setup (optional but recommended)

```bash
xli auth set courtlistener COURTLISTENER_TOKEN=<your-token>
```

The agent passes it as `Authorization: Token <token>` (NOT `Bearer`).

## Usage

### Search opinions

```bash
curl -s -H "Authorization: Token ${COURTLISTENER_TOKEN}" \
  'https://www.courtlistener.com/api/rest/v3/search/?q={QUERY}&type=o'
```

`type=o` for opinions. Other types: `r` (RECAP/PACER docket entries), `oa` (oral arguments), `p` (people/judges).

### Filter by court / date

```bash
curl -s -H "Authorization: Token ${COURTLISTENER_TOKEN}" \
  'https://www.courtlistener.com/api/rest/v3/search/?q={QUERY}&type=o&court=scotus&filed_after=2024-01-01'
```

Common court codes: `scotus`, `ca1`–`ca11`, `cadc`, `cafc`, `nysd`, `cand`, `txnd`. Full list at /api/rest/v3/courts/.

### Get one opinion in full

```bash
curl -s 'https://www.courtlistener.com/api/rest/v3/opinions/{OPINION_ID}/'
```

### Search dockets (case-level metadata)

```bash
curl -s -H "Authorization: Token ${COURTLISTENER_TOKEN}" \
  'https://www.courtlistener.com/api/rest/v3/search/?q={QUERY}&type=r'
```

### Look up a judge

```bash
curl -s 'https://www.courtlistener.com/api/rest/v3/people/?name_first={FIRST}&name_last={LAST}'
```

## Response shape

JSON with `count`, `next`, `previous`, `results[]`. Each opinion result: `id`, `caseName`, `court`, `dateFiled`, `citation` (Bluebook), `snippet` (matched excerpt).

## Notes

- The full opinion text isn't in search hits — fetch `/opinions/{id}/` to get `plain_text`.
- Without a token: rate-limited to ~5000 calls/day per IP. With a token: 5000/hour.
- For PACER/RECAP docket *contents* (not just metadata), some endpoints require additional permissions — see the docs.
