"""测试共用的场景构造。"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.service import TravelService

SH = ZoneInfo("Asia/Shanghai")
LDN = ZoneInfo("Europe/London")

ALL_MEMBERS = ["L1", "O1", "M1", "C1", "P1", "P2", "P3", "P4", "P5", "P6"]


def at(day, hour, minute, tz, month=10, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=tz)


def build_service(now=None, storage_path=None):
    """搭建标准客场场景：10 名成员、车辆、房间、班次、训练场与比赛。"""
    svc = TravelService(storage_path=storage_path, now=now)
    svc.add_member("李领队", "leader", member_id="L1")
    svc.add_member("王运营", "ops", member_id="O1")
    svc.add_member("张队医", "medic", member_id="M1")
    svc.add_member("陈教练", "coach", member_id="C1")
    for i in range(1, 7):
        svc.add_member(f"球员{i}", "player", member_id=f"P{i}", squad_number=i)
    for index, mid in enumerate(ALL_MEMBERS):
        svc.add_document(mid, "passport", f"E{mid}9988", date(2027, 5, 1))
        svc.set_emergency_contact(
            mid, name=f"家属{mid}", phone=f"1380000{index:04d}", relation="家属", by_member="O1"
        )
    svc.set_medical_notes("P2", "左膝术后恢复期，避免连续高强度训练", by_member="M1")
    svc.set_single_room_requirement("P2", True, by_member="M1")
    # 车辆与房间
    svc.add_vehicle("大巴", 6, "车队A", vehicle_id="V1")
    svc.add_vehicle("中巴", 6, "车队A", vehicle_id="V2")
    svc.add_room("酒店H", "single", 1, "酒店H", room_id="R1")
    svc.add_room("酒店H", "single", 1, "酒店H", room_id="R2")
    for i in range(3, 8):
        svc.add_room("酒店H", "twin", 2, "酒店H", room_id=f"R{i}")
    # 交通班次
    svc.add_transport_option(
        "flight", "航司A", "CA851", "上海浦东", "伦敦希思罗",
        at(1, 13, 0, SH), at(1, 17, 30, LDN), 30, 28000, option_id="F1",
    )
    svc.add_transport_option(
        "flight", "航司B", "BA168", "上海浦东", "伦敦希思罗",
        at(1, 15, 30, SH), at(1, 20, 5, LDN), 30, 31500, option_id="F2",
    )
    svc.add_transport_option(
        "flight", "航司C", "MU551", "上海浦东", "伦敦希思罗",
        at(2, 1, 30, SH), at(2, 13, 5, LDN), 30, 26000, option_id="F3",
    )
    # 训练场与比赛
    svc.add_training_slot(
        "客场训练基地", at(2, 10, 0, LDN), at(2, 11, 30, LDN), "基地运营方",
        cost=1500, slot_id="T1",
    )
    svc.add_match(
        "蓝桥FC", "客场球场", at(2, 19, 45, LDN), at(2, 12, 0, LDN), match_id="M1"
    )
    return svc


def build_itinerary(svc, members=None):
    """生成草稿行程：航班 + 大巴 + 酒店 + 训练，并完成车辆/房间分配。"""
    members = list(members or ALL_MEMBERS)
    svc.create_itinerary("客场征程", match_id="M1", itinerary_id="IT1")
    svc.add_segment_from_option("IT1", "F1", members, segment_id="S-FLY")
    svc.add_segment(
        "IT1",
        {
            "kind": "ground",
            "supplier": "车队A",
            "origin": "希思罗机场",
            "destination": "酒店H",
            "starts_at": at(1, 18, 0, LDN),
            "ends_at": at(1, 19, 0, LDN),
            "cost": 800,
            "member_ids": members,
        },
        segment_id="S-BUS",
    )
    svc.add_segment(
        "IT1",
        {
            "kind": "hotel",
            "supplier": "酒店H",
            "origin": None,
            "destination": "酒店H",
            "starts_at": at(1, 19, 0, LDN),
            "ends_at": at(3, 11, 0, LDN),
            "cost": 12000,
            "member_ids": members,
        },
        segment_id="S-HOTEL",
    )
    svc.add_segment(
        "IT1",
        {
            "kind": "training",
            "supplier": "基地运营方",
            "origin": None,
            "destination": "客场训练基地",
            "starts_at": at(2, 10, 0, LDN),
            "ends_at": at(2, 11, 30, LDN),
            "cost": 1500,
            "member_ids": members,
            "slot_id": "T1",
        },
        segment_id="S-TRN",
    )
    svc.auto_assign_vehicles("S-BUS")
    svc.auto_assign_rooms("S-HOTEL")
    return {
        "itinerary_id": "IT1",
        "flight": "S-FLY",
        "ground": "S-BUS",
        "hotel": "S-HOTEL",
        "training": "S-TRN",
    }


def build_confirmed(svc, members=None):
    ids = build_itinerary(svc, members)
    svc.confirm_itinerary("IT1", by_member="L1")
    return ids


def replacement_flight_draft(members=None):
    """航司B 的替换航班规格（F2）。"""
    return {
        "kind": "flight",
        "supplier": "航司B",
        "origin": "上海浦东",
        "destination": "伦敦希思罗",
        "starts_at": at(1, 15, 30, SH),
        "ends_at": at(1, 20, 5, LDN),
        "cost": 31500,
        "option_id": "F2",
        "member_ids": list(members or []),
    }
