"""Shared request options for Matrix search and detail calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SearchOptions:
    cabin: str = "COACH"
    max_stops: int | None = None
    page_size: int = 50
    sorts: str = "default"
    change_of_airport: bool = True
    currency: str | None = None
    sales_city: str | None = None

    def search_kwargs(self) -> dict[str, Any]:
        return {
            "cabin": self.cabin,
            "max_stops": self.max_stops,
            "page_size": self.page_size,
            "change_of_airport": self.change_of_airport,
            "currency": self.currency,
            "sales_city": self.sales_city,
            "sorts": self.sorts,
        }

    def detail_kwargs(self) -> dict[str, Any]:
        return {
            "cabin": self.cabin,
            "max_stops": self.max_stops,
            "change_of_airport": self.change_of_airport,
            "currency": self.currency,
            "sales_city": self.sales_city,
            "sorts": self.sorts,
        }
