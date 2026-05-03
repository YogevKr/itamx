"""itamx: CLI for ITA Matrix airfare search."""

from __future__ import annotations

import concurrent.futures
import csv as csv_module
import datetime as dt
import json as json_module
import shutil
import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from itamx import airlines as airline_db
from itamx import fleet_hints
from itamx.client import MatrixClient, Slice
from itamx.client import _normalize_cabin as _client_normalize_cabin
from itamx.models import SearchResponse
from itamx.request_options import SearchOptions
from itamx.render import extract_rbd, format_duration, format_time, price_float
from itamx.search_builder import (
    build_pax_count,
    build_routing,
    build_trip_slices,
    combine_commands,
    rbd_command,
)
from itamx.validation import (
    SearchOutput,
    ShowOutput,
    SortOrder,
    TableOutput,
    parse_int_range,
    parse_time_ranges,
    parse_weekdays,
)

app = typer.Typer(
    add_completion=False,
    help="Search flights via ITA Matrix's reverse-engineered JSON API.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)


def _log_airline_resolved(raw: str, resolved: str) -> None:
    err_console.print(f"[dim]  {raw!r} → {resolved} ({airline_db.by_iata(resolved)['name']})[/dim]")


def _log_airline_unresolved(raw: str) -> None:
    err_console.print(f"[yellow]  {raw!r}: no unique airline match — passing through as-is[/yellow]")


def _build_cli_routing(
    airlines: list[str] | None,
    via: str | None,
    *,
    strict_airline: bool = True,
) -> str | None:
    return build_routing(
        airlines,
        via,
        strict_airline=strict_airline,
        on_resolved=_log_airline_resolved,
        on_unresolved=_log_airline_unresolved,
    )


def _validated(parser, *args):
    try:
        return parser(*args)
    except ValueError as exc:
        raise typer.BadParameter(str(exc))


def _diagnose_empty_search(
    *,
    origin: str,
    destination: str,
    depart: str,
    ret: str | None,
    cabin: str,
    max_stops: int | None,
    out_routing: str | None,
    out_cmd: str | None,
    out_time: str | None,
    flight: str | None,
    aircraft: str | None = None,
    flex: int = 0,
) -> list[str]:
    """Return a list of human-readable hints for why a search returned 0 results.

    Tries successively-relaxed probes against Matrix to identify which filter
    most likely killed the search. Cheap: only runs when the original search
    returned nothing, and uses page_size=5.
    """
    hints: list[str] = []
    try:
        with MatrixClient() as client:
            base_slices = build_trip_slices(
                origin=origin, destination=destination,
                depart=depart, ret=ret, uppercase_codes=True,
            )
            base = client.search(
                slices=base_slices, cabin="COACH", page_size=5,
                summarizers=["solutionList"],
            )
            base_count = len(base.get("solutionList", {}).get("solutions", []))
            if base_count == 0:
                hints.append(
                    f"Even an unfiltered Y search {origin}→{destination} for "
                    f"{depart}{(' / ' + ret) if ret else ''} returned 0 — "
                    "the route or dates may not be operating at all."
                )
                return hints
            else:
                hints.append(
                    f"Base Y route works ({base_count} unfiltered results) — "
                    "a filter is narrowing it to zero."
                )
    except Exception as e:
        hints.append(f"Could not run diagnostic probe: {e}")
        return hints

    # List likely culprits in order of impact
    likely: list[str] = []
    if cabin and cabin.upper() not in ("COACH", "ECONOMY"):
        likely.append(f"--cabin {cabin}: try COACH first")
    if max_stops is not None:
        likely.append(
            f"--max-stops {max_stops}: relaxing or removing it widens the search"
        )
    if flight:
        likely.append(
            f"--flight {flight}: the requested flight # may not operate, "
            "or may be priced out of Matrix's top results"
        )
    if aircraft:
        likely.append(
            f"--aircraft {aircraft}: no segment in the top results uses a "
            "matching aircraft type"
        )
    if out_routing:
        likely.append(
            f"--out-routing/--via {out_routing!r}: forced routing may be too restrictive"
        )
        if flex > 0:
            likely.append(
                "--flex combined with airline/routing constraints often returns 0 from "
                "Matrix's date-modifier even when each filter works alone. Try `itamx flex` "
                "for a per-date sweep that filters in-process instead."
            )
    if out_cmd:
        likely.append(
            f"--out-cmd {out_cmd!r}: command-line filter may be excluding all options"
        )
    if out_time:
        likely.append(
            f"--out-time {out_time}: time window may be too narrow"
        )
    if likely:
        hints.append("Filters most likely killing the search:")
        hints.extend(f"  • {h}" for h in likely)
    return hints


def _outbound_flights_unverified(resp: dict) -> dict[int, set[str]]:
    """All flight #s seen, indexed by slice position. Y baseline (no
    verification needed: a Y search returns Y on every leg).
    """
    by_slice: dict[int, set[str]] = {}
    for sol in resp.get("solutionList", {}).get("solutions", []):
        for i, sl in enumerate(sol.get("itinerary", {}).get("slices", [])):
            by_slice.setdefault(i, set()).update(sl.get("flights") or [])
    return by_slice


def _verify_cabin_long_haul(
    client: MatrixClient,
    base_resp: dict,
    cabin: str,
    slices_for_detail: list[Slice],
    *,
    pax=None,
    max_unique_combos: int = 12,
    max_workers: int = 4,
) -> dict[int, set[str]]:
    """Return long-haul flight numbers (across all slices) genuinely in `cabin`.

    Matrix happily synthesizes itineraries that put a premium cabin only on
    the connecting leg while the long-haul stays in COACH. We catch this by
    fetching bookingDetails for the cheapest representative of each unique
    outbound-flight combo and checking, slice by slice, that the FIRST
    SEGMENT's bookingInfos.cabin matches what we asked for.

    Grouping by outbound flight means we don't miss flights whose cabin price
    is high (and thus would be ranked low in the response): e.g. LY 5 in
    Business is much more expensive than LY 7, but we still want to verify
    LY 5 specifically.

    For COACH, verification is a no-op (the response itself is authoritative).
    """
    if cabin.upper() in ("COACH", "ECONOMY"):
        return _outbound_flights_unverified(base_resp)

    # Per-slice verified flight numbers (index → set of long-haul flight #s).

    # Normalize to Matrix's wire form: e.g. PREMIUM_COACH → PREMIUM-COACH (with
    # hyphen). The detail response carries the wire form back, so we compare
    # against that.
    target_wire = _client_normalize_cabin(cabin).upper()

    # Pick the cheapest solution for each unique outbound first-flight #.
    cheapest_per_flight: dict[str, dict] = {}
    for sol in base_resp.get("solutionList", {}).get("solutions", []):
        slcs = sol.get("itinerary", {}).get("slices", [])
        if not slcs or not slcs[0].get("flights"):
            continue
        out_flt = slcs[0]["flights"][0]
        cur = cheapest_per_flight.get(out_flt)
        cur_p = price_float((cur or {}).get("displayTotal")) or float("inf")
        new_p = price_float(sol.get("displayTotal")) or float("inf")
        if new_p < cur_p:
            cheapest_per_flight[out_flt] = sol

    # Sort by price and cap to keep detail calls bounded.
    representatives = sorted(
        cheapest_per_flight.values(),
        key=lambda s: price_float(s.get("displayTotal")) or float("inf"),
    )[:max_unique_combos]

    if not representatives:
        return {}

    def _verify(sol: dict) -> dict[int, set[str]]:
        sid = sol.get("id")
        if not sid:
            return {}
        try:
            detail = client.detail(
                base_resp, sid, slices_for_detail, cabin=cabin, pax=pax,
            )
        except Exception:
            return {}
        per_slice: dict[int, set[str]] = {}
        booking_slices = detail.get("bookingDetails", {}).get("itinerary", {}).get("slices", [])
        for i, b_slice in enumerate(booking_slices):
            segs = b_slice.get("segments") or []
            if not segs:
                continue
            # Long-haul = longest-duration segment in the slice.
            long_haul = max(segs, key=lambda s: s.get("duration", 0))
            cabins_in_lh = {
                (bi.get("cabin") or "").upper()
                for bi in long_haul.get("bookingInfos", [])
            }
            if target_wire not in cabins_in_lh:
                continue
            carrier = (long_haul.get("carrier") or {}).get("code")
            num = (long_haul.get("flight") or {}).get("number")
            if carrier and num is not None:
                per_slice.setdefault(i, set()).add(f"{carrier}{num}")
        return per_slice

    verified: dict[int, set[str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for fut in concurrent.futures.as_completed(
            ex.submit(_verify, sol) for sol in representatives
        ):
            for i, flts in fut.result().items():
                verified.setdefault(i, set()).update(flts)
    return verified


@app.command("mcp-config")
def mcp_config(
    name: Annotated[str, typer.Option("--name", help="MCP server name in the client config")] = "itamx",
    command: Annotated[
        str | None,
        typer.Option("--command", help="Override the itamx-mcp executable path"),
    ] = None,
) -> None:
    """Print a Claude Desktop compatible MCP server configuration."""
    executable = command or shutil.which("itamx-mcp") or "itamx-mcp"
    print(json_module.dumps({"mcpServers": {name: {"command": executable, "args": []}}}, indent=2))


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
        SearchOutput, typer.Option("--output", "-o", help="Output format")
    ] = SearchOutput.text,
    top: Annotated[int, typer.Option("--top", help="Rows to show in text mode")] = 15,
    detail: Annotated[
        int,
        typer.Option(
            "--detail", "-d",
            help="Fetch fare-class detail for the top N solutions (1 extra round-trip per).",
            min=0, max=20,
        ),
    ] = 0,
    currency: Annotated[
        str | None,
        typer.Option("--currency", help="ISO 4217 code, e.g. USD or ILS"),
    ] = None,
    sales_city: Annotated[
        str | None,
        typer.Option(
            "--sales-city",
            help="IATA city code for point-of-sale (affects fare offers)",
        ),
    ] = None,
    sort: Annotated[
        SortOrder,
        typer.Option("--sort", help="Sort order to request from Matrix"),
    ] = SortOrder.default,
    max_duration: Annotated[
        int | None,
        typer.Option(
            "--max-duration",
            help="Drop solutions whose total duration exceeds this many hours (post-filter)",
            min=1,
        ),
    ] = None,
    scan_cabins: Annotated[
        bool,
        typer.Option(
            "--scan-cabins",
            help="Also probe Premium Economy and Business cabins; tag each row with "
                 "Y/W/J availability per outbound flight. ~3× slower (2 extra searches).",
        ),
    ] = False,
    flight: Annotated[
        str | None,
        typer.Option(
            "--flight",
            help="Restrict results to itineraries containing these flight numbers "
                 "(comma-separated, e.g. LY5 or LY5,LY10). Post-filter, AND across the list.",
        ),
    ] = None,
    aircraft: Annotated[
        str | None,
        typer.Option(
            "--aircraft",
            help="Restrict results to itineraries that use any of these aircraft "
                 "(comma-separated substrings, case-insensitive — e.g. '787-9' or "
                 "'787,A350'). Triggers detail lookups on top results.",
        ),
    ] = None,
) -> None:
    """Search flights. Returns a price-sorted table with optional fare-class detail.

    `origin`/`destination` may be a single IATA code, a comma-list, or a city
    code that Matrix expands to all metro airports.
    """
    # Note: keep the user's commas in origin/destination — Slice splits internally.

    # Compose slices
    al_list = [a for a in airlines.split(",")] if airlines else None
    out_routing_final = out_routing or _build_cli_routing(al_list, via)
    ret_routing_final = ret_routing or _build_cli_routing(al_list, via_back or via)

    out_cmd_final = combine_commands(
        rbd_command(rbd.split(",") if rbd else None), out_cmd
    )
    ret_cmd_final = combine_commands(
        rbd_command(rbd.split(",") if rbd else None), ret_cmd
    )

    slices = build_trip_slices(
        origin=origin,
        destination=destination,
        depart=depart,
        ret=ret,
        flex=flex,
        outbound_routing=out_routing_final,
        return_routing=ret_routing_final,
        outbound_command=out_cmd_final,
        return_command=ret_cmd_final,
        outbound_time_ranges=_validated(parse_time_ranges, out_time),
        return_time_ranges=_validated(parse_time_ranges, ret_time),
    )

    pax = build_pax_count(
        adults=adults,
        seniors=seniors,
        youths=youths,
        children=children,
        infants_seat=infants_seat,
        infants_lap=infants_lap,
    )
    options = SearchOptions(
        cabin=cabin.upper(),
        max_stops=max_stops,
        page_size=page_size,
        sorts=sort.value,
        currency=currency,
        sales_city=sales_city,
    )

    with MatrixClient() as client:
        try:
            raw = client.search(
                slices=slices,
                pax=pax,
                **options.search_kwargs(),
            )
        except Exception as e:
            console.print(f"[red]Search failed: {e}[/red]")
            raise typer.Exit(1)

        # Optional: probe W and J cabins in parallel and collect the set of
        # long-haul flight numbers each cabin carries, indexed by slice
        # position so we can tag per-direction. Verification via bookingDetails
        # filters out synthetic mixed-cabin itineraries.
        cabin_avail: dict[str, dict[int, set[str]]] = {"Y": {}, "W": {}, "J": {}}
        if scan_cabins:
            err_console.print(
                "[dim]Scanning Premium Economy and Business cabins (with cabin verification)…[/dim]"
            )

            cabin_avail["Y"] = _outbound_flights_unverified(raw)
            # Use a large page_size for cabin probes — flights that are bookable
            # in the cabin but priced steeply (e.g. LY 5 in J at the V.2 saver
            # bucket) may sit outside the default top 50 results.
            scan_kwargs = {**options.search_kwargs(), "page_size": 200}
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                futures = {
                    label: ex.submit(
                        client.search,
                        slices=slices,
                        pax=pax,
                        **{**scan_kwargs, "cabin": cabin_code},
                    )
                    for label, cabin_code in (("W", "PREMIUM_COACH"), ("J", "BUSINESS"))
                }
                cabin_responses: dict[str, dict] = {}
                for label, fut in futures.items():
                    try:
                        cabin_responses[label] = fut.result()
                    except Exception as e:
                        err_console.print(f"[yellow]  {label} scan failed: {e}[/yellow]")

            # Verify each W/J response by per-segment detail check
            for label, target_cabin in (("W", "PREMIUM_COACH"), ("J", "BUSINESS")):
                resp_for_cabin = cabin_responses.get(label)
                if resp_for_cabin is None:
                    continue
                cabin_avail[label] = _verify_cabin_long_haul(
                    client, resp_for_cabin, target_cabin, slices, pax=pax,
                )

        details_by_id: dict[str, dict] = {}
        # Aircraft filter requires segment-level data, only available via
        # bookingDetails. Bump the detail fetch count to cover top results
        # even when the user didn't pass --detail explicitly.
        effective_detail = detail
        if aircraft:
            effective_detail = max(effective_detail, 30)
        if effective_detail > 0:
            sols_for_detail = sorted(
                raw.get("solutionList", {}).get("solutions", []),
                key=lambda s: price_float(s.get("displayTotal")) or float("inf"),
            )[:effective_detail]
            for sol in sols_for_detail:
                sid = sol.get("id")
                if not sid:
                    continue
                try:
                    d = client.detail(
                        raw,
                        sid,
                        slices,
                        pax=pax,
                        **options.detail_kwargs(),
                    )
                    details_by_id[sid] = d.get("bookingDetails", {})
                except Exception as e:
                    details_by_id[sid] = {"error": str(e)}

    if output == SearchOutput.raw:
        print(json_module.dumps(raw, indent=2))
        return

    parsed = SearchResponse.model_validate(raw)
    solutions = sorted(
        parsed.solutionList.solutions,
        key=lambda s: price_float(s.displayTotal) or float("inf"),
    )

    if max_duration is not None:
        max_min = max_duration * 60
        solutions = [
            s for s in solutions
            if sum(sl.duration for sl in s.itinerary.slices) <= max_min
        ]

    # --flight filter: keep solutions whose union of slice-flights covers all
    # requested flight numbers. Comma-list, AND semantics (all must appear).
    if flight:
        wanted = {f.strip().upper() for f in flight.split(",") if f.strip()}
        def _flights_in(sol) -> set[str]:
            return {f for sl in sol.itinerary.slices for f in (sl.flights or [])}
        solutions = [s for s in solutions if wanted.issubset(_flights_in(s))]
        if not solutions:
            err_console.print(
                f"[yellow]No solutions contain all of {sorted(wanted)} (post-filter).[/yellow]"
            )

    # --aircraft filter: keep solutions where any segment uses an aircraft
    # whose shortName contains any of the comma-separated patterns. Needs
    # bookingDetails; we already bumped the detail fetch count above.
    if aircraft:
        patterns = [p.strip().lower() for p in aircraft.split(",") if p.strip()]
        if not patterns:
            pass
        else:
            def _aircraft_in(sol_id: str) -> set[str]:
                bd = details_by_id.get(sol_id) or {}
                if "error" in bd:
                    return set()
                names: set[str] = set()
                for sl in bd.get("itinerary", {}).get("slices", []):
                    for seg in sl.get("segments", []):
                        for leg in seg.get("legs", []):
                            ac = (leg.get("aircraft") or {}).get("shortName")
                            if ac:
                                names.add(ac.lower())
                return names

            kept = []
            unfetched = 0
            for s in solutions:
                if s.id not in details_by_id:
                    unfetched += 1
                    continue
                acs = _aircraft_in(s.id)
                if any(p in ac for ac in acs for p in patterns):
                    kept.append(s)
            solutions = kept
            if unfetched:
                err_console.print(
                    f"[dim]aircraft: {unfetched} solution(s) skipped (no detail fetched; "
                    f"raise --detail or top to widen).[/dim]"
                )
            if not solutions:
                err_console.print(
                    f"[yellow]No solutions used aircraft matching {patterns}. "
                    f"Aircraft seen across fetched details: "
                    f"{sorted({a for sid in details_by_id for a in _aircraft_in(sid)})}[/yellow]"
                )

    if output == SearchOutput.json:
        # Strip down to the most useful structured fields
        out = {
            "solutions": [s.model_dump(mode="json") for s in solutions],
            "carrierStopMatrix": parsed.carrierStopMatrix.model_dump(mode="json")
                if parsed.carrierStopMatrix else None,
            "details": {sid: d for sid, d in details_by_id.items()} or None,
        }
        print(json_module.dumps(out, indent=2, default=str))
        return

    if output == SearchOutput.csv:
        writer = csv_module.writer(sys.stdout)
        header = ["price", "carriers", "out_dep", "out_route", "out_flights", "out_dur_min",
                  "ret_dep", "ret_route", "ret_flights", "ret_dur_min", "total_dur_min"]
        if details_by_id:
            header.append("rbd")
        writer.writerow(header)
        for sol in solutions[:top]:
            slices_o = sol.itinerary.slices
            o = slices_o[0] if slices_o else None
            r = slices_o[1] if len(slices_o) > 1 else None
            row = [
                sol.displayTotal,
                "/".join(c.code for c in sol.itinerary.carriers),
                o.departure if o else "",
                f"{o.origin.code}->{o.destination.code}" if o else "",
                "/".join(o.flights) if o else "",
                o.duration if o else "",
                r.departure if r else "",
                f"{r.origin.code}->{r.destination.code}" if r else "",
                "/".join(r.flights) if r else "",
                r.duration if r else "",
                sum(s.duration for s in slices_o),
            ]
            if details_by_id:
                row.append(extract_rbd(details_by_id.get(sol.id)))
            writer.writerow(row)
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
        # Run diagnostic probes only for text output (would clutter json/csv)
        if output == SearchOutput.text:
            for h in _diagnose_empty_search(
                origin=origin, destination=destination,
                depart=depart, ret=ret, cabin=cabin,
                max_stops=max_stops, out_routing=out_routing_final,
                out_cmd=out_cmd_final, out_time=out_time, flight=flight,
                aircraft=aircraft, flex=flex,
            ):
                console.print(f"[dim]  {h}[/dim]")
        return

    sol_table = Table(title=f"Top {min(top, len(solutions))} solutions by price")
    sol_table.add_column("Price", justify="right")
    sol_table.add_column("Carriers")
    sol_table.add_column("Stops")
    sol_table.add_column("Out")
    sol_table.add_column("Return")
    sol_table.add_column("Duration")
    if scan_cabins:
        sol_table.add_column("Cabins")
    if details_by_id:
        sol_table.add_column("RBD")

    for sol in solutions[:top]:
        carriers = "/".join(c.code for c in sol.itinerary.carriers) or "?"
        slices_o = sol.itinerary.slices
        out = slices_o[0] if slices_o else None
        ret_s = slices_o[1] if len(slices_o) > 1 else None

        def _slice_desc(s):
            stops = max(0, len(s.flights) - 1)
            return (
                f"{format_time(s.departure)} {s.origin.code}→{s.destination.code} "
                f"({'/'.join(s.flights)}, {format_duration(s.duration)}"
                + (f", {stops} stop{'s' if stops != 1 else ''})" if stops else ", nonstop)")
            )

        out_desc = _slice_desc(out) if out else "?"
        ret_desc = _slice_desc(ret_s) if ret_s else "—"
        # Stops summary across slices
        stops_str = " / ".join(
            str(max(0, len(s.flights) - 1)) for s in slices_o
        )
        total_dur = sum(s.duration for s in slices_o)
        cells = [
            sol.displayTotal, carriers, stops_str,
            out_desc, ret_desc, format_duration(total_dur),
        ]
        if scan_cabins:
            # cabin_avail[X][i] holds verified long-haul flights for cabin X
            # in slice index i. Tag per-direction: ✓ if any flight in that
            # slice matches the verified set for that slice index.
            #   Y✓✓ W✗✓ J✓✗  reads as "Y both legs, W only return, J only out"
            tags = []
            for label in ("Y", "W", "J"):
                per_slice = cabin_avail[label]
                marks = []
                for i, sl in enumerate(slices_o):
                    avail = per_slice.get(i, set())
                    has = bool(avail) and bool(sl.flights) and any(f in avail for f in sl.flights)
                    marks.append("[green]✓[/green]" if has else "[red]✗[/red]")
                tags.append(f"{label}{''.join(marks)}")
            cells.append(" ".join(tags))
        if details_by_id:
            cells.append(extract_rbd(details_by_id.get(sol.id)))
        sol_table.add_row(*cells)
    console.print(sol_table)

    if scan_cabins:
        # Surface the per-cabin verified long-haul flights split by direction
        # (outbound = slice 0, return = slice 1+) so the user can see which
        # legs carry which cabin even when no full row matches.
        directions = ["outbound", "return", "leg 3", "leg 4"]
        for label, name in (("W", "Premium Economy"), ("J", "Business")):
            per_slice = cabin_avail.get(label, {})
            flat: set[str] = set().union(*per_slice.values()) if per_slice else set()
            if not flat:
                console.print(f"[bold]{name} on long-haul:[/bold] [red]none verified[/red]")
                continue
            parts = []
            for i in sorted(per_slice):
                flts = sorted(per_slice[i])
                d = directions[i] if i < len(directions) else f"leg {i+1}"
                parts.append(f"{d}: {', '.join(flts)}")
            console.print(f"[bold]{name} on long-haul[/bold] — " + " | ".join(parts))

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
        typer.Option("--duration", "-d", help="Round-trip length in days (one value)", min=0, max=60),
    ] = 7,
    stay: Annotated[
        str | None,
        typer.Option(
            "--stay",
            help="Range of trip lengths in days, e.g. '5-8' searches 5/6/7/8-night stays. Overrides --duration.",
        ),
    ] = None,
    ret_start: Annotated[
        str | None,
        typer.Option(
            "--ret-start",
            help="Earliest possible return YYYY-MM-DD. With --ret-end, runs a rectangular "
                 "depart × return sweep instead of computing return from --duration/--stay.",
        ),
    ] = None,
    ret_end: Annotated[
        str | None,
        typer.Option(
            "--ret-end",
            help="Latest possible return YYYY-MM-DD. Pair with --ret-start.",
        ),
    ] = None,
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
    scan_cabins: Annotated[
        bool,
        typer.Option(
            "--scan-cabins",
            help="For each date, also probe Premium Economy and Business and tag with "
                 "Y/W/J availability + per-flight cabin map. ~3× slower per date.",
        ),
    ] = False,
    output: Annotated[
        TableOutput, typer.Option("--output", "-o", help="Output format")
    ] = TableOutput.text,
) -> None:
    """Find the cheapest week in a date range. One Matrix search per candidate departure date.

    [start..end] is the depart window. Return is computed as depart + --duration
    (or each value in --stay), and may extend past `end`.

    Pass --ret-start/--ret-end to run a rectangular depart × return sweep instead.
    With --stay, only pairs whose stay length falls in the range are kept.

    Use --days SUN,MON to filter departure weekdays (helpful for "find cheapest
    Sun-Sat in May").
    """
    try:
        d_start = dt.date.fromisoformat(start)
        d_end = dt.date.fromisoformat(end)
    except ValueError as e:
        raise typer.BadParameter(f"Bad date: {e}")
    if d_end < d_start:
        raise typer.BadParameter("end is before start")

    if (ret_start is None) != (ret_end is None):
        raise typer.BadParameter("--ret-start and --ret-end must be used together")
    d_ret_start = d_ret_end = None
    if ret_start and ret_end:
        try:
            d_ret_start = dt.date.fromisoformat(ret_start)
            d_ret_end = dt.date.fromisoformat(ret_end)
        except ValueError as e:
            raise typer.BadParameter(f"Bad return-window date: {e}")
        if d_ret_end < d_ret_start:
            raise typer.BadParameter("--ret-end is before --ret-start")

    weekday_filter = _validated(parse_weekdays, days)

    # Determine trip lengths to try
    if stay:
        durations_to_try = _validated(parse_int_range, stay)
    else:
        durations_to_try = [duration]

    # Build candidate (depart, return) pairs.
    candidates: list[tuple[str, str | None]] = []
    cur = d_start
    while cur <= d_end:
        if weekday_filter and cur.weekday() not in weekday_filter:
            cur += dt.timedelta(days=1)
            continue
        if d_ret_start is not None and d_ret_end is not None:
            # Rectangular sweep over an explicit return window.
            r = d_ret_start
            while r <= d_ret_end:
                stay_days = (r - cur).days
                if stay_days <= 0:
                    r += dt.timedelta(days=1)
                    continue
                if stay and stay_days not in durations_to_try:
                    r += dt.timedelta(days=1)
                    continue
                candidates.append((cur.isoformat(), r.isoformat()))
                r += dt.timedelta(days=1)
        else:
            for dur_days in durations_to_try:
                ret_date = (
                    (cur + dt.timedelta(days=dur_days)).isoformat() if dur_days > 0 else None
                )
                candidates.append((cur.isoformat(), ret_date))
        cur += dt.timedelta(days=1)

    if not candidates:
        console.print("[yellow]No candidate dates after filtering[/yellow]")
        raise typer.Exit(1)

    # Route progress to stderr so JSON/CSV output to stdout stays clean for piping.
    err_console = Console(stderr=True)
    err_console.print(
        f"[dim]Searching {len(candidates)} (depart, return) combinations…[/dim]"
    )

    al_list = [a for a in airlines.split(",")] if airlines else None
    routing = _build_cli_routing(al_list, via)
    options = SearchOptions(cabin=cabin.upper(), max_stops=max_stops, page_size=20)

    def run_one(dep: str, ret: str | None) -> tuple[str, str | None, dict | None, dict[str, dict[int, set[str]]] | None, str | None]:
        slices = build_trip_slices(
            origin=origin,
            destination=destination,
            depart=dep,
            ret=ret,
            outbound_routing=routing,
            return_routing=routing,
            outbound_time_ranges=_validated(parse_time_ranges, out_time),
            return_time_ranges=_validated(parse_time_ranges, ret_time),
            uppercase_codes=True,
        )
        try:
            with MatrixClient() as client:
                resp = client.search(
                    slices=slices,
                    **options.search_kwargs(),
                )
                cabin_avail: dict[str, set[str]] | None = None
                if scan_cabins:
                    cabin_avail = {
                        "Y": _outbound_flights_unverified(resp),
                        "W": {},
                        "J": {},
                    }
                    scan_kwargs = {**options.search_kwargs(), "page_size": 200}
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as inner:
                        futures = {
                            label: inner.submit(
                                client.search,
                                slices=slices,
                                **{**scan_kwargs, "cabin": cabin_code},
                            )
                            for label, cabin_code in (("W", "PREMIUM_COACH"), ("J", "BUSINESS"))
                        }
                        cabin_responses: dict[str, dict] = {}
                        for label, fut in futures.items():
                            try:
                                cabin_responses[label] = fut.result()
                            except Exception:
                                pass
                    # Verify each cabin's results via bookingDetails per-segment
                    # cabin to filter out synthetic mixed-cabin itineraries.
                    for label, target_cabin in (("W", "PREMIUM_COACH"), ("J", "BUSINESS")):
                        resp_for_cabin = cabin_responses.get(label)
                        if resp_for_cabin is None:
                            continue
                        cabin_avail[label] = _verify_cabin_long_haul(
                            client, resp_for_cabin, target_cabin, slices,
                        )
            return dep, ret, resp, cabin_avail, None
        except Exception as e:
            return dep, ret, None, None, str(e)

    # row tuple: (dep, ret, price_float, displayPrice, carriers, cabin_tag, flight_map)
    rows: list[tuple[str, str | None, float | None, str | None, str | None, str | None, dict[str, list[str]] | None]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as ex:
        for dep, ret, resp, cabin_avail, err in ex.map(lambda c: run_one(*c), candidates):
            if err:
                rows.append((dep, ret, None, "—", err[:60], None, None))
                continue
            sols = resp.get("solutionList", {}).get("solutions", [])
            if not sols:
                rows.append((dep, ret, None, "—", None, None, None))
                continue
            cheapest = min(sols, key=lambda s: price_float(s.get("displayTotal")) or float("inf"))
            price = price_float(cheapest.get("displayTotal"))
            carriers = "/".join(c.get("code", "?") for c in cheapest.get("itinerary", {}).get("carriers", []))

            cabin_tag = None
            flight_map: dict[str, list[str]] | None = None
            if cabin_avail is not None:
                tags = []
                # Per-direction tag: one ✓/✗ per slice (outbound, return, …)
                num_slices = max(
                    (max(per_sl.keys(), default=-1) + 1 for per_sl in cabin_avail.values()),
                    default=0,
                )
                if num_slices == 0:
                    num_slices = 2 if ret else 1
                for lbl in ("Y", "W", "J"):
                    per_sl = cabin_avail[lbl]
                    marks = []
                    for i in range(num_slices):
                        marks.append("[green]✓[/green]" if per_sl.get(i) else "[red]✗[/red]")
                    tags.append(f"{lbl}{''.join(marks)}")
                cabin_tag = " ".join(tags)
                flight_map = {
                    lbl: sorted(set().union(*cabin_avail[lbl].values()))
                    if cabin_avail[lbl] else []
                    for lbl in ("Y", "W", "J")
                }
            rows.append((dep, ret, price, cheapest.get("displayTotal"), carriers, cabin_tag, flight_map))

    rows.sort(key=lambda r: (r[2] is None, r[2] or float("inf")))

    if output == TableOutput.json:
        out = []
        for dep, ret, price, disp, car, _tag, fmap in rows:
            entry = {
                "depart": dep, "return": ret,
                "day": dt.date.fromisoformat(dep).strftime("%A"),
                "price": price, "displayPrice": disp, "carriers": car,
            }
            if fmap is not None:
                entry["cabins"] = {
                    lbl: {"available": bool(fmap[lbl]), "flights": fmap[lbl]}
                    for lbl in ("Y", "W", "J")
                }
            out.append(entry)
        print(json_module.dumps(out, indent=2))
        return

    if output == TableOutput.csv:
        writer = csv_module.writer(sys.stdout)
        header = ["depart", "return", "day", "price", "display_price", "carriers"]
        if scan_cabins:
            header += ["y_flights", "w_flights", "j_flights"]
        writer.writerow(header)
        for dep, ret, price, disp, car, _tag, fmap in rows:
            wkday = dt.date.fromisoformat(dep).strftime("%a")
            row = [dep, ret or "", wkday, price or "", disp or "", car or ""]
            if scan_cabins:
                if fmap:
                    row += [
                        ",".join(fmap["Y"]),
                        ",".join(fmap["W"]),
                        ",".join(fmap["J"]),
                    ]
                else:
                    row += ["", "", ""]
            writer.writerow(row)
        return

    dur_label = (
        f"{stay}-day trip" if stay
        else (f"{duration}-day trip" if duration > 0 else "one-way")
    )
    table = Table(title=f"Cheapest week  {origin.upper()} ↔ {destination.upper()}  ({dur_label})")
    table.add_column("Depart")
    table.add_column("Return" if any(ret for _, ret, *_ in rows) else "")
    table.add_column("Day")
    table.add_column("Price", justify="right")
    table.add_column("Carriers")
    if scan_cabins:
        table.add_column("Cabins")
        table.add_column("PE flights")
    for dep, ret, price, disp, car, tag, fmap in rows:
        wkday = dt.date.fromisoformat(dep).strftime("%a")
        cells = [dep, ret or "—", wkday, disp or "—", car or "—"]
        if scan_cabins:
            cells.append(tag or "—")
            cells.append(", ".join(fmap["W"]) if fmap and fmap["W"] else "—")
        table.add_row(*cells)
    console.print(table)

    # Optional rollup: which outbound flights have PE on which dates
    if scan_cabins:
        pe_by_flight: dict[str, list[str]] = {}
        for dep, ret, *_, fmap in rows:
            if not fmap:
                continue
            for f in fmap["W"]:
                pe_by_flight.setdefault(f, []).append(dep)
        if pe_by_flight:
            console.print(
                "\n[bold]PE-equipped flights across the scan:[/bold]"
            )
            for flt in sorted(pe_by_flight):
                dates = pe_by_flight[flt]
                console.print(
                    f"  [green]{flt}[/green]: {len(dates)}/{len(rows)} dates  "
                    f"→ {', '.join(dates)}"
                )


@app.command()
def lookup(
    query: Annotated[str, typer.Argument(help="Partial city or airport name or IATA code")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=50)] = 10,
    output: Annotated[
        TableOutput, typer.Option("--output", "-o", help="Output format")
    ] = TableOutput.text,
) -> None:
    """Resolve city/airport names. Useful when you don't know the IATA code."""
    with MatrixClient() as client:
        try:
            locations = client.lookup_locations(query, page_size=limit)
        except Exception as e:
            console.print(f"[red]Lookup failed: {e}[/red]")
            raise typer.Exit(1)

    if output == TableOutput.json:
        print(json_module.dumps(locations, indent=2))
        return

    if output == TableOutput.csv:
        writer = csv_module.writer(sys.stdout)
        writer.writerow(["code", "type", "name", "city", "city_code"])
        for loc in locations:
            writer.writerow([
                loc.get("code", ""),
                loc.get("type", ""),
                loc.get("displayName", ""),
                loc.get("cityName", ""),
                loc.get("cityCode", ""),
            ])
        return

    if not locations:
        console.print(f"[yellow]No locations matched {query!r}[/yellow]")
        return

    table = Table(title=f"Locations matching {query!r}")
    table.add_column("Code")
    table.add_column("Type")
    table.add_column("Name")
    table.add_column("City")
    for loc in locations:
        table.add_row(
            loc.get("code", "?"),
            loc.get("type", ""),
            loc.get("displayName", ""),
            loc.get("cityName", ""),
        )
    console.print(table)


@app.command()
def multi(
    leg: Annotated[
        list[str],
        typer.Option(
            "--leg", "-l",
            help="A leg as SOURCE:DESTINATION:DATE. Pass once per leg.",
        ),
    ],
    cabin: Annotated[str, typer.Option("--cabin", "-c")] = "COACH",
    adults: Annotated[int, typer.Option("--adults", min=1, max=9)] = 1,
    max_stops: Annotated[int | None, typer.Option("--max-stops", "-s", min=0)] = None,
    airlines: Annotated[str | None, typer.Option("--airlines", "-a")] = None,
    currency: Annotated[str | None, typer.Option("--currency")] = None,
    sales_city: Annotated[str | None, typer.Option("--sales-city")] = None,
    detail: Annotated[int, typer.Option("--detail", "-d", min=0, max=20)] = 0,
    top: Annotated[int, typer.Option("--top")] = 10,
    output: Annotated[
        SearchOutput, typer.Option("--output", "-o", help="Output format")
    ] = SearchOutput.text,
) -> None:
    """Multi-city / open-jaw search. Pass `--leg SOURCE:DESTINATION:DATE` 2+ times.

    Example:
        itamx multi -l SOURCE:STOPOVER:LEG1_DATE -l STOPOVER:DESTINATION:LEG2_DATE
    """
    if len(leg) < 2:
        raise typer.BadParameter("Need at least 2 legs (use --leg multiple times)")

    al_list = [a for a in airlines.split(",")] if airlines else None
    slices: list[Slice] = []
    for spec in leg:
        parts = spec.split(":")
        if len(parts) != 3:
            raise typer.BadParameter(f"--leg must be SOURCE:DESTINATION:DATE (got {spec!r})")
        o, d, date = parts
        slices.append(Slice(
            origin=o.upper().strip(), destination=d.upper().strip(), date=date.strip(),
            route_language=_build_cli_routing(al_list, None),
        ))

    pax = build_pax_count(adults=adults)
    options = SearchOptions(
        cabin=cabin.upper(),
        max_stops=max_stops,
        page_size=50,
        currency=currency,
        sales_city=sales_city,
    )
    with MatrixClient() as client:
        try:
            raw = client.search(
                slices=slices,
                pax=pax,
                **options.search_kwargs(),
            )
        except Exception as e:
            console.print(f"[red]Search failed: {e}[/red]")
            raise typer.Exit(1)

        details_by_id: dict[str, dict] = {}
        if detail > 0:
            sols = sorted(
                raw.get("solutionList", {}).get("solutions", []),
                key=lambda s: price_float(s.get("displayTotal")) or float("inf"),
            )[:detail]
            for sol in sols:
                sid = sol.get("id")
                if not sid:
                    continue
                try:
                    d = client.detail(
                        raw,
                        sid,
                        slices,
                        pax=pax,
                        **options.detail_kwargs(),
                    )
                    details_by_id[sid] = d.get("bookingDetails", {})
                except Exception as e:
                    details_by_id[sid] = {"error": str(e)}

    if output == SearchOutput.raw:
        print(json_module.dumps(raw, indent=2))
        return

    parsed = SearchResponse.model_validate(raw)
    sols = sorted(
        parsed.solutionList.solutions,
        key=lambda s: price_float(s.displayTotal) or float("inf"),
    )

    if output == SearchOutput.json:
        print(json_module.dumps([s.model_dump(mode="json") for s in sols], indent=2, default=str))
        return

    if output == SearchOutput.csv:
        writer = csv_module.writer(sys.stdout)
        leg_cols = []
        for i in range(len(slices)):
            leg_cols += [f"leg{i+1}_dep", f"leg{i+1}_route", f"leg{i+1}_flights", f"leg{i+1}_dur_min"]
        header = ["price", "carriers"] + leg_cols + ["total_dur_min"]
        if details_by_id:
            header.append("rbd")
        writer.writerow(header)
        for sol in sols[:top]:
            row = [sol.displayTotal, "/".join(c.code for c in sol.itinerary.carriers)]
            for s in sol.itinerary.slices:
                row += [
                    s.departure,
                    f"{s.origin.code}->{s.destination.code}",
                    "/".join(s.flights),
                    s.duration,
                ]
            # pad if Matrix returned fewer slices than requested
            while len(row) - 2 < len(slices) * 4:
                row.append("")
            row.append(sum(s.duration for s in sol.itinerary.slices))
            if details_by_id:
                row.append(extract_rbd(details_by_id.get(sol.id)))
            writer.writerow(row)
        return

    if not sols:
        console.print("[yellow]No solutions returned[/yellow]")
        return

    table = Table(title=f"Multi-city: {' → '.join(s.origin + '→' + s.destination for s in slices)}")
    table.add_column("Price", justify="right")
    table.add_column("Carriers")
    for i in range(len(slices)):
        table.add_column(f"Leg {i+1}")
    table.add_column("Total dur")
    if details_by_id:
        table.add_column("RBD")

    for sol in sols[:top]:
        cells = [sol.displayTotal, "/".join(c.code for c in sol.itinerary.carriers)]
        for s in sol.itinerary.slices:
            cells.append(
                f"{format_time(s.departure)} {s.origin.code}→{s.destination.code} "
                f"({'/'.join(s.flights)}, {format_duration(s.duration)})"
            )
        # Pad if Matrix returned fewer slices than we asked
        while len(cells) - 2 < len(slices):
            cells.append("?")
        cells.append(format_duration(sum(s.duration for s in sol.itinerary.slices)))
        if details_by_id:
            cells.append(extract_rbd(details_by_id.get(sol.id)))
        table.add_row(*cells)
    console.print(table)


