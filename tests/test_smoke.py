"""End-to-end smoke tests.

Each test invokes a CLI command via Typer's CliRunner against the in-memory
fake Matrix client (see conftest.py). The point isn't to assert specific
prices but to catch crashes, schema regressions, and missing CLI flags as
the codebase evolves.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from itamx.cli import app

runner = CliRunner()


def test_search_text(fake_matrix):
    result = runner.invoke(app, ["search", "TLV", "SFO", "2026-05-18", "2026-05-24"])
    assert result.exit_code == 0, result.output
    assert "USD500.00" in result.output
    assert "LY" in result.output


def test_search_json(fake_matrix):
    result = runner.invoke(
        app,
        ["search", "TLV", "SFO", "2026-05-18", "2026-05-24", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "solutions" in data
    assert data["solutions"][0]["displayTotal"] == "USD500.00"


def test_search_csv(fake_matrix):
    result = runner.invoke(
        app,
        ["search", "TLV", "SFO", "2026-05-18", "2026-05-24", "--output", "csv"],
    )
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert "price,carriers" in lines[0]


def test_search_flight_filter_match(fake_matrix):
    result = runner.invoke(
        app,
        ["search", "TLV", "SFO", "2026-05-18", "2026-05-24", "--flight", "LY5,LY10"],
    )
    assert result.exit_code == 0, result.output
    assert "LY5" in result.output


def test_search_flight_filter_excludes(fake_matrix):
    result = runner.invoke(
        app,
        ["search", "TLV", "SFO", "2026-05-18", "2026-05-24", "--flight", "LY999"],
    )
    assert result.exit_code == 0
    # filtered to zero — empty diagnostic should NOT crash
    assert "No solutions" in result.output or "filter" in result.output.lower()


def test_search_max_duration(fake_matrix):
    # Fixture has ~32h total duration; --max-duration 10 hours filters it out.
    result = runner.invoke(
        app,
        ["search", "TLV", "SFO", "2026-05-18", "2026-05-24", "--max-duration", "10"],
    )
    assert result.exit_code == 0
    assert "No solutions" in result.output


def test_show_text(fake_matrix):
    result = runner.invoke(
        app,
        ["show", "TLV", "SFO", "2026-05-18", "2026-05-24", "--rank", "1"],
    )
    assert result.exit_code == 0, result.output
    # Rich may hard-wrap "Boeing 787" across lines; check loosely.
    assert "Boeing" in result.output
    assert "LY 5" in result.output


def test_show_list(fake_matrix):
    result = runner.invoke(
        app,
        ["show", "TLV", "SFO", "2026-05-18", "2026-05-24", "--list"],
    )
    assert result.exit_code == 0, result.output
    assert "Rank" in result.output


def test_show_json(fake_matrix):
    result = runner.invoke(
        app,
        ["show", "TLV", "SFO", "2026-05-18", "2026-05-24", "--rank", "1", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["displayTotal"] == "USD500.00"
    assert len(data["slices"]) == 2


def test_lookup(fake_matrix):
    result = runner.invoke(app, ["lookup", "Tel"])
    assert result.exit_code == 0, result.output
    assert "TLV" in result.output


def test_lookup_json(fake_matrix):
    result = runner.invoke(app, ["lookup", "Tel", "--output", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data[0]["code"] == "TLV"


def test_airlines_search():
    """No fake_matrix needed — bundled JSON file."""
    result = runner.invoke(app, ["airlines", "Air France"])
    assert result.exit_code == 0, result.output
    assert "AF" in result.output


def test_airlines_csv():
    result = runner.invoke(app, ["airlines", "lufthansa", "--output", "csv"])
    assert result.exit_code == 0, result.output
    assert "LH" in result.output


def test_flex_text(fake_matrix):
    result = runner.invoke(
        app,
        ["flex", "TLV", "SFO", "2026-05-01", "2026-05-08", "--duration", "6"],
    )
    assert result.exit_code == 0, result.output


def test_flex_json(fake_matrix):
    result = runner.invoke(
        app,
        ["flex", "TLV", "SFO", "2026-05-01", "2026-05-08",
         "--duration", "6", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    # CliRunner mixes stderr into stdout; the progress line is on stderr.
    # Strip non-JSON prefix before parsing.
    text = result.output
    json_start = text.find("[")
    assert json_start != -1, text
    data = json.loads(text[json_start:])
    assert isinstance(data, list)


def test_multi(fake_matrix):
    result = runner.invoke(
        app,
        [
            "multi",
            "--leg", "TLV:LHR:2026-08-05",
            "--leg", "LHR:CDG:2026-08-10",
            "--leg", "CDG:TLV:2026-08-15",
        ],
    )
    assert result.exit_code == 0, result.output


def test_cache_stats(fake_matrix, monkeypatch):
    monkeypatch.delenv("ITAMX_NO_CACHE", raising=False)
    result = runner.invoke(app, ["cache", "stats"])
    assert result.exit_code == 0, result.output
    assert "Cache entries:" in result.output


def test_cache_clear(fake_matrix, monkeypatch):
    monkeypatch.delenv("ITAMX_NO_CACHE", raising=False)
    result = runner.invoke(app, ["cache", "clear"])
    assert result.exit_code == 0, result.output


def test_mcp_config():
    result = runner.invoke(app, ["mcp-config"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "mcpServers" in data


def test_watch_once_no_command():
    result = runner.invoke(app, ["watch", "--once", "--"])
    # missing wrapped command
    assert result.exit_code == 2


# ---------- Library API ----------


def test_airlines_resolve():
    from itamx.airlines import resolve

    assert resolve("LY") == "LY"
    assert resolve("Air France") == "AF"
    assert resolve("klm") == "KL"
    assert resolve("nonexistentairline") is None


def test_fleet_hint_ly():
    from itamx import fleet_hints

    assert fleet_hints.hint_for("LY", "Boeing 787-9", has_w=True, has_j=True) is not None
    assert "V.1" in fleet_hints.hint_for("LY", "Boeing 787-9", has_w=True, has_j=True)
    assert "V.2" in fleet_hints.hint_for("LY", "Boeing 787-9", has_w=False, has_j=True)
    assert fleet_hints.hint_for("LY", "Boeing 787-9", has_w=False, has_j=False) is None


def test_fleet_hint_unknown_carrier():
    from itamx import fleet_hints

    assert fleet_hints.hint_for("XX", "Boeing 787", has_w=True, has_j=True) is None


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("ITAMX_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("ITAMX_NO_CACHE", raising=False)
    from itamx import cache

    payload = {"some": "request", "n": 1}
    response = {"some": "response"}
    assert cache.get(payload) is None
    cache.put(payload, response)
    assert cache.get(payload) == response


def test_client_normalize_cabin():
    from itamx.client import _normalize_cabin

    assert _normalize_cabin("PREMIUM_COACH") == "PREMIUM-COACH"
    assert _normalize_cabin("PREMIUM_ECONOMY") == "PREMIUM-COACH"
    assert _normalize_cabin("BUSINESS") == "BUSINESS"
    assert _normalize_cabin("COACH") == "COACH"


def test_keyfetch_env_var(monkeypatch):
    from itamx import keyfetch

    monkeypatch.setenv("ITAMX_API_KEY", "AIzaABCDEFGHIJKLMNOPQRSTUVWXYZ012345678")
    keyfetch.reset_cache()
    assert keyfetch.get_api_key() == "AIzaABCDEFGHIJKLMNOPQRSTUVWXYZ012345678"


def test_keyfetch_rejects_malformed(monkeypatch):
    from itamx import keyfetch

    monkeypatch.setenv("ITAMX_API_KEY", "not-a-real-key")
    keyfetch.reset_cache()
    # Falls through env (rejected as malformed) → cache (empty) → live (offline)
    # → fallback. We just verify it returns something matching the AIza pattern.
    import re
    assert re.fullmatch(r"AIza[0-9A-Za-z_-]{35}", keyfetch.get_api_key())
