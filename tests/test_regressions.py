import unittest
from unittest import mock

from typer.testing import CliRunner

from itamx.cli import app
from itamx.client import MatrixClient, Slice
from itamx.render import extract_rbd, format_duration, price_float
from itamx.request_options import SearchOptions
from itamx.validation import parse_int_range, parse_weekdays


runner = CliRunner()


class RecordingClient(MatrixClient):
    def __init__(self) -> None:
        self.path = ""

    def _get_batch(self, path: str) -> dict:
        self.path = path
        return {"locations": []}


class RecordingPostClient(MatrixClient):
    def __init__(self) -> None:
        self.body = {}
        self.path = ""

    def _post_batch(self, body: dict, path: str) -> dict:
        self.body = body
        self.path = path
        return {"ok": True}


class RegressionTests(unittest.TestCase):
    def test_lookup_locations_url_encodes_partial_name(self) -> None:
        client = RecordingClient()

        self.assertEqual(client.lookup_locations("New York"), [])
        self.assertIn("New%20York", client.path)
        self.assertNotIn("New York", client.path)

    def test_render_helpers_preserve_display_values(self) -> None:
        self.assertEqual(price_float("USD123.45"), 123.45)
        self.assertIsNone(price_float("bad"))
        self.assertEqual(format_duration(125), "2h05m")
        self.assertEqual(extract_rbd(None), "\u2014")

    def test_detail_preserves_search_input_knobs(self) -> None:
        client = RecordingPostClient()
        options = SearchOptions(
            cabin="BUSINESS",
            max_stops=0,
            sorts="duration",
            currency="ILS",
            sales_city="TLV",
        )

        self.assertEqual(
            client.detail(
                {"solutionSet": "set", "session": "session"},
                "solution",
                [Slice(origin="TLV", destination="SFO", date="2026-05-18")],
                **options.detail_kwargs(),
            ),
            {"ok": True},
        )

        inputs = client.body["inputs"]
        self.assertEqual(client.path, "/v1/summarize")
        self.assertEqual(inputs["cabin"], "BUSINESS")
        self.assertEqual(inputs["maxLegsRelativeToMin"], 0)
        self.assertEqual(inputs["sorts"], "duration")
        self.assertEqual(inputs["currency"], "ILS")
        self.assertEqual(inputs["salesCity"], "TLV")

    def test_search_options_share_search_and_detail_values(self) -> None:
        options = SearchOptions(
            cabin="FIRST",
            max_stops=2,
            page_size=25,
            sorts="price",
            currency="USD",
            sales_city="NYC",
        )

        self.assertEqual(
            options.search_kwargs(),
            {
                "cabin": "FIRST",
                "max_stops": 2,
                "page_size": 25,
                "change_of_airport": True,
                "currency": "USD",
                "sales_city": "NYC",
                "sorts": "price",
            },
        )
        self.assertEqual(
            options.detail_kwargs(),
            {
                "cabin": "FIRST",
                "max_stops": 2,
                "change_of_airport": True,
                "currency": "USD",
                "sales_city": "NYC",
                "sorts": "price",
            },
        )

    def test_parse_weekdays_accepts_known_values(self) -> None:
        self.assertEqual(parse_weekdays("sun, MON"), {6, 0})
        self.assertIsNone(parse_weekdays(""))

    def test_parse_weekdays_rejects_unknown_values(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_weekdays("FUNDAY")
        self.assertIn("FUNDAY", str(ctx.exception))

    def test_parse_int_range_bounds_and_order(self) -> None:
        self.assertEqual(parse_int_range("5-7"), [5, 6, 7])
        self.assertEqual(parse_int_range("0"), [0])

        for value in ("8-5", "-1", "61"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_int_range(value)

    def test_invalid_output_mode_is_rejected_before_command_body(self) -> None:
        result = runner.invoke(app, ["airlines", "AF", "--output", "xml"])

        self.assertEqual(result.exit_code, 2)
        self.assertIn("not one of", result.output)

    def test_invalid_sort_is_rejected_before_network_call(self) -> None:
        result = runner.invoke(app, ["search", "TLV", "SFO", "2026-05-18", "--sort", "bogus"])

        self.assertEqual(result.exit_code, 2)
        self.assertIn("Invalid value", result.output)
        self.assertIn("sort", result.output)
        self.assertIn("bogus", result.output)

    def test_flex_treats_window_as_depart_range(self) -> None:
        # Regression: previously rejected because Apr 1 + 7 days = May 8 > end.
        # New semantics: [start, end] is the depart window, return extends freely.
        # 3 depart dates × 1 duration = 3 candidates.
        with mock.patch.object(MatrixClient, "search", return_value={"solutionList": {"solutions": []}}):
            result = runner.invoke(
                app,
                ["flex", "TLV", "SFO", "2026-05-01", "2026-05-03",
                 "--duration", "7", "--output", "json"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Searching 3 (depart, return)", result.output)

    def test_flex_rectangular_sweep_with_ret_window(self) -> None:
        # 2 depart × 3 return = 6 candidates.
        with mock.patch.object(MatrixClient, "search", return_value={"solutionList": {"solutions": []}}):
            result = runner.invoke(
                app,
                ["flex", "TLV", "SFO", "2026-05-01", "2026-05-02",
                 "--ret-start", "2026-05-15", "--ret-end", "2026-05-17",
                 "--output", "json"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Searching 6 (depart, return)", result.output)

    def test_flex_rectangular_sweep_filters_by_stay(self) -> None:
        # 2 depart × 3 return = 6 raw, but --stay 14 keeps only depart+14 == return:
        # (2026-05-01, 2026-05-15) and (2026-05-02, 2026-05-16).
        with mock.patch.object(MatrixClient, "search", return_value={"solutionList": {"solutions": []}}):
            result = runner.invoke(
                app,
                ["flex", "TLV", "SFO", "2026-05-01", "2026-05-02",
                 "--ret-start", "2026-05-15", "--ret-end", "2026-05-17",
                 "--stay", "14", "--output", "json"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Searching 2 (depart, return)", result.output)

    def test_flex_ret_window_requires_both_endpoints(self) -> None:
        result = runner.invoke(
            app,
            ["flex", "TLV", "SFO", "2026-05-01", "2026-05-03",
             "--ret-start", "2026-05-15"],
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--ret-start and --ret-end must be used together", result.output)
