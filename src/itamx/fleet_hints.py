"""Carrier-specific sub-fleet hints based on aircraft type + cabin presence.

When `itamx show --scan-cabins` annotates a segment, we know the aircraft
short name and which cabins (W, J) verified for that flight on the queried
date. For dual-config carriers, that combination uniquely identifies the
sub-fleet:

  LY 787-9 with PE  →  V.1 (the original 8 aircraft, has Premium Economy)
  LY 787-9 no PE    →  V.2 (ex-Air China, removed PE cabin)
  BA 777-200 with W →  Club World World Traveller Plus (the standard product)
  BA 777-200 no W   →  ex-Bmi domestic config (no WTP)
  AF 777-300 with W →  with La Première / Premium Economy
  AF 777-300 no W   →  high-density 3-class config

Lookup keyed by (carrier, aircraft_substring). The aircraft substring is a
case-insensitive token search so "Boeing 787" matches "Boeing 787-9".
"""

from __future__ import annotations

# Each entry: (carrier_iata, aircraft_substring) → {"with_w": label, "without_w_with_j": label}
# The "without_w_with_j" key fires when W is not verified but J is — the
# stronger evidence that the aircraft actually lacks a PE cabin (vs PE just
# being closed today).
_HINTS: dict[tuple[str, str], dict[str, str]] = {
    ("LY", "787"): {
        "with_w": "787-9 V.1 — has PE",
        "without_w_with_j": "787-9 V.2 — no PE",
    },
    ("BA", "777-200"): {
        "with_w": "777-200 — World Traveller Plus",
        "without_w_with_j": "777-200 high-density — no WTP",
    },
    ("BA", "777-300"): {
        "with_w": "777-300ER — full premium config",
    },
    ("AF", "777-300"): {
        "with_w": "777-300ER — La Première or PE config",
        "without_w_with_j": "777-300ER high-density — no PE",
    },
    ("AF", "777-200"): {
        "with_w": "777-200ER",
        "without_w_with_j": "777-200ER ex-medium-haul — no PE",
    },
    ("LH", "747-8"): {
        "with_w": "747-8i — full premium",
    },
    ("LH", "A340"): {
        "with_w": "A340-600 — premium config",
        "without_w_with_j": "A340 — no PE",
    },
    ("LH", "A350"): {
        "with_w": "A350-900 — premium config",
    },
    ("LH", "777"): {
        "with_w": "777-9 (newest)",
    },
    ("UA", "787"): {
        "with_w": "787 — Premium Plus",
    },
    ("UA", "777"): {
        "with_w": "777 — Premium Plus",
        "without_w_with_j": "777 high-density — no Premium Plus",
    },
    ("UA", "767"): {
        "with_w": "767 — Polaris config",
    },
    ("DL", "767"): {
        "with_w": "767 — Premium Select config",
        "without_w_with_j": "767 transcon — no Premium Select",
    },
    ("DL", "A350"): {
        "with_w": "A350-900 — Premium Select",
    },
    ("DL", "A330"): {
        "with_w": "A330 — Premium Select",
    },
    ("VS", "A330"): {
        "with_w": "A330 — Premium",
    },
}


def hint_for(
    carrier: str | None,
    aircraft: str | None,
    *,
    has_w: bool,
    has_j: bool,
) -> str | None:
    """Return a short sub-fleet label or None.

    Resolution: (carrier, aircraft) substring match against the hint table.
    Then pick the most specific entry based on cabin presence.
    """
    if not carrier or not aircraft:
        return None
    aircraft_l = aircraft.lower()
    carrier_u = carrier.upper()
    for (c, ac), variants in _HINTS.items():
        if c != carrier_u:
            continue
        if ac.lower() not in aircraft_l:
            continue
        if has_w and "with_w" in variants:
            return variants["with_w"]
        if not has_w and has_j and "without_w_with_j" in variants:
            return variants["without_w_with_j"]
        # Falls through if no condition matched for this entry.
    return None
