import unittest
from datetime import datetime

import support
from app.errors import DeadlineError, ValidationError
from support import ALL_MEMBERS, LDN, at


class TimezoneTest(unittest.TestCase):
    def setUp(self):
        self.svc = support.build_service()

    def test_cross_timezone_arrival(self):
        support.build_confirmed(self.svc)
        view = self.svc.itinerary_view("IT1", viewer_id="L1")
        flight = next(s for s in view["segments"] if s["kind"] == "flight")
        # 上海 13:00 (+08:00) 起飞，伦敦 17:30 (+01:00) 落地，飞行 11.5 小时
        self.assertEqual(flight["starts_at"], "2026-10-01T13:00:00+08:00")
        self.assertEqual(flight["ends_at"], "2026-10-01T17:30:00+01:00")
        self.assertEqual(flight["duration_minutes"], 690)
        self.assertFalse(flight["crosses_midnight"])
        # 到达早于最晚报到时间
        self.assertTrue(view["deadlines"]["ok"])
        self.assertEqual(view["deadlines"]["report_by"], "2026-10-02T12:00:00+01:00")

    def test_overnight_cross_day_segment(self):
        self.svc.create_itinerary("夜间转场", itinerary_id="IT-NIGHT")
        self.svc.add_segment(
            "IT-NIGHT",
            {
                "kind": "ground",
                "supplier": "车队A",
                "origin": "酒店H",
                "destination": "客场球场",
                "starts_at": at(2, 22, 30, LDN),
                "ends_at": at(3, 1, 40, LDN),
                "cost": 500,
                "member_ids": ALL_MEMBERS[:4],
            },
            segment_id="S-NIGHT",
        )
        view = self.svc.itinerary_view("IT-NIGHT", viewer_id="L1")
        night = view["segments"][0]
        self.assertTrue(night["crosses_midnight"])
        self.assertEqual(night["duration_minutes"], 190)
        self.assertEqual(night["starts_at"], "2026-10-02T22:30:00+01:00")
        self.assertEqual(night["ends_at"], "2026-10-03T01:40:00+01:00")

    def test_naive_datetime_rejected(self):
        self.svc.create_itinerary("朴素时间", itinerary_id="IT-NAIVE")
        with self.assertRaises(ValidationError):
            self.svc.add_segment(
                "IT-NAIVE",
                {
                    "kind": "ground",
                    "supplier": "车队A",
                    "origin": "a",
                    "destination": "b",
                    "starts_at": datetime(2026, 10, 2, 22, 30),
                    "ends_at": datetime(2026, 10, 3, 1, 40),
                    "cost": 100,
                    "member_ids": ALL_MEMBERS[:2],
                },
            )

    def test_report_deadline_violation_blocks_confirm(self):
        svc = support.build_service()
        svc.create_itinerary("迟到航班", match_id="M1", itinerary_id="IT1")
        # F3 落地 10-02 13:05 伦敦，晚于 12:00 报到截止
        svc.add_segment_from_option("IT1", "F3", ALL_MEMBERS, segment_id="S-FLY")
        with self.assertRaises(DeadlineError) as ctx:
            svc.confirm_itinerary("IT1", by_member="L1")
        self.assertIn("最晚报到时间", str(ctx.exception))

    def test_deadline_info_in_view(self):
        support.build_confirmed(self.svc)
        view = self.svc.itinerary_view("IT1", viewer_id="L1")
        self.assertEqual(view["deadlines"]["last_arrival"], "2026-10-01T19:00:00+01:00")


if __name__ == "__main__":
    unittest.main()
