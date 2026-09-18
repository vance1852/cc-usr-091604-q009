"""球队出行编排系统的行为测试。"""

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from app.travel import (
    BookingConflictError, AccessDeniedError, ApprovalStateError, DocumentError,
    ChangeOp, Match, Person, ProposalStatus,
    ReportDeadlineError, Room, SegmentCompletedError, SegmentKind, SegmentSpec,
    SegmentStatus, TrainingSlot, TransportOption, TravelError, TravelService,
    Vehicle, Role,
)

SH = timezone(timedelta(hours= 8))   # 上海
XJ = timezone(timedelta(hours= 6))   # 客场（乌鲁木齐时区）

D0 = date(2026, 10, 9)
D1 = date(2026, 10, 10)
D2 = date(2026, 10, 11)
D3 = date(2026, 10, 12)


def dt(d, h, m, tz):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=tz)


def build_world(with_bad_doc=True):
    """构造一个完整客场场景：7 人随行，上海飞客场，跨夜到达。"""
    svc = TravelService()
    people = [
        Person("p1", "张峰", Role.PLAYER, injured=True,
               doc_expiry=date(2027, 1, 1),
               emergency_contact="张父 13800000001", medical_notes="左膝扭伤，需冰敷"),
        Person("p2", "李昂", Role.PLAYER,
               doc_expiry=date(2026, 9, 1) if with_bad_doc else date(2027, 1, 1),
               emergency_contact="李母 13800000002"),
        Person("p3", "王磊", Role.PLAYER, doc_expiry=date(2027, 1, 1),
               emergency_contact="王妻 13800000003"),
        Person("p4", "赵海", Role.PLAYER, doc_expiry=date(2027, 1, 1)),
        Person("p5", "陈涛", Role.PLAYER, doc_expiry=date(2027, 1, 1)),
        Person("p6", "刘越", Role.PLAYER, doc_expiry=date(2027, 1, 1)),
        Person("s1", "周教练", Role.STAFF, doc_expiry=date(2027, 1, 1)),
        Person("m1", "孙队医", Role.MEDICAL, doc_expiry=date(2027, 1, 1)),
        Person("g1", "钱领队", Role.MANAGER, doc_expiry=date(2027, 1, 1)),
        Person("o1", "吴运营", Role.OPS, doc_expiry=date(2027, 1, 1)),
    ]
    for p in people:
        svc.add_person(p)

    # 航班：22:00 上海起飞，次日 01:30 客场落地（跨夜且跨时区）
    svc.add_transport(TransportOption(
        "F1", "东方航空", "flight", "上海", "乌鲁木齐",
        dt(D0, 22, 0, SH), dt(D1, 1, 30, XJ), capacity=10, cost=7000))
    # 改签备选：更贵的新航班
    svc.add_transport(TransportOption(
        "F2", "南方航空", "flight", "上海", "乌鲁木齐",
        dt(D0, 20, 30, SH), dt(D1, 0, 15, XJ), capacity=10, cost=8500))
    # 小容量航班，用于并发抢占测试
    svc.add_transport(TransportOption(
        "FX", "临时航空", "flight", "上海", "乌鲁木齐",
        dt(D0, 21, 0, SH), dt(D1, 0, 45, XJ), capacity=2, cost=9000))
    # 晚点航班：落地晚于最晚报到
    svc.add_transport(TransportOption(
        "FL", "延误航空", "flight", "上海", "乌鲁木齐",
        dt(D1, 16, 0, XJ), dt(D1, 19, 30, XJ), capacity=10, cost=4000))

    svc.add_vehicle(Vehicle("B1", "客场客运", "大巴1号", capacity=20, cost=1200))
    svc.add_room(Room("R1", "客场大酒店", "单人房", 1, cost=400))   # 伤员
    svc.add_room(Room("R2", "客场大酒店", "双人房", 2, cost=500))
    svc.add_room(Room("R3", "客场大酒店", "双人房", 2, cost=500))
    svc.add_room(Room("R4", "客场大酒店", "单人房", 1, cost=400))
    svc.add_room(Room("RS", "客场大酒店", "单人房", 1, cost=900))
    svc.add_training_slot(TrainingSlot(
        "T1", "奥体训练场", dt(D2, 10, 0, XJ), dt(D2, 12, 0, XJ),
        capacity=20, cost=800))
    svc.add_match(Match(
        "M1", "雪豹队", "乌鲁木齐", "奥体中心",
        kickoff=dt(D3, 19, 0, XJ), report_deadline=dt(D1, 18, 0, XJ)))

    members = ["p1", "p2", "p3", "p4", "p5", "p6", "s1"]
    trip = svc.create_trip("雪豹客场", "M1", members, trip_id="T1")

    seg_flight = svc.add_segment(
        "T1", SegmentKind.TRANSPORT, "F1", members)
    svc.add_segment("T1", SegmentKind.VEHICLE, "B1", members,
                    start=dt(D1, 2, 0, XJ), end=dt(D1, 3, 0, XJ),
                    detail="机场->酒店")
    svc.add_segment("T1", SegmentKind.HOTEL, "R1", ["p1"], night=D1)
    svc.add_segment("T1", SegmentKind.HOTEL, "R1", ["p1"], night=D2)
    svc.add_segment("T1", SegmentKind.HOTEL, "R2", ["p3", "p4"], night=D1)
    svc.add_segment("T1", SegmentKind.HOTEL, "R2", ["p3", "p4"], night=D2)
    svc.add_segment("T1", SegmentKind.HOTEL, "R3", ["p5", "p6"], night=D1)
    svc.add_segment("T1", SegmentKind.HOTEL, "R3", ["p5", "p6"], night=D2)
    svc.add_segment("T1", SegmentKind.HOTEL, "R4", ["s1"], night=D1)
    svc.add_segment("T1", SegmentKind.HOTEL, "R4", ["s1"], night=D2)
    svc.add_segment("T1", SegmentKind.TRAINING, "T1", members)
    return svc, trip, seg_flight


