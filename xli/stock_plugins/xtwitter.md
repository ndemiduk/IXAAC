---
id: xtwitter
name: X (Twitter v2 API)
description: Tweet search, trends, user/timeline lookups via the X v2 API. Requires a paid tier.
categories: [social, news]
risk: low
effect: read-only
trust: subscription
auth_type: bearer
auth_env_vars:
  - X_BEARER_TOKEN
actions:
  - id: recent_search
    description: Search tweets from the last 7 days
    method: GET
    url: https://api.twitter.com/2/tweets/search/recent
    params:
      query: {required: true, description: "Search query (supports from:, to:, lang:, -is:retweet, has:images)"}
      max_results: {default: "25"}
      tweet.fields: {default: "created_at,public_metrics,author_id"}
    headers:
      Authorization: "Bearer ${X_BEARER_TOKEN}"
    response_shape: ".data[] → {id, text, created_at, public_metrics, author_id}"
  - id: user_by_handle
    description: Look up an X user by handle
    method: GET
    url: https://api.twitter.com/2/users/by/username/{username}
    params:
      username: {required: true, description: "X handle (without @)"}
      user.fields: {default: "created_at,public_metrics,verified,description"}
    headers:
      Authorization: "Bearer ${X_BEARER_TOKEN}"
    response_shape: ".data → {id, name, username, description, public_metrics}"
  - id: user_tweets
    description: Recent tweets from a user (need their numeric user ID)
    method: GET
    url: https://api.twitter.com/2/users/{user_id}/tweets
    params:
      user_id: {required: true, description: "Numeric user ID (from user_by_handle)"}
      max_results: {default: "25"}
      tweet.fields: {default: "created_at,public_metrics"}
    headers:
      Authorization: "Bearer ${X_BEARER_TOKEN}"
  - id: get_tweet
    description: Get a single tweet by ID
    method: GET
    url: https://api.twitter.com/2/tweets/{tweet_id}
    params:
      tweet_id: {required: true, description: "Tweet ID"}
      tweet.fields: {default: "created_at,public_metrics,author_id"}
      expansions: {default: "author_id"}
      user.fields: {default: "username"}
    headers:
      Authorization: "Bearer ${X_BEARER_TOKEN}"
---

# X (Twitter v2 API)

Read endpoints (search, trends, public timelines, user lookups) require **at minimum** the **Pay-As-You-Go** tier (released April 2026, billed per request) or any paid plan above it (Basic/Pro/Enterprise). Free tier provides only post-your-own + read-your-own-user, which won't help here.

## Auth setup

1. Sign up at https://developer.x.com/en/portal/dashboard. Subscribe to **Pay-As-You-Go** (cheapest read access; ~cents per request) or higher.
2. In the developer portal: Project → Keys & Tokens → generate the **App-Only Bearer Token**.
3. Store it:

```bash
xli auth set xtwitter X_BEARER_TOKEN=<your-bearer-token>
```

App-Only Bearer is sufficient for everything below. User-context OAuth2 (PKCE) is only needed for posting / private follows; out of scope for this plugin.

## Usage

### Recent search (last 7 days)

```bash
curl -s -H "Authorization: Bearer ${X_BEARER_TOKEN}" \
  'https://api.twitter.com/2/tweets/search/recent?query={QUERY}&max_results=25&tweet.fields=created_at,public_metrics,author_id'
```

Query language supports operators: `from:user`, `to:user`, `lang:en`, `-is:retweet`, `has:images`, etc. See the X docs for the full grammar.

### User by handle

```bash
curl -s -H "Authorization: Bearer ${X_BEARER_TOKEN}" \
  'https://api.twitter.com/2/users/by/username/{USERNAME}?user.fields=created_at,public_metrics,verified,description'
```

### A user's recent tweets (need their `id` from above)

```bash
curl -s -H "Authorization: Bearer ${X_BEARER_TOKEN}" \
  'https://api.twitter.com/2/users/{USER_ID}/tweets?max_results=25&tweet.fields=created_at,public_metrics'
```

### Single tweet by id

```bash
curl -s -H "Authorization: Bearer ${X_BEARER_TOKEN}" \
  'https://api.twitter.com/2/tweets/{TWEET_ID}?tweet.fields=created_at,public_metrics,author_id&expansions=author_id&user.fields=username'
```

### Trends (location-scoped — WOEID)

```bash
curl -s -H "Authorization: Bearer ${X_BEARER_TOKEN}" \
  'https://api.twitter.com/2/trends/by/woeid/{WOEID}'
```

Common WOEIDs: `1` global, `23424977` USA, `23424975` UK, `23424856` Japan, `2487956` San Francisco. Verify the trends endpoint path against the current X API docs — it has moved between revisions.

## Response shape

JSON with a top-level `data` (the requested resource) and `includes` (expanded entities like users, media). Most endpoints support `?expansions=author_id&user.fields=username` to inline author info into the response.

## Notes

- **Cost is per-request**, billed monthly to the developer account. Set a monthly cap in the dashboard before turning the agent loose. ~$0.01 per user-lookup, $0.08 per tweet-batch is a realistic baseline (your tier may differ).
- The endpoint host is `api.twitter.com` — `api.x.com` is also accepted for some endpoints. Stick with `api.twitter.com` unless docs say otherwise.
- Rate limits per endpoint are tighter than they look on PAYG — write your queries narrowly.
