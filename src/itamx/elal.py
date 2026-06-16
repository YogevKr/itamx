"""Direct EL AL Matmid award-search client.

EL AL's booking API keeps award-search state server-side: POST the search
request, then read outbound/inbound bound lists from separate endpoints.
The public site is protected by anti-bot/session headers, so this client
uses caller-provided browser session material via env vars or an exported HAR.
"""

from __future__ import annotations

import datetime as dt
import html.parser
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx

ELAL_BASE_URL = "https://booking.elal.com"
ELAL_FAST_PATH = "/bfm/service/extly/booking/search/points/fast"
ELAL_OUTBOUND_PATH = "/bfm/service/extly/booking/search/points/outbound"
ELAL_INBOUND_PATH = "/bfm/service/extly/booking/search/points/inbound"
ELAL_SESSION_CREATE_PATH = "/bfm/rest/session/create"
ELAL_SESSION_REFRESH_PATH = "/bfm/rest/session/refresh/by/header"

MATMID_BASE_URL = "https://matmid.elal.com"
MATMID_SSO_PATH = "/affwebservices/public/saml2sso"
ELAL_RELAY_STATE = f"{ELAL_BASE_URL}/booking/flights?lang=he&market=IL"

ELAL_SESSION_FILE_ENV = "ITAMX_ELAL_SESSION_FILE"
ELAL_CREDENTIALS_FILE_ENV = "ITAMX_ELAL_CREDENTIALS_FILE"

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)

_HAR_HEADER_ALLOWLIST = {
    "accept",
    "accept-language",
    "authorization",
    "content-language",
    "cookie",
    "origin",
    "referer",
    "user-agent",
}

_SESSION_FILE_HEADER_ALLOWLIST = {
    "authorization",
    "cookie",
    "user-agent",
}

_PUBLIC_SESSION_DATA = {
    "clientId": "-bqBinBiHz4Yg+87BN+PU3TaXUWyRrn1T/iV/LjxgeSU=",
    "clientSecret": (
        "DxKLkFeWzANc4JSIIarjoPSr6M+cXv1rcqWry2QV2Azr5EutGYR/oJ79IT3fMR+qM5H/RArvIPtyquvjHebM1Q=="
    ),
    "referralSessionId": "asdasda213123",
    "referralId": "sadassdasdsa",
}

_LINK11_STATUS_CODES = {247, 491, 492}
_TEXT_INPUT_TYPES = {"", "text", "email", "tel", "number"}

_CABIN_WIRE = {
    "ALL": ["E", "P", "B"],
    "ANY": ["E", "P", "B"],
    "E": ["E"],
    "ECONOMY": ["E"],
    "COACH": ["E"],
    "Y": ["E"],
    "P": ["P"],
    "PREMIUM": ["P"],
    "PREMIUM_COACH": ["P"],
    "PREMIUM-COACH": ["P"],
    "PREMIUM_ECONOMY": ["P"],
    "PREMIUM-ECONOMY": ["P"],
    "W": ["P"],
    "B": ["B"],
    "BUSINESS": ["B"],
    "J": ["B"],
}


class ElAlAwardAuthError(RuntimeError):
    """Raised when EL AL session/auth material is missing or rejected."""


@dataclass
class ElAlSession:
    headers: dict[str, str]
    path: Path | None = None

    @property
    def cookie(self) -> str | None:
        return _get_header(self.headers, "Cookie")

    @property
    def authorization(self) -> str | None:
        return _get_header(self.headers, "Authorization")


@dataclass
class ElAlCredentials:
    username: str
    password: str
    path: Path | None = None


@dataclass
class ElAlBrowserLoginResult:
    headers: dict[str, str]
    source: str
    captured_url: str | None = None


@dataclass
class _HtmlForm:
    action: str
    method: str
    inputs: dict[str, str]
    input_types: dict[str, str]


@dataclass
class ElAlPaxCount:
    adults: int = 1
    seniors: int = 0
    youths: int = 0
    children: int = 0
    infants: int = 0

    def to_payload(self) -> dict[str, int]:
        return {
            "SRC": self.seniors,
            "ADT": self.adults,
            "INF": self.infants,
            "CHD": self.children,
            "YTH": self.youths,
        }


def normalize_elal_cabins(cabin: str | list[str] | None) -> list[str]:
    """Translate CLI/Matrix-style cabin names to EL AL wire cabin codes."""
    if cabin is None or cabin == "":
        return ["E", "P", "B"]
    tokens = cabin if isinstance(cabin, list) else cabin.split(",")
    out: list[str] = []
    for token in tokens:
        key = token.strip().upper().replace(" ", "_")
        if not key:
            continue
        values = _CABIN_WIRE.get(key)
        if values is None:
            raise ValueError("EL AL cabin must be one of ALL, ECONOMY, PREMIUM_COACH, BUSINESS")
        for value in values:
            if value not in out:
                out.append(value)
    return out or ["E", "P", "B"]