def confirm_world():
    svc, trip, flight = build_world(with_bad_doc=False)
    svc.confirm_itinerary("T1")
    return svc, trip, flight


class RosterAndConfirmTest(unittest.TestCase):

    def test_expired_document_blocks_confirmation(self):
        svc, _, _ = build_world(with_bad_doc=True)
        with self.assertRaises(DocumentError) as cm:
            svc.confirm_itinerary("T1")
        self.assertIn("李昂", str(cm.exception))
        # 补好证件后可确认
        svc.persons["p2"].doc_expiry = date(2027, 1, 1)
        trip = svc.confirm_itinerary("T1")
        self.assertEqual(trip.status, "confirmed")
        self.assertTrue(all(
            s.status == SegmentStatus.CONFIRMED
            for s in svc.trip_segments("T1")))

    def test_late_arrival_violates_report_deadline(self):
        svc, trip, flight = build_world(with_bad_doc=False)
        # 把 p2 改签到落地 19:30 的晚班机（草稿阶段直接调整）
        flight.member_ids.remove("p2")
        svc.add_segment("T1", SegmentKind.TRANSPORT, "FL", ["p2"])
        with self.assertRaises(ReportDeadlineError) as cm:
            svc.confirm_itinerary("T1")
        self.assertIn("李昂", str(cm.exception))
        self.assertIn("最晚报到", str(cm.exception))

    def test_missing_arrival_violates_deadline(self):
        svc, _, flight = build_world(with_bad_doc=False)
        flight.member_ids.remove("p2")  # p2 没有任何抵达班次
        with self.assertRaises(ReportDeadlineError):
            svc.confirm_itinerary("T1")

    def test_timezone_overnight_arrival(self):
        svc, _, flight = build_world(with_bad_doc=False)
        # 起飞 UTC 14:00（D0），落地 UTC 19:30（D0），但当地日历已跨到 D1
        self.assertEqual(flight.start.astimezone(timezone.utc).date(), D0)
        self.assertEqual(flight.end.astimezone(XJ).date(), D1)
        self.assertLess(flight.start.astimezone(timezone.utc),
                        flight.end.astimezone(timezone.utc))

    def test_naive_datetime_rejected(self):
        svc = TravelService()
        with self.assertRaises(TravelError):
            svc.add_transport(TransportOption(
                "X", "v", "flight", "a", "b",
                datetime(2026, 10, 9, 10, 0), datetime(2026, 10, 9, 12, 0),
                capacity=1, cost=1))

    def test_vehicle_duty_across_midnight_allowed(self):
        svc, _, _ = confirm_world()
        seg = svc.add_segment("T1", SegmentKind.VEHICLE, "B1", ["p5", "p6"],
                              start=dt(D2, 23, 30, XJ),
                              end=dt(D3, 0, 30, XJ), detail="夜训接送")
        self.assertEqual(seg.end.date(), D3)
        # 时间倒挂（UTC 比较）必须拒绝
        with self.assertRaises(TravelError):
            svc.add_segment("T1", SegmentKind.VEHICLE, "B1", ["p5"],
                            start=dt(D3, 1, 0, XJ), end=dt(D2, 23, 0, XJ))


