import unittest
from datetime import date

import support
from app.errors import (
    DocumentExpiredError,
    NotAuthorizedError,
    ValidationError,
)
from support import ALL_MEMBERS, LDN, at


class ItineraryFlowTest(unittest.TestCase):
    def setUp(self):
        self.svc = support.build_service()

    def test_confirm_flow_books_costs(self):
        ids = support.build_itinerary(self.svc)
        result = self.svc.confirm_itinerary("IT1", by_member="L1")
        self.assertEqual(
            sorted(result["confirmed_segments"]),
            ["S-BUS", "S-FLY", "S-HOTEL", "S-TRN"],
        )
        view = self.svc.itinerary_view("IT1", viewer_id="L1")
        self.assertEqual(view["status"], "confirmed")
        self.assertTrue(view["deadlines"]["ok"])
        statuses = {s["segment_id"]: s["status"] for s in view["segments"]}
        self.assertTrue(all(s == "confirmed" for s in statuses.values()))
        summary = self.svc.cost_summary("IT1")
        self.assertEqual(summary["currencies"]["CNY"]["booked"], "42300.00")
        self.assertEqual(summary["currencies"]["CNY"]["retained_reimbursable"], "0.00")

    def test_confirm_requires_privileged_role(self):
        support.build_itinerary(self.svc)
        with self.assertRaises(NotAuthorizedError):
            self.svc.confirm_itinerary("IT1", by_member="P1")

    def test_confirm_rejects_missing_passport(self):
        self.svc.add_member("球员7", "player", member_id="P7", squad_number=7)
        support.build_itinerary(self.svc, members=ALL_MEMBERS + ["P7"])
        with self.assertRaises(DocumentExpiredError) as ctx:
            self.svc.confirm_itinerary("IT1", by_member="L1")
        self.assertIn("缺少passport", str(ctx.exception))

    def test_confirm_rejects_expired_passport(self):
        svc = support.build_service()
        # 把 P3 的唯一护照换成行程结束前过期的证件
        svc.state.members["P3"].documents.clear()
        svc.add_document("P3", "passport", "E-P3-OLD", date(2026, 9, 30))
        support.build_itinerary(svc)
        with self.assertRaises(DocumentExpiredError) as ctx:
            svc.confirm_itinerary("IT1", by_member="L1")
        self.assertIn("已过期", str(ctx.exception))

    def test_confirm_rejects_missing_assignments(self):
        svc = support.build_service()
        svc.create_itinerary("客场征程", match_id="M1", itinerary_id="IT1")
        svc.add_segment(
            "IT1",
            {
                "kind": "ground",
                "supplier": "车队A",
                "origin": "机场",
                "destination": "酒店",
                "starts_at": at(1, 18, 0, LDN),
                "ends_at": at(1, 19, 0, LDN),
                "cost": 800,
                "member_ids": ALL_MEMBERS,
            },
            segment_id="S-BUS",
        )
        with self.assertRaises(ValidationError) as ctx:
            svc.confirm_itinerary("IT1", by_member="L1")
        self.assertIn("缺少车辆分配", str(ctx.exception))

    def test_direct_edit_blocked_after_confirm(self):
        support.build_confirmed(self.svc)
        with self.assertRaises(ValidationError):
            self.svc.add_segment(
                "IT1",
                {
                    "kind": "ground",
                    "supplier": "车队A",
                    "origin": "酒店",
                    "destination": "球场",
                    "starts_at": at(2, 16, 0, LDN),
                    "ends_at": at(2, 16, 30, LDN),
                    "cost": 300,
                    "member_ids": ALL_MEMBERS,
                },
            )

    def test_split_segment(self):
        ids = support.build_itinerary(self.svc)
        group_a = ALL_MEMBERS[:5]
        group_b = ALL_MEMBERS[5:]
        new_ids = self.svc.split_segment("IT1", "S-BUS", [group_a, group_b])
        self.assertEqual(len(new_ids), 2)
        view = self.svc.itinerary_view("IT1", viewer_id="L1")
        by_id = {s["segment_id"]: s for s in view["segments"]}
        self.assertEqual(by_id["S-BUS"]["status"], "split")
        children = [by_id[i] for i in new_ids]
        self.assertEqual({m["member_id"] for m in children[0]["members"]}, set(group_a))
        self.assertEqual({m["member_id"] for m in children[1]["members"]}, set(group_b))
        # 成本按人数拆分且总额不变
        self.assertEqual(
            round(sum(float(c["cost"]) for c in children), 2),
            float(by_id["S-BUS"]["cost"]),
        )
        # 子段可继续分配车辆并确认
        for child_id in new_ids:
            self.svc.auto_assign_vehicles(child_id)
        self.svc.confirm_itinerary("IT1", by_member="L1")
        self.assertEqual(self.svc.validate_consistency(), [])

    def test_split_rejects_overlap_and_uncovered(self):
        support.build_itinerary(self.svc)
        with self.assertRaises(ValidationError):
            self.svc.split_segment("IT1", "S-BUS", [ALL_MEMBERS[:6], ALL_MEMBERS[5:]])
        with self.assertRaises(ValidationError):
            self.svc.split_segment("IT1", "S-BUS", [ALL_MEMBERS[:4], ALL_MEMBERS[4:9]])

    def test_split_blocked_after_confirm(self):
        support.build_confirmed(self.svc)
        with self.assertRaises(ValidationError):
            self.svc.split_segment("IT1", "S-BUS", [ALL_MEMBERS[:5], ALL_MEMBERS[5:]])

    def test_member_schedule_view(self):
        support.build_confirmed(self.svc)
        schedule = self.svc.member_schedule("IT1", "P1")
        self.assertEqual(len(schedule["segments"]), 4)
        self.assertEqual(schedule["member"]["member_id"], "P1")
        bus = next(s for s in schedule["segments"] if s["kind"] == "ground")
        self.assertTrue(bus["vehicles"])
        hotel = next(s for s in schedule["segments"] if s["kind"] == "hotel")
        self.assertTrue(hotel["rooms"])
        # 成员视图里看不到队友的医疗备注与紧急联系人
        teammate = next(
            m
            for s in schedule["segments"]
            for m in s["members"]
            if m["member_id"] == "P2"
        )
        self.assertIsNone(teammate["medical_notes"])
        self.assertIsNone(teammate["emergency_contact"])


if __name__ == "__main__":
    unittest.main()
