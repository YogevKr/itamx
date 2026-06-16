from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from itamx.cli import app
from itamx.elal import (
    ELAL_BASE_URL,
    ELAL_FAST_PATH,
    ELAL_SESSION_CREATE_PATH,
    ElAlAwardAuthError,
    ElAlBrowserLoginResult,
    ElAlAwardClient,
    ElAlPaxCount,
    build_award_search_body,
    build_booking_session_body,
    extract_elal_award_headers_from_har,
    load_elal_credentials,
    load_elal_session,
    login_with_elal_credentials,
    normalize_award_results,
    save_elal_credentials,
    save_elal_session,
)


@pytest.fixture(autouse=True)
def isolate_elal_config(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ITAMX_ELAL_SESSION_FILE", str(tmp_path / "elal-session.json"))
    monkeypatch.setenv("ITAMX_ELAL_CREDENTIALS_FILE", str(tmp_path / "elal-credentials.json"))


OUTBOUND_RESPONSE = {
    "data": {
        "trip": {
            "outbound": {
                "directBounds": {
                    "bounds": [
                        {
                            "id": "20261126_TLV_1",
                            "duration": 20100,
                            "segments": [
                                {
                                    "duration": 20100,
                                    "carrier": "LY",
                                    "flightNumber": "311",
                                    "departureDate": "2026-11-26T05:30:00.000Z",
                                    "arrivalDate": "2026-11-26T09:05:00.000Z",
                                    "departureAirport": {"code": "TLV"},
                                    "arrivalAirport": {"code": "LTN"},
                                    "aircraftType": "738",
                                }
                            ],
                            "fares": [
                                {
                                    "idOffer": 40,
                                    "rbd": "L",
                                    "name": "BONASECOL1",
                                    "bookingClassName": "economy",
                                    "familyName": "BONASECOL1",
                                    "netPrice": {
                                        "cash": {"amount": 296.37, "currencyCode": "USD"},
                                        "points": {
                                            "amount": 29775.0,
                                            "minAmountToConsume": 0.0,
                                            "taxes": {"amount": 97.87, "currencyCode": "USD"},
                                        },
                                    },
                                    "nbSeatLeft": 4,
                                }
                            ],
                        }
                    ]
                },
                "indirectBounds": {"bounds": []},
                "railAndFlyBounds": {"bounds": []},
            }
        }
    },
    "errors": [],
}

INBOUND_RESPONSE = {
    "data": {
        "trip": {
            "returnBound": {
                "directBounds": {
                    "bounds": [
                        {
                            "id": "20261201_LHR_1",
                            "duration": 16500,
                            "segments": [
                                {
                                    "duration": 16500,
                                    "carrier": "LY",
                                    "flightNumber": "316",
                                    "departureDate": "2026-12-01T14:20:00.000Z",
                                    "arrivalDate": "2026-12-01T20:55:00.000Z",
                                    "departureAirport": {"code": "LHR"},
                                    "arrivalAirport": {"code": "TLV"},
                                    "aircraftType": "789",
                                }
                            ],
                            "fares": [
                                {
                                    "idOffer": 268,
                                    "rbd": "E",
                                    "name": "BONCLECO",
                                    "bookingClassName": "economy",
                                    "familyName": "BONCLECO",
                                    "netPrice": {
                                        "cash": {"amount": 330.90, "currencyCode": "USD"},
                                        "points": {
                                            "amount": 18000.0,
                                            "minAmountToConsume": 0.0,
                                            "taxes": {"amount": 208.90, "currencyCode": "USD"},
                                        },
                                    },
                                    "nbSeatLeft": 8,
                                    "bestValue": True,
                                }
                            ],
                        }
                    ]
                },
                "indirectBounds": {"bounds": []},
                "railAndFlyBounds": {"bounds": []},
            }
        }
    },
    "errors": [],
}


def test_build_award_search_body_roundtrip() -> None:
    body = build_award_search_body(
        origin="tlv",
        destination="lon",
        depart_date="2026-11-26",
        return_date="2026-12-01",
        cabin="ECONOMY,BUSINESS",
        pax=ElAlPaxCount(adults=2, children=1, infants=1),
    )

    assert body["tripType"] == "R"
    assert body["origin"] == ["TLV"]
    assert body["destination"] == ["LON"]
    assert body["departureDate"] == ["2026-11-26T00:00:00.000Z"]
    assert body["returnDate"] == "2026-12-01T00:00:00.000Z"
    assert body["cabinClass"] == ["E", "B"]
    assert body["passengers"] == {"SRC": 0, "ADT": 2, "INF": 1, "CHD": 1, "YTH": 0}


def test_build_booking_session_body() -> None:
    body = build_booking_session_body(market="IL", language="he", time_zone="Asia/Jerusalem")

    assert body["market"] == "IL"
    assert body["language"] == "he"
    assert body["accessType"] == "web"
    assert body["timeZone"] == "Asia/Jerusalem"
    assert body["clientId"]
    assert body["clientSecret"]


def test_extract_elal_award_headers_from_har(tmp_path) -> None:
    har_path = tmp_path / "elal.har"
    har_path.write_text(
        json.dumps(
            {
                "log": {
                    "entries": [
                        {"request": {"url": "https://example.test", "headers": []}},
                        {
                            "request": {
                                "url": ELAL_BASE_URL + ELAL_FAST_PATH,
                                "headers": [
                                    {"name": "Authorization", "value": "Bearer token"},
                                    {"name": "Cookie", "value": "SESSION=abc"},
                                    {"name": "User-Agent", "value": "UA"},
                                    {"name": "Content-Length", "value": "123"},
                                ],
                            }
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    headers = extract_elal_award_headers_from_har(har_path)

    assert headers["Authorization"] == "Bearer token"
    assert headers["Cookie"] == "SESSION=abc"
    assert headers["User-Agent"] == "UA"
    assert "Content-Length" not in headers


def test_save_and_load_elal_session(tmp_path) -> None:
    path = tmp_path / "session.json"

    saved = save_elal_session(
        {
            "Authorization": "Bearer token",
            "Cookie": "BOOKINGSESSION=abc",
            "Content-Length": "123",
        },
        path,
        source="test",
    )
    loaded = load_elal_session(saved)

    assert loaded is not None
    assert loaded.path == path
    assert loaded.authorization == "Bearer token"
    assert loaded.cookie == "BOOKINGSESSION=abc"
    assert "Content-Length" not in loaded.headers


def test_save_and_load_elal_credentials(tmp_path) -> None:
    path = tmp_path / "credentials.json"

    saved = save_elal_credentials(username="member", password="secret", path=path)
    loaded = load_elal_credentials(saved)

    assert loaded is not None
    assert loaded.path == path
    assert loaded.username == "member"
    assert loaded.password == "secret"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_normalize_award_results() -> None:
    rows = normalize_award_results({"outbound": OUTBOUND_RESPONSE, "inbound": INBOUND_RESPONSE})

    assert len(rows) == 2
    assert rows[0]["direction"] == "outbound"
    assert rows[0]["flights"] == ["LY311"]
    assert rows[0]["points"] == 29775.0
    assert rows[0]["tax_amount"] == 97.87
    assert rows[0]["duration_minutes"] == 335
    assert rows[1]["direction"] == "inbound"
    assert rows[1]["best_value"] is True


def test_elal_client_posts_then_reads_bounds() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            body = json.loads(request.content.decode())
            assert body["tripType"] == "R"
            assert body["cabinClass"] == ["E", "P", "B"]
            return httpx.Response(200, json={"errors": []})
        if request.url.path.endswith("/outbound"):
            return httpx.Response(200, json=OUTBOUND_RESPONSE)
        if request.url.path.endswith("/inbound"):
            return httpx.Response(200, json=INBOUND_RESPONSE)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    http = httpx.Client(base_url=ELAL_BASE_URL, transport=httpx.MockTransport(handler))
    with ElAlAwardClient(authorization="token", http_client=http) as client:
        data = client.search_awards(
            origin="TLV",
            destination="LON",
            depart_date="2026-11-26",
            return_date="2026-12-01",
        )

    assert calls == [
        ("POST", "/bfm/service/extly/booking/search/points/fast"),
        ("GET", "/bfm/service/extly/booking/search/points/outbound"),
        ("GET", "/bfm/service/extly/booking/search/points/inbound"),
    ]
    assert data["success"] is True
    assert data["trip_type"] == "ROUND_TRIP"
    assert data["results"][0]["flights"] == ["LY311"]
    assert http.headers["Authorization"] == "Bearer token"


def test_elal_client_creates_authorization_from_cookie() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("Authorization")))
        if request.url.path == ELAL_SESSION_CREATE_PATH:
            body = json.loads(request.content.decode())
            assert body["clientId"]
            assert body["market"] == "IL"
            return httpx.Response(200, json={"id": "session-token"})
        if request.method == "POST":
            assert request.headers["Authorization"] == "Bearer session-token"
            return httpx.Response(200, json={"errors": []})
        if request.url.path.endswith("/outbound"):
            return httpx.Response(200, json=OUTBOUND_RESPONSE)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    http = httpx.Client(base_url=ELAL_BASE_URL, transport=httpx.MockTransport(handler))
    with ElAlAwardClient(cookie="BOOKINGSESSION=abc", http_client=http) as client:
        data = client.search_awards(
            origin="TLV",
            destination="LON",
            depart_date="2026-11-26",
        )

    assert calls[0] == ("POST", "/bfm/rest/session/create", None)
    assert data["success"] is True
    assert http.headers["Authorization"] == "Bearer session-token"


def test_elal_client_reports_link11_session_block() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == ELAL_SESSION_CREATE_PATH
        return httpx.Response(492, text="Link11 access denied.")

    http = httpx.Client(base_url=ELAL_BASE_URL, transport=httpx.MockTransport(handler))

    try:
        ElAlAwardClient(cookie="BOOKINGSESSION=abc", http_client=http)
    except ElAlAwardAuthError as exc:
        assert "Link11" in str(exc)
        assert "HTTP 492" in str(exc)
    else:
        raise AssertionError("expected ElAlAwardAuthError")


def test_elal_client_requires_session_material(monkeypatch) -> None:
    monkeypatch.delenv("ITAMX_ELAL_AUTHORIZATION", raising=False)
    monkeypatch.delenv("ITAMX_ELAL_COOKIE", raising=False)

    try:
        ElAlAwardClient()
    except ElAlAwardAuthError as exc:
        assert "elal-login" in str(exc)
    else:
        raise AssertionError("expected ElAlAwardAuthError")


def test_login_with_credentials_reports_link11_block() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "saml2sso" in str(request.url)
        return httpx.Response(492, text="Link11 access denied.")

    http = httpx.Client(transport=httpx.MockTransport(handler))

    try:
        login_with_elal_credentials(username="member", password="secret", http_client=http)
    except ElAlAwardAuthError as exc:
        assert "Link11" in str(exc)
        assert "raw credential login" in str(exc)
    else:
        raise AssertionError("expected ElAlAwardAuthError")


def test_elal_login_cli_accepts_username_password(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_login_with_credentials(**kwargs):
        captured.update(kwargs)
        return {
            "Authorization": "Bearer token",
            "Cookie": "BOOKINGSESSION=abc",
            "User-Agent": "UA",
        }

    monkeypatch.setattr("itamx.cli.login_with_elal_credentials", fake_login_with_credentials)
    session_path = tmp_path / "session.json"

    result = CliRunner().invoke(
        app,
        [
            "elal-login",
            "--username",
            "member",
            "--password",
            "secret",
            "--session-file",
            str(session_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["username"] == "member"
    assert captured["password"] == "secret"
    loaded = load_elal_session(session_path)
    assert loaded is not None
    assert loaded.authorization == "Bearer token"
    assert loaded.cookie == "BOOKINGSESSION=abc"


def test_elal_login_cli_loads_credentials_file(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_login_with_credentials(**kwargs):
        captured.update(kwargs)
        return {
            "Authorization": "Bearer token",
            "Cookie": "BOOKINGSESSION=abc",
            "User-Agent": "UA",
        }

    monkeypatch.setattr("itamx.cli.login_with_elal_credentials", fake_login_with_credentials)
    credentials_path = save_elal_credentials(
        username="member",
        password="secret",
        path=tmp_path / "credentials.json",
    )
    session_path = tmp_path / "session.json"

    result = CliRunner().invoke(
        app,
        [
            "elal-login",
            "--credentials-file",
            str(credentials_path),
            "--session-file",
            str(session_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["username"] == "member"
    assert captured["password"] == "secret"
    assert load_elal_session(session_path) is not None


def test_elal_login_cli_saves_credentials(monkeypatch, tmp_path) -> None:
    def fake_login_with_credentials(**kwargs):
        return {
            "Authorization": "Bearer token",
            "Cookie": "BOOKINGSESSION=abc",
            "User-Agent": "UA",
        }

    monkeypatch.setattr("itamx.cli.login_with_elal_credentials", fake_login_with_credentials)
    credentials_path = tmp_path / "credentials.json"

    result = CliRunner().invoke(
        app,
        [
            "elal-login",
            "--username",
            "member",
            "--password",
            "secret",
            "--save-credentials",
            "--credentials-file",
            str(credentials_path),
            "--session-file",
            str(tmp_path / "session.json"),
        ],
    )

    assert result.exit_code == 0, result.output
    loaded = load_elal_credentials(credentials_path)
    assert loaded is not None
    assert loaded.username == "member"
    assert loaded.password == "secret"


def test_elal_browser_login_cli_saves_session(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_browser_login(**kwargs):
        captured.update(kwargs)
        return ElAlBrowserLoginResult(
            headers={
                "Authorization": "Bearer browser-token",
                "Cookie": "BOOKINGSESSION=abc",
                "User-Agent": "UA",
            },
            source="points-request",
            captured_url="https://booking.elal.com/bfm/service/extly/booking/search/points/fast",
        )

    monkeypatch.setattr("itamx.cli.login_with_elal_browser", fake_browser_login)
    session_path = tmp_path / "session.json"

    result = CliRunner().invoke(
        app,
        [
            "elal-browser-login",
            "--session-file",
            str(session_path),
            "--cdp-url",
            "http://127.0.0.1:9222",
            "--timeout",
            "10",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["method"] == "manual"
    assert captured["cdp_url"] == "http://127.0.0.1:9222"
    loaded = load_elal_session(session_path)
    assert loaded is not None
    assert loaded.authorization == "Bearer browser-token"
    assert loaded.cookie == "BOOKINGSESSION=abc"


def test_elal_browser_login_cli_sms_loads_credentials(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_browser_login(**kwargs):
        captured.update(kwargs)
        return ElAlBrowserLoginResult(
            headers={"Authorization": "Bearer browser-token"},
            source="session-create-response",
        )

    monkeypatch.setattr("itamx.cli.login_with_elal_browser", fake_browser_login)
    credentials_path = save_elal_credentials(
        username="member",
        password="secret",
        path=tmp_path / "credentials.json",
    )

    result = CliRunner().invoke(
        app,
        [
            "elal-browser-login",
            "--method",
            "sms",
            "--phone",
            "0500000000",
            "--credentials-file",
            str(credentials_path),
            "--session-file",
            str(tmp_path / "session.json"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["method"] == "sms"
    assert captured["phone"] == "0500000000"
    assert captured["credentials"].username == "member"
    assert captured["credentials"].password == "secret"


def test_elal_browser_login_cli_requires_credentials_for_sms(tmp_path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "elal-browser-login",
            "--method",
            "sms",
            "--credentials-file",
            str(tmp_path / "missing.json"),
        ],
    )

    assert result.exit_code == 1
    assert "requires saved credentials" in result.output


def test_elal_awards_cli_json(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def search_awards(self, **kwargs):
            return {
                "success": True,
                "trip_type": "ONE_WAY",
                "request": {"origin": kwargs["origin"], "destination": kwargs["destination"]},
                "results": normalize_award_results({"outbound": OUTBOUND_RESPONSE}),
                "raw": {"ok": True},
            }

    monkeypatch.setattr("itamx.cli.ElAlAwardClient", FakeClient)
    result = CliRunner().invoke(
        app,
        [
            "elal-awards",
            "TLV",
            "LON",
            "2026-11-26",
            "--authorization",
            "token",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["success"] is True
    assert data["count"] == 1
    assert data["results"][0]["flights"] == ["LY311"]


def test_elal_matrix_cli_json_filters_saver_buckets(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def search_awards(self, **kwargs):
            assert kwargs["cabin"] == "ALL"
            return {
                "results": [
                    {
                        "flights": ["LY315"],
                        "origin": "TLV",
                        "destination": "LHR",
                        "departure": f"{kwargs['depart_date']}T09:10:00.000Z",
                        "arrival": f"{kwargs['depart_date']}T12:35:00.000Z",
                        "cabin": "COACH",
                        "rbd": "E",
                        "points": 18000.0,
                        "tax_amount": 37.87,
                        "tax_currency": "USD",
                        "seats_left": 4,
                        "segments": [{"aircraft": "789"}],
                    },
                    {
                        "flights": ["LY315"],
                        "origin": "TLV",
                        "destination": "LHR",
                        "departure": f"{kwargs['depart_date']}T09:10:00.000Z",
                        "arrival": f"{kwargs['depart_date']}T12:35:00.000Z",
                        "cabin": "PREMIUM_COACH",
                        "rbd": "A",
                        "points": 27200.0,
                        "tax_amount": 37.87,
                        "tax_currency": "USD",
                        "seats_left": 2,
                        "segments": [{"aircraft": "789"}],
                    },
                    {
                        "flights": ["LY315"],
                        "origin": "TLV",
                        "destination": "LHR",
                        "departure": f"{kwargs['depart_date']}T09:10:00.000Z",
                        "arrival": f"{kwargs['depart_date']}T12:35:00.000Z",
                        "cabin": "BUSINESS",
                        "rbd": "X",
                        "points": 48000.0,
                        "tax_amount": 37.87,
                        "tax_currency": "USD",
                        "seats_left": 1,
                        "segments": [{"aircraft": "789"}],
                    },
                    {
                        "flights": ["LY315"],
                        "origin": "TLV",
                        "destination": "LHR",
                        "departure": f"{kwargs['depart_date']}T09:10:00.000Z",
                        "arrival": f"{kwargs['depart_date']}T12:35:00.000Z",
                        "cabin": "BUSINESS",
                        "rbd": "C",
                        "points": 411600.0,
                        "tax_amount": 97.87,
                        "tax_currency": "USD",
                        "seats_left": 9,
                        "segments": [{"aircraft": "789"}],
                    },
                ]
            }

    monkeypatch.setattr("itamx.cli.ElAlAwardClient", FakeClient)
    result = CliRunner().invoke(
        app,
        [
            "elal-matrix",
            "TLV",
            "LON",
            "2026-11-01",
            "2026-11-01",
            "--authorization",
            "token",
            "--sleep",
            "0",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["saver_buckets"] == {"coach": "E", "premium": "A", "business": "X"}
    assert data["count"] == 3
    assert [row["award_class"] for row in data["results"]] == ["coach", "premium", "business"]
    assert [row["rbd"] for row in data["results"]] == ["E", "A", "X"]