class SplitSegmentTest(unittest.TestCase):

    def test_split_draft_segment(self):
        svc, _, flight = build_world(with_bad_doc=False)
        members = flight.member_ids
        new = svc.split_segment(flight.id, [
            SegmentSpec(SegmentKind.TRANSPORT, "F1", ["p1"]),
            SegmentSpec(SegmentKind.TRANSPORT, "F1",
                        [m for m in members if m != "p1"]),
        ])
        self.assertEqual(len(new), 2)
        covered = {m for s in new for m in s.member_ids}
        self.assertEqual(covered, set(members))
        self.assertNotIn(flight.id, svc.segments)

    def test_split_must_partition_members(self):
        svc, _, flight = build_world(with_bad_doc=False)
        with self.assertRaises(TravelError):
            svc.split_segment(flight.id, [
                SegmentSpec(SegmentKind.TRANSPORT, "F1", ["p1", "p2"]),
                SegmentSpec(SegmentKind.TRANSPORT, "F1", ["p2", "p3"]),  # p2 重复
            ])

    def test_confirmed_segment_requires_approval_flow(self):
        svc, _, flight = confirm_world()
        with self.assertRaises(ApprovalStateError):
            svc.split_segment(flight.id, [
                SegmentSpec(SegmentKind.TRANSPORT, "F2", ["p1"]),
                SegmentSpec(SegmentKind.TRANSPORT, "F2",
                            ["p2", "p3", "p4", "p5", "p6", "s1"]),
            ])


