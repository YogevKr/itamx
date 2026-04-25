import unittest

from itamx.mcp.service import (
    DateSearchParams,
    FlightDetailParams,
    FlightSearchParams,
    LookupParams,
    _execute_date_search,
    _execute_flight_detail,
    _execute_flight_search,
    _execute_location_lookup,
)


def _raw_response(price: str = "USD123.45", date: str = "2099-01-01") -> dict:
    return {
        "solutionCount": 1,
        "solutionSet": "fixture-set",
        "session": "fixture-session",
        "solutionList": {
            "solutions": [
                {
                    "id": "solution-1",
                    "displayTotal": price,
                    "itinerary": {
                        "carriers": [{"code": "ZZ"}],
                        "slices": [
                            {
                                "origin": {"code": "SRC"},
                                "destination": {"code": "DST"},
                                "departure": f"{date}T08:00:00",
                                "arrival": f"{date}T10:00:00",
                                "flights": ["ZZ1"],
                                "cabins": ["COACH"],
                                "duration": 120,
                            }
                        ],
                    },
                }
            ]
        },
    }


class FakeMatrixClient:
    def __init__(self) -> None:
        self.detail_calls = []
        self.search_calls = []
        self.lookup_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def search(self, slices, *, pax, **kwargs):
        self.search_calls.append({"slices": slices, "pax": pax, "kwargs": kwargs})
        price = "USD100.00" if slices[0].date.endswith("-02") else "USD200.00"
        return _raw_response(price=price, date=slices[0].date)

    def lookup_locations(self, query: str, *, page_size: int):
        self.lookup_calls.append({"query": query, "page_size": page_size})
        return [{"code": "SRC", "displayName": query}]

    def detail(self, search_response, solution_id, slices, *, pax=None, **kwargs):
        self.detail_calls.append(
            {
                "search_response": search_response,
                "solution_id": solution_id,
                "slices": slices,
                "pax": pax,
                "kwargs": kwargs,
            }
        )
        return {
            "bookingDetails": {
                "itinerary": {
                    "slices": [
                        {
                            "origin": {"code": "SRC"},
                            "destination": {"code": "DST"},
                            "departure": "2099-01-01T08:00:00",
                            "arrival": "2099-01-01T10:00:00",
                            "segments": [
                                {
                                    "carrier": {"code": "ZZ"},
                                    "flight": {"number": "1"},
                                    "origin": {"code": "SRC"},
                                    "destination": {"code": "DST"},
                                    "departure": "2099-01-01T08:00:00",
                                    "arrival": "2099-01-01T10:00:00",
                                    "duration": 120,
                                    "bookingInfos": [
                                        {"bookingCode": "Y", "cabin": "COACH"},
                                    ],
                                    "legs": [
                                        {"aircraft": {"shortName": "Test Jet"}},
                                    ],
                                }
                            ],
                        }
                    ]
                }
            }
        }


class MCPServiceTests(unittest.TestCase):
    def test_flight_search_serializes_solution_and_options(self) -> None:
        created = []

        def factory():
            client = FakeMatrixClient()
            created.append(client)
            return client

        result = _execute_flight_search(
            FlightSearchParams(
                source="src",
                destination="dst",
                depart_date="2099-01-01",
                return_date="2099-01-08",
                cabin="BUSINESS",
                max_stops=0,
                airlines=["ZZ"],
                via="hub",
                limit=5,
            ),
            client_factory=factory,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["trip_type"], "ROUND_TRIP")
        self.assertEqual(result["solutions"][0]["price"], "USD200.00")

        call = created[0].search_calls[0]
        self.assertEqual(call["kwargs"]["cabin"], "BUSINESS")
        self.assertEqual(call["kwargs"]["max_stops"], 0)
        self.assertEqual(call["slices"][0].origin, "SRC")
        self.assertEqual(call["slices"][1].destination, "SRC")
        self.assertIn("HUB", call["slices"][0].route_language)

    def test_date_search_returns_cheapest_dates_first(self) -> None:
        result = _execute_date_search(
            DateSearchParams(
                source="SRC",
                destination="DST",
                start_date="2099-01-01",
                end_date="2099-01-03",
                duration_days=3,
                limit=2,
            ),
            client_factory=FakeMatrixClient,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["trip_type"], "ROUND_TRIP")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["dates"][0]["depart_date"], "2099-01-02")
        self.assertEqual(result["dates"][0]["price"], "USD100.00")

    def test_location_lookup_uses_requested_limit(self) -> None:
        created = []

        def factory():
            client = FakeMatrixClient()
            created.append(client)
            return client

        result = _execute_location_lookup(
            LookupParams(query="source", limit=7),
            client_factory=factory,
        )

        self.assertTrue(result["success"])
        self.assertEqual(created[0].lookup_calls[0]["page_size"], 7)
        self.assertEqual(result["locations"][0]["code"], "SRC")

    def test_flight_detail_fetches_booking_details(self) -> None:
        created = []

        def factory():
            client = FakeMatrixClient()
            created.append(client)
            return client

        result = _execute_flight_detail(
            FlightDetailParams(
                source="SRC",
                destination="DST",
                depart_date="2099-01-01",
                rank=1,
            ),
            client_factory=factory,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["solution"]["id"], "solution-1")
        self.assertEqual(result["detail"]["slices"][0]["segments"][0]["booking_codes"], ["Y"])
        self.assertEqual(result["detail"]["slices"][0]["segments"][0]["aircraft"], "Test Jet")
        self.assertEqual(created[0].detail_calls[0]["solution_id"], "solution-1")

    def test_invalid_sort_is_reported_as_tool_error(self) -> None:
        result = _execute_flight_search(
            FlightSearchParams(
                source="SRC",
                destination="DST",
                depart_date="2099-01-01",
                sort="bogus",
            ),
            client_factory=FakeMatrixClient,
        )

        self.assertFalse(result["success"])
        self.assertIn("sort must be", result["error"])


if __name__ == "__main__":
    unittest.main()
