"""Small rendering/formatting helpers shared by CLI commands."""

from __future__ import annotations

import re


PRICE_RE = re.compile(r"^([A-Z]{3})([\d.]+)$")


def price_float(value: str | None) -> float | None:
    if not value:
        return None
    match = PRICE_RE.match(value)
    if match:
        return float(match.group(2))
    return None


def format_duration(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m"


def format_time(iso_ts: str) -> str:
    """Trim ISO timestamp to 'MM-DD HH:MM'."""
    if "T" not in iso_ts:
        return iso_ts
    date, time = iso_ts.split("T", 1)
    return f"{date[5:]} {time[:5]}"


def extract_rbd(booking_details: dict | None) -> str:
    """Format RBD letters per slice from a bookingDetails response."""
    if not booking_details:
        return "\u2014"
    if "error" in booking_details:
        return "err"
    out_parts: list[str] = []
    for slice_ in booking_details.get("itinerary", {}).get("slices", []):
        codes = []
        for seg in slice_.get("segments", []):
            for booking_info in seg.get("bookingInfos", []):
                code = booking_info.get("bookingCode")
                if code:
                    codes.append(code)
        out_parts.append("/".join(codes) if codes else "?")
    return " | ".join(out_parts) if out_parts else "\u2014"
