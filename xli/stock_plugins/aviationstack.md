---
id: aviationstack
name: AviationStack
description: Flight status, schedules, airport and airline metadata — by flight number, route, or IATA code
categories: [travel, flights]
risk: low
auth_type: query_param
auth_env_vars:
  - AVIATIONSTACK_KEY
---

# AviationStack

Flight status by flight number — covers "is BA286 on time?" use case directly. Free key at https://aviationstack.com/signup/free (free tier: 100 calls/month). Paid tiers raise limits and add HTTPS.

## ⚠ HTTPS gotcha

The **free tier is HTTP-only** — your API key travels in cleartext. Keys are easy to rotate via the dashboard, but treat the free tier as low-stakes only; for anything you'd be sad to leak, use the paid tier (which exposes the same endpoints over `https://`).

## Auth setup

```bash
xli auth set aviationstack AVIATIONSTACK_KEY=<your-key>
```

## Usage

### By IATA flight number

```bash
curl -s "http://api.aviationstack.com/v1/flights?access_key=${AVIATIONSTACK_KEY}&flight_iata={FLIGHT_IATA}"
```

E.g. `BA286`, `UA123`, `JL45`. Returns scheduled/actual departure + arrival, status (`scheduled`, `active`, `landed`, `cancelled`, `diverted`), aircraft, terminals, gates.

### By route (IATA airport codes)

```bash
curl -s "http://api.aviationstack.com/v1/flights?access_key=${AVIATIONSTACK_KEY}&dep_iata={ORIGIN}&arr_iata={DEST}"
```

E.g. `dep_iata=SFO&arr_iata=JFK` for all SFO→JFK flights today.

### By airline + date

```bash
curl -s "http://api.aviationstack.com/v1/flights?access_key=${AVIATIONSTACK_KEY}&airline_iata={AIRLINE}&flight_date={YYYY-MM-DD}"
```

### Airport metadata (lookup by IATA)

```bash
curl -s "http://api.aviationstack.com/v1/airports?access_key=${AVIATIONSTACK_KEY}&iata_code={IATA}"
```

Coordinates, country, timezone, ICAO code.

### Airline metadata

```bash
curl -s "http://api.aviationstack.com/v1/airlines?access_key=${AVIATIONSTACK_KEY}&iata_code={IATA}"
```

## Response shape

```json
{
  "pagination": { "limit": 100, "offset": 0, "count": 1, "total": 1 },
  "data": [
    {
      "flight_date": "2026-04-30",
      "flight_status": "active",
      "departure": { "airport": "...", "iata": "SFO", "scheduled": "...", "actual": "..." },
      "arrival":   { "airport": "...", "iata": "JFK", "scheduled": "...", "estimated": "..." },
      "airline":   { "name": "United Airlines", "iata": "UA", "icao": "UAL" },
      "flight":    { "number": "123", "iata": "UA123", "icao": "UAL123" },
      "aircraft":  { "registration": "...", "iata": "B789", "icao": "B789" },
      "live":      { "updated": "...", "latitude": 39.1, "longitude": -94.5, "altitude": 11280, "speed_horizontal": 850, "is_ground": false }
    }
  ]
}
```

## Notes

- "On time" judgment: compare `arrival.scheduled` vs `arrival.estimated` (or `actual` once landed). Most airlines report a 15-minute threshold internally.
- The `live` block populates only for in-flight flights with ADS-B coverage; ground flights and cancelled flights have no `live` data.
- Free tier covers ~last day or two of historical depth; deeper history needs a paid tier.
