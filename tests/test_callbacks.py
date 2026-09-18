import unittest

import support
from app.errors import ValidationError


class SupplierCallbackTest(unittest.TestCase):
    def setUp(self):
        self.svc = support.build_service()
        support.build_confirmed(self.svc)

    def test_duplicate_callback_is_idempotent(self):
        first = self.svc.supplier_callback("cb-1", "航司A", "S-FLY", "departed")
        self.assertTrue(first["applied"])
        self.assertFalse(first["duplicate"])
        self.assertEqual(first["segment_status"], "in_progress")

        second = self.svc.supplier_callback("cb-1", "航司A", "S-FLY", "departed")
        self.assertFalse(second["applied"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["original_event"], "departed")
        self.assertEqual(second["segment_status"], "in_progress")
        self.assertEqual(len(self.svc.state.callbacks), 1)

    def test_duplicate_cancel_callback_records_once(self):
        self.svc.supplier_callback("cb-c", "航司A", "S-FLY", "cancelled")
        again = self.svc.supplier_callback("cb-c", "航司A", "S-FLY", "cancelled")
        self.assertTrue(again["duplicate"])
        records = [
            r
            for r in self.svc.change_records("IT1")
            if r["action"] == "supplier_cancelled"
        ]
        self.assertEqual(len(records), 1)
        # 费用台账没有因为重放而变化
        summary = self.svc.cost_summary("IT1")
        self.assertEqual(summary["currencies"]["CNY"]["booked"], "42300.00")
        self.assertEqual(summary["entry_count"], 4)

    def test_full_lifecycle_to_completed(self):
        self.svc.supplier_callback("cb-1", "航司A", "S-FLY", "confirmed")
        view = self.svc.itinerary_view("IT1", viewer_id="O1")
        flight = next(s for s in view["segments"] if s["segment_id"] == "S-FLY")
        self.assertTrue(flight["supplier_confirmed"])
        self.svc.supplier_callback("cb-2", "航司A", "S-FLY", "departed")
        result = self.svc.supplier_callback("cb-3", "航司A", "S-FLY", "arrived")
        self.assertEqual(result["segment_status"], "completed")

    def test_wrong_supplier_rejected(self):
        with self.assertRaises(ValidationError):
            self.svc.supplier_callback("cb-x", "航司B", "S-FLY", "departed")

    def test_invalid_transition_rejected(self):
        svc = support.build_service()
        support.build_itinerary(svc)
        # 草稿状态不接受 departed
        with self.assertRaises(ValidationError):
            svc.supplier_callback("cb-1", "航司A", "S-FLY", "departed")
        svc.confirm_itinerary("IT1", by_member="L1")
        svc.supplier_callback("cb-2", "航司A", "S-FLY", "arrived")
        # 已完成是终态，不再接受任何事件
        with self.assertRaises(ValidationError):
            svc.supplier_callback("cb-3", "航司A", "S-FLY", "departed")
        with self.assertRaises(ValidationError):
            svc.supplier_callback("cb-4", "航司A", "S-FLY", "cancelled")

    def test_unknown_event_rejected(self):
        with self.assertRaises(ValidationError):
            self.svc.supplier_callback("cb-1", "航司A", "S-FLY", "delayed")


if __name__ == "__main__":
    unittest.main()
