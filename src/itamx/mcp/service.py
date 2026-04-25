"""Dependency-light service layer for MCP tools."""

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
    max_stops: int | None = Field(None, ge=0, description="Matrix max stops relative to route minimum")
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


class DateSearchParams(BaseModel):
    """Parameters for scanning multiple Matrix departure dates."""

    source: str = Field(description="Source IATA city or airport code")
    destination: str = Field(description="Destination IATA city or airport code")
    start_date: str = Field(description="First outbound date in YYYY-MM-DD format")
    end_date: str = Field(description="Last outbound date in YYYY-MM-DD format")
    duration_days: int = Field(0, ge=0, le=60, description="Round-trip stay length; 0 means one-way")
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


def _validate_cabin(cabin: str) -> str:
    value = cabin.upper()
    if value not in VALID_CABINS:
        raise ValueError(f"cabin must be one of {', '.join(sorted(VALID_CABINS))}")
    return value


def _validate_sort(sort: str) -> str:
    if sort not in VALID_SORTS:
        raise ValueError(f"sort must be one of {', '.join(sorted(VALID_SORTS))}")
    return sort


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"date must be YYYY-MM-DD (got {value!r})")


def _resolve_airlines(tokens: list[str] | None) -> list[str]:
    out: list[str] = []
    for token in tokens or []:
        cleaned = token.strip()
        if not cleaned:
            continue
        out.append(airline_db.resolve(cleaned) or cleaned.upper())
    return out


def _build_routing(airlines: list[str] | None, via: str | None) -> str | None:
    codes = _resolve_airlines(airlines)
    airline_token = None
    if codes:
        airline_token = f"{codes[0]}+" if len(codes) == 1 else f"({'|'.join(codes)})+"

    via_token = via.strip().upper() if via else None
    if airline_token and via_token:
        return f"{airline_token} {via_token} {airline_token}"
    return airline_token or via_token


def _pax(adults: int, *, seniors: int = 0, youths: int = 0, children: int = 0,
         infants_seat: int = 0, infants_lap: int = 0) -> PaxCount:
    return PaxCount(
        adults=adults,
        seniors=seniors,
        youths=youths,
        children=children,
        infants_in_seat=infants_seat,
        infants_in_lap=infants_lap,
    )


def _serialize_solution(solution: Solution) -> dict[str, Any]:
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


def _sorted_solutions(raw: dict[str, Any]) -> tuple[SearchResponse, list[Solution]]:
    parsed = SearchResponse.model_validate(raw)
    solutions = sorted(
        parsed.solutionList.solutions,
        key=lambda solution: price_float(solution.displayTotal) or float("inf"),
    )
    return parsed, solutions


def _search_with_client(
    client: MatrixClient,
    slices: list[Slice],
    pax: PaxCount,
    options: SearchOptions,
) -> dict[str, Any]:
    return client.search(slices=slices, pax=pax, **options.search_kwargs())


def _execute_flight_search(
    params: FlightSearchParams,
    *,
    client_factory: ClientFactory = MatrixClient,
) -> dict[str, Any]:
    try:
        _parse_date(params.depart_date)
        if params.return_date:
            _parse_date(params.return_date)
        cabin = _validate_cabin(params.cabin)
        sort = _validate_sort(params.sort)
        routing = _build_routing(params.airlines, params.via)
        outbound = Slice(
            origin=params.source.upper(),
            destination=params.destination.upper(),
            date=params.depart_date,
            flex_minus=params.flex_days,
            flex_plus=params.flex_days,
            route_language=routing,
            time_ranges=parse_time_ranges(params.outbound_time),
        )
        slices = [outbound]
        if params.return_date:
            slices.append(
                Slice(
                    origin=params.destination.upper(),
                    destination=params.source.upper(),
                    date=params.return_date,
                    flex_minus=params.flex_days,
                    flex_plus=params.flex_days,
                    route_language=routing,
                    time_ranges=parse_time_ranges(params.return_time),
                )
            )

        pax = _pax(
            params.adults,
            seniors=params.seniors,
            youths=params.youths,
            children=params.children,
            infants_seat=params.infants_seat,
            infants_lap=params.infants_lap,
        )
        options = SearchOptions(
            cabin=cabin,
            max_stops=params.max_stops,
            page_size=max(params.limit, 1),
            sorts=sort,
            currency=params.currency,
            sales_city=params.sales_city,
        )
        with client_factory() as client:
            raw = _search_with_client(client, slices, pax, options)

        parsed, solutions = _sorted_solutions(raw)
        serialized = [_serialize_solution(solution) for solution in solutions[: params.limit]]
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


def _execute_date_search(
    params: DateSearchParams,
    *,
    client_factory: ClientFactory = MatrixClient,
) -> dict[str, Any]:
    try:
        start = _parse_date(params.start_date)
        end = _parse_date(params.end_date)
        if end < start:
            raise ValueError("end_date must be on or after start_date")
        cabin = _validate_cabin(params.cabin)
        sort = _validate_sort(params.sort)
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

        routing = _build_routing(params.airlines, params.via)
        pax = _pax(params.adults)
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
                outbound = Slice(
                    origin=params.source.upper(),
                    destination=params.destination.upper(),
                    date=depart.isoformat(),
                    route_language=routing,
                    time_ranges=parse_time_ranges(params.outbound_time),
                )
                slices = [outbound]
                if return_date:
                    slices.append(
                        Slice(
                            origin=params.destination.upper(),
                            destination=params.source.upper(),
                            date=return_date.isoformat(),
                            route_language=routing,
                        )
                    )
                raw = _search_with_client(client, slices, pax, options)
                _, solutions = _sorted_solutions(raw)
                if not solutions:
                    continue
                best = _serialize_solution(solutions[0])
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


def _execute_location_lookup(
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


def _execute_airline_lookup(params: AirlineLookupParams) -> dict[str, Any]:
    if not params.query:
        airlines = list(airline_db.all_airlines().values())[: params.limit]
    else:
        airlines = airline_db.search(params.query, limit=params.limit)
    return {"success": True, "count": len(airlines), "airlines": airlines}


def _search_flights_from_params(params: FlightSearchParams) -> dict[str, Any]:
    return _execute_flight_search(params)


def _search_dates_from_params(params: DateSearchParams) -> dict[str, Any]:
    return _execute_date_search(params)
