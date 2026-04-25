"""itamx: CLI for ITA Matrix airfare search."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json as json_module
import re
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from itamx.client import MatrixClient, PaxCount, Slice
from itamx.models import SearchResponse

app = typer.Typer(
    add_completion=False,
    help="Search flights via ITA Matrix's reverse-engineered JSON API.",
    no_args_is_help=True,
)
console = Console()


_PRICE_RE = re.compile(r"^([A-Z]{3})([\d.]+)$")


def _price_float(s: str | None) -> float | None:
    if not s:
        return None
    m = _PRICE_RE.match(s)
    if m:
        return float(m.group(2))
    return None


def _format_duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}m"


def _format_time(iso_ts: str) -> str:
    """Trim ISO timestamp to 'MM-DD HH:MM'."""
    if "T" not in iso_ts:
        return iso_ts
    date, time = iso_ts.split("T", 1)
    return f"{date[5:]} {time[:5]}"


def _extract_rbd(booking_details: dict | None) -> str:
    """Format RBD letters per slice from a bookingDetails response."""
    if not booking_details:
        return "—"
    if "error" in booking_details:
        return "err"
    out_parts: list[str] = []
    for slice_ in booking_details.get("itinerary", {}).get("slices", []):
        codes = []
        for seg in slice_.get("segments", []):
            for bi in seg.get("bookingInfos", []):
                code = bi.get("bookingCode")
                if code:
                    codes.append(code)
        out_parts.append("/".join(codes) if codes else "?")
    return " | ".join(out_parts) if out_parts else "—"


def _parse_time_ranges(spec: str | None) -> list[tuple[str, str]]:
    """Parse time-window spec like '6-20' or '0-6,18-23' into (HH:MM,HH:MM) pairs."""
    if not spec:
        return []
    out: list[tuple[str, str]] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" not in chunk:
            raise typer.BadParameter(f"Time range must be HH-HH (got {chunk!r})")
        a, b = chunk.split("-", 1)
        out.append((_to_hhmm(a), _to_hhmm(b)))
    return out


def _to_hhmm(s: str) -> str:
    s = s.strip()
    if ":" in s:
        return s
    if s.isdigit():
        return f"{int(s):02d}:00"
    raise typer.BadParameter(f"Bad time {s!r}; expected HH or HH:MM")


def _rbd_command(rbds: list[str] | None) -> str | None:
    """Convert ['S','M','H'] into Matrix's `f bc=S|M|H` fare-class filter."""
    if not rbds:
        return None
    codes = [r.strip().upper() for r in rbds if r.strip()]
    if not codes:
        return None
    return f"f bc={'|'.join(codes)}"


def _build_routing(
    airlines: list[str] | None,
    via: str | None,
    *,
    strict_airline: bool = True,
) -> str | None:
    """Compose Matrix's RouteLanguage from --airlines + --via.

    Examples:
        --airlines AIRLINE              -> "AIRLINE+"
        --via TRANSIT                  -> "TRANSIT"
        --airlines AIRLINE --via TRANSIT    -> "AIRLINE TRANSIT AIRLINE"  (force that airline through that transit point)
        --airlines AIRLINE1,AIRLINE2           -> "(AIRLINE1|AIRLINE2)+"
    """
    parts: list[str] = []
    al_codes = [a.strip().upper() for a in (airlines or []) if a.strip()]
    suffix = "+" if strict_airline else ""
    airline_token = None
    if al_codes:
        airline_token = (
            f"{al_codes[0]}{suffix}"
            if len(al_codes) == 1
            else f"({'|'.join(al_codes)}){suffix}"
        )

    via_token = via.strip().upper() if via else None

    if airline_token and via_token:
        # Force airline through the via airport (both segments)
        return f"{airline_token} {via_token} {airline_token}"
    if airline_token:
        return airline_token
    if via_token:
        return via_token
    return None


def _combine_commands(*cmds: str | None) -> str | None:
    parts = [c for c in cmds if c]
    return " ".join(parts) if parts else None


