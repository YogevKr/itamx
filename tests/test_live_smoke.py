import datetime as dt
import os
import unittest

from itamx.client import MatrixClient, Slice


@unittest.skipUnless(os.getenv("ITAMX_LIVE") == "1", "set ITAMX_LIVE=1 to hit Matrix")
class LiveSmokeTests(unittest.TestCase):
    def test_lookup_search_and_detail(self) -> None:
        depart_date = (dt.date.today() + dt.timedelta(days=90)).isoformat()

        with MatrixClient(timeout=120.0) as client:
            locations = client.lookup_locations("New York", page_size=5)
            self.assertTrue(locations)

            slices = [Slice(origin="NYC", destination="LON", date=depart_date)]
            raw = client.search(slices=slices, page_size=1)
            solutions = raw.get("solutionList", {}).get("solutions", [])
            self.assertTrue(solutions)

            detail = client.detail(raw, solutions[0]["id"], slices)
            self.assertIn("bookingDetails", detail)


if __name__ == "__main__":
    unittest.main()
