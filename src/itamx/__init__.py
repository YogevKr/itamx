"""Public package exports for itamx."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from itamx.client import MatrixClient, PaxCount, Slice, build_search_body

try:
    __version__ = version("itamx")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = [
    "MatrixClient",
    "PaxCount",
    "Slice",
    "__version__",
    "build_search_body",
]