@app.command()
def search(
    origin: Annotated[str, typer.Argument(help="Source IATA code")],
    destination: Annotated[str, typer.Argument(help="Destination IATA code")],
    depart: Annotated[str, typer.Argument(help="Depart date YYYY-MM-DD")],
    ret: Annotated[
        str | None,
        typer.Argument(help="Return date YYYY-MM-DD (omit for one-way)"),
    ] = None,
    cabin: Annotated[
        str,
        typer.Option("--cabin", "-c", help="COACH, PREMIUM_COACH, BUSINESS, FIRST"),
    ] = "COACH",
    max_stops: Annotated[
        int | None,
        typer.Option(
            "--max-stops", "-s",
            help="0 = nonstop only relative to route minimum; 1 = up to 1 extra stop; etc.",
            min=0,
        ),
    ] = None,
    # Pax breakdown
    adults: Annotated[int, typer.Option("--adults", min=1, max=9)] = 1,
    seniors: Annotated[int, typer.Option("--seniors", min=0, max=9)] = 0,
    youths: Annotated[int, typer.Option("--youths", min=0, max=9)] = 0,
    children: Annotated[int, typer.Option("--children", min=0, max=9)] = 0,
    infants_seat: Annotated[int, typer.Option("--infants-seat", min=0, max=9)] = 0,
    infants_lap: Annotated[int, typer.Option("--infants-lap", min=0, max=9)] = 0,
    # Routing & filters
    via: Annotated[
        str | None,
        typer.Option(
            "--via",
            help="Route through this airport in BOTH directions. "
                 "Set --via-back separately for asymmetric routing.",
        ),
    ] = None,
    via_back: Annotated[
        str | None,
        typer.Option("--via-back", help="Override return-leg routing airport"),
    ] = None,
    airlines: Annotated[
        str | None,
        typer.Option(
            "--airlines", "-a",
            help="Comma-separated airline IATA codes or names to restrict to",
        ),
    ] = None,
    rbd: Annotated[
        str | None,
        typer.Option(
            "--rbd",
            help="Comma-separated RBD letters to restrict to, e.g. S,M,H",
        ),
    ] = None,
    out_routing: Annotated[
        str | None,
        typer.Option("--out-routing", help="Raw outbound routeLanguage (overrides --via)"),
    ] = None,
    ret_routing: Annotated[
        str | None,
        typer.Option("--ret-routing", help="Raw return routeLanguage (overrides --via-back)"),
    ] = None,
    out_cmd: Annotated[
        str | None,
        typer.Option(
            "--out-cmd",
            help="Raw outbound commandLine (e.g. 'f AIRLINE+ bc=S|M'). "
                 "Combined with --airlines/--rbd if both provided.",
        ),
    ] = None,
    ret_cmd: Annotated[
        str | None,
        typer.Option("--ret-cmd", help="Raw return commandLine"),
    ] = None,
    # Time windows & date flex
    out_time: Annotated[
        str | None,
        typer.Option("--out-time", help="Outbound time window(s), e.g. '6-12' or '0-6,18-23'"),
    ] = None,
    ret_time: Annotated[
        str | None,
        typer.Option("--ret-time", help="Return time window(s)"),
    ] = None,
    flex: Annotated[
        int,
        typer.Option(
            "--flex",
            help="Search ± N days around each date (Matrix dateModifier).",
            min=0, max=7,
        ),
    ] = 0,
    # Output
    page_size: Annotated[
        int, typer.Option("--limit", help="Max solutions to return", min=1, max=500)
    ] = 50,
    output: Annotated[
        str, typer.Option("--output", "-o", help="text | json | raw")
    ] = "text",
    top: Annotated[int, typer.Option("--top", help="Rows to show in text mode")] = 15,
    detail: Annotated[
        int,
        typer.Option(
            "--detail", "-d",
            help="Fetch fare-class detail for the top N solutions (1 extra round-trip per).",
            min=0, max=20,
        ),
    ] = 0,
) -> None:
    """Search flights. Returns a price-sorted table with optional fare-class detail."""
    origin = origin.upper()
    destination = destination.upper()

    # Compose slices
    al_list = [a for a in airlines.split(",")] if airlines else None
    out_routing_final = out_routing or _build_routing(al_list, via)
    ret_routing_final = ret_routing or _build_routing(al_list, via_back or via)

    out_cmd_final = _combine_commands(
        _rbd_command(rbd.split(",") if rbd else None), out_cmd
    )
    ret_cmd_final = _combine_commands(
        _rbd_command(rbd.split(",") if rbd else None), ret_cmd
    )

    out_slice = Slice(
        origin=origin, destination=destination, date=depart,
        flex_minus=flex, flex_plus=flex,
        route_language=out_routing_final,
        command_line=out_cmd_final,
        time_ranges=_parse_time_ranges(out_time),
    )
    slices = [out_slice]
    if ret:
        ret_slice = Slice(
            origin=destination, destination=origin, date=ret,
            flex_minus=flex, flex_plus=flex,
            route_language=ret_routing_final,
            command_line=ret_cmd_final,
            time_ranges=_parse_time_ranges(ret_time),
        )
        slices.append(ret_slice)

    pax = PaxCount(
        adults=adults, seniors=seniors, youths=youths, children=children,
        infants_in_seat=infants_seat, infants_in_lap=infants_lap,
    )

    with MatrixClient() as client:
        try:
            raw = client.search(
                slices=slices,
                pax=pax,
                cabin=cabin.upper(),
                max_stops=max_stops,
                page_size=page_size,
            )
        except Exception as e:
            console.print(f"[red]Search failed: {e}[/red]")
            raise typer.Exit(1)

        details_by_id: dict[str, dict] = {}
        if detail > 0:
            sols_for_detail = sorted(
                raw.get("solutionList", {}).get("solutions", []),
                key=lambda s: _price_float(s.get("displayTotal")) or float("inf"),
            )[:detail]
            for sol in sols_for_detail:
                sid = sol.get("id")
                if not sid:
                    continue
                try:
                    d = client.detail(raw, sid, slices, pax=pax, cabin=cabin.upper())
                    details_by_id[sid] = d.get("bookingDetails", {})
                except Exception as e:
                    details_by_id[sid] = {"error": str(e)}

    if output == "raw":
        print(json_module.dumps(raw, indent=2))
        return

    parsed = SearchResponse.model_validate(raw)
    solutions = sorted(
        parsed.solutionList.solutions,
        key=lambda s: _price_float(s.displayTotal) or float("inf"),
    )

    if output == "json":
        # Strip down to the most useful structured fields
        out = {
            "solutions": [s.model_dump(mode="json") for s in solutions],
            "carrierStopMatrix": parsed.carrierStopMatrix.model_dump(mode="json")
                if parsed.carrierStopMatrix else None,
            "details": {sid: d for sid, d in details_by_id.items()} or None,
        }
        print(json_module.dumps(out, indent=2, default=str))
        return

    if parsed.carrierStopMatrix and parsed.carrierStopMatrix.columns:
        table = Table(title=f"Cheapest by carrier × stops  ({origin} ↔ {destination})")
        table.add_column("Stops")
        cols = parsed.carrierStopMatrix.columns
        for col in cols:
            table.add_column(col.get("label", {}).get("code", "?"))
        for row in parsed.carrierStopMatrix.rows:
            row_cells = [str(row.label)]
            for cell in row.cells:
                if cell.minPrice:
                    suffix = " ⭐" if cell.minPriceInGrid else ""
                    row_cells.append(cell.minPrice + suffix)
                else:
                    row_cells.append("—")
            table.add_row(*row_cells)
        console.print(table)
        console.print()

    if not solutions:
        console.print("[yellow]No solutions returned[/yellow]")
        return

    sol_table = Table(title=f"Top {min(top, len(solutions))} solutions by price")
    sol_table.add_column("Price", justify="right")
    sol_table.add_column("Carriers")
    sol_table.add_column("Out")
    sol_table.add_column("Return")
    sol_table.add_column("Duration")
    if details_by_id:
        sol_table.add_column("RBD")

    for sol in solutions[:top]:
        carriers = "/".join(c.code for c in sol.itinerary.carriers) or "?"
        slices_o = sol.itinerary.slices
        out = slices_o[0] if slices_o else None
        ret_s = slices_o[1] if len(slices_o) > 1 else None
        out_desc = (
            f"{_format_time(out.departure)} {out.origin.code}→{out.destination.code} "
            f"({'/'.join(out.flights)}, {_format_duration(out.duration)})"
            if out else "?"
        )
        ret_desc = (
            f"{_format_time(ret_s.departure)} {ret_s.origin.code}→{ret_s.destination.code} "
            f"({'/'.join(ret_s.flights)}, {_format_duration(ret_s.duration)})"
            if ret_s else "—"
        )
        total_dur = sum(s.duration for s in slices_o)
        cells = [sol.displayTotal, carriers, out_desc, ret_desc, _format_duration(total_dur)]
        if details_by_id:
            cells.append(_extract_rbd(details_by_id.get(sol.id)))
        sol_table.add_row(*cells)
    console.print(sol_table)
    console.print(
        f"\n[dim]Returned {len(solutions)} solution(s) of {parsed.solutionCount} total."
        + (f"  {len(details_by_id)} detail call(s) made." if details_by_id else "")
        + "[/dim]"
    )


