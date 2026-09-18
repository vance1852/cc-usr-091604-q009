import json
import os
import tempfile
import unittest

import support
from app.errors import (
    CapacityError,
    ConsistencyError,
    DoubleBookingError,
    ValidationError,
)
from app.service import TravelService


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "state.json")

    def _build_rich_state(self):
        svc = support.build_service(storage_path=self.path)
        support.build_confirmed(svc)
        svc.withdraw_members("IT1", ["P5"], by_member="O1", reason="临时召回")
        proposal_id = svc.propose_change(
            "IT1",
            proposer_id="L1",
            reason="航司A 航班取消，改签航司B",
            actions=[
                {
                    "type": "replace",
                    "segment_id": "S-FLY",
                    "replacement": support.replacement_flight_draft(),
                    "notify": ["L1", "O1"],
                }
            ],
        )
        svc.approve_change(proposal_id, by_member="O1")
        return svc

    def test_restart_restores_consistent_state(self):
        svc = self._build_rich_state()
        svc.snapshot()
        restored = TravelService.recover(self.path)
        self.assertEqual(restored.validate_consistency(), [])
        # 视图与费用汇总在重启前后一致
        self.assertEqual(
            svc.itinerary_view("IT1", viewer_id="L1"),
            restored.itinerary_view("IT1", viewer_id="L1"),
        )
        self.assertEqual(svc.cost_summary("IT1"), restored.cost_summary("IT1"))
        self.assertEqual(
            svc.change_records("IT1"), restored.change_records("IT1")
        )
        self.assertEqual(
            svc.member_profile("P2", viewer_id="M1"),
            restored.member_profile("P2", viewer_id="M1"),
        )

    def test_restart_keeps_resource_protection(self):
        svc = self._build_rich_state()
        restored = TravelService.recover(self.path)
        # 重复占用校验在重启后依然生效
        with self.assertRaises(DoubleBookingError):
            restored.assign_vehicle("S-BUS", "V2", ["P1"])
        with self.assertRaises(DoubleBookingError):
            restored.assign_room("S-HOTEL", "R7", ["P1"])
        # 恢复后的实例仍可继续走完整变更流程
        proposal_id = restored.propose_change(
            "IT1",
            proposer_id="L1",
            actions=[{"type": "cancel", "segment_id": "S-TRN", "notify": ["L1"]}],
        )
        restored.approve_change(proposal_id, by_member="O1")
        again = TravelService.recover(self.path)
        self.assertEqual(again.validate_consistency(), [])
        view = again.itinerary_view("IT1", viewer_id="O1")
        by_id = {s["segment_id"]: s for s in view["segments"]}
        self.assertEqual(by_id["S-TRN"]["status"], "cancelled")

    def test_restart_keeps_capacity_check(self):
        svc = support.build_service(storage_path=self.path)
        svc.create_itinerary("容量", itinerary_id="IT1")
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
        svc.assign_vehicle("S-BUS", "V9", ["P1"])
        restored = TravelService.recover(self.path)
        with self.assertRaises(CapacityError):
            restored.assign_vehicle("S-BUS", "V9", ["P3"])
        self.assertEqual(restored.validate_consistency(), [])

    def test_recover_strict_detects_corruption(self):
        svc = self._build_rich_state()
        svc.snapshot()
        with open(self.path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        # 人为破坏：把 P1 同时塞进两个房间
        rooms = payload["state"]["room_assignments"]["S-HOTEL"]
        rooms["R7"] = {"__set__": ["P1"]}
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        with self.assertRaises(ConsistencyError):
            TravelService.recover(self.path, strict=True)
        relaxed = TravelService.recover(self.path, strict=False)
        issues = relaxed.validate_consistency()
        self.assertTrue(any("重复分配房间" in issue for issue in issues))

    def test_snapshot_requires_path(self):
        svc = support.build_service()
        with self.assertRaises(ValidationError):
            svc.snapshot()


if __name__ == "__main__":
    unittest.main()
