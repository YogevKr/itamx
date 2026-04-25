"""Public package exports for itamx."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from itamx.client import MatrixClient, PaxCount, Slice, build_search_body
from itamx.core import (
    DateSearchParams,
    FlightDetailParams,
    FlightSearchParams,
    execute_date_search,
    execute_flight_detail,
    execute_flight_search,
)

try:
    __version__ = version("itamx")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = [
    "MatrixClient",
    "DateSearchParams",
    "FlightDetailParams",
    "FlightSearchParams",
    "PaxCount",
    "Slice",
    "__version__",
    "build_search_body",
    "execute_date_search",
    "execute_flight_detail",
    "execute_flight_search",
]
