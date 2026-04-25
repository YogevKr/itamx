"""HTTP client for ITA Matrix search API.

Matrix is a Google "Alkali" framework app. Single POST to
content-alkalimatrix-pa.googleapis.com/batch wraps an inner JSON-RPC call in
multipart/mixed format. Auth is just a public API key embedded in the page —
no OAuth, no anti-bot tokens needed for search/summarize calls.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
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


def _multipart_request(
    inner_body: dict[str, Any], boundary: str, *, path: str = "/v1/search"
) -> bytes:
    """Wrap a JSON-RPC call in the multipart/mixed batch envelope Matrix expects."""
    body_json = json.dumps(inner_body, separators=(",", ":"))
    parts = [
        f"--{boundary}",
        "Content-Type: application/http",
        "Content-Transfer-Encoding: binary",
        f"Content-ID: <{boundary}+gapiRequest@googleapis.com>",
        "",
        f"POST {path}?key={API_KEY}&alt=json",
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
    return json.loads(text[start : end + 1])


@dataclass
class Slice:
    """One leg of a trip. For one-way: just the outbound. For round-trip: two."""

    origin: str
    destination: str
    date: str  # YYYY-MM-DD
    flex_minus: int = 0  # date flexibility — search this many days earlier
    flex_plus: int = 0  # search this many days later
    is_arrival_date: bool = False
    route_language: str | None = None  # e.g. "LAX" forces transit through LAX
    command_line: str | None = None  # e.g. "f bc=S|M|H" filters fare buckets
    time_ranges: list[tuple[str, str]] = field(default_factory=list)
    # ^ list of (min, max) HH:MM windows the slice's flight must depart within

    def to_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "origins": [self.origin],
            "destinations": [self.destination],
            "date": self.date,
            "dateModifier": {"minus": self.flex_minus, "plus": self.flex_plus},
            "isArrivalDate": self.is_arrival_date,
            "filter": {"warnings": {"values": []}},
            "selected": False,
        }
        if self.route_language:
            out["routeLanguage"] = self.route_language
        if self.command_line:
            out["commandLine"] = self.command_line
        if self.time_ranges:
            out["timeRanges"] = [{"min": a, "max": b} for a, b in self.time_ranges]
        return out


@dataclass
class PaxCount:
    adults: int = 1
    seniors: int = 0
    youths: int = 0
    children: int = 0
    infants_in_seat: int = 0
    infants_in_lap: int = 0

    def to_payload(self) -> dict[str, int]:
        out: dict[str, int] = {"adults": self.adults}
        if self.seniors:
            out["seniors"] = self.seniors
        if self.youths:
            out["youths"] = self.youths
        if self.children:
            out["children"] = self.children
        if self.infants_in_seat:
            out["infantsInSeat"] = self.infants_in_seat
        if self.infants_in_lap:
            out["infantsInLap"] = self.infants_in_lap
        return out


def build_search_body(
    slices: list[Slice],
    *,
    pax: PaxCount | None = None,
    cabin: str = "COACH",
    max_stops: int | None = None,
    page_size: int = 50,
    summarizers: list[str] | None = None,
    sorts: str = "default",
    change_of_airport: bool = True,
) -> dict[str, Any]:
    """Build the inner JSON-RPC payload for a Matrix /v1/search call.

    `cabin`: COACH, PREMIUM_COACH, BUSINESS, FIRST.
    `max_stops`: number of stops *relative to the route minimum*. 0 = nonstop only,
                 1 = up to 1 extra stop, etc. Matrix calls this maxLegsRelativeToMin.
    """
    return {
        "summarizers": summarizers or DEFAULT_SUMMARIZERS,
        "inputs": {
            "filter": {},
            "page": {"current": 1, "size": page_size},
            "pax": (pax or PaxCount()).to_payload(),
            "slices": [s.to_payload() for s in slices],
            "firstDayOfWeek": "SUNDAY",
            "internalUser": False,
            "sliceIndex": 0,
            "sorts": sorts,
            "cabin": cabin,
            "maxLegsRelativeToMin": 1 if max_stops is None else max_stops,
            "changeOfAirport": change_of_airport,
            "checkAvailability": True,
        },
        "summarizerSet": "wholeTrip",
        "name": "specificDatesSlice",
    }


class MatrixClient:
    """Synchronous client for ITA Matrix search.

    Each call is a single HTTPS round-trip. No token juggling, no cookies —
    just the public API key embedded in the page.
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

    def _post_batch(self, body: dict[str, Any], path: str) -> dict[str, Any]:
        boundary = _boundary()
        multipart = _multipart_request(body, boundary, path=path)
        url = f"{BATCH_URL}?%24ct=multipart%2Fmixed%3B%20boundary%3D{boundary}"
        resp = self._http.post(
            url,
            content=multipart,
            headers={"Content-Type": "text/plain; charset=UTF-8"},
        )
        resp.raise_for_status()
        return _parse_multipart_response(resp.content)

    def search(
        self,
        slices: list[Slice],
        *,
        pax: PaxCount | None = None,
        cabin: str = "COACH",
        max_stops: int | None = None,
        page_size: int = 50,
        summarizers: list[str] | None = None,
        change_of_airport: bool = True,
    ) -> dict[str, Any]:
        """Run a search. Returns the raw JSON response dict."""
        body = build_search_body(
            slices=slices,
            pax=pax,
            cabin=cabin,
            max_stops=max_stops,
            page_size=page_size,
            summarizers=summarizers,
            change_of_airport=change_of_airport,
        )
        return self._post_batch(body, "/v1/search")

    def detail(
        self,
        search_response: dict[str, Any],
        solution_id: str,
        slices: list[Slice],
        *,
        pax: PaxCount | None = None,
        cabin: str = "COACH",
    ) -> dict[str, Any]:
        """Fetch booking details (incl. fare bookingCode) for one solution."""
        solution_set = search_response.get("solutionSet")
        session = search_response.get("session")
        if not solution_set or not session:
            raise ValueError("search_response missing solutionSet/session")

        body = {
            "summarizers": ["bookingDetails"],
            "inputs": {
                "filter": {},
                "page": {"current": 1, "size": 25},
                "pax": (pax or PaxCount()).to_payload(),
                "slices": [s.to_payload() for s in slices],
                "firstDayOfWeek": "SUNDAY",
                "internalUser": False,
                "sliceIndex": 0,
                "sorts": "default",
                "solution": f"{solution_set}/{solution_id}",
                "cabin": cabin,
                "maxLegsRelativeToMin": 1,
                "changeOfAirport": True,
                "checkAvailability": True,
            },
            "summarizerSet": "viewDetails",
            "solutionSet": solution_set,
            "session": session,
        }
        return self._post_batch(body, "/v1/summarize")
