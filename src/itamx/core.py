"""Core Matrix search services shared by CLI, MCP, and tests."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from itamx import airlines as airline_db
from itamx.client import MatrixClient, PaxCount, Slice
from itamx.models import SearchResponse, Solution
from itamx.render import price_float
from itamx.request_options import SearchOptions
from itamx.search_builder import build_pax_count, build_routing, build_trip_slices
from itamx.validation import parse_time_ranges, parse_weekdays

ClientFactory = Callable[[], MatrixClient]

VALID_CABINS = {"COACH", "PREMIUM_COACH", "BUSINESS", "FIRST"}
VALID_SORTS = {"default", "price", "duration", "departureTime", "arrivalTime"}


class FlightSearchParams(BaseModel):
    """Parameters for a specific-date Matrix flight search."""

    source: str = Field(description="Source IATA city or airport code")
    destination: str = Field(description="Destination IATA city or airport code")
    depart_date: str = Field(description="Outbound travel date in YYYY-MM-DD format")
    return_date: str | None = Field(None, description="Return date for round trips")
    cabin: str = Field("COACH", description="COACH, PREMIUM_COACH, BUSINESS, or FIRST")
    max_stops: int | None = Field(
        None,
        ge=0,
        description="Matrix max stops relative to route minimum",
    )
    airlines: list[str] | None = Field(None, description="Airline IATA codes or names")
    via: str | None = Field(None, description="Transit airport for both directions")
    outbound_time: str | None = Field(None, description="Outbound time window, e.g. '6-12'")
    return_time: str | None = Field(None, description="Return time window, e.g. '18-24'")
    flex_days: int = Field(0, ge=0, le=7, description="Date flexibility on each side")
    adults: int = Field(1, ge=1, le=9)
    seniors: int = Field(0, ge=0, le=9)
    youths: int = Field(0, ge=0, le=9)
    children: int = Field(0, ge=0, le=9)
    infants_seat: int = Field(0, ge=0, le=9)
    infants_lap: int = Field(0, ge=0, le=9)
    currency: str | None = Field(None, description="ISO 4217 currency code")
    sales_city: str | None = Field(None, description="IATA city code for point of sale")
    sort: str = Field("price", description="default, price, duration, departureTime, or arrivalTime")
    limit: int = Field(10, ge=1, le=100, description="Maximum serialized solutions")


class FlightDetailParams(FlightSearchParams):
    """Parameters for searching and expanding one Matrix solution."""

    rank: int = Field(1, ge=1, description="Rank to expand after applying the requested sort")


class DateSearchParams(BaseModel):
    """Parameters for scanning multiple Matrix departure dates."""

    source: str = Field(description="Source IATA city or airport code")
    destination: str = Field(description="Destination IATA city or airport code")
    start_date: str = Field(description="First outbound date in YYYY-MM-DD format")
    end_date: str = Field(description="Last outbound date in YYYY-MM-DD format")
    duration_days: int = Field(
        0,
        ge=0,
        le=60,
        description="Round-trip stay length; 0 means one-way",
    )
    departure_weekdays: list[str] | None = Field(None, description="Optional weekday filters")
    cabin: str = Field("COACH", description="COACH, PREMIUM_COACH, BUSINESS, or FIRST")
    max_stops: int | None = Field(None, ge=0)
    airlines: list[str] | None = None
    via: str | None = None
    outbound_time: str | None = None
    adults: int = Field(1, ge=1, le=9)
    currency: str | None = None
    sales_city: str | None = None
    sort: str = Field("price", description="default, price, duration, departureTime, or arrivalTime")
    limit: int = Field(20, ge=1, le=100, description="Maximum date results")


class LookupParams(BaseModel):
    """Parameters for Matrix location autocomplete."""

    query: str = Field(description="Partial city, airport name, or IATA code")
    limit: int = Field(10, ge=1, le=50)


class AirlineLookupParams(BaseModel):
    """Parameters for airline code/name lookup."""

    query: str | None = Field(None, description="Carrier name, IATA code, ICAO code, or callsign")
    limit: int = Field(25, ge=1, le=2000)


def validate_cabin(cabin: str) -> str:
    value = cabin.upper()
    if value not in VALID_CABINS:
        raise ValueError(f"cabin must be one of {', '.join(sorted(VALID_CABINS))}")
    return value


def validate_sort(sort: str) -> str:
    if sort not in VALID_SORTS:
        raise ValueError(f"sort must be one of {', '.join(sorted(VALID_SORTS))}")
    return sort


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"date must be YYYY-MM-DD (got {value!r})")


def serialize_solution(solution: Solution) -> dict[str, Any]:
    slices = []
    for slice_ in solution.itinerary.slices:
        slices.append(
            {
                "source": slice_.origin.code,
                "destination": slice_.destination.code,
                "departure": slice_.departure,
                "arrival": slice_.arrival,
                "flights": slice_.flights,
                "cabins": slice_.cabins,
                "duration_minutes": slice_.duration,
                "stops": max(0, len(slice_.flights) - 1),
            }
        )

    return {
        "id": solution.id,
        "price": solution.displayTotal,
        "price_value": price_float(solution.displayTotal),
        "carriers": [carrier.code for carrier in solution.itinerary.carriers],
        "duration_minutes": sum(slice_.duration for slice_ in solution.itinerary.slices),
        "slices": slices,
    }


def _solution_sort_key(solution: Solution, sort: str) -> tuple[bool, float | str]:
    if sort == "default":
        return (False, "")
    if sort == "duration":
        return (False, sum(slice_.duration for slice_ in solution.itinerary.slices))
    if sort == "departureTime":
        departure = solution.itinerary.slices[0].departure if solution.itinerary.slices else ""
        return (not bool(departure), departure)
    if sort == "arrivalTime":
        arrival = solution.itinerary.slices[-1].arrival if solution.itinerary.slices else ""
        return (not bool(arrival), arrival)
    price = price_float(solution.displayTotal)
    return (price is None, price if price is not None else float("inf"))


def sorted_solutions(
    raw: dict[str, Any],
    *,
    sort: str = "price",
) -> tuple[SearchResponse, list[Solution]]:
    parsed = SearchResponse.model_validate(raw)
    sort = validate_sort(sort)
    if sort == "default":
        return parsed, list(parsed.solutionList.solutions)

    solutions = sorted(
        parsed.solutionList.solutions,
        key=lambda solution: _solution_sort_key(solution, sort),
    )
    return parsed, solutions


def _search_with_client(
    client: MatrixClient,
    slices: list[Slice],
    pax: PaxCount,
    options: SearchOptions,
) -> dict[str, Any]:
    return client.search(slices=slices, pax=pax, **options.search_kwargs())


def _flight_request_parts(
    params: FlightSearchParams,
    *,
    page_size: int | None = None,
) -> tuple[list[Slice], PaxCount, SearchOptions]:
    parse_date(params.depart_date)
    if params.return_date:
        parse_date(params.return_date)
    cabin = validate_cabin(params.cabin)
    sort = validate_sort(params.sort)
    routing = build_routing(params.airlines, params.via)
    slices = build_trip_slices(
        origin=params.source.upper(),
        destination=params.destination.upper(),
        depart=params.depart_date,
        ret=params.return_date,
        flex=params.flex_days,
        outbound_routing=routing,
        return_routing=routing,
        outbound_time_ranges=parse_time_ranges(params.outbound_time),
        return_time_ranges=parse_time_ranges(params.return_time),
    )
    pax = build_pax_count(
        adults=params.adults,
        seniors=params.seniors,
        youths=params.youths,
        children=params.children,
        infants_seat=params.infants_seat,
        infants_lap=params.infants_lap,
    )
    options = SearchOptions(
        cabin=cabin,
        max_stops=params.max_stops,
        page_size=page_size or max(params.limit, 1),
        sorts=sort,
        currency=params.currency,
        sales_city=params.sales_city,
    )
    return slices, pax, options


def execute_flight_search(
    params: FlightSearchParams,
    *,
    client_factory: ClientFactory = MatrixClient,
) -> dict[str, Any]:
    try:
        slices, pax, options = _flight_request_parts(params)
        with client_factory() as client:
            raw = _search_with_client(client, slices, pax, options)

        parsed, solutions = sorted_solutions(raw, sort=params.sort)
        serialized = [serialize_solution(solution) for solution in solutions[: params.limit]]
        return {
            "success": True,
            "trip_type": "ROUND_TRIP" if params.return_date else "ONE_WAY",
            "count": len(serialized),
            "total_count": parsed.solutionCount or len(solutions),
            "solutions": serialized,
        }
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "trip_type": "ROUND_TRIP" if params.return_date else "ONE_WAY",
            "count": 0,
            "solutions": [],
        }


def _aircraft_for_segment(segment: dict[str, Any]) -> str | None:
    for leg in segment.get("legs", []):
        aircraft = leg.get("aircraft", {})
        if aircraft.get("shortName"):
            return aircraft["shortName"]
    return None


def serialize_booking_detail(
    booking: dict[str, Any] | None,
    *,
    fallback_solution: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    booking_slices = (booking or {}).get("itinerary", {}).get("slices", [])
    fallback_slices = (fallback_solution or {}).get("itinerary", {}).get("slices", [])
    source_slices = booking_slices or fallback_slices

    out = []
    for index, slice_ in enumerate(source_slices):
        segments = []
        for segment in slice_.get("segments", []):
            booking_infos = segment.get("bookingInfos", [])
            segments.append(
                {
                    "carrier": segment.get("carrier", {}).get("code"),
                    "flight": segment.get("flight", {}).get("number"),
                    "source": segment.get("origin", {}).get("code"),
                    "destination": segment.get("destination", {}).get("code"),
                    "departure": segment.get("departure"),
                    "arrival": segment.get("arrival"),
                    "duration_minutes": segment.get("duration"),
                    "booking_codes": [
                        info.get("bookingCode") for info in booking_infos if info.get("bookingCode")
                    ],
                    "cabins": sorted(
                        {info.get("cabin") for info in booking_infos if info.get("cabin")}
                    ),
                    "aircraft": _aircraft_for_segment(segment),
                }
            )

        duration = slice_.get("duration")
        if not duration:
            duration = sum(segment.get("duration", 0) for segment in slice_.get("segments", []))
            if not duration and index < len(fallback_slices):
                duration = fallback_slices[index].get("duration")

        out.append(
            {
                "source": slice_.get("origin", {}).get("code"),
                "destination": slice_.get("destination", {}).get("code"),
                "departure": slice_.get("departure"),
                "arrival": slice_.get("arrival"),
                "duration_minutes": duration,
                "stop_count": slice_.get("stopCount"),
                "segments": segments,
            }
        )
    return out


def execute_flight_detail(
    params: FlightDetailParams,
    *,
    client_factory: ClientFactory = MatrixClient,
) -> dict[str, Any]:
    try:
        slices, pax, options = _flight_request_parts(params, page_size=max(params.rank, 1))
        with client_factory() as client:
            raw = _search_with_client(client, slices, pax, options)
            _, solutions = sorted_solutions(raw, sort=params.sort)
            if not solutions:
                raise ValueError("No solutions returned")
            if params.rank > len(solutions):
                raise ValueError(f"Only {len(solutions)} solutions available")
            target = solutions[params.rank - 1]
            detail = client.detail(
                raw,
                target.id,
                slices,
                pax=pax,
                **options.detail_kwargs(),
            )

        target_raw = target.model_dump(mode="json", by_alias=True)
        booking = detail.get("bookingDetails", {})
        return {
            "success": True,
            "trip_type": "ROUND_TRIP" if params.return_date else "ONE_WAY",
            "rank": params.rank,
            "solution": serialize_solution(target),
            "detail": {
                "slices": serialize_booking_detail(booking, fallback_solution=target_raw),
                "raw_available": bool(booking),
            },
        }
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "trip_type": "ROUND_TRIP" if params.return_date else "ONE_WAY",
            "rank": params.rank,
            "detail": {"slices": []},
        }


def execute_date_search(
    params: DateSearchParams,
    *,
    client_factory: ClientFactory = MatrixClient,
) -> dict[str, Any]:
    try:
        start = parse_date(params.start_date)
        end = parse_date(params.end_date)
        if end < start:
            raise ValueError("end_date must be on or after start_date")
        cabin = validate_cabin(params.cabin)
        sort = validate_sort(params.sort)
        weekdays = None
        if params.departure_weekdays:
            weekdays = parse_weekdays(",".join(params.departure_weekdays))

        dates = []
        current = start
        while current <= end:
            if weekdays is None or current.weekday() in weekdays:
                dates.append(current)
            current += dt.timedelta(days=1)
        if not dates:
            raise ValueError("No candidate dates match the requested range and weekdays")

        routing = build_routing(params.airlines, params.via)
        pax = build_pax_count(adults=params.adults)
        options = SearchOptions(
            cabin=cabin,
            max_stops=params.max_stops,
            page_size=1,
            sorts=sort,
            currency=params.currency,
            sales_city=params.sales_city,
        )
        results = []
        with client_factory() as client:
            for depart in dates:
                return_date = (
                    depart + dt.timedelta(days=params.duration_days)
                    if params.duration_days
                    else None
                )
                slices = build_trip_slices(
                    origin=params.source.upper(),
                    destination=params.destination.upper(),
                    depart=depart.isoformat(),
                    ret=return_date.isoformat() if return_date else None,
                    outbound_routing=routing,
                    return_routing=routing,
                    outbound_time_ranges=parse_time_ranges(params.outbound_time),
                )
                raw = _search_with_client(client, slices, pax, options)
                _, solutions = sorted_solutions(raw, sort="price")
                if not solutions:
                    continue
                best = serialize_solution(solutions[0])
                results.append(
                    {
                        "depart_date": depart.isoformat(),
                        "return_date": return_date.isoformat() if return_date else None,
                        "price": best["price"],
                        "price_value": best["price_value"],
                        "solution": best,
                    }
                )
                results.sort(key=lambda item: item["price_value"] or float("inf"))
                if len(results) >= params.limit:
                    results = results[: params.limit]

        return {
            "success": True,
            "trip_type": "ROUND_TRIP" if params.duration_days else "ONE_WAY",
            "date_range": f"{params.start_date} to {params.end_date}",
            "count": len(results),
            "dates": results,
        }
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "trip_type": "ROUND_TRIP" if params.duration_days else "ONE_WAY",
            "count": 0,
            "dates": [],
        }


def execute_location_lookup(
    params: LookupParams,
    *,
    client_factory: ClientFactory = MatrixClient,
) -> dict[str, Any]:
    try:
        with client_factory() as client:
            locations = client.lookup_locations(params.query, page_size=params.limit)
        return {"success": True, "count": len(locations), "locations": locations}
    except Exception as exc:
        return {"success": False, "error": str(exc), "count": 0, "locations": []}


def execute_airline_lookup(params: AirlineLookupParams) -> dict[str, Any]:
    if not params.query:
        airlines = list(airline_db.all_airlines().values())[: params.limit]
    else:
        airlines = airline_db.search(params.query, limit=params.limit)
    return {"success": True, "count": len(airlines), "airlines": airlines}
