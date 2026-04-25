"""FastMCP server for ITA Matrix search."""

from __future__ import annotations

import json
import os
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from itamx.mcp.service import (
    AirlineLookupParams,
    DateSearchParams,
    FlightSearchParams,
    LookupParams,
    _execute_airline_lookup,
    _execute_date_search,
    _execute_flight_search,
    _execute_location_lookup,
)

mcp = FastMCP("ITA Matrix MCP Server")


@mcp.tool(annotations={"title": "Search Flights", "readOnlyHint": True, "idempotentHint": True})
def search_flights(
    source: Annotated[str, Field(description="Source IATA city or airport code")],
    destination: Annotated[str, Field(description="Destination IATA city or airport code")],
    depart_date: Annotated[str, Field(description="Outbound date in YYYY-MM-DD format")],
    return_date: Annotated[
        str | None,
        Field(description="Optional return date in YYYY-MM-DD format"),
    ] = None,
    cabin: Annotated[str, Field(description="COACH, PREMIUM_COACH, BUSINESS, or FIRST")] = "COACH",
    max_stops: Annotated[
        int | None,
        Field(description="Max stops relative to route minimum; 0 requests nonstop", ge=0),
    ] = None,
    airlines: Annotated[
        list[str] | None,
        Field(description="Airline IATA codes or carrier names"),
    ] = None,
    via: Annotated[
        str | None,
        Field(description="Transit airport to route through in both directions"),
    ] = None,
    outbound_time: Annotated[
        str | None,
        Field(description="Outbound departure window, e.g. '6-12' or '0-6,18-23'"),
    ] = None,
    return_time: Annotated[
        str | None,
        Field(description="Return departure window, e.g. '18-24'"),
    ] = None,
    flex_days: Annotated[int, Field(description="Date flexibility on each side", ge=0, le=7)] = 0,
    adults: Annotated[int, Field(description="Adult passengers", ge=1, le=9)] = 1,
    currency: Annotated[str | None, Field(description="ISO 4217 currency code")] = None,
    sales_city: Annotated[str | None, Field(description="IATA city code for point of sale")] = None,
    sort: Annotated[
        str,
        Field(description="default, price, duration, departureTime, or arrivalTime"),
    ] = "price",
    limit: Annotated[int, Field(description="Maximum serialized solutions", ge=1, le=100)] = 10,
) -> dict[str, Any]:
    """Search ITA Matrix for one-way or round-trip flights."""
    return _execute_flight_search(
        FlightSearchParams(
            source=source,
            destination=destination,
            depart_date=depart_date,
            return_date=return_date,
            cabin=cabin,
            max_stops=max_stops,
            airlines=airlines,
            via=via,
            outbound_time=outbound_time,
            return_time=return_time,
            flex_days=flex_days,
            adults=adults,
            currency=currency,
            sales_city=sales_city,
            sort=sort,
            limit=limit,
        )
    )


@mcp.tool(annotations={"title": "Search Dates", "readOnlyHint": True, "idempotentHint": True})
def search_dates(
    source: Annotated[str, Field(description="Source IATA city or airport code")],
    destination: Annotated[str, Field(description="Destination IATA city or airport code")],
    start_date: Annotated[str, Field(description="First outbound date in YYYY-MM-DD format")],
    end_date: Annotated[str, Field(description="Last outbound date in YYYY-MM-DD format")],
    duration_days: Annotated[
        int,
        Field(description="Round-trip stay length; 0 means one-way", ge=0, le=60),
    ] = 0,
    departure_weekdays: Annotated[
        list[str] | None,
        Field(description="Optional weekdays such as MON, TUE, SUN"),
    ] = None,
    cabin: Annotated[str, Field(description="COACH, PREMIUM_COACH, BUSINESS, or FIRST")] = "COACH",
    max_stops: Annotated[int | None, Field(description="Max stops relative to route minimum", ge=0)] = None,
    airlines: Annotated[list[str] | None, Field(description="Airline IATA codes or names")] = None,
    via: Annotated[str | None, Field(description="Transit airport")] = None,
    outbound_time: Annotated[str | None, Field(description="Outbound departure window")] = None,
    adults: Annotated[int, Field(description="Adult passengers", ge=1, le=9)] = 1,
    currency: Annotated[str | None, Field(description="ISO 4217 currency code")] = None,
    sales_city: Annotated[str | None, Field(description="IATA city code for point of sale")] = None,
    sort: Annotated[
        str,
        Field(description="default, price, duration, departureTime, or arrivalTime"),
    ] = "price",
    limit: Annotated[int, Field(description="Maximum date results", ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Scan a departure-date range and return the cheapest date results."""
    return _execute_date_search(
        DateSearchParams(
            source=source,
            destination=destination,
            start_date=start_date,
            end_date=end_date,
            duration_days=duration_days,
            departure_weekdays=departure_weekdays,
            cabin=cabin,
            max_stops=max_stops,
            airlines=airlines,
            via=via,
            outbound_time=outbound_time,
            adults=adults,
            currency=currency,
            sales_city=sales_city,
            sort=sort,
            limit=limit,
        )
    )


@mcp.tool(annotations={"title": "Search Locations", "readOnlyHint": True, "idempotentHint": True})
def search_locations(
    query: Annotated[str, Field(description="Partial city, airport name, or IATA code")],
    limit: Annotated[int, Field(description="Maximum locations", ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Autocomplete Matrix city and airport locations."""
    return _execute_location_lookup(LookupParams(query=query, limit=limit))


@mcp.tool(annotations={"title": "Search Airlines", "readOnlyHint": True, "idempotentHint": True})
def search_airlines(
    query: Annotated[
        str | None,
        Field(description="Carrier name, IATA code, ICAO code, callsign, or country"),
    ] = None,
    limit: Annotated[int, Field(description="Maximum airlines", ge=1, le=2000)] = 25,
) -> dict[str, Any]:
    """Resolve airline names and codes."""
    return _execute_airline_lookup(AirlineLookupParams(query=query, limit=limit))


@mcp.resource(
    "resource://itamx-mcp/configuration",
    name="itamx MCP Configuration",
    description="Available itamx MCP tools and expected date/code placeholders.",
    mime_type="application/json",
)
def configuration_resource() -> str:
    """Expose tool names and input conventions."""
    return json.dumps(
        {
            "tools": ["search_flights", "search_dates", "search_locations", "search_airlines"],
            "date_format": "YYYY-MM-DD",
            "code_inputs": "Use IATA city or airport codes; comma-separated codes are accepted by search tools.",
            "http_endpoint": "http://127.0.0.1:8000/mcp/",
        },
        indent=2,
    )


@mcp.prompt("search-direct-flight")
def direct_flight_prompt(source: str, destination: str) -> str:
    """Prompt template for a direct flight search."""
    return (
        f"Use `search_flights` for {source.upper()} to {destination.upper()} "
        "with `max_stops` set to 0. Ask for the outbound date if it is missing."
    )


@mcp.prompt("find-budget-window")
def budget_window_prompt(source: str, destination: str) -> str:
    """Prompt template for a flexible date search."""
    return (
        f"Use `search_dates` for {source.upper()} to {destination.upper()} "
        "when the user gives a date range or asks for cheapest travel dates."
    )


def run() -> None:
    """Run the MCP server on STDIO."""
    mcp.run(transport="stdio")


def run_http(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the MCP server over streamable HTTP."""
    bind_host = os.getenv("HOST") or host
    bind_port = int(os.getenv("PORT") or port)
    mcp.run(transport="http", host=bind_host, port=bind_port)


if __name__ == "__main__":
    run()
