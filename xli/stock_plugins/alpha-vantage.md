---
id: alpha-vantage
name: Alpha Vantage
description: Stock quotes, time-series, fundamentals, FX, crypto, and technical indicators
categories: [finance, stocks]
risk: low
auth_type: query_param
auth_env_vars:
  - ALPHA_VANTAGE_KEY
---

# Alpha Vantage

Free-tier US/global stocks, FX, crypto, and ~50 technical indicators. Free key at https://www.alphavantage.co/support/#api-key (no email verification, instant). Free limit: 25 calls/day, 5 calls/minute. Premium starts at $50/mo for higher limits.

## Auth setup

```bash
xli auth set alpha-vantage ALPHA_VANTAGE_KEY=<your-key>
```

## Usage

### Latest quote

```bash
curl -s "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={SYMBOL}&apikey=${ALPHA_VANTAGE_KEY}"
```

### Daily time-series (full history)

```bash
curl -s "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol={SYMBOL}&outputsize=compact&apikey=${ALPHA_VANTAGE_KEY}"
```

`outputsize=compact` returns last 100 trading days; `full` returns 20+ years.

### Intraday (1, 5, 15, 30, 60 minute bars)

```bash
curl -s "https://www.alphavantage.co/query?function=TIME_SERIES_INTRADAY&symbol={SYMBOL}&interval=5min&apikey=${ALPHA_VANTAGE_KEY}"
```

### Symbol search

```bash
curl -s "https://www.alphavantage.co/query?function=SYMBOL_SEARCH&keywords={QUERY}&apikey=${ALPHA_VANTAGE_KEY}"
```

### Company overview / fundamentals

```bash
curl -s "https://www.alphavantage.co/query?function=OVERVIEW&symbol={SYMBOL}&apikey=${ALPHA_VANTAGE_KEY}"
```

PE, market cap, EPS, dividend yield, sector, etc. — one big flat object.

### FX rate

```bash
curl -s "https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE&from_currency={FROM}&to_currency={TO}&apikey=${ALPHA_VANTAGE_KEY}"
```

### Technical indicator (e.g. 14-day RSI)

```bash
curl -s "https://www.alphavantage.co/query?function=RSI&symbol={SYMBOL}&interval=daily&time_period=14&series_type=close&apikey=${ALPHA_VANTAGE_KEY}"
```

Other functions: `SMA`, `EMA`, `MACD`, `BBANDS`, `ADX`, `STOCH`, …

## Response shape

JSON. Time-series endpoints nest data under keys like `"Time Series (Daily)"` keyed by date. Quote endpoint returns under `"Global Quote"`. Numbers are strings — cast before doing arithmetic.

## Notes

- **Rate-limit watch:** when you hit the free quota, the API still returns 200 OK but with an `Information` or `Note` field instead of data. Always check for those before parsing.
- **Symbol coverage:** US tickers (`AAPL`, `MSFT`) work directly; non-US use exchange suffix (`.LON`, `.TYO`) or full ISIN search via `SYMBOL_SEARCH`.
- For crypto OHLCV use `DIGITAL_CURRENCY_DAILY`; for forex daily use `FX_DAILY`.
