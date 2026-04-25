"""Compatibility exports for MCP service tests and adapters."""

from __future__ import annotations

from itamx.core import (
    AirlineLookupParams,
    DateSearchParams,
    FlightDetailParams,
    FlightSearchParams,
    LookupParams,
    execute_airline_lookup as _execute_airline_lookup,
    execute_date_search as _execute_date_search,
    execute_flight_detail as _execute_flight_detail,
    execute_flight_search as _execute_flight_search,
    execute_location_lookup as _execute_location_lookup,
    serialize_booking_detail as _serialize_booking_detail,
    serialize_solution as _serialize_solution,
    sorted_solutions as _sorted_solutions,
)

__all__ = [
    "AirlineLookupParams",
    "DateSearchParams",
    "FlightDetailParams",
    "FlightSearchParams",
    "LookupParams",
    "_execute_airline_lookup",
    "_execute_date_search",
    "_execute_flight_detail",
    "_execute_flight_search",
    "_execute_location_lookup",
    "_search_dates_from_params",
    "_search_flights_from_params",
    "_serialize_booking_detail",
    "_serialize_solution",
    "_sorted_solutions",
]


def _search_flights_from_params(params: FlightSearchParams) -> dict:
    return _execute_flight_search(params)


def _search_dates_from_params(params: DateSearchParams) -> dict:
    return _execute_date_search(params)