class ResourceOccupancyTest(unittest.TestCase):

    def test_room_double_booking_same_night(self):
        svc, _, _ = build_world(with_bad_doc=False)
        # R2 当晚已住 p3/p4，再排 p5 即超容
        with self.assertRaises(BookingConflictError):
            svc.add_segment("T1", SegmentKind.HOTEL, "R2", ["p5"], night=D1)
        # 另一个夜不受影响
        svc.add_segment("T1", SegmentKind.HOTEL, "RS", ["p2"], night=D1)

    def test_person_cannot_hold_two_rooms_same_night(self):
        svc, _, _ = build_world(with_bad_doc=False)
        with self.assertRaises(BookingConflictError):
            svc.add_segment("T1", SegmentKind.HOTEL, "RS", ["p3"], night=D1)

    def test_injured_player_must_have_single_room(self):
        svc, _, _ = build_world(with_bad_doc=False)
        with self.assertRaises(BookingConflictError) as cm:
            svc.add_segment("T1", SegmentKind.HOTEL, "R3",
                            ["p1", "p4"], night=D1)
        self.assertIn("单人房", str(cm.exception))

    def test_vehicle_overlapping_duties_double_booking(self):
        svc, _, _ = build_world(with_bad_doc=False)
        with self.assertRaises(BookingConflictError):
            # 与 02:00-03:00 全员大巴重叠且超 20 座
            svc.add_segment("T1", SegmentKind.VEHICLE, "B1", ["p3", "p4"],
                            start=dt(D1, 2, 30, XJ), end=dt(D1, 3, 30, XJ))
        # 不重叠的派车可以
        svc.add_segment("T1", SegmentKind.VEHICLE, "B1", ["p3", "p4"],
                        start=dt(D1, 4, 0, XJ), end=dt(D1, 4, 30, XJ))

    def test_person_schedule_conflict(self):
        svc, _, _ = build_world(with_bad_doc=False)
        with self.assertRaises(BookingConflictError):
            svc.add_vehicle(Vehicle("B2", "客场客运", "大巴2号", 5, 300))
            svc.add_segment("T1", SegmentKind.VEHICLE, "B2", ["p3", "p4"],
                            start=dt(D2, 11, 0, XJ), end=dt(D2, 11, 30, XJ))


class ApprovalFlowTest(unittest.TestCase):

    def test_only_manager_proposes_and_ops_approves(self):
        svc, _, flight = confirm_world()
        with self.assertRaises(AccessDeniedError):
            svc.propose_change("T1", "p1", "我想改", [])
        proposal = svc.propose_change(
            "T1", "g1", "航班取消通知",
            [ChangeOp(action="replace", segment_id=flight.id,
                      new=SegmentSpec(SegmentKind.TRANSPORT, "F2",
                                      flight.member_ids),
                      notify=["全队群", "客场地勤"])])
        self.assertEqual(proposal.status, ProposalStatus.PENDING)

        # 领队自己不能审批
        with self.assertRaises(AccessDeniedError):
            svc.approve_change(proposal.id, "g1")
        # 提案未批准前不生效
        self.assertEqual(svc.get_segment(flight.id).status,
                         SegmentStatus.CONFIRMED)
        svc.approve_change(proposal.id, "o1")
        self.assertEqual(svc.get_segment(flight.id).status,
                         SegmentStatus.CANCELLED)
        new_id = svc.change_history("T1")[0].new_segment_id
        self.assertEqual(svc.get_segment(new_id).resource_id, "F2")
        self.assertEqual(svc.get_segment(new_id).status,
                         SegmentStatus.CONFIRMED)

        # 记录供应商、费用差额、通知对象
        record = svc.change_history("T1")[0]
        self.assertEqual(record.vendor, "东方航空")
        self.assertEqual(record.old_cost, 7000)
        self.assertEqual(record.new_cost, 8500)
        self.assertEqual(record.cost_delta, 1500)
        self.assertEqual(record.notified, ["全队群", "客场地勤"])

    def test_proposal_allowed_before_confirmation(self):
        svc, _, flight = build_world(with_bad_doc=False)
        proposal = svc.propose_change(
            "T1", "g1", "出发前航班取消，提前改签",
            [ChangeOp(action="replace", segment_id=flight.id,
                      new=SegmentSpec(SegmentKind.TRANSPORT, "F2",
                                      flight.member_ids))])
        svc.approve_change(proposal.id, "o1")
        new_id = svc.change_history("T1")[0].new_segment_id
        # 行程尚未确认，新段保持 proposed
        self.assertEqual(svc.get_segment(new_id).status, SegmentStatus.PROPOSED)
        trip = svc.confirm_itinerary("T1")
        self.assertEqual(trip.status, "confirmed")
        self.assertEqual(svc.get_segment(new_id).status, SegmentStatus.CONFIRMED)
        self.assertEqual(svc.get_segment(flight.id).status, SegmentStatus.CANCELLED)

    def test_completed_segment_cannot_be_rewritten(self):
        svc, _, _ = confirm_world()
        bus = next(s for s in svc.trip_segments("T1")
                   if s.kind == SegmentKind.VEHICLE)
        svc.complete_segment(bus.id)
        with self.assertRaises(SegmentCompletedError):
            svc.propose_change(
                "T1", "g1", "想改已完成的接机",
                [ChangeOp(action="replace", segment_id=bus.id,
                          new=SegmentSpec(SegmentKind.VEHICLE, "B1",
                                          ["p1"], start=dt(D1, 5, 0, XJ),
                                          end=dt(D1, 6, 0, XJ)))])

    def test_cancel_op_keeps_cost_record(self):
        svc, _, _ = confirm_world()
        room = next(s for s in svc.trip_segments("T1")
                    if s.kind == SegmentKind.HOTEL and s.resource_id == "R4"
                    and s.night == D2)
        proposal = svc.propose_change(
            "T1", "g1", "教练提前返程",
            [ChangeOp(action="cancel", segment_id=room.id,
                      notify=["周教练"])])
        svc.approve_change(proposal.id, "o1")
        rec = svc.change_history("T1")[0]
        self.assertEqual(rec.action, "cancel")
        self.assertEqual(rec.old_cost, 400)
        self.assertEqual(rec.cost_delta, -400)
        self.assertIsNone(rec.new_segment_id)

    def test_double_approval_rejected(self):
        svc, _, flight = confirm_world()
        p = svc.propose_change(
            "T1", "g1", "改",
            [ChangeOp(action="replace", segment_id=flight.id,
                      new=SegmentSpec(SegmentKind.TRANSPORT, "F2",
                                      flight.member_ids))])
        svc.approve_change(p.id, "o1")
        with self.assertRaises(ApprovalStateError):
            svc.approve_change(p.id, "o1")

    def test_reject_leaves_itinerary_untouched(self):
        svc, _, flight = confirm_world()
        p = svc.propose_change(
            "T1", "g1", "改",
            [ChangeOp(action="cancel", segment_id=flight.id)])
        svc.reject_change(p.id, "o1", reason="预算不足")
        self.assertEqual(svc.get_segment(flight.id).status,
                         SegmentStatus.CONFIRMED)
        self.assertEqual(p.status, ProposalStatus.REJECTED)
        self.assertEqual(svc.change_history("T1"), [])


class ConcurrentRebookingTest(unittest.TestCase):

    def _two_group_trip(self):
        svc = TravelService()
        for pid, name, role in [("a", "甲", Role.PLAYER), ("b", "乙", Role.PLAYER),
                                ("c", "丙", Role.PLAYER), ("d", "丁", Role.PLAYER),
                                ("g1", "领队", Role.MANAGER),
                                ("o1", "运营", Role.OPS)]:
            svc.add_person(Person(pid, name, role, doc_expiry=date(2027, 1, 1)))
        svc.add_transport(TransportOption(
            "FA", "航司", "flight", "上海", "乌鲁木齐",
            dt(D0, 22, 0, SH), dt(D1, 1, 0, XJ), 4, 4000))
        svc.add_transport(TransportOption(
            "FX", "小航司", "flight", "上海", "乌鲁木齐",
            dt(D0, 21, 0, SH), dt(D1, 0, 45, XJ), 2, 9000))
        svc.add_match(Match("M1", "对手", "乌鲁木齐", "球场",
                            dt(D3, 19, 0, XJ), dt(D1, 18, 0, XJ)))
        svc.create_trip("并发客场", "M1", ["a", "b", "c", "d"], trip_id="T")
        ab = svc.add_segment("T", SegmentKind.TRANSPORT, "FA", ["a", "b"])
        cd = svc.add_segment("T", SegmentKind.TRANSPORT, "FA", ["c", "d"])
        svc.confirm_itinerary("T")
        return svc, ab, cd

    def test_concurrent_proposals_for_capacity_two_flight(self):
        svc, ab, cd = self._two_group_trip()
        p1 = svc.propose_change(
            "T", "g1", "AB 改签小航班",
            [ChangeOp("replace", ab.id,
                      new=SegmentSpec(SegmentKind.TRANSPORT, "FX", ["a", "b"]))])
        p2 = svc.propose_change(
            "T", "g1", "CD 改签小航班",
            [ChangeOp("replace", cd.id,
                      new=SegmentSpec(SegmentKind.TRANSPORT, "FX", ["c", "d"]))])

        outcomes = {}

        def approve(pid):
            try:
                svc.approve_change(pid, "o1")
                outcomes[pid] = "ok"
            except TravelError as exc:
                outcomes[pid] = type(exc).__name__

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(approve, [p1.id, p2.id]))

        self.assertEqual(sorted(outcomes.values()),
                         sorted(["ok", "BookingConflictError"]))
        # FX 上最终只能有一组人（2 座）
        fx_members = {m for s in svc.trip_segments("T")
                      if s.resource_id == "FX"
                      and s.status == SegmentStatus.CONFIRMED
                      for m in s.member_ids}
        self.assertEqual(len(fx_members), 2)
        # 失败的审批单保持 pending，行程未被破坏
        self.assertEqual(svc.audit_consistency()["ok"], True)


