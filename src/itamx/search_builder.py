"""Shared Matrix request construction helpers."""

from __future__ import annotations

from collections.abc import Callable

from itamx import airlines as airline_db
from itamx.client import PaxCount, Slice

AirlineResolvedCallback = Callable[[str, str], None]
AirlineUnresolvedCallback = Callable[[str], None]


def rbd_command(rbds: list[str] | None) -> str | None:
    """Convert RBD letters into Matrix's fare-class command line."""
    if not rbds:
        return None
    codes = [rbd.strip().upper() for rbd in rbds if rbd.strip()]
    if not codes:
        return None
    return f"f bc={'|'.join(codes)}"


def combine_commands(*cmds: str | None) -> str | None:
    parts = [cmd for cmd in cmds if cmd]
    return " ".join(parts) if parts else None


def resolve_airlines(
    tokens: list[str] | None,
    *,
    on_resolved: AirlineResolvedCallback | None = None,
    on_unresolved: AirlineUnresolvedCallback | None = None,
) -> list[str]:
    """Resolve airline names or codes into Matrix-ready IATA tokens."""
    out: list[str] = []
    for raw in tokens or []:
        token = raw.strip()
        if not token:
            continue
        resolved = airline_db.resolve(token)
        if resolved:
            out.append(resolved)
            if on_resolved and resolved != token.upper():
                on_resolved(token, resolved)
        else:
            if on_unresolved:
                on_unresolved(token)
            out.append(token.upper())
    return out


def build_routing(
    airlines: list[str] | None,
    via: str | None,
    *,
    strict_airline: bool = True,
    on_resolved: AirlineResolvedCallback | None = None,
    on_unresolved: AirlineUnresolvedCallback | None = None,
) -> str | None:
    """Compose Matrix RouteLanguage from airline and transit-airport filters."""
    airline_codes = resolve_airlines(
        airlines,
        on_resolved=on_resolved,
        on_unresolved=on_unresolved,
    )
    suffix = "+" if strict_airline else ""
    airline_token = None
    if airline_codes:
        airline_token = (
            f"{airline_codes[0]}{suffix}"
            if len(airline_codes) == 1
            else f"({'|'.join(airline_codes)}){suffix}"
        )

    via_token = via.strip().upper() if via else None
    if airline_token and via_token:
        return f"{airline_token} {via_token} {airline_token}"
    return airline_token or via_token


def build_pax_count(
    *,
    adults: int = 1,
    seniors: int = 0,
    youths: int = 0,
    children: int = 0,
    infants_seat: int = 0,
    infants_lap: int = 0,
) -> PaxCount:
    return PaxCount(
        adults=adults,
        seniors=seniors,
        youths=youths,
        children=children,
        infants_in_seat=infants_seat,
        infants_in_lap=infants_lap,
    )


def build_trip_slices(
    *,
    origin: str,
    destination: str,
    depart: str,
    ret: str | None = None,
    flex: int = 0,
    outbound_routing: str | None = None,
    return_routing: str | None = None,
    outbound_command: str | None = None,
    return_command: str | None = None,
    outbound_time_ranges: list[tuple[str, str]] | None = None,
    return_time_ranges: list[tuple[str, str]] | None = None,
    uppercase_codes: bool = False,
) -> list[Slice]:
    """Build one-way or round-trip Matrix slices."""
    out_origin = origin.upper() if uppercase_codes else origin
    out_destination = destination.upper() if uppercase_codes else destination
    slices = [
        Slice(
            origin=out_origin,
            destination=out_destination,
            date=depart,
            flex_minus=flex,
            flex_plus=flex,
            route_language=outbound_routing,
            command_line=outbound_command,
            time_ranges=outbound_time_ranges or [],
        )
    ]
    if ret:
        slices.append(
            Slice(
                origin=out_destination,
                destination=out_origin,
                date=ret,
                flex_minus=flex,
                flex_plus=flex,
                route_language=return_routing,
                command_line=return_command,
                time_ranges=return_time_ranges or [],
            )
        )
    return slices
