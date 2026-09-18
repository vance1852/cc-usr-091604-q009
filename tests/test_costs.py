import unittest

import support


class CostSummaryTest(unittest.TestCase):
    def setUp(self):
        self.svc = support.build_service()
        support.build_confirmed(self.svc)

    def test_booked_totals_after_confirm(self):
        summary = self.svc.cost_summary("IT1")
        cny = summary["currencies"]["CNY"]
        self.assertEqual(cny["booked"], "42300.00")
        self.assertEqual(cny["retained_reimbursable"], "0.00")
        self.assertEqual(cny["by_supplier"]["航司A"], "28000.00")
        self.assertEqual(cny["by_supplier"]["酒店H"], "12000.00")
        self.assertEqual(summary["entry_count"], 4)

    def test_replace_keeps_incurred_cost_for_reimbursement(self):
        proposal_id = self.svc.propose_change(
            "IT1",
            proposer_id="L1",
            reason="航班取消改签",
            actions=[
                {
                    "type": "replace",
                    "segment_id": "S-FLY",
                    "replacement": support.replacement_flight_draft(),
                    "notify": ["L1"],
                }
            ],
        )
        self.svc.approve_change(proposal_id, by_member="O1")
        summary = self.svc.cost_summary("IT1")
        cny = summary["currencies"]["CNY"]
        # 原航班 28000 已发生，保留待报销；新航班 31500 另行入账
        self.assertEqual(cny["booked"], "73800.00")
        self.assertEqual(cny["retained_reimbursable"], "28000.00")
        self.assertEqual(cny["by_supplier"]["航司B"], "31500.00")

    def test_cancel_adds_to_retained(self):
        proposal_id = self.svc.propose_change(
            "IT1",
            proposer_id="L1",
            actions=[{"type": "cancel", "segment_id": "S-TRN", "notify": ["L1"]}],
        )
        self.svc.approve_change(proposal_id, by_member="O1")
        summary = self.svc.cost_summary("IT1")
        self.assertEqual(
            summary["currencies"]["CNY"]["retained_reimbursable"], "1500.00"
        )

    def test_withdraw_does_not_change_costs(self):
        before = self.svc.cost_summary("IT1")
        self.svc.withdraw_members("IT1", ["P3"], by_member="O1")
        self.assertEqual(self.svc.cost_summary("IT1"), before)

    def test_summary_scoped_by_itinerary(self):
        svc = support.build_service()
        support.build_confirmed(svc)
        svc.create_itinerary("空行程", itinerary_id="IT2")
        self.assertEqual(svc.cost_summary("IT2")["entry_count"], 0)
        self.assertEqual(svc.cost_summary()["entry_count"], 4)


if __name__ == "__main__":
    unittest.main()
