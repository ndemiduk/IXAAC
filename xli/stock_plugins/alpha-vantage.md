---
id: alpha-vantage
name: Alpha Vantage
description: Stock quotes, time-series, fundamentals, FX, crypto, and technical indicators
categories: [finance, stocks]
risk: low
effect: read-only
trust: subscription
auth_type: query_param
auth_env_vars:
  - ALPHA_VANTAGE_KEY
actions:
  - id: quote
    description: Latest quote for a stock symbol
    method: GET
    url: https://www.alphavantage.co/query
    params:
      function: {const: "GLOBAL_QUOTE"}
      symbol: {required: true, description: "Ticker symbol (e.g. AAPL, MSFT, .LON suffix for non-US)"}
      apikey: {const: "${ALPHA_VANTAGE_KEY}"}
    response_shape: ".['Global Quote'] → {symbol, open, high, low, price, volume, change, change_percent}"
  - id: daily_series
    description: Daily OHLCV time series for a stock
    method: GET
    url: https://www.alphavantage.co/query
    params:
      function: {const: "TIME_SERIES_DAILY"}
      symbol: {required: true, description: "Ticker symbol"}
      outputsize: {default: "compact", enum: ["compact", "full"], description: "compact=100 days, full=20+ years"}
      apikey: {const: "${ALPHA_VANTAGE_KEY}"}
    response_shape: ".['Time Series (Daily)'] → {date: {open, high, low, close, volume}}"
  - id: intraday
    description: Intraday bars (1/5/15/30/60 min) for a stock
    method: GET
    url: https://www.alphavantage.co/query
    params:
      function: {const: "TIME_SERIES_INTRADAY"}
      symbol: {required: true, description: "Ticker symbol"}
      interval: {required: true, enum: ["1min", "5min", "15min", "30min", "60min"]}
      apikey: {const: "${ALPHA_VANTAGE_KEY}"}
  - id: symbol_search
    description: Search for a stock symbol by keyword
    method: GET
    url: https://www.alphavantage.co/query
    params:
      function: {const: "SYMBOL_SEARCH"}
      keywords: {required: true, description: "Search keywords (company name, partial ticker)"}
      apikey: {const: "${ALPHA_VANTAGE_KEY}"}
    response_shape: ".bestMatches[] → {symbol, name, type, region, currency}"
  - id: company_overview
    description: Fundamentals — PE, market cap, EPS, dividend yield, sector
    method: GET
    url: https://www.alphavantage.co/query
    params:
      function: {const: "OVERVIEW"}
      symbol: {required: true, description: "Ticker symbol"}
      apikey: {const: "${ALPHA_VANTAGE_KEY}"}
  - id: fx_rate
    description: Real-time FX exchange rate between two currencies
    method: GET
    url: https://www.alphavantage.co/query
    params:
      function: {const: "CURRENCY_EXCHANGE_RATE"}
      from_currency: {required: true, description: "Source currency code (USD, EUR, BTC, etc.)"}
      to_currency: {required: true, description: "Target currency code"}
      apikey: {const: "${ALPHA_VANTAGE_KEY}"}
  - id: technical_indicator
    description: Technical indicator (RSI, SMA, EMA, MACD, BBANDS, etc.)
    method: GET
    url: https://www.alphavantage.co/query
    params:
      function: {required: true, description: "Indicator name: RSI, SMA, EMA, MACD, BBANDS, ADX, STOCH, etc."}
      symbol: {required: true, description: "Ticker symbol"}
      interval: {default: "daily", enum: ["1min", "5min", "15min", "30min", "60min", "daily", "weekly", "monthly"]}
      time_period: {default: "14", description: "Number of data points for the indicator"}
      series_type: {default: "close", enum: ["close", "open", "high", "low"]}
      apikey: {const: "${ALPHA_VANTAGE_KEY}"}
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
