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
from itamx.elal import (
    ElAlAwardClient,
    ElAlBrowserLoginResult,
    ElAlCredentials,
    ElAlPaxCount,
    build_award_search_body,
    build_booking_session_body,
    bootstrap_elal_session_from_sso,
    default_elal_browser_profile_path,
    load_elal_credentials,
    load_elal_session,
    login_with_elal_browser,
    login_with_elal_credentials,
    normalize_award_results,
    save_elal_credentials,
    save_elal_session,
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
    "ElAlAwardClient",
    "ElAlBrowserLoginResult",
    "ElAlCredentials",
    "ElAlPaxCount",
    "PaxCount",
    "Slice",
    "__version__",
    "build_award_search_body",
    "build_booking_session_body",
    "build_search_body",
    "bootstrap_elal_session_from_sso",
    "default_elal_browser_profile_path",
    "execute_date_search",
    "execute_flight_detail",
    "execute_flight_search",
    "load_elal_credentials",
    "load_elal_session",
    "login_with_elal_browser",
    "login_with_elal_credentials",
    "normalize_award_results",
    "save_elal_credentials",
    "save_elal_session",
]