@app.command()
def flex(
    origin: Annotated[str, typer.Argument(help="Source IATA code")],
    destination: Annotated[str, typer.Argument(help="Destination IATA code")],
    start: Annotated[str, typer.Argument(help="Earliest possible departure YYYY-MM-DD")],
    end: Annotated[str, typer.Argument(help="Latest possible departure YYYY-MM-DD")],
    duration: Annotated[
        int,
        typer.Option("--duration", "-d", help="Round-trip length in days", min=0, max=60),
    ] = 7,
    days: Annotated[
        str,
        typer.Option(
            "--days",
            help="Comma-separated weekday names to depart on (e.g. SUN,MON). Default: all.",
        ),
    ] = "",
    cabin: Annotated[str, typer.Option("--cabin", "-c")] = "COACH",
    airlines: Annotated[str | None, typer.Option("--airlines", "-a")] = None,
    via: Annotated[str | None, typer.Option("--via")] = None,
    out_time: Annotated[str | None, typer.Option("--out-time")] = None,
    ret_time: Annotated[str | None, typer.Option("--ret-time")] = None,
    max_stops: Annotated[int | None, typer.Option("--max-stops", "-s", min=0)] = None,
    parallel: Annotated[
        int, typer.Option("--parallel", "-p", help="Concurrent searches", min=1, max=8)
    ] = 3,
    output: Annotated[str, typer.Option("--output", "-o")] = "text",
) -> None:
    """Find the cheapest week in a date range. One Matrix search per candidate departure date.

    For round-trip: searches every depart in [start..end-duration] and pairs with depart+duration.
    Use --days SUN,MON to filter to specific weekdays (helpful for "find cheapest Sun-Sat in May").
    """
    try:
        d_start = dt.date.fromisoformat(start)
        d_end = dt.date.fromisoformat(end)
    except ValueError as e:
        raise typer.BadParameter(f"Bad date: {e}")
    if d_end < d_start:
        raise typer.BadParameter("end is before start")

    weekday_filter: set[int] | None = None
    if days.strip():
        name_to_idx = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
        weekday_filter = {
            name_to_idx[d.strip().upper()] for d in days.split(",") if d.strip()
        }

    # Build candidate departure dates
    candidates: list[tuple[str, str | None]] = []
    cur = d_start
    while cur <= d_end:
        if weekday_filter and cur.weekday() not in weekday_filter:
            cur += dt.timedelta(days=1)
            continue
        ret_date = (cur + dt.timedelta(days=duration)).isoformat() if duration > 0 else None
        candidates.append((cur.isoformat(), ret_date))
        cur += dt.timedelta(days=1)

    if not candidates:
        console.print("[yellow]No candidate dates after filtering[/yellow]")
        raise typer.Exit(1)

    console.print(f"[dim]Searching {len(candidates)} departure date(s)…[/dim]")

    al_list = [a for a in airlines.split(",")] if airlines else None
    routing = _build_routing(al_list, via)

    def run_one(dep: str, ret: str | None) -> tuple[str, str | None, dict | None, str | None]:
        out_slice = Slice(
            origin=origin.upper(), destination=destination.upper(), date=dep,
            route_language=routing,
            time_ranges=_parse_time_ranges(out_time),
        )
        slices = [out_slice]
        if ret:
            slices.append(Slice(
                origin=destination.upper(), destination=origin.upper(), date=ret,
                route_language=routing,
                time_ranges=_parse_time_ranges(ret_time),
            ))
        try:
            with MatrixClient() as client:
                resp = client.search(
                    slices=slices, cabin=cabin.upper(), max_stops=max_stops, page_size=20
                )
            return dep, ret, resp, None
        except Exception as e:
            return dep, ret, None, str(e)

    rows: list[tuple[str, str | None, float | None, str | None, str | None]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as ex:
        for dep, ret, resp, err in ex.map(lambda c: run_one(*c), candidates):
            if err:
                rows.append((dep, ret, None, "—", err[:60]))
                continue
            sols = resp.get("solutionList", {}).get("solutions", [])
            if not sols:
                rows.append((dep, ret, None, "—", None))
                continue
            cheapest = min(sols, key=lambda s: _price_float(s.get("displayTotal")) or float("inf"))
            price = _price_float(cheapest.get("displayTotal"))
            carriers = "/".join(c.get("code", "?") for c in cheapest.get("itinerary", {}).get("carriers", []))
            rows.append((dep, ret, price, cheapest.get("displayTotal"), carriers))

    rows.sort(key=lambda r: (r[2] is None, r[2] or float("inf")))

    if output == "json":
        print(json_module.dumps([
            {"depart": dep, "return": ret, "cheapest": disp, "carriers": car}
            for dep, ret, _, disp, car in rows
        ], indent=2))
        return

    table = Table(title=f"Cheapest week  {origin.upper()} ↔ {destination.upper()}  ({duration}-day trip)")
    table.add_column("Depart")
    table.add_column("Return" if duration > 0 else "")
    table.add_column("Day")
    table.add_column("Price", justify="right")
    table.add_column("Carriers")
    for dep, ret, price, disp, car in rows:
        wkday = dt.date.fromisoformat(dep).strftime("%a")
        table.add_row(dep, ret or "—", wkday, disp or "—", car or "—")
    console.print(table)


if __name__ == "__main__":
    app()