def _iso_midnight(value: str) -> str:
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"date must be YYYY-MM-DD (got {value!r})")
    return f"{parsed.isoformat()}T00:00:00.000Z"


def build_award_search_body(
    *,
    origin: str,
    destination: str,
    depart_date: str,
    return_date: str | None = None,
    cabin: str | list[str] | None = None,
    pax: ElAlPaxCount | None = None,
    market: str = "IL",
    language: str = "he",
    promo_code: str = "",
) -> dict[str, Any]:
    """Build the EL AL points-search POST body."""
    body: dict[str, Any] = {
        "market": market,
        "airlineId": "LY",
        "tripType": "R" if return_date else "O",
        "origin": [origin.upper()],
        "destination": [destination.upper()],
        "departureDate": [_iso_midnight(depart_date)],
        "cabinClass": normalize_elal_cabins(cabin),
        "passengers": (pax or ElAlPaxCount()).to_payload(),
        "promoCode": promo_code,
        "retrieveEmployeeData": False,
        "campaignId": None,
        "language": language,
    }
    if return_date:
        body["returnDate"] = _iso_midnight(return_date)
    return body


def extract_elal_award_headers_from_har(har_path: str | Path) -> dict[str, str]:
    """Extract reusable request headers from the latest EL AL points-fast call."""
    path = Path(har_path)
    with path.open("r", encoding="utf-8") as f:
        har = json.load(f)

    entries = har.get("log", {}).get("entries", [])
    matching = [
        entry
        for entry in entries
        if (entry.get("request", {}).get("url") or "").startswith(ELAL_BASE_URL + ELAL_FAST_PATH)
    ]
    if not matching:
        raise ValueError(f"No EL AL points search request found in HAR: {path}")

    headers: dict[str, str] = {}
    for header in matching[-1].get("request", {}).get("headers", []):
        name = header.get("name") or ""
        value = header.get("value") or ""
        if name.lower() in _HAR_HEADER_ALLOWLIST and value:
            headers[name] = value
    return headers


