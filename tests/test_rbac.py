import unittest

import support
from app.errors import NotAuthorizedError


class RbacTest(unittest.TestCase):
    def setUp(self):
        self.svc = support.build_service()

    def test_player_cannot_see_teammate_protected_fields(self):
        profile = self.svc.member_profile("P2", viewer_id="P1")
        self.assertIsNone(profile["medical_notes"])
        self.assertIsNone(profile["emergency_contact"])
        self.assertIsNone(profile["requires_single_room"])
        self.assertIsNone(profile["documents"][0]["number"])
        # 但能看到基础信息与证件有效期状态
        self.assertEqual(profile["name"], "球员2")
        self.assertTrue(profile["documents"][0]["valid"])

    def test_medic_sees_medical_notes(self):
        profile = self.svc.member_profile("P2", viewer_id="M1")
        self.assertIn("左膝", profile["medical_notes"])
        self.assertIsNotNone(profile["emergency_contact"])
        self.assertTrue(profile["requires_single_room"])

    def test_leader_sees_emergency_contact_but_not_medical(self):
        profile = self.svc.member_profile("P2", viewer_id="L1")
        self.assertEqual(profile["emergency_contact"]["name"], "家属P2")
        self.assertIsNone(profile["medical_notes"])

    def test_self_can_see_own_protected_fields(self):
        profile = self.svc.member_profile("P2", viewer_id="P2")
        self.assertIn("左膝", profile["medical_notes"])
        self.assertEqual(profile["emergency_contact"]["phone"], "13800000005")

    def test_roster_view_filtered_per_viewer(self):
        roster = self.svc.roster(viewer_id="P1")
        self.assertEqual(len(roster), 10)
        # 普通球员看不到任何队友的医疗备注与紧急联系人（自己的除外）
        teammates = [p for p in roster if p["member_id"] != "P1"]
        self.assertFalse(any(p["medical_notes"] for p in teammates))
        self.assertFalse(any(p["emergency_contact"] for p in teammates))
        roster = self.svc.roster(viewer_id="M1")
        p2 = next(p for p in roster if p["member_id"] == "P2")
        self.assertIn("左膝", p2["medical_notes"])

    def test_medical_notes_write_requires_medic(self):
        with self.assertRaises(NotAuthorizedError):
            self.svc.set_medical_notes("P1", "扭伤", by_member="L1")
        with self.assertRaises(NotAuthorizedError):
            self.svc.set_medical_notes("P1", "扭伤", by_member="O1")
        self.svc.set_medical_notes("P1", "轻微扭伤", by_member="M1")
        self.assertIn("扭伤", self.svc.member_profile("P1", viewer_id="M1")["medical_notes"])

    def test_single_room_flag_requires_medic(self):
        with self.assertRaises(NotAuthorizedError):
            self.svc.set_single_room_requirement("P3", True, by_member="O1")
        self.svc.set_single_room_requirement("P3", True, by_member="M1")
        self.assertTrue(self.svc.state.members["P3"].requires_single_room)

    def test_emergency_contact_write_requires_ops_or_leader(self):
        with self.assertRaises(NotAuthorizedError):
            self.svc.set_emergency_contact("P1", "某人", "139", by_member="M1")
        self.svc.set_emergency_contact("P1", "某人", "139", by_member="L1")
        self.assertEqual(
            self.svc.member_profile("P1", viewer_id="L1")["emergency_contact"]["phone"],
            "139",
        )


if __name__ == "__main__":
    unittest.main()