@app.command()
def show(
    origin: Annotated[str, typer.Argument()],
    destination: Annotated[str, typer.Argument()],
    depart: Annotated[str, typer.Argument()],
    ret: Annotated[str | None, typer.Argument()] = None,
    cabin: Annotated[str, typer.Option("--cabin", "-c")] = "COACH",
    airlines: Annotated[str | None, typer.Option("--airlines", "-a")] = None,
    via: Annotated[str | None, typer.Option("--via")] = None,
    out_time: Annotated[str | None, typer.Option("--out-time")] = None,
    ret_time: Annotated[str | None, typer.Option("--ret-time")] = None,
    rank: Annotated[
        int,
        typer.Option(
            "--rank", "-r",
            help="Which solution to expand (1 = cheapest). Use --list to see options.",
            min=1,
        ),
    ] = 1,
    list_only: Annotated[
        bool, typer.Option("--list", help="Just list solutions with their ranks")
    ] = False,
    scan_cabins: Annotated[
        bool,
        typer.Option(
            "--scan-cabins",
            help="Probe Premium Economy and Business cabins in parallel and tag each "
                 "segment with cabin availability + sub-fleet hint.",
        ),
    ] = False,
    flight: Annotated[
        str | None,
        typer.Option(
            "--flight",
            help="Only consider itineraries containing these flight numbers (comma-list).",
        ),
    ] = None,
    output: Annotated[
        ShowOutput, typer.Option("--output", "-o", help="Output format")
    ] = ShowOutput.text,
) -> None:
    """Show full segment-by-segment detail for one solution: aircraft, layovers, RBD.

    Re-runs the search and expands the rank-th cheapest solution.
    """
    al_list = airlines.split(",") if airlines else None
    routing = _build_cli_routing(al_list, via)
    slices = build_trip_slices(
        origin=origin,
        destination=destination,
        depart=depart,
        ret=ret,
        outbound_routing=routing,
        return_routing=routing,
        outbound_time_ranges=_validated(parse_time_ranges, out_time),
        return_time_ranges=_validated(parse_time_ranges, ret_time),
    )
    options = SearchOptions(cabin=cabin.upper(), page_size=50)

    with MatrixClient() as client:
        try:
            raw = client.search(slices=slices, **options.search_kwargs())
        except Exception as e:
            console.print(f"[red]Search failed: {e}[/red]")
            raise typer.Exit(1)

        sols = sorted(
            raw.get("solutionList", {}).get("solutions", []),
            key=lambda s: price_float(s.get("displayTotal")) or float("inf"),
        )
        if flight:
            wanted = {f.strip().upper() for f in flight.split(",") if f.strip()}
            def _flights_in_sol(sol: dict) -> set[str]:
                return {
                    f for sl in sol.get("itinerary", {}).get("slices", [])
                    for f in (sl.get("flights") or [])
                }
            sols = [s for s in sols if wanted.issubset(_flights_in_sol(s))]

        if not sols:
            console.print("[yellow]No solutions returned[/yellow]")
            raise typer.Exit(0)

        if list_only:
            ranked = []
            for i, s in enumerate(sols[:30], 1):
                slcs = s.get("itinerary", {}).get("slices", [])
                ranked.append({
                    "rank": i,
                    "id": s.get("id"),
                    "displayTotal": s.get("displayTotal"),
                    "carriers": [c.get("code") for c in s.get("itinerary", {}).get("carriers", [])],
                    "slices": [
                        {
                            "origin": sl["origin"]["code"],
                            "destination": sl["destination"]["code"],
                            "departure": sl.get("departure"),
                            "arrival": sl.get("arrival"),
                            "flights": sl.get("flights", []),
                            "duration": sl.get("duration"),
                        }
                        for sl in slcs
                    ],
                })

            if output == ShowOutput.json:
                print(json_module.dumps(ranked, indent=2))
                return
            if output == ShowOutput.raw:
                print(json_module.dumps(raw, indent=2))
                return

            tbl = Table(title="Available solutions")
            tbl.add_column("Rank")
            tbl.add_column("Price", justify="right")
            tbl.add_column("Carriers")
            tbl.add_column("Itinerary")
            for r in ranked:
                summary = " / ".join(
                    f"{sl['origin']}→{sl['destination']} {'/'.join(sl['flights'])}"
                    for sl in r["slices"]
                )
                tbl.add_row(
                    str(r["rank"]),
                    r.get("displayTotal", "?"),
                    "/".join(r["carriers"]),
                    summary,
                )
            console.print(tbl)
            return

        if rank > len(sols):
            console.print(f"[red]Only {len(sols)} solutions available[/red]")
            raise typer.Exit(1)

        target = sols[rank - 1]
        sid = target.get("id")
        try:
            d = client.detail(raw, sid, slices, **options.detail_kwargs())
            booking = d.get("bookingDetails", {})
        except Exception as e:
            if output == ShowOutput.text:
                console.print(f"[yellow]Detail fetch failed: {e}[/yellow]")
            booking = None

        # Optional: probe W and J cabins so we can annotate each segment with
        # which cabins are bookable on that long-haul flight.
        flight_cabins: dict[str, set[str]] = {}
        if scan_cabins:
            scan_kwargs = {**options.search_kwargs(), "page_size": 200}
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as inner:
                cabin_futures = {
                    label: inner.submit(
                        client.search,
                        slices=slices,
                        **{**scan_kwargs, "cabin": cabin_code},
                    )
                    for label, cabin_code in (("W", "PREMIUM_COACH"), ("J", "BUSINESS"))
                }
                cabin_resps: dict[str, dict] = {}
                for label, fut in cabin_futures.items():
                    try:
                        cabin_resps[label] = fut.result()
                    except Exception:
                        pass
            for label, target_cabin in (("W", "PREMIUM_COACH"), ("J", "BUSINESS")):
                resp = cabin_resps.get(label)
                if resp is None:
                    continue
                per_slice = _verify_cabin_long_haul(client, resp, target_cabin, slices)
                for flts in per_slice.values():
                    for flt in flts:
                        flight_cabins.setdefault(flt, set()).add(label)

    # JSON / raw output: emit and return before rendering text
    if output == ShowOutput.raw:
        print(json_module.dumps({"search": raw, "detail": booking}, indent=2))
        return
    if output == ShowOutput.json:
        out = {
            "rank": rank,
            "displayTotal": target.get("displayTotal"),
            "carriers": [c.get("code") for c in target.get("itinerary", {}).get("carriers", [])],
            "distance": target.get("itinerary", {}).get("distance"),
            "ext": target.get("ext"),
            "slices": [],
        }
        booking_slices = (booking or {}).get("itinerary", {}).get("slices", [])
        target_slices = target.get("itinerary", {}).get("slices", [])
        iter_slices = booking_slices or target_slices
        for i, sl in enumerate(iter_slices):
            seg_data = []
            for seg in sl.get("segments", []):
                aircraft = None
                for leg in seg.get("legs", []):
                    if leg.get("aircraft", {}).get("shortName"):
                        aircraft = leg["aircraft"]["shortName"]
                        break
                seg_data.append({
                    "carrier": seg.get("carrier", {}).get("code"),
                    "flight": seg.get("flight", {}).get("number"),
                    "origin": seg.get("origin", {}).get("code"),
                    "destination": seg.get("destination", {}).get("code"),
                    "departure": seg.get("departure"),
                    "arrival": seg.get("arrival"),
                    "duration": seg.get("duration"),
                    "bookingCodes": [bi.get("bookingCode") for bi in seg.get("bookingInfos", [])],
                    "cabins": list(set(bi.get("cabin") for bi in seg.get("bookingInfos", []))),
                    "aircraft": aircraft,
                })
            slice_dur = sl.get("duration") or sum(s.get("duration", 0) for s in sl.get("segments", []))
            if not slice_dur and i < len(target_slices):
                slice_dur = target_slices[i].get("duration")
            out["slices"].append({
                "origin": sl.get("origin", {}).get("code"),
                "destination": sl.get("destination", {}).get("code"),
                "departure": sl.get("departure"),
                "arrival": sl.get("arrival"),
                "duration": slice_dur,
                "stopCount": sl.get("stopCount"),
                "segments": seg_data,
            })
        print(json_module.dumps(out, indent=2, default=str))
        return

    # Render — header
    console.print(
        f"[bold]Rank #{rank}[/bold]  [cyan]{target.get('displayTotal')}[/cyan]  "
        f"({len(sols)} solutions returned)"
    )
    console.print(
        f"[dim]carriers: {'/'.join(c.get('code','?') for c in target.get('itinerary',{}).get('carriers', []))}[/dim]"
    )
    distance = target.get("itinerary", {}).get("distance", {})
    if distance:
        console.print(
            f"[dim]distance: {distance.get('value')} {distance.get('units','')} | "
            f"price/mile: {target.get('ext',{}).get('pricePerMile','?')}[/dim]"
        )
    console.print()

    # Per-slice segments
    booking_slices = (booking or {}).get("itinerary", {}).get("slices", [])
    target_slices = target.get("itinerary", {}).get("slices", [])
    iter_slices = booking_slices or target_slices
    for i, sl in enumerate(iter_slices, 1):
        origin_code = sl.get("origin", {}).get("code", "?")
        dest_code = sl.get("destination", {}).get("code", "?")
        dep = sl.get("departure", "")
        arr = sl.get("arrival", "")
        # Booking details strips slice.duration — sum from segments or fall back to target
        dur = sl.get("duration", 0)
        if not dur:
            dur = sum(seg.get("duration", 0) for seg in sl.get("segments", []))
            if not dur and i - 1 < len(target_slices):
                dur = target_slices[i - 1].get("duration", 0)
        console.print(
            f"[bold]Leg {i}[/bold]  {origin_code} → {dest_code}  "
            f"{format_time(dep)} → {format_time(arr)}  ({format_duration(dur)})"
        )

        segs = sl.get("segments", [])
        prev_arr = None
        for seg in segs:
            carrier = seg.get("carrier", {}).get("code", "?")
            flt_num = seg.get("flight", {}).get("number", "?")
            seg_o = seg.get("origin", {}).get("code", "?")
            seg_d = seg.get("destination", {}).get("code", "?")
            seg_dep = seg.get("departure", "")
            seg_arr = seg.get("arrival", "")
            seg_dur = seg.get("duration", 0)
            booking_codes = "/".join(
                bi.get("bookingCode", "?") for bi in seg.get("bookingInfos", [])
            ) or "?"
            cabin_classes = "/".join(
                set(bi.get("cabin", "?") for bi in seg.get("bookingInfos", []))
            )
            aircraft = "?"
            for leg in seg.get("legs", []):
                if leg.get("aircraft", {}).get("shortName"):
                    aircraft = leg["aircraft"]["shortName"]
                    break
            if prev_arr:
                from datetime import datetime as _dt
                try:
                    a = _dt.fromisoformat(prev_arr)
                    b = _dt.fromisoformat(seg_dep)
                    layover = int((b - a).total_seconds() / 60)
                    console.print(f"   [dim]layover at {seg_o}: {format_duration(layover)}[/dim]")
                except Exception:
                    pass
            cabin_tag = ""
            fleet_hint = ""
            if scan_cabins:
                flt_key = f"{carrier}{flt_num}"
                cabins_here = flight_cabins.get(flt_key, set())
                marks = []
                marks.append("[green]Y✓[/green]")  # Y always works for a Y-priced search
                marks.append("[green]W✓[/green]" if "W" in cabins_here else "[red]W✗[/red]")
                marks.append("[green]J✓[/green]" if "J" in cabins_here else "[red]J✗[/red]")
                cabin_tag = "  " + " ".join(marks)
                hint = fleet_hints.hint_for(
                    carrier, aircraft,
                    has_w="W" in cabins_here, has_j="J" in cabins_here,
                )
                if hint:
                    fleet_hint = f"  [dim]({hint})[/dim]"
            console.print(
                f"   {carrier} {flt_num:<5}  {seg_o}→{seg_d}  "
                f"{format_time(seg_dep)} → {format_time(seg_arr)}  "
                f"[{format_duration(seg_dur)}]  "
                f"{cabin_classes} ({booking_codes})  •  {aircraft}"
                f"{cabin_tag}{fleet_hint}"
            )
            prev_arr = seg_arr
        console.print()


