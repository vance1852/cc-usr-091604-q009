import unittest

from app.travel import TravelService, TripSegment


class TravelSmokeTest(unittest.TestCase):
    def test_segment_and_health(self):
        self.assertEqual(TripSegment("上海", "成都").destination, "成都")
        self.assertEqual(TravelService().health()["status"], "ok")


if __name__ == "__main__":
    unittest.main()

