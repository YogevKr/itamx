import json
import unittest
from pathlib import Path

from itamx.mcp.service import _serialize_solution, _sorted_solutions
from itamx.models import SearchResponse


FIXTURE = Path(__file__).parent / "fixtures" / "search_response.json"


class ResponseFixtureTests(unittest.TestCase):
    def test_search_response_fixture_matches_models(self) -> None:
        raw = json.loads(FIXTURE.read_text())

        parsed = SearchResponse.model_validate(raw)

        self.assertEqual(parsed.solutionCount, 2)
        self.assertEqual(parsed.solutionList.solutions[0].id, "expensive")
        self.assertEqual(parsed.carrierStopMatrix.rows[0].cells[0].minPrice, "USD100.00")

    def test_sorted_solution_serialization_uses_price_order(self) -> None:
        raw = json.loads(FIXTURE.read_text())

        _, solutions = _sorted_solutions(raw)
        serialized = [_serialize_solution(solution) for solution in solutions]

        self.assertEqual([solution["id"] for solution in serialized], ["cheap", "expensive"])
        self.assertEqual(serialized[0]["price_value"], 100.0)
        self.assertEqual(serialized[0]["slices"][0]["source"], "SRC")
