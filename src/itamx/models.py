"""Pydantic models for Matrix search responses.

These cover the shapes we care about for CLI output. Matrix returns much more
than this — we keep `extra="allow"` so the raw dict stays accessible when we
need it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow")


class Carrier(_Base):
    code: str
    shortName: str = Field(alias="shortName", default="")


class Airport(_Base):
    code: str
    name: str = ""


class Slice(_Base):
    origin: Airport
    destination: Airport
    departure: str
    arrival: str
    flights: list[str] = []
    cabins: list[str] = []
    duration: int = 0  # minutes


class Itinerary(_Base):
    slices: list[Slice]
    carriers: list[Carrier] = []
    singleCarrier: Carrier | None = None


class Solution(_Base):
    id: str
    displayTotal: str
    itinerary: Itinerary


class SolutionPage(_Base):
    count: int = 0
    pages: int = 0
    current: int = 0


class SolutionList(_Base):
    solutions: list[Solution] = []
    pages: SolutionPage = Field(default_factory=SolutionPage)


class MatrixCell(_Base):
    minPrice: str | None = None
    minPriceInRow: bool = False
    minPriceInColumn: bool = False
    minPriceInGrid: bool = False


class MatrixRow(_Base):
    label: int | str = 0
    cells: list[MatrixCell] = []


class CarrierStopMatrix(_Base):
    columns: list[dict] = []
    rows: list[MatrixRow] = []


class SearchResponse(_Base):
    solutionList: SolutionList = Field(default_factory=SolutionList)
    carrierStopMatrix: CarrierStopMatrix | None = None
    solutionCount: int = 0
    session: str | None = None
