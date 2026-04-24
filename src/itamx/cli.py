"""itamx: CLI for ITA Matrix airfare search."""

from __future__ import annotations

import json as json_module
import re
import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from itamx.client import MatrixClient
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
        typer.Option(
            "--cabin", "-c",
            help="COACH, PREMIUM_COACH, BUSINESS, FIRST",
        ),
    ] = "COACH",
    adults: Annotated[int, typer.Option("--adults", "-a", min=1, max=9)] = 1,
    max_stops: Annotated[
        int | None,
        typer.Option(
            "--max-stops", "-s",
            help="0 = nonstop only; 1 = up to 1 stop; etc. (relative to min)",
            min=0,
        ),
    ] = None,
    page_size: Annotated[
        int, typer.Option("--limit", help="Max solutions to return", min=1, max=500)
    ] = 50,
    output: Annotated[
        str, typer.Option("--output", "-o", help="text | json | raw")
    ] = "text",
    top: Annotated[int, typer.Option("--top", help="Rows to show in text mode")] = 15,
) -> None:
    """Search flights. Returns a price-sorted table of solutions."""
    origin = origin.upper()
    destination = destination.upper()

    with MatrixClient() as client:
        try:
            raw = client.search(
                origin=origin,
                destination=destination,
                depart_date=depart,
                return_date=ret,
                cabin=cabin.upper(),
                adults=adults,
                max_stops=max_stops,
                page_size=page_size,
            )
        except Exception as e:
            console.print(f"[red]Search failed: {e}[/red]")
            raise typer.Exit(1)

    if output == "raw":
        print(json_module.dumps(raw, indent=2))
        return

    parsed = SearchResponse.model_validate(raw)
    solutions = parsed.solutionList.solutions
    solutions.sort(key=lambda s: _price_float(s.displayTotal) or float("inf"))

    if output == "json":
        print(json_module.dumps(raw, indent=2))
        return

    # Text output: carrier matrix summary + top N solutions
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

    for sol in solutions[:top]:
        carriers = "/".join(c.code for c in sol.itinerary.carriers) or "?"
        slices = sol.itinerary.slices
        out = slices[0] if slices else None
        ret_s = slices[1] if len(slices) > 1 else None
        out_desc = (
            f"{_format_time(out.departure)} {out.origin.code}→{out.destination.code} "
            f"({'/'.join(out.flights)}, {_format_duration(out.duration)})"
            if out
            else "?"
        )
        ret_desc = (
            f"{_format_time(ret_s.departure)} {ret_s.origin.code}→{ret_s.destination.code} "
            f"({'/'.join(ret_s.flights)}, {_format_duration(ret_s.duration)})"
            if ret_s
            else "—"
        )
        total_dur = sum(s.duration for s in slices)
        sol_table.add_row(
            sol.displayTotal,
            carriers,
            out_desc,
            ret_desc,
            _format_duration(total_dur),
        )
    console.print(sol_table)
    console.print(f"\n[dim]Returned {len(solutions)} solution(s) of {parsed.solutionCount} total.[/dim]")


if __name__ == "__main__":
    app()
