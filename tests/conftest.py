"""Shared pytest fixtures for itamx smoke tests.

The whole suite hits a fake Matrix client — never the live API. We patch
`itamx.client.MatrixClient._post_batch` and `MatrixClient._get_batch` to
return canned JSON shaped like real responses.
"""

from __future__ import annotations

import pytest


# Minimal but realistic Matrix /v1/search response — one solution, round-trip.
SEARCH_RESPONSE = {
    "id": "fakeReq",
    "session": "fakeSess",
    "solutionSet": "fakeSolSet",
    "solutionCount": 1,
    "solutionList": {
        "pages": {"count": 1, "current": 1},
        "solutions": [
            {
                "ext": {"price": "USD500.00", "totalPrice": "USD500.00"},
                "displayTotal": "USD500.00",
                "id": "sol001",
                "passengerCount": 1,
                "itinerary": {
                    "ext": {"dominantCarrier": {"code": "LY", "shortName": "El Al"}},
                    "carriers": [{"code": "LY", "shortName": "El Al"}],
                    "singleCarrier": {"code": "LY", "shortName": "El Al"},
                    "distance": {"units": "MI", "value": 7000},
                    "slices": [
                        {
                            "origin": {"code": "TLV", "name": "Tel Aviv"},
                            "destination": {"code": "SFO", "name": "San Francisco"},
                            "departure": "2026-05-18T01:05+03:00",
                            "arrival": "2026-05-18T10:53-07:00",
                            "duration": 978,
                            "flights": ["LY5", "LY4556"],
                            "cabins": ["COACH"],
                        },
                        {
                            "origin": {"code": "SFO", "name": "San Francisco"},
                            "destination": {"code": "TLV", "name": "Tel Aviv"},
                            "departure": "2026-05-24T22:30-07:00",
                            "arrival": "2026-05-26T06:50+03:00",
                            "duration": 955,
                            "flights": ["LY4438", "LY10"],
                            "cabins": ["COACH"],
                        },
                    ],
                },
                "pricings": [{"ext": {"pax": {"adults": 1}}, "displayPrice": "USD500.00"}],
            }
        ],
    },
    "carrierStopMatrix": {
        "columns": [{"label": {"code": "LY", "shortName": "El Al"}}],
        "rows": [
            {
                "label": 1,
                "cells": [{"minPrice": "USD500.00", "minPriceInGrid": True}],
            }
        ],
    },
}

# /v1/summarize bookingDetails response — matches the search solution sol001.
DETAIL_RESPONSE = {
    "id": "fakeReq2",
    "session": "fakeSess",
    "solutionSet": "fakeSolSet",
    "solutionCount": 1,
    "bookingDetails": {
        "id": "sol001",
        "displayTotal": "USD500.00",
        "passengerCount": 1,
        "ext": {"totalPrice": "USD500.00"},
        "itinerary": {
            "distance": {"units": "MI", "value": 7000},
            "slices": [
                {
                    "origin": {"code": "TLV"},
                    "destination": {"code": "SFO"},
                    "departure": "2026-05-18T01:05+03:00",
                    "arrival": "2026-05-18T10:53-07:00",
                    "stopCount": 1,
                    "segments": [
                        {
                            "carrier": {"code": "LY"},
                            "flight": {"number": 5},
                            "origin": {"code": "TLV"},
                            "destination": {"code": "LAX"},
                            "departure": "2026-05-18T01:05+03:00",
                            "arrival": "2026-05-18T06:00-07:00",
                            "duration": 895,
                            "bookingInfos": [{"bookingCode": "S", "cabin": "COACH"}],
                            "legs": [{"aircraft": {"shortName": "Boeing 787"}}],
                        },
                        {
                            "carrier": {"code": "LY"},
                            "flight": {"number": 4556},
                            "origin": {"code": "LAX"},
                            "destination": {"code": "SFO"},
                            "departure": "2026-05-18T09:30-07:00",
                            "arrival": "2026-05-18T10:53-07:00",
                            "duration": 83,
                            "bookingInfos": [{"bookingCode": "Y", "cabin": "COACH"}],
                            "legs": [{"aircraft": {"shortName": "Boeing 737"}}],
                        },
                    ],
                },
                {
                    "origin": {"code": "SFO"},
                    "destination": {"code": "TLV"},
                    "departure": "2026-05-24T22:30-07:00",
                    "arrival": "2026-05-26T06:50+03:00",
                    "stopCount": 1,
                    "segments": [
                        {
                            "carrier": {"code": "LY"},
                            "flight": {"number": 4438},
                            "origin": {"code": "SFO"},
                            "destination": {"code": "JFK"},
                            "departure": "2026-05-24T22:30-07:00",
                            "arrival": "2026-05-25T07:05-04:00",
                            "duration": 335,
                            "bookingInfos": [{"bookingCode": "Y", "cabin": "COACH"}],
                            "legs": [{"aircraft": {"shortName": "Boeing 767"}}],
                        },
                        {
                            "carrier": {"code": "LY"},
                            "flight": {"number": 10},
                            "origin": {"code": "JFK"},
                            "destination": {"code": "TLV"},
                            "departure": "2026-05-25T13:30-04:00",
                            "arrival": "2026-05-26T06:50+03:00",
                            "duration": 620,
                            "bookingInfos": [{"bookingCode": "Y", "cabin": "COACH"}],
                            "legs": [{"aircraft": {"shortName": "Boeing 787"}}],
                        },
                    ],
                },
            ],
        },
        "pricings": [{"ext": {"pax": {"adults": 1}}, "displayPrice": "USD500.00"}],
    },
}

LOCATIONS_RESPONSE = {
    "locations": [
        {
            "code": "TLV", "type": "airport",
            "displayName": "Tel Aviv-Yafo Ben Gurion International, Israel (TLV)",
            "cityCode": "TLV", "cityName": "Tel Aviv-Yafo",
            "latLng": {"latitude": 32.0, "longitude": 34.9},
        }
    ]
}


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Point the disk cache at a tempdir per test, with cache disabled by default."""
    monkeypatch.setenv("ITAMX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("ITAMX_NO_CACHE", "1")
    yield


@pytest.fixture
def fake_matrix(monkeypatch):
    """Patch MatrixClient internals to return canned responses."""
    from itamx.client import MatrixClient

    def fake_post(self, body, path):
        if path.endswith("/v1/search"):
            return SEARCH_RESPONSE
        if path.endswith("/v1/summarize"):
            return DETAIL_RESPONSE
        raise AssertionError(f"unexpected path: {path}")

    def fake_get(self, path):
        if "locationTypes" in path:
            return LOCATIONS_RESPONSE
        raise AssertionError(f"unexpected GET path: {path}")

    monkeypatch.setattr(MatrixClient, "_post_batch", fake_post)
    monkeypatch.setattr(MatrixClient, "_get_batch", fake_get)
