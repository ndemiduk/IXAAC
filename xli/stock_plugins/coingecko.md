---
id: coingecko
name: CoinGecko
description: Cryptocurrency prices, market data, history, and exchange info — no API key needed
categories: [finance, crypto]
risk: low
effect: read-only
trust: subscription
auth_type: none
auth_env_vars: []
actions:
  - id: price
    description: Quick price lookup for one or more coins in one or more currencies
    method: GET
    url: https://api.coingecko.com/api/v3/simple/price
    params:
      ids: {required: true, description: "Comma-separated CoinGecko slugs (bitcoin, ethereum, solana)"}
      vs_currencies: {default: "usd", description: "Comma-separated currency codes (usd, eur, btc)"}
    response_shape: "{coin_id: {currency: number}}"
  - id: search
    description: Search coins by ticker or name to find the canonical CoinGecko id
    method: GET
    url: https://api.coingecko.com/api/v3/search
    params:
      query: {required: true, description: "Ticker or coin name"}
    response_shape: ".coins[] → {id, name, symbol, market_cap_rank}"
  - id: coin_detail
    description: Detailed info for a coin — description, links, market data
    method: GET
    url: https://api.coingecko.com/api/v3/coins/{coin_id}
    params:
      coin_id: {required: true, description: "CoinGecko slug (e.g. bitcoin)"}
  - id: market_chart
    description: Historical price/volume over last N days
    method: GET
    url: https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart
    params:
      coin_id: {required: true, description: "CoinGecko slug"}
      vs_currency: {default: "usd"}
      days: {required: true, description: "Number of days (1, 7, 30, 90, 365, max)"}
    response_shape: ".prices[] → [unix_ms, value]; .market_caps[]; .total_volumes[]"
  - id: trending
    description: Top trending coin searches in the last 24 hours
    method: GET
    url: https://api.coingecko.com/api/v3/search/trending
    params: {}
  - id: global
    description: Global crypto market summary — total market cap, BTC dominance
    method: GET
    url: https://api.coingecko.com/api/v3/global
    params: {}
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
