# itamx

CLI for **ITA Matrix** airfare search — the engine behind Google Flights, Kayak, and most fare research — using its reverse-engineered JSON API. No browser automation, no scraping, ~1-second searches.

## Install

```bash
uv tool install /path/to/itamx
# or from source
uv sync && uv run itamx --help
```

## Commands

### `itamx search` — single round-trip or one-way

```bash
# Round-trip (cheapest first)
itamx search SOURCE DESTINATION DEPART_DATE RETURN_DATE

# One-way (omit return)
itamx search SOURCE DESTINATION DEPART_DATE

# Premium cabins
itamx search SOURCE DESTINATION DEPART_DATE RETURN_DATE --cabin BUSINESS
itamx search SOURCE DESTINATION DEPART_DATE RETURN_DATE --cabin PREMIUM_COACH

# Force airline + transit airport (Matrix routing language)
itamx search SOURCE DESTINATION DEPART_DATE RETURN_DATE --airlines AIRLINE --via TRANSIT

# Restrict fare classes (RBD)
itamx search SOURCE DESTINATION DEPART_DATE RETURN_DATE --rbd S,M,H

# Time windows (24-hour, comma-separated for multiple)
itamx search SOURCE DESTINATION DEPART_DATE RETURN_DATE --out-time 0-6 --ret-time 18-24

# Date flexibility (± N days)
itamx search SOURCE DESTINATION DEPART_DATE RETURN_DATE --flex 2

# Stops cap (relative to min for the route)
itamx search SOURCE DESTINATION DEPART_DATE --max-stops 0   # nonstop only

# Pax breakdown
itamx search SOURCE DESTINATION DEPART_DATE RETURN_DATE --adults 2 --children 1 --infants-lap 1

# Show actual fare-class letters (RBD) for cheapest N
itamx search SOURCE DESTINATION DEPART_DATE RETURN_DATE --detail 5

# Raw routing / commandLine for advanced QPX-style queries
itamx search SOURCE DESTINATION DEPART_DATE RETURN_DATE --out-routing "AIRLINE+ TRANSIT" --out-cmd "f bc=B|H"

# Currency / point-of-sale (affects which fares are offered)
itamx search SOURCE DESTINATION DEPART_DATE RETURN_DATE --currency CURRENCY --sales-city SALES_CITY

# Machine-readable
itamx search SOURCE DESTINATION DEPART_DATE RETURN_DATE --output json | jq '.solutions[0]'
itamx search SOURCE DESTINATION DEPART_DATE RETURN_DATE --output csv > prices.csv
itamx search SOURCE DESTINATION DEPART_DATE RETURN_DATE --output raw    # full API response
```

### `itamx flex` — find the cheapest week in a date range

```bash
# Cheapest allowed departure days, overnight outbound via transit, 6-night trip
itamx flex SOURCE DESTINATION START_DATE END_DATE \
    --duration 6 --days SUN,MON \
    --airlines AIRLINE --via TRANSIT --out-time 0-6 --max-stops 1
```

Each candidate departure date is searched in parallel (default 3 concurrent;
`--parallel N` to tune). Output is a price-ranked table.

### `itamx multi` — multi-city / open-jaw

```bash
# Three legs: source → stopover → destination → source
itamx multi \
    --leg SOURCE:STOPOVER:LEG1_DATE \
    --leg STOPOVER:DESTINATION:LEG2_DATE \
    --leg DESTINATION:SOURCE:LEG3_DATE
```

### `itamx lookup` — airport / city autocomplete

```bash
itamx lookup "Tel"          # find IATA codes matching a partial name
itamx lookup "CODE" -o json
```

## How it works

Matrix's web app is a Google "Alkali" mini-app. Search is a single POST to
`content-alkalimatrix-pa.googleapis.com/batch` wrapping a `/v1/search`
JSON-RPC call in `multipart/mixed`. Auth is just a public API key embedded
in the page — **no OAuth, no session cookies, no anti-bot token required**
for search/summarize calls. Detail (RBD) lookups hit `/v1/summarize` with
the `bookingDetails` summarizer.

## Routing-language cheat sheet

The `--via`, `--airlines` and raw `--out-routing` flags drive Matrix's
[RouteLanguage](https://en.wikipedia.org/wiki/QPX_(software)) (a remnant of
the QPX engine):

| Token | Meaning |
|---|---|
| `TRANSIT` | itinerary may transit that airport |
| `TRANSIT+` | itinerary must transit that airport |
| `AIRLINE` | should include that airline |
| `AIRLINE+` | must use that airline |
| `(AIRLINE1\|AIRLINE2)+` | must use one of those airlines |
| `AIRLINE TRANSIT AIRLINE` | force that airline through that transit point |

`--airlines AIRLINE --via TRANSIT` is shorthand for `AIRLINE TRANSIT AIRLINE`.

## Known limits

- Matrix rate limits are undocumented; if Google starts requiring the
  `bgProgramResponse` (WAA) token, we'll need to add token handling.
- Time windows that wrap midnight (e.g. 22:00–02:00) need to be expressed as
  two ranges (`--out-time 22-23,0-2`).

## Re-capturing the API spec

If Matrix changes the request format, run `node /tmp/matrix-cap-node.js` (in
this repo's history) — a Playwright + CDP harness that captures live
request/response bodies for both `/v1/search` and `/v1/summarize`.
