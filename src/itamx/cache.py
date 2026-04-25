"""Lightweight on-disk cache for Matrix search responses.

Keyed by a stable hash of the inner JSON-RPC payload. Stored as one JSON file
per cache key under `~/.cache/itamx/` (or `$ITAMX_CACHE_DIR`). Each file carries
its own TTL header — readers that find an expired entry skip it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_TTL_SECONDS = 60 * 60  # 1 hour: stale-but-fresh-enough for flex sweeps
_DISABLED_ENV = "ITAMX_NO_CACHE"


def _cache_dir() -> Path:
    base = os.environ.get("ITAMX_CACHE_DIR")
    if base:
        d = Path(base).expanduser()
    else:
        d = Path.home() / ".cache" / "itamx"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key_for(payload: dict[str, Any]) -> str:
    """Stable cache key — the inner /v1/search JSON, normalized."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_disabled() -> bool:
    return bool(os.environ.get(_DISABLED_ENV))


def get(payload: dict[str, Any], *, ttl: int = DEFAULT_TTL_SECONDS) -> dict[str, Any] | None:
    """Return a cached response for `payload` if fresh, else None."""
    if is_disabled():
        return None
    key = _key_for(payload)
    path = _cache_dir() / f"{key}.json"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return None
    written = doc.get("_cached_at")
    if not isinstance(written, (int, float)):
        return None
    if time.time() - written > ttl:
        return None
    return doc.get("response")


def put(payload: dict[str, Any], response: dict[str, Any]) -> None:
    """Persist a response. Atomic via tempfile-rename."""
    if is_disabled():
        return
    key = _key_for(payload)
    path = _cache_dir() / f"{key}.json"
    doc = {"_cached_at": time.time(), "response": response}
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{key}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def purge(*, max_age_seconds: int | None = None) -> int:
    """Delete cached entries. Returns count deleted.

    With max_age_seconds=None, deletes the whole cache.
    """
    d = _cache_dir()
    now = time.time()
    deleted = 0
    for p in d.glob("*.json"):
        if max_age_seconds is None:
            p.unlink()
            deleted += 1
            continue
        try:
            with p.open() as f:
                doc = json.load(f)
            if now - doc.get("_cached_at", 0) > max_age_seconds:
                p.unlink()
                deleted += 1
        except Exception:
            p.unlink()
            deleted += 1
    return deleted


def stats() -> dict[str, int]:
    """Return basic cache stats."""
    d = _cache_dir()
    files = list(d.glob("*.json"))
    total_bytes = sum(f.stat().st_size for f in files)
    return {"entries": len(files), "bytes": total_bytes}
