"""Optional MCP server integration for itamx.

The FastMCP server is available when the ``itamx[mcp]`` extra is installed.
Importing this package without the extra keeps the regular CLI usable.
"""

try:
    from itamx.mcp.server import (
        DateSearchParams,
        FlightDetailParams,
        FlightSearchParams,
        LookupParams,
        mcp,
        run,
        run_http,
        search_airlines,
        search_dates,
        search_flights,
        search_locations,
        show_flight_details,
    )

    __all__ = [
        "DateSearchParams",
        "FlightDetailParams",
        "FlightSearchParams",
        "LookupParams",
        "mcp",
        "run",
        "run_http",
        "search_airlines",
        "search_dates",
        "search_flights",
        "search_locations",
        "show_flight_details",
    ]
except ModuleNotFoundError:
    __all__: list[str] = []