@app.command(name="airlines")
def airlines_cmd(
    query: Annotated[
        str | None,
        typer.Argument(
            help="Search term (IATA code, ICAO, name substring, country, callsign). "
                 "Omit to dump the full table.",
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=2000)] = 50,
    output: Annotated[
        TableOutput, typer.Option("--output", "-o", help="Output format")
    ] = TableOutput.text,
) -> None:
    """Look up airline IATA codes by name (or vice versa).

    Examples:
        itamx airlines "Airline Name"
        itamx airlines AIRLINE
        itamx airlines "partial name"
        itamx airlines --output csv     # full mapping
    """
    if query:
        results = airline_db.search(query, limit=limit)
    else:
        results = list(airline_db.all_airlines().values())[:limit]

    if output == TableOutput.json:
        print(json_module.dumps(results, indent=2, ensure_ascii=False))
        return

    if output == TableOutput.csv:
        writer = csv_module.writer(sys.stdout)
        writer.writerow(["iata", "icao", "name", "callsign", "country"])
        for a in results:
            writer.writerow([
                a.get("iata", ""), a.get("icao") or "", a.get("name", ""),
                a.get("callsign") or "", a.get("country") or "",
            ])
        return

    if not results:
        console.print(f"[yellow]No airlines matched {query!r}[/yellow]")
        return

    title = f"Airlines matching {query!r}" if query else f"Airlines (first {len(results)})"
    table = Table(title=title)
    table.add_column("IATA")
    table.add_column("ICAO")
    table.add_column("Name")
    table.add_column("Country")
    for a in results:
        table.add_row(
            a.get("iata", ""),
            a.get("icao") or "—",
            a.get("name", ""),
            a.get("country") or "—",
        )
    console.print(table)


