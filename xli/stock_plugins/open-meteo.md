---
id: open-meteo
name: Open-Meteo
description: Free weather forecasts and current conditions, no API key required
categories: [weather]
risk: low
effect: read-only
trust: subscription
auth_type: none
auth_env_vars: []
actions:
  - id: geocode
    description: Look up latitude/longitude for a city name (e.g. 'Aurora, IL' or 'London')
    method: GET
    url: https://geocoding-api.open-meteo.com/v1/search
    params:
      name: {required: true, description: "City name (e.g. 'Aurora, IL'), auto URL-encoded"}
      count: {default: "1", description: "Number of results (1-10)"}
    response_shape: ".results[0] → {latitude, longitude, name, country, timezone}"
  - id: current_weather
    description: Current weather + hourly forecast for lat/lon (use geocode first for cities)
    method: GET
    url: https://api.open-meteo.com/v1/forecast
    params:
      latitude: {required: true, description: "Decimal degrees (-90 to 90), e.g. 41.77"}
      longitude: {required: true, description: "Decimal degrees (-180 to 180), e.g. -88.32"}
      current_weather: {const: "true"}
      hourly: {default: "temperature_2m,precipitation_probability,wind_speed_10m"}
      timezone: {default: "auto"}
    response_shape: ".current_weather → {temperature_2m, windspeed_10m, weathercode, ...}; .hourly → arrays"
  - id: daily_forecast
    description: Daily forecast (next 7 days) for a location
    method: GET
    url: https://api.open-meteo.com/v1/forecast
    params:
      latitude: {required: true, description: "Decimal degrees, negative = south"}
      longitude: {required: true, description: "Decimal degrees, negative = west"}
      daily: {default: "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max"}
      timezone: {default: "auto"}
    response_shape: ".daily → parallel arrays of date, temp_max, temp_min, precip, wind"
---

# Open-Meteo

Free weather API. No signup, no key, generous free-tier limits (10k calls/day for non-commercial use). Two endpoints worth knowing: a geocoding service (city name → lat/lon) and the forecast itself.

## Usage

### Look up coords for a city

```bash
curl -s 'https://geocoding-api.open-meteo.com/v1/search?name={CITY}&count=1' | jq '.results[0]'
```

`{CITY}` URL-encoded city name (`Tokyo`, `Seattle, US`, etc.). Response includes `latitude`, `longitude`, `country`, `timezone`.

### Current weather + hourly forecast

```bash
curl -s 'https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&current_weather=true&hourly=temperature_2m,precipitation_probability,wind_speed_10m&timezone=auto'
```

### Daily forecast (next 7 days)

```bash
curl -s 'https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max&timezone=auto'
```

### Combined geocode-then-forecast (typical agent flow)

Always check that geocoding actually returned something before chaining into the forecast — `aurora illinois` (lowercase, no comma) sometimes returns zero results, and a missing-result `null` would silently produce a broken `latitude=&longitude=` URL.

```bash
GEO=$(curl -s "https://geocoding-api.open-meteo.com/v1/search?name={CITY}&count=1")
LAT=$(echo "$GEO" | jq -r '.results[0].latitude // empty')
LON=$(echo "$GEO" | jq -r '.results[0].longitude // empty')
if [ -z "$LAT" ] || [ -z "$LON" ]; then
  echo "no geocoding match for {CITY} — try a different spelling like 'Aurora, IL'" >&2
  exit 1
fi
curl -s "https://api.open-meteo.com/v1/forecast?latitude=${LAT}&longitude=${LON}&current_weather=true&timezone=auto"
```

If the city is ambiguous (multiple "Springfield"s), bump `count=10` on the geocoding call and inspect `.results[]` to disambiguate by `admin1`, `country`, or population before picking a row.

## Response shape

JSON. `current_weather` has `temperature`, `windspeed`, `winddirection`, `weathercode`. Hourly/daily come as parallel arrays under `hourly` / `daily`. `weathercode` follows WMO codes (0=clear, 1-3=mostly clear/cloudy, 45-48=fog, 51-67=rain, 71-77=snow, 80-99=showers/storms).

## Notes

- All lat/lon are decimal degrees, not DMS. Negative = south/west.
- `timezone=auto` makes timestamps local to the queried location — much easier to interpret than UTC.
- Commercial use requires the paid tier; everything above is free.
