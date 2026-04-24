"""HTTP client for ITA Matrix search API.

Matrix uses Google's Alkali framework: single POST to
content-alkalimatrix-pa.googleapis.com/batch wrapping an inner JSON-RPC call
in multipart/mixed format. Public API key embedded in the page.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import httpx

API_KEY = "AIza<redacted-public-key>"
BATCH_URL = "https://content-alkalimatrix-pa.googleapis.com/batch"
ORIGIN = "https://matrix.itasoftware.com"

DEFAULT_SUMMARIZERS = [
    "carrierStopMatrix",
    "currencyNotice",
    "solutionList",
    "itineraryPriceSlider",
    "itineraryCarrierList",
    "itineraryDepartureTimeRanges",
    "itineraryArrivalTimeRanges",
    "durationSliderItinerary",
    "itineraryOrigins",
    "itineraryDestinations",
    "itineraryStopCountList",
    "warningsItinerary",
]

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)


def _boundary() -> str:
    return f"batch{secrets.randbelow(10**19)}"


def _multipart_request(inner_body: dict[str, Any], boundary: str) -> bytes:
    """Wrap a JSON-RPC call in the multipart/mixed batch envelope Matrix expects."""
    body_json = json.dumps(inner_body, separators=(",", ":"))
    parts = [
        f"--{boundary}",
        "Content-Type: application/http",
        "Content-Transfer-Encoding: binary",
        f"Content-ID: <{boundary}+gapiRequest@googleapis.com>",
        "",
        f"POST /v1/search?key={API_KEY}&alt=json",
        "x-alkali-application-key: applications/matrix",
        "x-alkali-auth-apps-namespace: alkali_v2",
        "x-alkali-auth-entities-namespace: alkali_v2",
        "X-Requested-With: XMLHttpRequest",
        "Content-Type: application/json",
        "",
        body_json,
        f"--{boundary}--",
        "",
    ]
    return "\r\n".join(parts).encode("utf-8")


def _parse_multipart_response(raw: bytes) -> dict[str, Any]:
    """Extract the inner JSON body from the multipart/mixed response."""
    text = raw.decode("utf-8", errors="replace")
    start = text.find("{")
    if start < 0:
        raise ValueError(f"No JSON found in response: {text[:500]!r}")
    end = text.rfind("}")
    if end < start:
        raise ValueError("Malformed JSON body in response")
    # The JSON sits between the inner HTTP headers and the closing boundary.
    # rfind('}') gives us the end of the outermost object.
    candidate = text[start : end + 1]
    return json.loads(candidate)


def build_search_body(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: str | None = None,
    adults: int = 1,
    cabin: str = "COACH",
    max_stops: int | None = None,
    summarizers: list[str] | None = None,
    page_size: int = 50,
) -> dict[str, Any]:
    """Build the inner JSON-RPC payload for a Matrix search.

    Dates in YYYY-MM-DD. Cabin is one of COACH, PREMIUM_COACH, BUSINESS, FIRST.
    """

    def slice_(o: str, d: str, date: str) -> dict[str, Any]:
        return {
            "origins": [o],
            "destinations": [d],
            "date": date,
            "dateModifier": {"minus": 0, "plus": 0},
            "isArrivalDate": False,
            "filter": {"warnings": {"values": []}},
            "selected": False,
        }

    slices = [slice_(origin, destination, depart_date)]
    if return_date:
        slices.append(slice_(destination, origin, return_date))

    return {
        "summarizers": summarizers or DEFAULT_SUMMARIZERS,
        "inputs": {
            "filter": {},
            "page": {"current": 1, "size": page_size},
            "pax": {"adults": adults},
            "slices": slices,
            "firstDayOfWeek": "SUNDAY",
            "internalUser": False,
            "sliceIndex": 0,
            "sorts": "default",
            "cabin": cabin,
            "maxLegsRelativeToMin": 0 if max_stops is None else max_stops,
            "changeOfAirport": True,
            "checkAvailability": True,
        },
        "summarizerSet": "wholeTrip",
        "name": "specificDatesSlice",
    }


class MatrixClient:
    """Synchronous client for ITA Matrix search.

    Each `.search()` call is a single HTTPS round-trip. No token juggling,
    no cookies required — the API is currently keyed only by the embedded
    public API key.
    """

    def __init__(
        self,
        *,
        timeout: float = 90.0,
        user_agent: str = DEFAULT_UA,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._timeout = timeout
        self._ua = user_agent
        self._http = http_client or httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": user_agent,
                "Origin": ORIGIN,
                "Referer": ORIGIN + "/",
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    def __enter__(self) -> MatrixClient:
        return self

    def __exit__(self, *_: object) -> None:
        self._http.close()

    def search(
        self,
        origin: str,
        destination: str,
        depart_date: str,
        return_date: str | None = None,
        *,
        adults: int = 1,
        cabin: str = "COACH",
        max_stops: int | None = None,
        page_size: int = 50,
        summarizers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run a search. Returns the raw JSON response dict."""
        body = build_search_body(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            return_date=return_date,
            adults=adults,
            cabin=cabin,
            max_stops=max_stops,
            page_size=page_size,
            summarizers=summarizers,
        )
        boundary = _boundary()
        multipart = _multipart_request(body, boundary)
        url = (
            f"{BATCH_URL}?%24ct=multipart%2Fmixed%3B%20boundary%3D{boundary}"
        )
        resp = self._http.post(
            url,
            content=multipart,
            headers={"Content-Type": "text/plain; charset=UTF-8"},
        )
        resp.raise_for_status()
        return _parse_multipart_response(resp.content)