def default_elal_session_path() -> Path:
    explicit = os.getenv(ELAL_SESSION_FILE_ENV)
    if explicit:
        return Path(explicit).expanduser()
    config_home = Path(os.getenv("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return config_home / "itamx" / "elal-session.json"


def default_elal_credentials_path() -> Path:
    explicit = os.getenv(ELAL_CREDENTIALS_FILE_ENV)
    if explicit:
        return Path(explicit).expanduser()
    config_home = Path(os.getenv("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return config_home / "itamx" / "elal-credentials.json"


def default_elal_browser_profile_path() -> Path:
    config_home = Path(os.getenv("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return config_home / "itamx" / "elal-browser-profile"


def load_elal_session(path: str | Path | None = None) -> ElAlSession | None:
    session_path = Path(path).expanduser() if path else default_elal_session_path()
    if not session_path.exists():
        return None
    with session_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    headers = data.get("headers") if isinstance(data, dict) else None
    if not isinstance(headers, dict):
        raise ValueError(f"Bad EL AL session file: {session_path}")
    clean = {
        str(k): str(v)
        for k, v in headers.items()
        if str(k).lower() in _SESSION_FILE_HEADER_ALLOWLIST and v
    }
    return ElAlSession(headers=clean, path=session_path)


def load_elal_credentials(path: str | Path | None = None) -> ElAlCredentials | None:
    credentials_path = Path(path).expanduser() if path else default_elal_credentials_path()
    if not credentials_path.exists():
        return None
    with credentials_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Bad EL AL credentials file: {credentials_path}")
    username = data.get("username")
    password = data.get("password")
    if not isinstance(username, str) or not username:
        raise ValueError(f"Bad EL AL credentials file: missing username in {credentials_path}")
    if not isinstance(password, str) or not password:
        raise ValueError(f"Bad EL AL credentials file: missing password in {credentials_path}")
    return ElAlCredentials(username=username, password=password, path=credentials_path)


def save_elal_session(
    headers: dict[str, str],
    path: str | Path | None = None,
    *,
    source: str = "manual",
) -> Path:
    session_path = Path(path).expanduser() if path else default_elal_session_path()
    clean = {
        str(k): str(v)
        for k, v in headers.items()
        if str(k).lower() in _SESSION_FILE_HEADER_ALLOWLIST and v
    }
    if not clean:
        raise ValueError("No EL AL session headers to save")
    session_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": source,
        "saved_at": dt.datetime.now(dt.UTC).isoformat(),
        "headers": clean,
    }
    with session_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    try:
        session_path.chmod(0o600)
    except OSError:
        pass
    return session_path


def save_elal_credentials(
    *,
    username: str,
    password: str,
    path: str | Path | None = None,
) -> Path:
    if not username:
        raise ValueError("EL AL username is required")
    if not password:
        raise ValueError("EL AL password is required")
    credentials_path = Path(path).expanduser() if path else default_elal_credentials_path()
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "username": username,
        "password": password,
        "saved_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    with credentials_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    try:
        credentials_path.chmod(0o600)
    except OSError:
        pass
    return credentials_path


def build_booking_session_body(
    *,
    market: str = "IL",
    language: str = "he",
    use_new_services: bool = False,
    access_type: str = "web",
    time_zone: str | None = None,
) -> dict[str, Any]:
    return {
        **_PUBLIC_SESSION_DATA,
        "market": market,
        "language": language,
        "useNewServices": use_new_services,
        "accessType": access_type,
        "timeZone": time_zone or os.getenv("TZ") or "Asia/Jerusalem",
    }


def build_matmid_sso_url(*, relay_state: str = ELAL_RELAY_STATE) -> str:
    return f"{MATMID_BASE_URL}{MATMID_SSO_PATH}?{urlencode({'SPID': 'IndraNew-SP-HE', 'RelayState': relay_state})}"


def _default_headers(language: str = "he") -> dict[str, str]:
    return {
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Language": language,
        "Content-Type": "application/json; charset=UTF-8",
        "Origin": ELAL_BASE_URL,
        "Referer": f"{ELAL_BASE_URL}/booking/flights?lang={language}&market=IL",
    }


def _header_present(headers: dict[str, str], name: str) -> bool:
    return any(k.lower() == name.lower() for k in headers)


def _get_header(headers: dict[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _set_header(headers: dict[str, str], name: str, value: str | None) -> None:
    if value:
        headers[name] = (
            value
            if name.lower() != "authorization" or value.startswith("Bearer ")
            else f"Bearer {value}"
        )


def _cookie_map(cookie_header: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not cookie_header:
        return out
    for chunk in cookie_header.split(";"):
        if "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        if name:
            out[name] = value.strip()
    return out


def _merge_set_cookies(cookie_header: str | None, set_cookie_values: list[str]) -> str | None:
    cookies = _cookie_map(cookie_header)
    for value in set_cookie_values:
        first = value.split(";", 1)[0]
        if "=" not in first:
            continue
        name, cookie_value = first.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = cookie_value.strip()
    if not cookies:
        return None
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _cookie_header_from_browser_cookies(cookies: list[dict[str, Any]]) -> str | None:
    pairs = [
        f"{cookie['name']}={cookie['value']}"
        for cookie in cookies
        if cookie.get("name") and cookie.get("value") is not None
    ]
    return "; ".join(pairs) if pairs else None


def _raise_for_link11(resp: httpx.Response, action: str) -> None:
    text = resp.text[:512]
    if resp.status_code in _LINK11_STATUS_CODES or "Link11 access denied" in text:
        raise ElAlAwardAuthError(
            f"EL AL Link11 blocked {action} (HTTP {resp.status_code}). "
            "The Matmid username/password form is behind EL AL's browser protection, "
            "so raw credential login cannot be completed over plain HTTP. Complete "
            "login in a browser and run `itamx elal-login --matmid-cookie ...`."
        )


class _HtmlFormParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[_HtmlForm] = []
        self._current: _HtmlForm | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): v or "" for k, v in attrs}
        if tag.lower() == "form":
            self._current = _HtmlForm(
                action=attr.get("action", ""),
                method=(attr.get("method") or "GET").upper(),
                inputs={},
                input_types={},
            )
            return
        if tag.lower() != "input" or self._current is None:
            return
        name = attr.get("name")
        if not name:
            return
        self._current.inputs[name] = attr.get("value", "")
        self._current.input_types[name] = (attr.get("type") or "").lower()

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def _extract_forms(html_text: str) -> list[_HtmlForm]:
    parser = _HtmlFormParser()
    parser.feed(html_text)
    if parser._current is not None:
        parser.forms.append(parser._current)
    return parser.forms


def _find_login_fields(form: _HtmlForm) -> tuple[str, str] | None:
    password_name = next(
        (name for name, input_type in form.input_types.items() if input_type == "password"),
        None,
    )
    if not password_name:
        return None

    text_names = [
        name
        for name, input_type in form.input_types.items()
        if name != password_name and input_type in _TEXT_INPUT_TYPES
    ]
    for needle in ("user", "email", "member", "matmid", "login", "id"):
        for name in text_names:
            if needle in name.lower():
                return name, password_name
    if text_names:
        return text_names[0], password_name
    return None


def _extract_login_form(html_text: str) -> tuple[_HtmlForm, str, str] | None:
    for form in _extract_forms(html_text):
        fields = _find_login_fields(form)
        if fields:
            username_field, password_field = fields
            return form, username_field, password_field
    return None


class _SamlFormParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.inputs: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): v or "" for k, v in attrs}
        if tag.lower() == "form" and attr.get("action"):
            self.action = attr["action"]
        if tag.lower() == "input" and attr.get("name"):
            self.inputs[attr["name"]] = attr.get("value", "")


def _extract_saml_form(html: str) -> tuple[str, dict[str, str]]:
    parser = _SamlFormParser()
    parser.feed(html)
    if not parser.action or "SAMLResponse" not in parser.inputs:
        raise ElAlAwardAuthError("Matmid SSO did not return a SAML auto-post form")
    return parser.action, parser.inputs


def _post_saml_assertion(
    client: httpx.Client,
    *,
    html_text: str,
    booking_cookie: str | None,
    headers: dict[str, str],
) -> str:
    action, form = _extract_saml_form(html_text)

    acs = client.post(
        action,
        data=form,
        headers={
            "User-Agent": headers["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": headers["Accept-Language"],
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": MATMID_BASE_URL,
            "Referer": MATMID_BASE_URL + "/",
        },
    )
    _raise_for_link11(acs, "Matmid SAML handoff")
    if acs.status_code not in (200, 302, 303):
        acs.raise_for_status()
    cookie = _merge_set_cookies(booking_cookie, acs.headers.get_list("set-cookie"))
    if not cookie:
        raise ElAlAwardAuthError("SAML assertion consumer did not set booking cookies")
    return cookie


class ElAlAwardClient:
    """Synchronous client for EL AL Matmid award availability.

    Session material can be supplied directly, via env vars
    `ITAMX_ELAL_AUTHORIZATION` / `ITAMX_ELAL_COOKIE`, or by passing `har_path`.
    """

    def __init__(
        self,
        *,
        timeout: float = 60.0,
        authorization: str | None = None,
        cookie: str | None = None,
        har_path: str | Path | None = None,
        session_path: str | Path | None = None,
        use_session_file: bool = True,
        language: str = "he",
        market: str = "IL",
        base_url: str = ELAL_BASE_URL,
        http_client: httpx.Client | None = None,
    ) -> None:
        headers = _default_headers(language)
        if use_session_file:
            saved = load_elal_session(session_path)
            if saved:
                headers.update(saved.headers)
        if har_path:
            headers.update(extract_elal_award_headers_from_har(har_path))

        _set_header(
            headers, "Authorization", authorization or os.getenv("ITAMX_ELAL_AUTHORIZATION")
        )
        _set_header(headers, "Cookie", cookie or os.getenv("ITAMX_ELAL_COOKIE"))

        if not _header_present(headers, "authorization") and not _header_present(headers, "cookie"):
            raise ElAlAwardAuthError(
                "EL AL awards requires a saved/login session. Run `itamx elal-login`, "
                "or set ITAMX_ELAL_AUTHORIZATION / ITAMX_ELAL_COOKIE."
            )

        self._http = http_client or httpx.Client(base_url=base_url, timeout=timeout)
        self._owns_http = http_client is None
        self._base_url = base_url.rstrip("/")
        self._market = market
        self._language = language
        if hasattr(self._http, "headers"):
            self._http.headers.update(headers)
        if not _header_present(dict(self._http.headers), "authorization"):
            self.create_booking_session(market=market, language=language)

    def __enter__(self) -> ElAlAwardClient:
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_http:
            self._http.close()

    def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = self._http.request(method, path, **kwargs)
        self._capture_set_cookies(resp)
        _raise_for_link11(resp, "award/session request")
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as exc:
            text = resp.text[:160].replace("\n", " ")
            raise ElAlAwardAuthError(f"EL AL returned non-JSON response: {text!r}") from exc
        if isinstance(data, dict):
            return data
        raise ValueError(f"Expected JSON object from EL AL, got {type(data).__name__}")

    def _capture_set_cookies(self, resp: httpx.Response) -> None:
        set_cookie_values = resp.headers.get_list("set-cookie")
        if not set_cookie_values:
            return
        current = self._http.headers.get("Cookie")
        merged = _merge_set_cookies(current, set_cookie_values)
        if merged:
            self._http.headers["Cookie"] = merged

    def create_booking_session(
        self,
        *,
        market: str | None = None,
        language: str | None = None,
        use_new_services: bool = False,
    ) -> str:
        body = build_booking_session_body(
            market=market or self._market,
            language=language or self._language,
            use_new_services=use_new_services,
        )
        data = self._request_json("POST", ELAL_SESSION_CREATE_PATH, json=body)
        token = data.get("id")
        if not token:
            raise ElAlAwardAuthError(f"EL AL session create did not return a token: {data}")
        self._http.headers["Authorization"] = f"Bearer {token}"
        return str(token)

    def refresh_booking_session(self) -> dict[str, Any]:
        return self._request_json("GET", ELAL_SESSION_REFRESH_PATH)

    def search_awards(
        self,
        *,
        origin: str,
        destination: str,
        depart_date: str,
        return_date: str | None = None,
        cabin: str | list[str] | None = None,
        pax: ElAlPaxCount | None = None,
        market: str = "IL",
        language: str = "he",
        promo_code: str = "",
    ) -> dict[str, Any]:
        body = build_award_search_body(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            return_date=return_date,
            cabin=cabin,
            pax=pax,
            market=market,
            language=language,
            promo_code=promo_code,
        )

        init = self._request_json("POST", ELAL_FAST_PATH, json=body)
        if init.get("errors"):
            raise ValueError(f"EL AL search rejected request: {init['errors']}")

        outbound = self._request_json("GET", ELAL_OUTBOUND_PATH)
        inbound = self._request_json("GET", ELAL_INBOUND_PATH) if return_date else None
        raw = {"request": body, "init": init, "outbound": outbound, "inbound": inbound}
        return {
            "success": True,
            "trip_type": "ROUND_TRIP" if return_date else "ONE_WAY",
            "request": body,
            "results": normalize_award_results(raw),
            "raw": raw,
        }


def normalize_award_results(raw: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    results.extend(_normalize_direction(raw.get("outbound") or {}, "outbound"))
    if raw.get("inbound"):
        results.extend(_normalize_direction(raw.get("inbound") or {}, "inbound"))
    results.sort(
        key=lambda row: (
            0 if row["direction"] == "outbound" else 1,
            row.get("points") if row.get("points") is not None else float("inf"),
            row.get("tax_amount") if row.get("tax_amount") is not None else float("inf"),
        )
    )
    return results


def _normalize_direction(response: dict[str, Any], direction: str) -> list[dict[str, Any]]:
    trip = response.get("data", {}).get("trip", {})
    branch_name = "outbound" if direction == "outbound" else "returnBound"
    branch = trip.get(branch_name) or {}
    rows: list[dict[str, Any]] = []
    for bound_type in ("directBounds", "indirectBounds", "railAndFlyBounds"):
        bounds = (branch.get(bound_type) or {}).get("bounds") or []
        for bound in bounds:
            segments = [_normalize_segment(segment) for segment in bound.get("segments") or []]
            flights = [segment["flight"] for segment in segments if segment.get("flight")]
            for fare in bound.get("fares") or []:
                net_price = fare.get("netPrice") or {}
                points = net_price.get("points") or {}
                taxes = points.get("taxes") or {}
                cash = net_price.get("cash") or {}
                rows.append(
                    {
                        "direction": direction,
                        "bound_type": bound_type.replace("Bounds", ""),
                        "bound_id": bound.get("id"),
                        "fare_id": fare.get("idOffer"),
                        "flights": flights,
                        "origin": segments[0].get("origin") if segments else None,
                        "destination": segments[-1].get("destination") if segments else None,
                        "departure": segments[0].get("departure") if segments else None,
                        "arrival": segments[-1].get("arrival") if segments else None,
                        "duration_minutes": _seconds_to_minutes(bound.get("duration")),
                        "segments": segments,
                        "cabin": _display_cabin(fare),
                        "rbd": fare.get("rbd"),
                        "fare_family": fare.get("familyName") or fare.get("name"),
                        "points": points.get("amount"),
                        "min_points": points.get("minAmountToConsume"),
                        "tax_amount": taxes.get("amount"),
                        "tax_currency": taxes.get("currencyCode"),
                        "cash_amount": cash.get("amount"),
                        "cash_currency": cash.get("currencyCode"),
                        "seats_left": fare.get("nbSeatLeft"),
                        "recommended": bool(fare.get("recommended")),
                        "best_value": bool(fare.get("bestValue")),
                    }
                )
    return rows


def _normalize_segment(segment: dict[str, Any]) -> dict[str, Any]:
    carrier = segment.get("carrier") or (segment.get("airline") or {}).get("name")
    flight_number = segment.get("flightNumber")
    flight = f"{carrier}{flight_number}" if carrier and flight_number else None
    return {
        "carrier": carrier,
        "flight_number": str(flight_number) if flight_number is not None else None,
        "flight": flight,
        "origin": (segment.get("departureAirport") or {}).get("code"),
        "destination": (segment.get("arrivalAirport") or {}).get("code"),
        "departure": segment.get("departureDate"),
        "arrival": segment.get("arrivalDate"),
        "duration_minutes": _seconds_to_minutes(segment.get("duration")),
        "aircraft": segment.get("aircraftType")
        or (segment.get("flightInfo") or {}).get("aircraftType"),
    }


def _seconds_to_minutes(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return round(float(value) / 60)
    except (TypeError, ValueError):
        return None


def _display_cabin(fare: dict[str, Any]) -> str | None:
    value = (fare.get("bookingClassName") or fare.get("cabinTypeName") or "").lower()
    if "business" in value:
        return "BUSINESS"
    if "premium" in value:
        return "PREMIUM_COACH"
    if "economy" in value:
        return "COACH"
    return value.upper() or None


def bootstrap_elal_session_from_sso(
    *,
    matmid_cookie: str,
    booking_cookie: str | None = None,
    market: str = "IL",
    language: str = "he",
    timeout: float = 60.0,
) -> dict[str, str]:
    """Exchange a browser-authenticated Matmid SSO session for booking headers.

    This reproduces the SAML handoff visible in EL AL's web app. It still needs
    a valid Matmid/WAAP browser session cookie; raw username/password login is
    behind EL AL's IdP and anti-bot challenge.
    """
    headers = _default_headers(language)
    headers["Cookie"] = booking_cookie or ""
    with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers) as client:
        sso_headers = {
            "User-Agent": headers["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": headers["Accept-Language"],
            "Referer": ELAL_BASE_URL + "/",
            "Cookie": matmid_cookie,
        }
        sso = client.get(build_matmid_sso_url(), headers=sso_headers)
        _raise_for_link11(sso, "Matmid SSO")
        sso.raise_for_status()
        cookie = _post_saml_assertion(
            client,
            html_text=sso.text,
            booking_cookie=booking_cookie,
            headers=headers,
        )

        with ElAlAwardClient(
            cookie=cookie,
            language=language,
            market=market,
            use_session_file=False,
        ) as award_client:
            return {
                "Cookie": award_client._http.headers["Cookie"],
                "Authorization": award_client._http.headers["Authorization"],
                "User-Agent": award_client._http.headers["User-Agent"],
            }


def login_with_elal_credentials(
    *,
    username: str,
    password: str,
    booking_cookie: str | None = None,
    market: str = "IL",
    language: str = "he",
    timeout: float = 60.0,
    http_client: httpx.Client | None = None,
) -> dict[str, str]:
    """Attempt Matmid username/password SSO and return reusable booking headers.

    EL AL currently places the unauthenticated Matmid SSO entrypoint behind
    Link11 browser protection. This function supports the simple HTML form
    shape if EL AL serves it, but does not bypass anti-bot, captcha, or MFA.
    """
    if not username:
        raise ValueError("EL AL username is required")
    if not password:
        raise ValueError("EL AL password is required")

    headers = _default_headers(language)
    if booking_cookie:
        headers["Cookie"] = booking_cookie
    client = http_client or httpx.Client(timeout=timeout, follow_redirects=False)
    owns_http = http_client is None
    try:
        client.headers.update(headers)
        sso_headers = {
            "User-Agent": headers["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": headers["Accept-Language"],
            "Referer": ELAL_BASE_URL + "/",
        }
        sso = client.get(build_matmid_sso_url(), headers=sso_headers)
        _raise_for_link11(sso, "Matmid username/password login")
        sso.raise_for_status()

        cookie: str | None = None
        try:
            _extract_saml_form(sso.text)
        except ElAlAwardAuthError:
            login_form = _extract_login_form(sso.text)
            if not login_form:
                raise ElAlAwardAuthError(
                    "Matmid did not return a username/password login form. "
                    "The observed flow is browser SAML SSO, not a raw credential API."
                )
        else:
            cookie = _post_saml_assertion(
                client,
                html_text=sso.text,
                booking_cookie=booking_cookie,
                headers=headers,
            )

        if cookie is None:
            form, username_field, password_field = login_form
            form_data = dict(form.inputs)
            form_data[username_field] = username
            form_data[password_field] = password
            method = form.method if form.method in {"GET", "POST"} else "POST"
            action = urljoin(str(sso.url), form.action or str(sso.url))
            login_headers = {
                "User-Agent": headers["User-Agent"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": headers["Accept-Language"],
                "Origin": MATMID_BASE_URL,
                "Referer": str(sso.url),
            }
            if method == "POST":
                login_headers["Content-Type"] = "application/x-www-form-urlencoded"
            login = client.request(
                method,
                action,
                data=form_data if method == "POST" else None,
                params=form_data if method == "GET" else None,
                headers=login_headers,
                follow_redirects=True,
            )
            _raise_for_link11(login, "Matmid username/password login")
            login.raise_for_status()
            try:
                _extract_saml_form(login.text)
            except ElAlAwardAuthError as exc:
                raise ElAlAwardAuthError(
                    "Matmid credential login did not return a SAML assertion. "
                    "The flow may require browser JavaScript, captcha, or MFA."
                ) from exc
            try:
                cookie = _post_saml_assertion(
                    client,
                    html_text=login.text,
                    booking_cookie=booking_cookie,
                    headers=headers,
                )
            except ElAlAwardAuthError:
                raise

        with ElAlAwardClient(
            cookie=cookie,
            language=language,
            market=market,
            use_session_file=False,
        ) as award_client:
            return {
                "Cookie": award_client._http.headers["Cookie"],
                "Authorization": award_client._http.headers["Authorization"],
                "User-Agent": award_client._http.headers["User-Agent"],
            }
    finally:
        if owns_http:
            client.close()


def _default_chrome_executable_path() -> str | None:
    mac_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return str(mac_chrome) if mac_chrome.exists() else None


def _click_visible_browser_text(page: Any, text: str, *, contains: bool = False) -> bool:
    return bool(
        page.evaluate(
            """
            ({ text, contains }) => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 2 && r.height > 2 &&
                  s.display !== 'none' && s.visibility !== 'hidden';
              };
              const elements = [...document.querySelectorAll(
                'button,a,[role="button"]'
              )].filter(visible);
              const found = elements.find((el) => {
                const value = (el.innerText || el.textContent || '')
                  .replace(/\\s+/g, ' ')
                  .trim();
                return contains ? value.includes(text) : value === text;
              });
              if (!found) return false;
              found.click();
              return true;
            }
            """,
            {"text": text, "contains": contains},
        )
    )


def _fill_first_visible_inputs(page: Any, values_by_type: dict[str, str]) -> None:
    page.evaluate(
        """
        (valuesByType) => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 2 && r.height > 2 &&
              s.display !== 'none' && s.visibility !== 'hidden';
          };
          for (const [type, value] of Object.entries(valuesByType)) {
            const input = [...document.querySelectorAll(`input[type="${type}"]`)]
              .filter(visible)[0];
            if (!input) throw new Error(`missing visible ${type} input`);
            input.focus();
            input.value = '';
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.value = value;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
          }
        }
        """,
        values_by_type,
    )


def _auto_submit_elal_browser_login(
    page: Any,
    *,
    method: str,
    credentials: ElAlCredentials,
    phone: str | None,
) -> None:
    if method not in {"password", "sms"}:
        return
    page.wait_for_timeout(3000)
    if not _click_visible_browser_text(page, "התחברות"):
        raise ElAlAwardAuthError("Could not find EL AL login button in the browser")
    page.wait_for_timeout(1500)

    if method == "sms":
        if not phone:
            raise ValueError("EL AL SMS browser login requires --phone")
        if not _click_visible_browser_text(page, "קוד חד פעמי", contains=True):
            raise ElAlAwardAuthError("Could not find EL AL one-time-code login link")
        page.wait_for_timeout(1500)
        _fill_first_visible_inputs(page, {"text": credentials.username, "tel": phone})
    else:
        _fill_first_visible_inputs(page, {"text": credentials.username, "password": credentials.password})

    if not _click_visible_browser_text(page, "כניסה"):
        raise ElAlAwardAuthError("Could not find EL AL login submit button")


def login_with_elal_browser(
    *,
    credentials: ElAlCredentials | None = None,
    phone: str | None = None,
    method: str = "manual",
    cdp_url: str | None = None,
    profile_dir: str | Path | None = None,
    chrome_path: str | Path | None = None,
    start_url: str = "https://www.elal.com/heb/israel",
    timeout: float = 300.0,
    headless: bool = False,
) -> ElAlBrowserLoginResult:
    """Open EL AL in Playwright and capture a reusable booking session.

    The browser must complete EL AL's own login and browser-protection checks.
    This helper only observes resulting booking API requests/responses and saves
    their session material; it does not bypass anti-bot, captcha, or MFA.
    """
    method = method.lower().strip()
    if method not in {"manual", "password", "sms"}:
        raise ValueError("browser login method must be one of manual, password, sms")
    if method in {"password", "sms"} and credentials is None:
        raise ValueError(f"EL AL browser login method {method!r} requires credentials")

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ElAlAwardAuthError(
            "Playwright is not installed. Run `uv sync --extra browser` and retry."
        ) from exc

    browser_profile = Path(profile_dir).expanduser() if profile_dir else default_elal_browser_profile_path()
    executable_path = str(Path(chrome_path).expanduser()) if chrome_path else _default_chrome_executable_path()

    captured: dict[str, str] = {}
    captured_source = ""
    captured_url: str | None = None
    www_logged_in = False
    link11_events: list[str] = []

    def set_captured(headers: dict[str, str], source: str, url: str | None) -> None:
        nonlocal captured, captured_source, captured_url
        if captured:
            return
        auth = _get_header(headers, "Authorization")
        if not auth:
            return
        clean: dict[str, str] = {}
        _set_header(clean, "Authorization", auth)
        _set_header(clean, "Cookie", _get_header(headers, "Cookie"))
        _set_header(clean, "User-Agent", _get_header(headers, "User-Agent") or DEFAULT_UA)
        captured = clean
        captured_source = source
        captured_url = url

    with sync_playwright() as playwright:
        context_to_close = None
        if cdp_url:
            try:
                browser = playwright.chromium.connect_over_cdp(cdp_url)
            except PlaywrightError as exc:
                raise ElAlAwardAuthError(
                    f"Could not connect to Chrome DevTools at {cdp_url}. "
                    "Check it with `curl http://127.0.0.1:9222/json/version`. "
                    "Chrome 136+ requires launching remote debugging with a non-default "
                    "`--user-data-dir`, for example: "
                    '`"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" '
                    '--remote-debugging-port=9222 --user-data-dir="$HOME/.config/itamx/elal-cdp-profile"`.'
                ) from exc
            if not browser.contexts:
                raise ElAlAwardAuthError(f"No Chrome browser context available at {cdp_url}")
            context = browser.contexts[0]
            page = context.new_page()
        else:
            browser_profile.mkdir(parents=True, exist_ok=True)
            context = playwright.chromium.launch_persistent_context(
                str(browser_profile),
                executable_path=executable_path,
                headless=headless,
                viewport={"width": 1365, "height": 900},
                args=["--no-first-run", "--no-default-browser-check"],
            )
            context_to_close = context
            page = context.pages[0] if context.pages else context.new_page()

        def booking_cookie_header() -> str | None:
            try:
                return _cookie_header_from_browser_cookies(context.cookies([ELAL_BASE_URL]))
            except PlaywrightError:
                return None

        def capture_request(request: Any, source: str) -> None:
            request_headers = {str(k): str(v) for k, v in request.headers.items()}
            auth = request_headers.get("authorization")
            if not auth:
                return
            headers = {
                "Authorization": auth,
                "User-Agent": request_headers.get("user-agent", DEFAULT_UA),
            }
            cookie = request_headers.get("cookie") or booking_cookie_header()
            if cookie:
                headers["Cookie"] = cookie
            set_captured(headers, source, request.url)

        def capture_token(token: str, source: str, url: str) -> None:
            headers = {
                "Authorization": f"Bearer {token}",
                "User-Agent": DEFAULT_UA,
            }
            cookie = booking_cookie_header()
            if cookie:
                headers["Cookie"] = cookie
            set_captured(headers, source, url)

        def on_request(request: Any) -> None:
            url = request.url
            if "booking.elal.com/bfm/service/extly/booking/search/points" in url:
                capture_request(request, "points-request")
            elif "booking.elal.com/bfm/rest/session/refresh" in url:
                capture_request(request, "session-refresh-request")

        def on_response(response: Any) -> None:
            nonlocal www_logged_in
            url = response.url
            status = response.status
            if status in _LINK11_STATUS_CODES:
                link11_events.append(f"{status} {url}")
                return
            if "www.elal.com/api/login" in url and status == 200:
                try:
                    data = response.json()
                except PlaywrightError:
                    data = None
                if isinstance(data, dict) and data.get("token"):
                    www_logged_in = True
            elif "www.elal.com/api/user/details" in url and status == 200:
                www_logged_in = True
            elif "booking.elal.com/bfm/rest/session/create" in url and status == 200:
                if not www_logged_in:
                    return
                try:
                    data = response.json()
                except PlaywrightError:
                    data = None
                if isinstance(data, dict) and data.get("id"):
                    capture_token(str(data["id"]), "session-create-response", url)

        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
        if credentials is not None:
            try:
                _auto_submit_elal_browser_login(
                    page,
                    method=method,
                    credentials=credentials,
                    phone=phone,
                )
            except PlaywrightError as exc:
                raise ElAlAwardAuthError(f"EL AL browser login automation failed: {exc}") from exc

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not captured:
            page.wait_for_timeout(1000)

        if context_to_close is not None:
            context_to_close.close()

    if captured:
        return ElAlBrowserLoginResult(
            headers=captured,
            source=captured_source,
            captured_url=captured_url,
        )

    detail = f" Last Link11 block: {link11_events[-1]}" if link11_events else ""
    raise ElAlAwardAuthError(
        "Timed out waiting for a logged-in EL AL booking session. Complete login "
        "in the opened browser and navigate to an EL AL bonus search/results page."
        f"{detail}"
    )
