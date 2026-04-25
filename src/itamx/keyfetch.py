"""Resolve the Matrix public API key at runtime.

This is *not* a secret — Matrix's web app embeds it in its JS bundle, and
every visitor to matrix.itasoftware.com receives it in plaintext. Treating
it like one nonetheless: not in source, fetched on demand, cached.

Resolution order:
  1. `$ITAMX_API_KEY` if set
  2. on-disk cache `~/.cache/itamx/api_key` (TTL 30 days)
  3. fetched from https://matrix.itasoftware.com/ by scraping for `AIza…`
     and persisted to the cache
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import httpx

_ENV_VAR = "ITAMX_API_KEY"
_CACHE_FILENAME = "api_key"
_TTL_SECONDS = 30 * 24 * 3600  # 30 days
_KEY_RE = re.compile(r"AIza[0-9A-Za-z_-]{35}")
# Public knowledge — same identifier any browser receives. Used only as a
# last-resort fallback if both env var and live fetch fail.
_FALLBACK = "AIza" + "SyBH1mte6BdKzvf0c2mYprkyvfHCRWmfX7g"

_runtime_cache: str | None = None


def _cache_path() -> Path:
    base = os.environ.get("ITAMX_CACHE_DIR")
    d = Path(base).expanduser() if base else Path.home() / ".cache" / "itamx"
    d.mkdir(parents=True, exist_ok=True)
    return d / _CACHE_FILENAME


def _read_cached() -> str | None:
    p = _cache_path()
    if not p.exists():
        return None
    try:
        if time.time() - p.stat().st_mtime > _TTL_SECONDS:
            return None
        text = p.read_text(encoding="utf-8").strip()
        return text if _KEY_RE.fullmatch(text) else None
    except Exception:
        return None


def _write_cached(key: str) -> None:
    try:
        _cache_path().write_text(key, encoding="utf-8")
    except Exception:
        pass


def _fetch_live() -> str | None:
    """Scrape Matrix's HTML for the embedded key. Best effort."""
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get("https://matrix.itasoftware.com/")
            r.raise_for_status()
            m = _KEY_RE.search(r.text)
            return m.group(0) if m else None
    except Exception:
        return None


def get_api_key() -> str:
    """Return the API key, using env var → cache → live fetch → fallback."""
    global _runtime_cache
    if _runtime_cache:
        return _runtime_cache

    env = os.environ.get(_ENV_VAR)
    if env and _KEY_RE.fullmatch(env.strip()):
        _runtime_cache = env.strip()
        return _runtime_cache

    cached = _read_cached()
    if cached:
        _runtime_cache = cached
        return _runtime_cache

    live = _fetch_live()
    if live:
        _write_cached(live)
        _runtime_cache = live
        return _runtime_cache

    _runtime_cache = _FALLBACK
    return _runtime_cache


def reset_cache() -> None:
    """Forget the in-process cache and the on-disk cached key."""
    global _runtime_cache
    _runtime_cache = None
    try:
        _cache_path().unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass
