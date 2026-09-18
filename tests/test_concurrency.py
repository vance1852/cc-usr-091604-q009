import threading
import unittest

import support
from app.errors import CapacityError, ConflictError
from support import ALL_MEMBERS


def run_concurrently(fn, count):
    barrier = threading.Barrier(count)
    results, errors = [], []

    def worker():
        barrier.wait(timeout=10)
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001 - 测试需要收集所有异常类型
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, errors


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_approvals_single_winner(self):
        svc = support.build_service()
        support.build_confirmed(svc)
        svc.add_transport_option(
            "flight", "航司D", "MU553", "上海浦东", "伦敦希思罗",
            support.at(1, 17, 30, support.SH), support.at(1, 21, 35, support.LDN),
            30, 30000, option_id="F4",
        )
        draft_b = support.replacement_flight_draft()
        draft_d = dict(draft_b, supplier="航司D", cost=30000, option_id="F4",
                       starts_at=support.at(1, 17, 30, support.SH),
                       ends_at=support.at(1, 21, 35, support.LDN))
        proposal_b = svc.propose_change(
            "IT1", proposer_id="L1", reason="改签航司B",
            actions=[{"type": "replace", "segment_id": "S-FLY",
                      "replacement": draft_b, "notify": ["L1"]}],
        )
        proposal_d = svc.propose_change(
            "IT1", proposer_id="L1", reason="改签航司D",
            actions=[{"type": "replace", "segment_id": "S-FLY",
                      "replacement": draft_d, "notify": ["L1"]}],
        )
        proposals = [proposal_b, proposal_d]

        def approve():
            return svc.approve_change(proposals.pop(), by_member="O1")

        results, errors = run_concurrently(approve, 2)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ConflictError)
        # 只有一个方案生效，航班段只被替换一次
        view = svc.itinerary_view("IT1", viewer_id="O1")
        active_flights = [
            s for s in view["segments"]
            if s["kind"] == "flight" and s["status"] == "confirmed"
        ]
        self.assertEqual(len(active_flights), 1)
        replaced = [s for s in view["segments"] if s["status"] == "replaced"]
        self.assertEqual(len(replaced), 1)
        statuses = {p["proposal_id"]: p["status"] for p in svc.list_proposals("IT1")}
        self.assertEqual(sorted(statuses.values()), ["applied", "conflict"])
        self.assertEqual(svc.validate_consistency(), [])

    def test_concurrent_vehicle_assignment_no_overbook(self):
        svc = support.build_service()
        svc.create_itinerary("并发排座", itinerary_id="IT1")
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
                "member_ids": ALL_MEMBERS[:6],
            },
            segment_id="S-BUS",
        )
        svc.add_vehicle("小车", 2, "车队A", vehicle_id="V9")
        members = iter(ALL_MEMBERS[:6])

        def assign():
            return svc.assign_vehicle("S-BUS", "V9", [next(members)])

        results, errors = run_concurrently(assign, 6)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(errors), 4)
        self.assertTrue(all(isinstance(e, CapacityError) for e in errors))
        occupants = svc.state.vehicle_assignments["S-BUS"]["V9"]
        self.assertEqual(len(occupants), 2)
        self.assertEqual(svc.validate_consistency(), [])

    def test_concurrent_duplicate_callbacks_applied_once(self):
        svc = support.build_service()
        support.build_confirmed(svc)

        def callback():
            return svc.supplier_callback("cb-race", "航司A", "S-FLY", "cancelled")

        results, errors = run_concurrently(callback, 4)
        self.assertEqual(errors, [])
        applied = [r for r in results if r["applied"]]
        duplicates = [r for r in results if r["duplicate"]]
        self.assertEqual(len(applied), 1)
        self.assertEqual(len(duplicates), 3)
        records = [
            r for r in svc.change_records("IT1") if r["action"] == "supplier_cancelled"
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(len(svc.state.callbacks), 1)
        self.assertEqual(svc.validate_consistency(), [])


if __name__ == "__main__":
    unittest.main()
