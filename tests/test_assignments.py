import unittest

import support
from app.errors import (
    CapacityError,
    DoubleBookingError,
    NotAuthorizedError,
    NotFoundError,
    SegmentLockedError,
    ValidationError,
)
from support import ALL_MEMBERS


class AssignmentTest(unittest.TestCase):
    def setUp(self):
        self.svc = support.build_service()

    def test_vehicle_capacity(self):
        svc = support.build_service()
        svc.create_itinerary("容量测试", itinerary_id="IT1")
        svc.add_segment(
            "IT1",
            {
                "kind": "ground",
                "supplier": "车队A",
                "origin": "机场",
                "destination": "酒店",
                "starts_at": support.at(1, 18, 0, support.LDN),
                "ends_at": support.at(1, 19, 0, support.LDN),
                "cost": 100,
                "member_ids": ["P1", "P3"],
            },
            segment_id="S-BUS",
        )
        svc.add_vehicle("小车", 1, "车队A", vehicle_id="V9")
        with self.assertRaises(CapacityError):
            svc.assign_vehicle("S-BUS", "V9", ["P1", "P3"])

    def test_vehicle_double_booking(self):
        support.build_itinerary(self.svc)
        with self.assertRaises(DoubleBookingError):
            self.svc.assign_vehicle("S-BUS", "V2", ["P1"])

    def test_room_double_booking(self):
        support.build_itinerary(self.svc)
        with self.assertRaises(DoubleBookingError):
            self.svc.assign_room("S-HOTEL", "R7", ["P1"])

    def test_single_room_requirement(self):
        svc = support.build_service()
        svc.create_itinerary("住宿测试", itinerary_id="IT1")
        svc.add_segment(
            "IT1",
            {
                "kind": "hotel",
                "supplier": "酒店H",
                "origin": None,
                "destination": "酒店H",
                "starts_at": support.at(1, 19, 0, support.LDN),
                "ends_at": support.at(3, 11, 0, support.LDN),
                "cost": 1000,
                "member_ids": ["P1", "P2", "P3"],
            },
            segment_id="S-HOTEL",
        )
        # 伤员 P2 不能与他人合住双人间
        with self.assertRaises(ValidationError):
            svc.assign_room("S-HOTEL", "R3", ["P2", "P3"])
        # 伤员必须住单间且独住
        svc.assign_room("S-HOTEL", "R1", ["P2"])
        with self.assertRaises(CapacityError):
            svc.assign_room("S-HOTEL", "R1", ["P3"])
        svc.assign_room("S-HOTEL", "R3", ["P1", "P3"])
        self.assertEqual(svc.validate_consistency(), [])

    def test_auto_assign_rooms_respects_single_requirement(self):
        support.build_itinerary(self.svc)
        rooms = self.svc.state.room_assignments["S-HOTEL"]
        single_rooms = {
            rid for rid, occ in rooms.items() if self.svc.state.rooms[rid].room_type.value == "single"
        }
        p2_room = next(rid for rid, occ in rooms.items() if "P2" in occ)
        self.assertIn(p2_room, single_rooms)
        self.assertEqual(len(rooms[p2_room]), 1)
        assigned = set().union(*rooms.values())
        self.assertEqual(assigned, set(ALL_MEMBERS))

    def test_auto_assign_rooms_insufficient(self):
        svc = support.build_service()
        svc.create_itinerary("房间不足", itinerary_id="IT1")
        svc.add_segment(
            "IT1",
            {
                "kind": "hotel",
                "supplier": "酒店H",
                "origin": None,
                "destination": "酒店H",
                "starts_at": support.at(1, 19, 0, support.LDN),
                "ends_at": support.at(3, 11, 0, support.LDN),
                "cost": 1000,
                "member_ids": ALL_MEMBERS,
            },
            segment_id="S-HOTEL",
        )
        for room_id in ["R3", "R4", "R5", "R6", "R7"]:
            del svc.state.rooms[room_id]
        with self.assertRaises(CapacityError):
            svc.auto_assign_rooms("S-HOTEL")
        # 失败后不留半拉子分配
        self.assertEqual(svc.state.room_assignments.get("S-HOTEL"), {})

    def test_withdraw_releases_only_their_resources(self):
        support.build_confirmed(self.svc)
        before_rooms = {
            rid: set(occ)
            for rid, occ in self.svc.state.room_assignments["S-HOTEL"].items()
        }
        result = self.svc.withdraw_members(
            "IT1", ["P3", "P4"], by_member="O1", reason="临时召回"
        )
        self.assertEqual(result["withdrawn"], ["P3", "P4"])
        view = self.svc.itinerary_view("IT1", viewer_id="O1")
        for segment in view["segments"]:
            member_ids = {m["member_id"] for m in segment["members"]}
            self.assertNotIn("P3", member_ids)
            self.assertNotIn("P4", member_ids)
        # 车辆：P3/P4 的座位被释放，其他人不变
        vehicles = self.svc.state.vehicle_assignments["S-BUS"]
        self.assertNotIn("P3", vehicles["V2"])
        self.assertNotIn("P4", vehicles["V2"])
        self.assertEqual(vehicles["V2"], {"P5", "P6"})
        self.assertEqual(vehicles["V1"], {"C1", "L1", "M1", "O1", "P1", "P2"})
        # 房间：只释放退出成员的床位
        rooms = self.svc.state.room_assignments["S-HOTEL"]
        for rid, occupants in rooms.items():
            before = before_rooms.get(rid, set())
            self.assertEqual(occupants, before - {"P3", "P4"})
        # 留痕
        record = next(
            r for r in self.svc.change_records("IT1") if r["action"] == "withdraw"
        )
        self.assertEqual(record["notify"], ["P3", "P4"])
        self.assertEqual(record["detail"], "临时召回")
        self.assertEqual(self.svc.validate_consistency(), [])

    def test_withdraw_unknown_member_rejected(self):
        support.build_confirmed(self.svc)
        with self.assertRaises(NotAuthorizedError):
            self.svc.withdraw_members("IT1", ["P3"], by_member="P1")
        with self.assertRaises(NotFoundError):
            self.svc.withdraw_members("IT1", ["NOBODY"], by_member="O1")

    def test_assignment_blocked_on_completed_segment(self):
        support.build_confirmed(self.svc)
        self.svc.supplier_callback("cb-1", "车队A", "S-BUS", "departed")
        self.svc.supplier_callback("cb-2", "车队A", "S-BUS", "arrived")
        with self.assertRaises(SegmentLockedError):
            self.svc.assign_vehicle("S-BUS", "V1", ["P1"])


if __name__ == "__main__":
    unittest.main()