@app.command(name="cache")
def cache_cmd(
    action: Annotated[
        str, typer.Argument(help="Action: stats | clear | clear-stale")
    ] = "stats",
    max_age: Annotated[
        int,
        typer.Option(
            "--max-age",
            help="For clear-stale: drop entries older than this many seconds",
            min=0,
        ),
    ] = 24 * 3600,
) -> None:
    """Inspect or purge the on-disk search cache."""
    from itamx import cache as _cache
    if action == "stats":
        s = _cache.stats()
        console.print(
            f"Cache entries: [bold]{s['entries']}[/bold]  "
            f"size: [bold]{s['bytes'] / 1024:.1f} KB[/bold]"
        )
        console.print(
            f"[dim]Disabled: {_cache.is_disabled()}  "
            f"Default TTL: {_cache.DEFAULT_TTL_SECONDS}s[/dim]"
        )
    elif action == "clear":
        n = _cache.purge()
        console.print(f"Cleared [bold]{n}[/bold] entries.")
    elif action == "clear-stale":
        n = _cache.purge(max_age_seconds=max_age)
        console.print(f"Cleared [bold]{n}[/bold] entries older than {max_age}s.")
    else:
        console.print(f"[red]Unknown action: {action!r}[/red]")
        raise typer.Exit(2)


