---
id: coingecko
name: CoinGecko
description: Cryptocurrency prices, market data, history, and exchange info — no API key needed
categories: [finance, crypto]
risk: low
auth_type: none
auth_env_vars: []
---

# CoinGecko

Free tier (the "Demo" plan) is open and unauthenticated, 30 calls/min, no signup. Endpoints below all work without a key. (CoinGecko Pro adds higher limits + extra endpoints; not used here.)

## Usage

### Quick price (one or many coins, one or many vs currencies)

```bash
curl -s 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd,eur'
```

`ids` are CoinGecko's slugs (lowercase, kebab-case) — *not* tickers. `bitcoin`, `ethereum`, `solana`, `dogecoin`, `the-graph`, etc. Use `/search` if uncertain.

### Search by ticker or name

```bash
curl -s 'https://api.coingecko.com/api/v3/search?query={QUERY}'
```

Returns matching coins with their canonical `id`.

### Detailed coin info

```bash
curl -s 'https://api.coingecko.com/api/v3/coins/{COIN_ID}'
```

Includes description, links, market data across many currencies, community/developer scores.

### Historical (daily) over last N days

```bash
curl -s 'https://api.coingecko.com/api/v3/coins/{COIN_ID}/market_chart?vs_currency=usd&days=30'
```

Returns parallel arrays for `prices`, `market_caps`, `total_volumes` — each entry `[unix_ms, value]`.

### Trending (top searches in last 24h)

```bash
curl -s 'https://api.coingecko.com/api/v3/search/trending'
```

### Global market summary

```bash
curl -s 'https://api.coingecko.com/api/v3/global'
```

Total market cap, BTC/ETH dominance, active cryptocurrencies count.

## Response shape

Simple JSON. The `simple/price` endpoint returns `{coin_id: {currency: number}}`. Most other endpoints return rich objects — pipe through `jq` for the specific field.

## Notes

- The free demo tier doesn't carry a stable SLA — occasional 429s under load. Backing off a few seconds usually clears them.
- For seconds-level intraday charts use `market_chart/range?from=&to=` with unix timestamps (granularity adjusts based on the window).
- Coin IDs are stable but new tokens get added daily — `/search` is the safe entry point when unsure.
