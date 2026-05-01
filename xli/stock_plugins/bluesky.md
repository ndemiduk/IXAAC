---
id: bluesky
name: Bluesky (atproto)
description: Search posts, look up profiles, browse popular feeds — public reads, no authentication
categories: [social, news]
risk: low
auth_type: none
auth_env_vars: []
---

# Bluesky (atproto)

Bluesky's `public.api.bsky.app` host serves anonymous read access for the headline endpoints. Posts, profiles, feeds — all without auth. Authenticated writes (posting, follows, likes) need an app password / OAuth and are deliberately out of scope here.

## Usage

### Search posts

```bash
curl -s 'https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q={QUERY}&limit=25'
```

Returns `posts[]` with author DID/handle, indexed timestamp, text, embeds, like/repost counts.

### Profile by handle

```bash
curl -s 'https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor={HANDLE}'
```

`{HANDLE}` is `name.bsky.social` form (or any custom domain handle).

### Author's recent posts

```bash
curl -s 'https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor={HANDLE}&limit=25'
```

### Search profiles

```bash
curl -s 'https://public.api.bsky.app/xrpc/app.bsky.actor.searchActors?q={QUERY}&limit=25'
```

### Popular custom feeds

```bash
curl -s 'https://public.api.bsky.app/xrpc/app.bsky.unspecced.getPopularFeedGenerators'
```

Each entry has a `uri` you can pass back to `app.bsky.feed.getFeed?feed=<uri>` to read its posts.

### Trending topics — EXPERIMENTAL

```bash
curl -s 'https://public.api.bsky.app/xrpc/app.bsky.unspecced.getTrendingTopics'
```

⚠ The `app.bsky.unspecced.*` namespace is explicitly experimental and can change/disappear without notice. Works at the time this plugin shipped; verify before depending on it.

## Response shape

Each XRPC endpoint returns a single JSON object. Cursor-paginated endpoints include a `cursor` field — pass it back as `&cursor=...` for the next page.

## Notes

- For private/authenticated endpoints (your own timeline, follow lists requiring permission), you need a Bluesky app password — out of scope for this plugin.
- Handles can change; DIDs (`did:plc:...`) are the stable identifier. Use `getProfile` to map handle ↔ DID.
- Rate limits are unpublished but generous for public reads.
