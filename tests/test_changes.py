import unittest

import support
from app.errors import (
    ConflictError,
    NotAuthorizedError,
    SegmentLockedError,
    ValidationError,
)
from support import ALL_MEMBERS, LDN, at


class ChangeApprovalTest(unittest.TestCase):
    def setUp(self):
        self.svc = support.build_service()
        support.build_confirmed(self.svc)

    def _propose_replace_flight(self):
        return self.svc.propose_change(
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

    def test_propose_requires_leader(self):
        with self.assertRaises(NotAuthorizedError):
            self.svc.propose_change(
                "IT1",
                proposer_id="P1",
                actions=[{"type": "cancel", "segment_id": "S-TRN", "notify": ["L1"]}],
            )

    def test_propose_requires_confirmed_itinerary(self):
        svc = support.build_service()
        support.build_itinerary(svc)
        with self.assertRaises(ValidationError):
            svc.propose_change(
                "IT1",
                proposer_id="L1",
                actions=[{"type": "cancel", "segment_id": "S-TRN", "notify": ["L1"]}],
            )

    def test_approve_requires_ops(self):
        proposal_id = self._propose_replace_flight()
        with self.assertRaises(NotAuthorizedError):
            self.svc.approve_change(proposal_id, by_member="L1")

    def test_replace_flow_records_supplier_delta_and_notify(self):
        proposal_id = self._propose_replace_flight()
        result = self.svc.approve_change(proposal_id, by_member="O1")
        self.assertEqual(len(result["records"]), 1)

        view = self.svc.itinerary_view("IT1", viewer_id="O1")
        by_id = {s["segment_id"]: s for s in view["segments"]}
        self.assertEqual(by_id["S-FLY"]["status"], "replaced")
        new_segments = [
            s for s in view["segments"] if s["kind"] == "flight" and s["status"] == "confirmed"
        ]
        self.assertEqual(len(new_segments), 1)
        self.assertEqual(new_segments[0]["supplier"], "航司B")
        self.assertEqual(len(new_segments[0]["members"]), len(ALL_MEMBERS))

        records = self.svc.change_records("IT1")
        record = next(r for r in records if r["action"] == "replaced")
        self.assertEqual(record["old_supplier"], "航司A")
        self.assertEqual(record["new_supplier"], "航司B")
        self.assertEqual(record["cost_delta"], "3500.00")
        self.assertEqual(record["retained_cost"], "28000.00")
        self.assertEqual(record["notify"], ["L1", "O1"])
        self.assertEqual(record["proposal_id"], proposal_id)

        summary = self.svc.cost_summary("IT1")
        self.assertEqual(summary["currencies"]["CNY"]["booked"], "73800.00")
        self.assertEqual(
            summary["currencies"]["CNY"]["retained_reimbursable"], "28000.00"
        )
        self.assertEqual(self.svc.validate_consistency(), [])

    def test_cancel_flow_retains_cost_and_releases_slot(self):
        proposal_id = self.svc.propose_change(
            "IT1",
            proposer_id="L1",
            reason="训练计划调整",
            actions=[{"type": "cancel", "segment_id": "S-TRN", "notify": ["L1", "M1"]}],
        )
        self.svc.approve_change(proposal_id, by_member="O1")
        view = self.svc.itinerary_view("IT1", viewer_id="O1")
        by_id = {s["segment_id"]: s for s in view["segments"]}
        self.assertEqual(by_id["S-TRN"]["status"], "cancelled")
        record = next(
            r for r in self.svc.change_records("IT1") if r["action"] == "cancelled"
        )
        self.assertEqual(record["old_supplier"], "基地运营方")
        self.assertEqual(record["retained_cost"], "1500.00")
        self.assertEqual(record["cost_delta"], "-1500.00")
        self.assertEqual(record["notify"], ["L1", "M1"])
        # 训练场时段已释放，可重新预订
        rebook = self.svc.propose_change(
            "IT1",
            proposer_id="L1",
            reason="重新预订训练场",
            actions=[
                {
                    "type": "add",
                    "segment": {
                        "kind": "training",
                        "supplier": "基地运营方",
                        "origin": None,
                        "destination": "客场训练基地",
                        "starts_at": at(2, 10, 0, LDN),
                        "ends_at": at(2, 11, 30, LDN),
                        "cost": 1500,
                        "member_ids": ALL_MEMBERS,
                        "slot_id": "T1",
                    },
                    "notify": ["L1"],
                }
            ],
        )
        self.svc.approve_change(rebook, by_member="O1")
        self.assertEqual(self.svc.validate_consistency(), [])

    def test_cancel_requires_notify_targets(self):
        proposal_id = self.svc.propose_change(
            "IT1",
            proposer_id="L1",
            actions=[{"type": "cancel", "segment_id": "S-TRN", "notify": []}],
        )
        with self.assertRaises(ValidationError):
            self.svc.approve_change(proposal_id, by_member="O1")

    def test_completed_segment_cannot_be_rewritten(self):
        self.svc.supplier_callback("cb-1", "航司A", "S-FLY", "departed")
        self.svc.supplier_callback("cb-2", "航司A", "S-FLY", "arrived")
        view = self.svc.itinerary_view("IT1", viewer_id="O1")
        by_id = {s["segment_id"]: s for s in view["segments"]}
        self.assertEqual(by_id["S-FLY"]["status"], "completed")

        replace_id = self._propose_replace_flight()
        with self.assertRaises(SegmentLockedError):
            self.svc.approve_change(replace_id, by_member="O1")
        cancel_id = self.svc.propose_change(
            "IT1",
            proposer_id="L1",
            actions=[{"type": "cancel", "segment_id": "S-FLY", "notify": ["L1"]}],
        )
        with self.assertRaises(SegmentLockedError):
            self.svc.approve_change(cancel_id, by_member="O1")
        # 方案保持待审，行程段未被改动
        proposals = {p["proposal_id"]: p for p in self.svc.list_proposals("IT1")}
        self.assertEqual(proposals[replace_id]["status"], "pending")
        self.assertEqual(proposals[cancel_id]["status"], "pending")

    def test_version_conflict_between_proposals(self):
        first = self._propose_replace_flight()
        second = self.svc.propose_change(
            "IT1",
            proposer_id="L1",
            actions=[{"type": "cancel", "segment_id": "S-TRN", "notify": ["L1"]}],
        )
        self.svc.approve_change(first, by_member="O1")
        with self.assertRaises(ConflictError):
            self.svc.approve_change(second, by_member="O1")
        proposals = {p["proposal_id"]: p for p in self.svc.list_proposals("IT1")}
        self.assertEqual(proposals[first]["status"], "applied")
        self.assertEqual(proposals[second]["status"], "conflict")

    def test_reject_change(self):
        proposal_id = self._propose_replace_flight()
        self.svc.reject_change(proposal_id, by_member="O1", note="预算超限")
        proposals = {p["proposal_id"]: p for p in self.svc.list_proposals("IT1")}
        self.assertEqual(proposals[proposal_id]["status"], "rejected")
        self.assertEqual(proposals[proposal_id]["decision_note"], "预算超限")
        with self.assertRaises(ValidationError):
            self.svc.approve_change(proposal_id, by_member="O1")

    def test_approve_is_atomic_when_one_action_invalid(self):
        # 先取消训练段，再提交一个同时包含替换航班和重复取消训练段的方案
        first = self.svc.propose_change(
            "IT1",
            proposer_id="L1",
            actions=[{"type": "cancel", "segment_id": "S-TRN", "notify": ["L1"]}],
        )
        self.svc.approve_change(first, by_member="O1")
        bundle = self.svc.propose_change(
            "IT1",
            proposer_id="L1",
            actions=[
                {
                    "type": "replace",
                    "segment_id": "S-FLY",
                    "replacement": support.replacement_flight_draft(),
                    "notify": ["L1"],
                },
                {"type": "cancel", "segment_id": "S-TRN", "notify": ["L1"]},
            ],
        )
        with self.assertRaises(SegmentLockedError):
            self.svc.approve_change(bundle, by_member="O1")
        # 原子性：航班段未被替换
        view = self.svc.itinerary_view("IT1", viewer_id="O1")
        by_id = {s["segment_id"]: s for s in view["segments"]}
        self.assertEqual(by_id["S-FLY"]["status"], "confirmed")

    def test_supplier_cancel_callback_then_rebook(self):
        # 供应商通知航班取消
        result = self.svc.supplier_callback("cb-cancel", "航司A", "S-FLY", "cancelled")
        self.assertEqual(result["segment_status"], "cancelled")
        record = next(
            r
            for r in self.svc.change_records("IT1")
            if r["action"] == "supplier_cancelled"
        )
        self.assertEqual(record["old_supplier"], "航司A")
        self.assertEqual(record["retained_cost"], "28000.00")
        self.assertEqual(sorted(record["notify"]), sorted(ALL_MEMBERS))
        # 领队提出改签方案，运营审核后生效
        proposal_id = self.svc.propose_change(
            "IT1",
            proposer_id="L1",
            reason="航班取消后改签",
            actions=[
                {
                    "type": "add",
                    "segment": support.replacement_flight_draft(),
                    "notify": ["L1", "O1"],
                }
            ],
        )
        self.svc.approve_change(proposal_id, by_member="O1")
        view = self.svc.itinerary_view("IT1", viewer_id="O1")
        flights = [
            s
            for s in view["segments"]
            if s["kind"] == "flight" and s["status"] == "confirmed"
        ]
        self.assertEqual(len(flights), 1)
        self.assertEqual(flights[0]["supplier"], "航司B")
        self.assertEqual(self.svc.validate_consistency(), [])


if __name__ == "__main__":
    unittest.main()