class WithdrawalTest(unittest.TestCase):

    def test_partial_withdrawal_releases_only_own_seats(self):
        svc, _, _ = confirm_world()
        result = svc.withdraw_person(
            "T1", "p3", "g1", notify=["王磊家属", "酒店前台"], reason="家中急事")
        self.assertIn("p3", svc.trips["T1"].withdrawn_ids)
        # 合住房仍由 p4 占用，未被整体取消
        r2_nights = [s for s in svc.trip_segments("T1")
                     if s.resource_id == "R2"]
        self.assertTrue(r2_nights)
        self.assertTrue(all(s.status == SegmentStatus.CONFIRMED for s in r2_nights))
        self.assertTrue(all("p3" not in s.member_ids for s in r2_nights))
        self.assertTrue(all("p4" in s.member_ids for s in r2_nights))
        # 其他人的资源完全不动
        r1 = [s for s in svc.trip_segments("T1") if s.resource_id == "R1"]
        self.assertTrue(all("p1" in s.member_ids for s in r1))
        self.assertEqual(len(result["released_segments"]),
                         5)  # 航班 + 大巴 + 两晚房 + 训练

        rec = next(r for r in svc.change_history("T1")
                   if "王磊" in r.reason and r.action == "withdraw-partial")
        self.assertEqual(rec.notified, ["王磊家属", "酒店前台"])

    def test_last_person_cancels_resource(self):
        svc, _, _ = confirm_world()
        # s1 独占单人房 R4，退出后两晚房间全部释放
        svc.withdraw_person("T1", "s1", "g1")
        r4 = [s for s in svc.trip_segments("T1") if s.resource_id == "R4"]
        self.assertTrue(all(s.status == SegmentStatus.CANCELLED for s in r4))
        self.assertTrue(any(r.action == "withdraw"
                            for r in svc.change_history("T1")))

    def test_withdrawal_relieves_deadline_requirement(self):
        svc, _, flight = build_world(with_bad_doc=False)
        flight.member_ids.remove("p2")  # p2 没有抵达班次
        with self.assertRaises(ReportDeadlineError):
            svc.confirm_itinerary("T1")
        svc.withdraw_person("T1", "p2", "g1")
        svc.confirm_itinerary("T1")  # 退出后不再要求 p2 报到

    def test_withdrawal_does_not_touch_completed_segments(self):
        svc, _, _ = confirm_world()
        bus = next(s for s in svc.trip_segments("T1")
                   if s.kind == SegmentKind.VEHICLE and "p3" in s.member_ids)
        svc.complete_segment(bus.id)
        svc.withdraw_person("T1", "p3", "g1")
        self.assertIn("p3", bus.member_ids)  # 已完成段保留，不重写


