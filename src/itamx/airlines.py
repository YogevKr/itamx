"""Airline IATA ↔ name lookup.

Bundles ~986 active carriers with IATA codes, sourced from OpenFlights
(https://github.com/jpatokal/openflights/blob/master/data/airlines.dat,
public domain). Loaded lazily on first call.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import TypedDict


class Airline(TypedDict):
    iata: str
    icao: str | None
    name: str
    callsign: str | None
    country: str | None


@lru_cache(maxsize=1)
def _load() -> dict[str, Airline]:
    raw = (files("itamx.data") / "airlines.json").read_text(encoding="utf-8")
    return json.loads(raw)


def all_airlines() -> dict[str, Airline]:
    """Return the full IATA → Airline mapping."""
    return _load()


def by_iata(iata: str) -> Airline | None:
    """Look up a carrier by IATA code. Case-insensitive."""
    return _load().get(iata.upper())


def search(query: str, *, limit: int = 20) -> list[Airline]:
    """Substring-match across name / IATA / ICAO / callsign / country.

    Sort order (lower rank = better):
      0. exact IATA match
      1. exact name match (case-insensitive)
      2. exact ICAO match
      3. name starts with query (case-insensitive)
      4. name contains query
      5. country / callsign contains query
    """
    if not query:
        return []
    q = query.strip()
    qu = q.upper()
    ql = q.lower()
    table = _load()

    exact_iata = table.get(qu)
    matches: list[tuple[int, Airline]] = []
    if exact_iata:
        matches.append((0, exact_iata))

    for code, a in table.items():
        if a is exact_iata:
            continue
        rank: int | None = None
        name_l = a["name"].lower()
        icao = (a.get("icao") or "").upper()
        callsign = (a.get("callsign") or "").lower()
        country = (a.get("country") or "").lower()
        if name_l == ql:
            rank = 1
        elif icao == qu:
            rank = 2
        elif name_l.startswith(ql):
            rank = 3
        elif ql in name_l:
            rank = 4
        elif ql and (ql in callsign or ql in country):
            rank = 5
        if rank is not None:
            matches.append((rank, a))

    # Within a rank, shorter name first (preferred) then alphabetical IATA
    matches.sort(key=lambda r: (r[0], len(r[1]["name"]), r[1]["iata"]))
    return [m[1] for m in matches[:limit]]


def resolve(token: str) -> str | None:
    """Best-effort: turn `'Air France'` or `'lufthansa'` or `'AF'` → `'AF'`.

    Returns the IATA code if there's a strong match, else None.
    Resolution order:
      1. exact IATA (2-letter)
      2. exact name (case-insensitive)
      3. exact ICAO (3-letter)
      4. exactly one airline whose name starts with the token
      5. otherwise None (ambiguous or unknown)
    """
    if not token:
        return None
    token = token.strip()
    table = _load()

    # 1. exact IATA
    if len(token) == 2:
        a = table.get(token.upper())
        if a:
            return a["iata"]

    tl = token.lower()
    tu = token.upper()

    # 2. exact name match (e.g. "klm" → "KLM Royal Dutch Airlines"? no — that's not exact;
    #    but "lufthansa" → "Lufthansa" yes)
    for code, a in table.items():
        if a["name"].lower() == tl:
            return code

    # 3. exact ICAO
    for code, a in table.items():
        if (a.get("icao") or "").upper() == tu:
            return code

    # 4. unique startswith match
    starts = [a for a in table.values() if a["name"].lower().startswith(tl)]
    if len(starts) == 1:
        return starts[0]["iata"]

    return None
