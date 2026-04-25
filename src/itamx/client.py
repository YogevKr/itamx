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
from urllib.parse import quote

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


# Matrix's cabin enum uses a hyphen for Premium Economy ("PREMIUM-COACH") even
# though the rest of the values (COACH, BUSINESS, FIRST) match the standard
# Google/IATA underscore form. We accept both spellings from callers and
# emit the wire form Matrix actually expects.
_CABIN_WIRE = {
    "COACH": "COACH",
    "ECONOMY": "COACH",
    "PREMIUM_COACH": "PREMIUM-COACH",
    "PREMIUM-COACH": "PREMIUM-COACH",
    "PREMIUM_ECONOMY": "PREMIUM-COACH",
    "PREMIUM-ECONOMY": "PREMIUM-COACH",
    "BUSINESS": "BUSINESS",
    "FIRST": "FIRST",
}


def _normalize_cabin(cabin: str) -> str:
    if not cabin:
        return "COACH"
    return _CABIN_WIRE.get(cabin.upper().strip(), cabin)


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
    """One leg of a trip. For one-way: just the outbound. For round-trip: two.

    `origin` and `destination` accept either a single IATA code or a
    comma-separated list (e.g. "JFK,LGA,EWR" or "NYC"). City codes (LON, NYC)
    are interpreted by Matrix to include all metro airports.
    """

    origin: str  # may contain commas for multi-airport
    destination: str  # may contain commas for multi-airport
    date: str  # YYYY-MM-DD
    flex_minus: int = 0  # date flexibility — search this many days earlier
    flex_plus: int = 0  # search this many days later
    is_arrival_date: bool = False
    route_language: str | None = None  # e.g. "LAX" forces transit through LAX
    command_line: str | None = None  # e.g. "f bc=S|M|H" filters fare buckets
    time_ranges: list[tuple[str, str]] = field(default_factory=list)
    # ^ list of (min, max) HH:MM windows the slice's flight must depart within

    @staticmethod
    def _split(codes: str) -> list[str]:
        return [c.strip().upper() for c in codes.split(",") if c.strip()]

    def to_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "origins": self._split(self.origin),
            "destinations": self._split(self.destination),
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
    currency: str | None = None,
    sales_city: str | None = None,
) -> dict[str, Any]:
    """Build the inner JSON-RPC payload for a Matrix /v1/search call.

    `cabin`: COACH, PREMIUM_COACH, BUSINESS, FIRST.
    `max_stops`: number of stops *relative to the route minimum*. 0 = nonstop only,
                 1 = up to 1 extra stop, etc. Matrix calls this maxLegsRelativeToMin.
    `currency`: ISO 4217 code (e.g. "USD", "ILS"). Overrides Matrix's auto-pick.
    `sales_city`: IATA code of the point-of-sale city — affects which fares are
                  offered (some only available from certain origins).
    """
    inputs: dict[str, Any] = {
        "filter": {},
        "page": {"current": 1, "size": page_size},
        "pax": (pax or PaxCount()).to_payload(),
        "slices": [s.to_payload() for s in slices],
        "firstDayOfWeek": "SUNDAY",
        "internalUser": False,
        "sliceIndex": 0,
        "sorts": sorts,
        # Matrix uses 'PREMIUM-COACH' (hyphen) on the wire even though every
        # other docs / UI form is 'PREMIUM_COACH'. Translate quietly.
        "cabin": _normalize_cabin(cabin),
        "maxLegsRelativeToMin": 1 if max_stops is None else max_stops,
        "changeOfAirport": change_of_airport,
        "checkAvailability": True,
    }
    if currency:
        inputs["currency"] = currency
    if sales_city:
        inputs["salesCity"] = sales_city
    return {
        "summarizers": summarizers or DEFAULT_SUMMARIZERS,
        "inputs": inputs,
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
        currency: str | None = None,
        sales_city: str | None = None,
        sorts: str = "default",
    ) -> dict[str, Any]:
        """Run a search. Returns the raw JSON response dict.

        `slices` may have any length: 1 (one-way), 2 (round-trip), or 3+ (multi-city).
        `sorts`: "default" (Matrix's blend), "price", "duration", "departureTime",
                 "arrivalTime". Note: the response always carries enough data to
                 re-sort client-side; this just changes Matrix's chosen ordering.
        """
        body = build_search_body(
            slices=slices,
            pax=pax,
            cabin=cabin,
            max_stops=max_stops,
            page_size=page_size,
            summarizers=summarizers,
            change_of_airport=change_of_airport,
            currency=currency,
            sales_city=sales_city,
            sorts=sorts,
        )
        return self._post_batch(body, "/v1/search")

    def lookup_locations(
        self, partial_name: str, *, page_size: int = 10
    ) -> list[dict[str, Any]]:
        """Resolve a partial city/airport name to candidate locations.

        Hits Matrix's autocomplete endpoint:
            GET /v1/locationTypes/CITIES_AND_AIRPORTS/partialNames/<q>/locations

        Returns a list of dicts with code, displayName, type, cityCode, latLng.
        """
        encoded_name = quote(partial_name, safe="")
        path = (
            f"/v1/locationTypes/CITIES_AND_AIRPORTS/partialNames/"
            f"{encoded_name}/locations?pageSize={page_size}"
        )
        return self._get_batch(path).get("locations", [])

    def _get_batch(self, path: str) -> dict[str, Any]:
        """GET via the same multipart batch envelope (Matrix uses GET inside multipart for autocomplete)."""
        boundary = _boundary()
        body_parts = [
            f"--{boundary}",
            "Content-Type: application/http",
            "Content-Transfer-Encoding: binary",
            f"Content-ID: <{boundary}+gapiRequest@googleapis.com>",
            "",
            f"GET {path}&key={API_KEY}" if "?" in path else f"GET {path}?key={API_KEY}",
            "x-alkali-application-key: applications/matrix",
            "x-alkali-auth-apps-namespace: alkali_v2",
            "x-alkali-auth-entities-namespace: alkali_v2",
            "X-Requested-With: XMLHttpRequest",
            "",
            "",
            f"--{boundary}--",
            "",
        ]
        multipart = "\r\n".join(body_parts).encode("utf-8")
        url = f"{BATCH_URL}?%24ct=multipart%2Fmixed%3B%20boundary%3D{boundary}"
        resp = self._http.post(
            url,
            content=multipart,
            headers={"Content-Type": "text/plain; charset=UTF-8"},
        )
        resp.raise_for_status()
        return _parse_multipart_response(resp.content)

    def detail(
        self,
        search_response: dict[str, Any],
        solution_id: str,
        slices: list[Slice],
        *,
        pax: PaxCount | None = None,
        cabin: str = "COACH",
        max_stops: int | None = None,
        sorts: str = "default",
        change_of_airport: bool = True,
        currency: str | None = None,
        sales_city: str | None = None,
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
                "sorts": sorts,
                "solution": f"{solution_set}/{solution_id}",
                "cabin": _normalize_cabin(cabin),
                "maxLegsRelativeToMin": 1 if max_stops is None else max_stops,
                "changeOfAirport": change_of_airport,
                "checkAvailability": True,
            },
            "summarizerSet": "viewDetails",
            "solutionSet": solution_set,
            "session": session,
        }
        if currency:
            body["inputs"]["currency"] = currency
        if sales_city:
            body["inputs"]["salesCity"] = sales_city
        return self._post_batch(body, "/v1/summarize")