class PrivacyViewTest(unittest.TestCase):

    def test_self_view_and_cross_member_denied(self):
        svc, _, _ = confirm_world()
        mine = svc.member_view("p3", "T1")
        self.assertEqual(mine["person"]["emergency_contact"], "王妻 13800000003")
        self.assertNotIn("medical_notes", mine["person"])  # 本人无医疗备注
        with self.assertRaises(AccessDeniedError):
            svc.member_view("p3", "T1", target_id="p4")

    def test_medical_sees_notes_manager_sees_contacts(self):
        svc, _, _ = confirm_world()
        med = svc.member_view("m1", "T1", target_id="p1")
        self.assertEqual(med["person"]["medical_notes"], "左膝扭伤，需冰敷")
        self.assertIn("emergency_contact", med["person"])

        mgr = svc.list_roster("g1", "T1")
        p1_row = next(r for r in mgr if r["id"] == "p1")
        self.assertIn("emergency_contact", p1_row)
        self.assertNotIn("medical_notes", p1_row)  # 领队不看病历

        player_roster = svc.list_roster("p4", "T1")
        self.assertTrue(all("emergency_contact" not in r for r in player_roster))
        self.assertTrue(all("medical_notes" not in r for r in player_roster))

    def test_withdrawn_status_visible_in_view(self):
        svc, _, _ = confirm_world()
        svc.withdraw_person("T1", "p3", "g1")
        view = svc.member_view("g1", "T1", target_id="p3")
        self.assertEqual(view["person"]["status"], "withdrawn")
        # 取消的段不再出现在成员视图
        self.assertTrue(all(s["status"] != SegmentStatus.CANCELLED
                            for s in view["segments"]))

    def test_member_view_shows_local_and_utc_times(self):
        svc, _, _ = confirm_world()
        view = svc.member_view("p1", "T1")
        flight_item = next(s for s in view["segments"]
                           if s["kind"] == SegmentKind.TRANSPORT)
        self.assertIn("+06:00", flight_item["end_local"])
        self.assertIn("+00:00", flight_item["end_utc"])


class CostAndVendorTest(unittest.TestCase):

    def test_cost_summary_and_refund_callback(self):
        svc, _, flight = confirm_world()
        # 供应商取消 F1 → 领队改签 F2 → 运营批准
        r1 = svc.vendor_callback("evt-1", "F1", "cancelled",
                                 message="航班取消")
        self.assertTrue(r1["applied"])
        proposal = svc.propose_change(
            "T1", "g1", "航班取消",
            [ChangeOp("replace", flight.id,
                      new=SegmentSpec(SegmentKind.TRANSPORT, "F2",
                                      flight.member_ids),
                      notify=["全队群"])])
        svc.approve_change(proposal.id, "o1")

        summary = svc.cost_summary("T1")
        # 旧航班 7000 仍计入已发生费用（等待报销），新航班 8500 已落位；
        # 7000+8500 + 大巴1200 + 房费3600 + 训练场800 = 21100
        self.assertEqual(summary["gross_incurred"], 21100.0)
        self.assertEqual(summary["rebooking_delta"], 1500.0)
        self.assertEqual(summary["by_vendor"]["东方航空"]["cancelled_cost"], 7000.0)

        # 供应商就旧航班退款 6000（回调挂在变更记录上）
        rec_id = next(r.id for r in svc.change_history("T1")
                      if r.action == "replace")
        svc.vendor_callback("refund-1", rec_id, "refund", amount=6000)
        summary = svc.cost_summary("T1")
        self.assertEqual(summary["refunds"], 6000.0)
        self.assertEqual(summary["net_payable"], 15100.0)
        self.assertIn(rec_id, summary["reimbursable_records"])

    def test_duplicate_vendor_callback_is_idempotent(self):
        svc, _, flight = confirm_world()
        proposal = svc.propose_change(
            "T1", "g1", "取消",
            [ChangeOp("replace", flight.id,
                      new=SegmentSpec(SegmentKind.TRANSPORT, "F2",
                                      flight.member_ids))])
        svc.approve_change(proposal.id, "o1")
        rec_id = svc.change_history("T1")[0].id

        first = svc.vendor_callback("dup-key", rec_id, "refund", amount=6000)
        second = svc.vendor_callback("dup-key", rec_id, "refund", amount=6000)
        self.assertNotIn("duplicate", first)
        self.assertTrue(second["duplicate"])
        # 退款只入账一次
        record = next(r for r in svc.records if r.id == rec_id)
        self.assertEqual(record.refunds, [6000])

        # 取消回调重复发送：班次保持取消，不产生重复效果
        svc.vendor_callback("cancel-key", "F1", "cancelled")
        svc.vendor_callback("cancel-key", "F1", "cancelled")
        self.assertTrue(svc.transports["F1"].cancelled)
        with self.assertRaises(BookingConflictError):
            # 已取消班次不能再被新段引用
            svc.add_segment("T1", SegmentKind.TRANSPORT, "F1", ["p5"])


