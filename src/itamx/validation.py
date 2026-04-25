"""Pure input validation and CLI choice types."""

from __future__ import annotations

from enum import Enum


WEEKDAY_INDEX = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


class SearchOutput(str, Enum):
    text = "text"
    json = "json"
    csv = "csv"
    raw = "raw"


class TableOutput(str, Enum):
    text = "text"
    json = "json"
    csv = "csv"


class ShowOutput(str, Enum):
    text = "text"
    json = "json"
    raw = "raw"


class SortOrder(str, Enum):
    default = "default"
    price = "price"
    duration = "duration"
    departure_time = "departureTime"
    arrival_time = "arrivalTime"


def parse_time_ranges(spec: str | None) -> list[tuple[str, str]]:
    """Parse time-window spec like '6-20' or '0-6,18-23' into HH:MM pairs."""
    if not spec:
        return []
    out: list[tuple[str, str]] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" not in chunk:
            raise ValueError(f"Time range must be HH-HH (got {chunk!r})")
        start, end = chunk.split("-", 1)
        out.append((_to_hhmm(start), _to_hhmm(end)))
    return out


def parse_int_range(spec: str) -> list[int]:
    """Parse '5' -> [5] or '5-8' -> [5, 6, 7, 8] for use in --stay."""
    try:
        if "-" in spec:
            start, end = (int(part.strip()) for part in spec.split("-", 1))
            if end < start:
                raise ValueError(f"--stay range end is before start (got {spec!r})")
            values = list(range(start, end + 1))
        else:
            values = [int(spec.strip())]
    except ValueError:
        raise ValueError(f"--stay must be 'N' or 'N-M' (got {spec!r})")

    bad = [value for value in values if value < 0 or value > 60]
    if bad:
        raise ValueError("--stay values must be between 0 and 60 days")
    return values


def parse_weekdays(spec: str) -> set[int] | None:
    tokens = [d.strip().upper() for d in spec.split(",") if d.strip()]
    if not tokens:
        return None
    invalid = [d for d in tokens if d not in WEEKDAY_INDEX]
    if invalid:
        valid = ", ".join(WEEKDAY_INDEX)
        raise ValueError(f"--days must contain only {valid}; got {', '.join(invalid)}")
    return {WEEKDAY_INDEX[d] for d in tokens}


def _to_hhmm(value: str) -> str:
    value = value.strip()
    if ":" in value:
        return value
    if value.isdigit():
        return f"{int(value):02d}:00"
    raise ValueError(f"Bad time {value!r}; expected HH or HH:MM")
