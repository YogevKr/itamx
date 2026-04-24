# itamx

CLI for **ITA Matrix** airfare search — the fare-shopping engine behind Google Flights, Kayak, and others — using its reverse-engineered JSON API. No browser automation, no scraping, ~1-second searches.

## Install

```bash
uv tool install /path/to/itamx
# or from source
uv sync && uv run itamx --help
```

## Use

```bash
# Round-trip
itamx SOURCE DESTINATION DEPART_DATE RETURN_DATE

# Premium cabins
itamx SOURCE DESTINATION DEPART_DATE RETURN_DATE --cabin BUSINESS
itamx SOURCE DESTINATION DEPART_DATE RETURN_DATE --cabin PREMIUM_COACH

# Nonstop only (relative to the min-stop count for the route)
itamx SOURCE DESTINATION DEPART_DATE RETURN_DATE --max-stops 0

# Machine-readable
itamx SOURCE DESTINATION DEPART_DATE RETURN_DATE --output json | jq '.solutionList.solutions[0]'
itamx SOURCE DESTINATION DEPART_DATE RETURN_DATE --output raw    # unmodified API response
```

## How it works

Matrix's web app is a Google "Alkali" mini-app that POSTs a `multipart/mixed`
batch to `content-alkalimatrix-pa.googleapis.com/batch`. Inside the multipart
envelope is a single JSON-RPC call to `/v1/search`. Authentication is just a
public API key baked into the page — **no OAuth, no session cookies, no
anti-bot token required** for search calls.

This CLI constructs that envelope directly in Python and parses the JSON
response.

## Known limits

- Fare class / RBD letter (e.g. "Classic S") is surfaced in per-solution
  detail pages on matrix.itasoftware.com. We don't request that endpoint yet.
- No airport autocomplete — you must pass IATA codes.
- Matrix rate limits are unknown; the web app fires ~20 batch calls on page load.