class SnapshotRestoreTest(unittest.TestCase):

    def _path(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        return path

    def test_restore_preserves_state_and_consistency(self):
        svc, _, flight = confirm_world()
        svc.vendor_callback("k1", "F1", "cancelled")
        proposal = svc.propose_change(
            "T1", "g1", "航班取消",
            [ChangeOp("replace", flight.id,
                      new=SegmentSpec(SegmentKind.TRANSPORT, "F2",
                                      flight.member_ids),
                      notify=["全队群"])])
        svc.approve_change(proposal.id, "o1")
        rec_id = svc.change_history("T1")[0].id
        svc.vendor_callback("k2", rec_id, "refund", amount=6000)
        svc.withdraw_person("T1", "p3", "g1")
        before = svc.cost_summary("T1")

        path = self._path()
        try:
            svc.save(path)
            restored = TravelService.restore(path)
            self.assertEqual(restored.audit_consistency()["ok"], True)
            self.assertEqual(restored.cost_summary("T1"), before)
            # 审批单、变更记录、供应商回调幂等键全部保留
            self.assertEqual(restored.proposals[proposal.id].status,
                             ProposalStatus.APPROVED)
            self.assertEqual(len(restored.change_history("T1")),
                             len(svc.change_history("T1")))
            dup = restored.vendor_callback("k2", rec_id, "refund", amount=6000)
            self.assertTrue(dup["duplicate"])
            self.assertEqual(
                next(r for r in restored.records if r.id == rec_id).refunds,
                [6000])
            # 恢复后仍可继续改签
            bus = next(s for s in restored.trip_segments("T1")
                       if s.kind == SegmentKind.VEHICLE)
            p2 = restored.propose_change(
                "T1", "g1", "重启后改签",
                [ChangeOp("replace", bus.id,
                          new=SegmentSpec(SegmentKind.VEHICLE, "B1",
                                          ["p1", "p2"],
                                          start=dt(D1, 4, 0, XJ),
                                          end=dt(D1, 4, 30, XJ)))])
            restored.approve_change(p2.id, "o1")
            self.assertEqual(restored.audit_consistency()["ok"], True)
        finally:
            os.unlink(path)

    def test_restore_detects_no_drift_on_clean_state(self):
        svc, _, _ = confirm_world()
        path = self._path()
        try:
            svc.save(path)
            restored = TravelService.restore(path)
            report = restored.audit_consistency()
            self.assertTrue(report["ok"])
            self.assertEqual(report["problems"], [])
        finally:
            os.unlink(path)


class HealthTest(unittest.TestCase):

    def test_health_and_legacy_value_object(self):
        from app.travel import TripSegment
        self.assertEqual(TravelService().health(),
                         {"service": "travel", "status": "ok"})
        self.assertEqual(TripSegment("上海", "成都").destination, "成都")


if __name__ == "__main__":
    unittest.main()