@app.command(
    name="watch",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def watch(
    ctx: typer.Context,
    interval: Annotated[
        int,
        typer.Option(
            "--interval", "-i",
            help="Seconds between runs (default 600 = 10 min). Min 30s.",
            min=30, max=86400,
        ),
    ] = 600,
    diff_only: Annotated[
        bool,
        typer.Option(
            "--diff-only",
            help="Only print output when something changed vs previous run.",
        ),
    ] = False,
    once: Annotated[
        bool,
        typer.Option("--once", help="Run once then exit (sanity-check the command)."),
    ] = False,
    on_change: Annotated[
        str | None,
        typer.Option(
            "--on-change",
            help="Shell command to run when output changes. Diff is piped on stdin.",
        ),
    ] = None,
) -> None:
    """Re-run any itamx command on an interval, alerting when output changes.

    Pass the wrapped command after `--`. Examples:

        itamx watch -i 600 -- search TLV SFO 2026-05-18 2026-05-24 --airlines LY --scan-cabins
        itamx watch -i 1800 --on-change 'osascript -e "display notification \"itamx changed\""' -- \\
            flex TLV SFO 2026-06-01 2026-06-30 --days SUN,MON --airlines LY --scan-cabins

    The wrapped command is run as a subprocess against the same `itamx`
    executable. Stdout is hashed; on hash change the new output is printed
    (and a diff vs previous, line-by-line). With --diff-only, identical runs
    are silent — useful for cron-style background watching.
    """
    import hashlib
    import subprocess
    import time
    import os
    import difflib

    cmd = list(ctx.args)
    if not cmd:
        err_console.print("[red]watch: no command given. Pass it after `--`.[/red]")
        raise typer.Exit(2)

    exe = os.environ.get("ITAMX_BIN") or shutil.which("itamx") or sys.argv[0]
    full_cmd = [exe, *cmd]

    err_console.print(
        f"[dim]watch: every {interval}s — `{' '.join(cmd)}`[/dim]"
    )

    prev_hash: str | None = None
    prev_output: str = ""
    iter_count = 0
    while True:
        iter_count += 1
        started = time.time()
        try:
            proc = subprocess.run(
                full_cmd, capture_output=True, text=True, check=False,
            )
        except Exception as e:
            err_console.print(f"[red]watch: invocation failed: {e}[/red]")
            if once:
                raise typer.Exit(1)
            time.sleep(interval)
            continue

        out = proc.stdout
        h = hashlib.sha256(out.encode("utf-8")).hexdigest()
        changed = (prev_hash is not None and h != prev_hash)

        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        if iter_count == 1:
            err_console.print(f"[dim]{ts}  initial run[/dim]")
            print(out, end="")
        elif changed:
            err_console.print(f"[bold yellow]{ts}  CHANGE detected[/bold yellow]")
            diff = "".join(
                difflib.unified_diff(
                    prev_output.splitlines(keepends=True),
                    out.splitlines(keepends=True),
                    fromfile="previous", tofile="current", n=3,
                )
            )
            print(diff or out, end="")
            if on_change:
                try:
                    subprocess.run(
                        on_change, shell=True, input=diff, text=True, timeout=30,
                    )
                except Exception as e:
                    err_console.print(f"[yellow]on-change failed: {e}[/yellow]")
        else:
            if not diff_only:
                err_console.print(f"[dim]{ts}  no change[/dim]")

        prev_hash = h
        prev_output = out

        if once:
            return
        elapsed = time.time() - started
        sleep_s = max(5, interval - elapsed)
        time.sleep(sleep_s)


if __name__ == "__main__":
    app()
