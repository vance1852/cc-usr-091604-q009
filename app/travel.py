"""足球队客场出行编排系统。

维护名单与证件、交通班次、车辆、住宿、训练场、比赛日程，
生成可拆分行程段，处理跨时区到达与最晚报到时间；
支持领队提方案、运营审批的改签流程、供应商回调幂等、
费用留痕、角色化隐私保护与快照恢复。

仅依赖 Python 标准库。所有时间比较统一换算为 UTC。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# 常量与异常
# ---------------------------------------------------------------------------

class Role:
    PLAYER = "player"
    STAFF = "staff"
    MEDICAL = "medical"      # 队医
    MANAGER = "manager"      # 领队：提出改签
    OPS = "ops"              # 俱乐部运营：审批改签


class SegmentKind:
    TRANSPORT = "transport"  # 航班/火车等跨城班次
    VEHICLE = "vehicle"      # 当地大巴/车辆班次
    HOTEL = "hotel"          # 酒店房间（按夜）
    TRAINING = "training"    # 训练场时段


class SegmentStatus:
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ProposalStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


ACTIVE_STATUSES = (SegmentStatus.PROPOSED, SegmentStatus.CONFIRMED, SegmentStatus.COMPLETED)


class TravelError(Exception):
    """出行领域错误基类。"""


class BookingConflictError(TravelError):
    """资源或人员在同一时间被重复占用。"""


class DocumentError(TravelError):
    """证件缺失或已过期。"""


class ReportDeadlineError(TravelError):
    """行程无法保证在最晚报到时间前到达。"""


class SegmentCompletedError(TravelError):
    """已完成行程段不可重写。"""


class ApprovalStateError(TravelError):
    """审批单状态不允许该操作。"""


class AccessDeniedError(TravelError):
    """角色无权查看或操作该信息。"""


class VendorCallbackError(TravelError):
    """供应商回调内容无法处理。"""


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise TravelError("时间必须带时区信息")
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 基础值对象与资源目录
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TripSegment:
    """一段行程的起终点（保留给早期调用方的简单值对象）。"""

    origin: str
    destination: str


@dataclass
class Person:
    id: str
    name: str
    role: str
    doc_type: str = "passport"
    doc_expiry: Optional[date] = None
    injured: bool = False
    # 以下字段按角色保护，见 TravelService.member_view / list_roster
    emergency_contact: Optional[str] = None
    medical_notes: Optional[str] = None
    active: bool = True


@dataclass
class TransportOption:
    """航班/车次等跨城交通班次。"""

    id: str
    vendor: str
    mode: str                 # flight / train / ...
    origin: str
    destination: str
    departure: datetime       # 带时区
    arrival: datetime         # 带时区，可能跨日/跨时区
    capacity: int
    cost: float
    cancelled: bool = False


@dataclass
class Vehicle:
    """当地车辆（大巴/商务车），按派车时段占用。"""

    id: str
    vendor: str
    name: str
    capacity: int
    cost: float = 0.0


@dataclass
class Room:
    """酒店房间。capacity=1 为单人房，2 为双人房。"""

    id: str
    hotel: str
    room_type: str
    capacity: int
    cost: float = 0.0


@dataclass
class TrainingSlot:
    """训练场时段。"""

    id: str
    venue: str
    start: datetime
    end: datetime
    capacity: int
    cost: float = 0.0


@dataclass
class Match:
    id: str
    opponent: str
    city: str
    venue: str
    kickoff: datetime
    report_deadline: datetime  # 最晚报到时间（当地，带时区）


@dataclass
class Trip:
    id: str
    name: str
    match_id: str
    member_ids: list[str] = field(default_factory=list)
    withdrawn_ids: list[str] = field(default_factory=list)
    status: str = "draft"      # draft / confirmed


@dataclass
class ItinerarySegment:
    """可拆分的行程段。

    transport/vehicle/training 用 start/end（UTC 比较）；
    hotel 按 night（入住日期）占用。
    """

    id: str
    trip_id: str
    kind: str
    resource_id: str
    member_ids: list[str]
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    night: Optional[date] = None
    vendor: str = ""
    cost: float = 0.0
    status: str = SegmentStatus.PROPOSED
    detail: str = ""              # 如 上海->成都 / 房间号说明
    vendor_cancelled: bool = False  # 供应商通知该班次取消


@dataclass
class SegmentSpec:
    """改签新段的规格（审批通过后才落位）。"""

    kind: str
    resource_id: str
    member_ids: list[str]
    start: Optional[datetime] = None  # vehicle 必须显式给出
    end: Optional[datetime] = None
    night: Optional[date] = None
    cost: Optional[float] = None
    vendor: Optional[str] = None
    detail: str = ""


@dataclass
class ChangeOp:
    action: str                 # cancel / replace
    segment_id: str
    new: Optional[SegmentSpec] = None
    notify: list[str] = field(default_factory=list)


@dataclass
class ChangeProposal:
    id: str
    trip_id: str
    proposer_id: str
    reason: str
    ops: list[ChangeOp]
    status: str = ProposalStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None


@dataclass
class ChangeRecord:
    """取消/替换/退出的不可变审计记录。"""

    id: str
    trip_id: str
    proposal_id: Optional[str]
    action: str                 # cancel / replace / withdraw
    old_segment_id: Optional[str]
    new_segment_id: Optional[str]
    vendor: str
    old_cost: float
    new_cost: float
    cost_delta: float
    reason: str
    notified: list[str]
    actor_id: str
    at: datetime
    refunds: list[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------

def _encode(value):
    if isinstance(value, datetime):
        return {"__type__": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"__type__": "date", "value": value.isoformat()}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


def _decode(value):
    if isinstance(value, dict):
        if value.get("__type__") == "datetime":
            return datetime.fromisoformat(value["value"])
        if value.get("__type__") == "date":
            return date.fromisoformat(value["value"])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# 核心服务
# ---------------------------------------------------------------------------

class TravelService:
    """球队出行编排服务。线程安全，可快照恢复。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.persons: dict[str, Person] = {}
        self.transports: dict[str, TransportOption] = {}
        self.vehicles: dict[str, Vehicle] = {}
        self.rooms: dict[str, Room] = {}
        self.training_slots: dict[str, TrainingSlot] = {}
        self.matches: dict[str, Match] = {}
        self.trips: dict[str, Trip] = {}
        self.segments: dict[str, ItinerarySegment] = {}
        self.proposals: dict[str, ChangeProposal] = {}
        self.records: list[ChangeRecord] = []
        self.callbacks: dict[str, dict] = {}  # idempotency_key -> 结果

    def health(self) -> dict[str, str]:
        return {"service": "travel", "status": "ok"}

    # ---- 目录维护 ---------------------------------------------------------

    def add_person(self, person: Person) -> Person:
        with self._lock:
            self.persons[person.id] = person
            return person

    def add_transport(self, option: TransportOption) -> TransportOption:
        with self._lock:
            _utc(option.departure)
            _utc(option.arrival)
            self.transports[option.id] = option
            return option

    def add_vehicle(self, vehicle: Vehicle) -> Vehicle:
        with self._lock:
            self.vehicles[vehicle.id] = vehicle
            return vehicle

    def add_room(self, room: Room) -> Room:
        with self._lock:
            if room.capacity < 1:
                raise TravelError("房间容量至少为 1")
            self.rooms[room.id] = room
            return room

    def add_training_slot(self, slot: TrainingSlot) -> TrainingSlot:
        with self._lock:
            _utc(slot.start)
            _utc(slot.end)
            self.training_slots[slot.id] = slot
            return slot

    def add_match(self, match: Match) -> Match:
        with self._lock:
            _utc(match.kickoff)
            _utc(match.report_deadline)
            self.matches[match.id] = match
            return match

    def create_trip(self, name: str, match_id: str, member_ids: Iterable[str],
                    trip_id: Optional[str] = None) -> Trip:
        with self._lock:
            if match_id not in self.matches:
                raise TravelError(f"未知比赛 {match_id}")
            members = list(member_ids)
            for pid in members:
                if pid not in self.persons:
                    raise TravelError(f"未知成员 {pid}")
            trip = Trip(id=trip_id or _new_id(), name=name, match_id=match_id,
                        member_ids=list(dict.fromkeys(members)))
            self.trips[trip.id] = trip
            return trip

    # ---- 行程段 -----------------------------------------------------------

    def add_segment(self, trip_id: str, kind: str, resource_id: str,
                    member_ids: Iterable[str], *, night: Optional[date] = None,
                    start: Optional[datetime] = None, end: Optional[datetime] = None,
                    cost: Optional[float] = None, vendor: Optional[str] = None,
                    detail: str = "", segment_id: Optional[str] = None,
                    status: str = SegmentStatus.PROPOSED) -> ItinerarySegment:
        with self._lock:
            spec = SegmentSpec(kind=kind, resource_id=resource_id,
                               member_ids=list(member_ids), night=night,
                               start=start, end=end, cost=cost, vendor=vendor,
                               detail=detail)
            seg = self._build_segment(trip_id, spec, status=status, segment_id=segment_id)
            self._check_occupancy(trip_id, [seg], cancelling=set())
            self.segments[seg.id] = seg
            return seg

    def split_segment(self, segment_id: str, new_specs: list[SegmentSpec]
                      ) -> list[ItinerarySegment]:
        """把草稿行程段拆成多段（成员必须是原成员的一个划分）。

        已确认的段需要改签流程（propose_change + 审批），不能直接拆。
        """
        with self._lock:
            old = self._require_segment(segment_id)
            if old.status == SegmentStatus.COMPLETED:
                raise SegmentCompletedError(f"行程段 {segment_id} 已完成，不可拆分")
            if old.status != SegmentStatus.PROPOSED:
                raise ApprovalStateError(
                    f"行程段 {segment_id} 已确认，请走改签审批流程")
            groups = [set(s.member_ids) for s in new_specs]
            union: set[str] = set().union(*groups) if groups else set()
            if union != set(old.member_ids) or any(
                    sum(1 for g in groups if m in g) > 1
                    for m in union):
                raise TravelError("拆分后的成员必须恰好划分原成员，不重不漏")
            del self.segments[old.id]
            new_all: list[ItinerarySegment] = []
            try:
                for spec in new_specs:
                    new_all.append(self._build_segment(old.trip_id, spec))
                # 一次性校验全部新段（旧段已移除）
                self._check_occupancy(old.trip_id, new_all, cancelling=set())
                for seg in new_all:
                    self.segments[seg.id] = seg
            except Exception:
                # 拆分失败回滚，恢复原段（新段尚未插入）
                for s in new_all:
                    self.segments.pop(s.id, None)
                self.segments[old.id] = old
                raise
            return new_all

    def complete_segment(self, segment_id: str) -> ItinerarySegment:
        with self._lock:
            seg = self._require_segment(segment_id)
            if seg.status == SegmentStatus.CANCELLED:
                raise TravelError("已取消的行程段不能标记完成")
            seg.status = SegmentStatus.COMPLETED
            return seg

    # ---- 确认行程（证件 + 最晚报到）--------------------------------------

    def confirm_itinerary(self, trip_id: str) -> Trip:
        with self._lock:
            trip = self._require_trip(trip_id)
            self._check_documents(trip)
            self._check_report_deadline(trip)
            for seg in self._trip_segments(trip_id):
                if seg.status == SegmentStatus.PROPOSED:
                    seg.status = SegmentStatus.CONFIRMED
            trip.status = "confirmed"
            return trip

    def _check_documents(self, trip: Trip) -> None:
        match = self.matches[trip.match_id]
        bad: list[str] = []
        for pid in self._active_members(trip):
            p = self.persons[pid]
            if p.doc_expiry is None or p.doc_expiry < match.kickoff.date():
                bad.append(f"{p.name}({p.doc_type})")
        if bad:
            raise DocumentError("以下成员证件缺失或在比赛日前过期: " + "、".join(bad))

    def _check_report_deadline(self, trip: Trip,
                               active_segments: Optional[list["ItinerarySegment"]] = None,
                               cancelling: Optional[set[str]] = None) -> None:
        match = self.matches[trip.match_id]
        deadline = _utc(match.report_deadline)
        missing: list[str] = []
        late: list[str] = []
        cancelling = cancelling or set()
        if active_segments is None:
            active_segments = self._trip_segments(trip.id)
        segs = [s for s in active_segments
                if s.status in (SegmentStatus.PROPOSED, SegmentStatus.CONFIRMED)
                and s.id not in cancelling]
        for pid in self._active_members(trip):
            arrivals = [_utc(s.end) for s in segs
                        if s.kind == SegmentKind.TRANSPORT and pid in s.member_ids
                        and self.transports[s.resource_id].destination == match.city]
            if not arrivals:
                missing.append(self.persons[pid].name)
            elif min(arrivals) > deadline:
                late.append(self.persons[pid].name)
        if missing:
            raise ReportDeadlineError(
                "以下成员没有在报到前抵达 " + match.city + " 的班次: " + "、".join(missing))
        if late:
            raise ReportDeadlineError(
                "以下成员最早抵达时间晚于最晚报到时间 "
                + match.report_deadline.isoformat() + ": " + "、".join(late))

    # ---- 改签：领队提案，运营审批 ----------------------------------------

    def propose_change(self, trip_id: str, proposer_id: str, reason: str,
                       ops: list[ChangeOp]) -> ChangeProposal:
        with self._lock:
            trip = self._require_trip(trip_id)
            proposer = self._require_person(proposer_id)
            if proposer.role != Role.MANAGER:
                raise AccessDeniedError("只有领队可以提出改签方案")
            for op in ops:
                seg = self._require_segment(op.segment_id)
                if seg.trip_id != trip_id:
                    raise TravelError("改签段不属于该行程")
                if seg.status == SegmentStatus.COMPLETED:
                    raise SegmentCompletedError(f"行程段 {seg.id} 已完成，不可改签")
                if seg.status == SegmentStatus.CANCELLED:
                    raise ApprovalStateError(f"行程段 {seg.id} 已取消")
                if op.action == "replace":
                    if op.new is None:
                        raise TravelError("replace 必须提供新段规格")
                elif op.action != "cancel":
                    raise TravelError(f"未知改签动作 {op.action}")
            proposal = ChangeProposal(id=_new_id(), trip_id=trip_id,
                                      proposer_id=proposer_id, reason=reason, ops=ops)
            self.proposals[proposal.id] = proposal
            return proposal

    def approve_change(self, proposal_id: str, ops_user_id: str) -> ChangeProposal:
        with self._lock:
            approver = self._require_person(ops_user_id)
            if approver.role != Role.OPS:
                raise AccessDeniedError("只有俱乐部运营可以审批改签")
            proposal = self.proposals.get(proposal_id)
            if proposal is None:
                raise ApprovalStateError(f"未知审批单 {proposal_id}")
            if proposal.status != ProposalStatus.PENDING:
                raise ApprovalStateError(f"审批单已{proposal.status}")

            trip = self._require_trip(proposal.trip_id)
            cancelling = {op.segment_id for op in proposal.ops}
            active_members = set(self._active_members(trip))
            # 二次校验：提案期间状态可能变化（并发/已完成）
            new_segments: list[ItinerarySegment] = []
            for op in proposal.ops:
                old = self._require_segment(op.segment_id)
                if old.status == SegmentStatus.COMPLETED:
                    raise SegmentCompletedError(f"行程段 {old.id} 已完成，不可改签")
                if old.status == SegmentStatus.CANCELLED:
                    raise ApprovalStateError(f"行程段 {old.id} 已被取消")
                if op.action == "replace":
                    assert op.new is not None
                    extra = set(op.new.member_ids) - active_members
                    if extra:
                        raise ApprovalStateError(
                            "新段包含已退出成员: "
                            + "、".join(self.persons[m].name for m in extra))
                    new_status = (SegmentStatus.CONFIRMED if trip.status == "confirmed"
                                  else SegmentStatus.PROPOSED)
                    new_seg = self._build_segment(proposal.trip_id, op.new,
                                                  status=new_status)
                    new_segments.append(new_seg)
            # 资源占用在移除旧段的前提下重新校验，防止并发抢同一资源
            self._check_occupancy(proposal.trip_id, new_segments, cancelling=cancelling)
            # 取消旧段、加入新段后仍须满足最晚报到时间
            projected = [s for s in self._trip_segments(proposal.trip_id)
                         if s.id not in cancelling] + new_segments
            self._check_report_deadline(trip, projected)

            # 落位：旧段取消（费用留痕），新段确认
            spec_iter = iter(new_segments)
            for op in proposal.ops:
                old = self._require_segment(op.segment_id)
                old.status = SegmentStatus.CANCELLED
                new_seg = next(spec_iter) if op.action == "replace" else None
                if new_seg is not None:
                    self.segments[new_seg.id] = new_seg
                delta = (new_seg.cost - old.cost) if new_seg else -old.cost
                record = ChangeRecord(
                    id=_new_id(), trip_id=proposal.trip_id,
                    proposal_id=proposal.id, action=op.action,
                    old_segment_id=old.id,
                    new_segment_id=new_seg.id if new_seg else None,
                    vendor=old.vendor or (new_seg.vendor if new_seg else ""),
                    old_cost=old.cost, new_cost=new_seg.cost if new_seg else 0.0,
                    cost_delta=delta, reason=proposal.reason,
                    notified=list(op.notify), actor_id=ops_user_id,
                    at=datetime.now(timezone.utc))
                self.records.append(record)

            proposal.status = ProposalStatus.APPROVED
            proposal.decided_by = ops_user_id
            proposal.decided_at = datetime.now(timezone.utc)
            return proposal

    def reject_change(self, proposal_id: str, ops_user_id: str,
                      reason: str = "") -> ChangeProposal:
        with self._lock:
            approver = self._require_person(ops_user_id)
            if approver.role != Role.OPS:
                raise AccessDeniedError("只有俱乐部运营可以审批改签")
            proposal = self.proposals.get(proposal_id)
            if proposal is None or proposal.status != ProposalStatus.PENDING:
                raise ApprovalStateError("审批单不存在或已处理")
            proposal.status = ProposalStatus.REJECTED
            proposal.decided_by = ops_user_id
            proposal.decided_at = datetime.now(timezone.utc)
            proposal.reason = (proposal.reason + f" | 驳回: {reason}").strip(" |")
            return proposal

    # ---- 成员临时退出：只释放本人资源 ------------------------------------

    def withdraw_person(self, trip_id: str, person_id: str, actor_id: str,
                        notify: Optional[list[str]] = None,
                        reason: str = "临时退出") -> dict:
        with self._lock:
            trip = self._require_trip(trip_id)
            self._require_person(person_id)
            if person_id not in trip.member_ids:
                raise TravelError("该成员不在行程名单中")
            notify = list(notify or [])
            released: list[str] = []
            for seg in self._trip_segments(trip_id):
                if person_id not in seg.member_ids:
                    continue
                if seg.status in (SegmentStatus.COMPLETED, SegmentStatus.CANCELLED):
                    continue  # 已完成/已取消的历史段不动
                seg.member_ids.remove(person_id)
                released.append(seg.id)
                if not seg.member_ids:
                    # 最后一个人离开，房间/车辆整体释放
                    seg.status = SegmentStatus.CANCELLED
                    action = "withdraw"
                else:
                    action = "withdraw-partial"
                self.records.append(ChangeRecord(
                    id=_new_id(), trip_id=trip_id, proposal_id=None,
                    action=action, old_segment_id=seg.id, new_segment_id=None,
                    vendor=seg.vendor, old_cost=seg.cost, new_cost=0.0,
                    cost_delta=0.0,
                    reason=f"{self.persons[person_id].name}{reason}",
                    notified=list(notify), actor_id=actor_id,
                    at=datetime.now(timezone.utc)))
            if person_id not in trip.withdrawn_ids:
                trip.withdrawn_ids.append(person_id)
            return {"person_id": person_id, "released_segments": released}

    # ---- 供应商回调（幂等）----------------------------------------------

    def vendor_callback(self, idempotency_key: str, reference: str, event: str,
                        *, amount: float = 0.0, message: str = "") -> dict:
        """处理供应商回调。

        event=cancelled：reference 为交通班次 id，标记班次取消；
        event=refund：reference 为变更记录 id，登记退款金额。
        相同 idempotency_key 的重复回调只返回首次结果，绝不重复入账。
        """
        with self._lock:
            if idempotency_key in self.callbacks:
                return dict(self.callbacks[idempotency_key], duplicate=True)

            if event == "cancelled":
                option = self.transports.get(reference)
                if option is None:
                    raise VendorCallbackError(f"未知班次 {reference}")
                option.cancelled = True
                for seg in self.segments.values():
                    if seg.kind == SegmentKind.TRANSPORT and seg.resource_id == reference:
                        seg.vendor_cancelled = True
                result = {"key": idempotency_key, "event": event,
                          "reference": reference, "applied": True}
            elif event == "refund":
                target = next((r for r in self.records if r.id == reference), None)
                if target is None:
                    raise VendorCallbackError(f"未知变更记录 {reference}")
                target.refunds.append(amount)
                result = {"key": idempotency_key, "event": event,
                          "reference": reference, "amount": amount, "applied": True}
            else:
                raise VendorCallbackError(f"未知回调事件 {event}")

            self.callbacks[idempotency_key] = result
            return dict(result)

    # ---- 成员视图（角色化隐私）------------------------------------------

    def member_view(self, viewer_id: str, trip_id: str,
                    target_id: Optional[str] = None) -> dict:
        with self._lock:
            viewer = self._require_person(viewer_id)
            target_id = target_id or viewer_id
            target = self._require_person(target_id)
            trip = self._require_trip(trip_id)
            if target_id not in trip.member_ids:
                raise TravelError("目标成员不在该行程中")
            if target_id != viewer_id:
                # 只能查看医疗/管理职责范围内的他人档案；普通成员互不可查
                if viewer.role not in (Role.MEDICAL, Role.MANAGER, Role.OPS):
                    raise AccessDeniedError("无权查看他人成员视图")

            view = {
                "person": {
                    "id": target.id, "name": target.name, "role": target.role,
                    "doc_type": target.doc_type,
                    "doc_expiry": target.doc_expiry.isoformat()
                    if target.doc_expiry else None,
                    "injured": target.injured,
                    "status": "withdrawn" if target_id in trip.withdrawn_ids else "active",
                },
                "segments": [],
                "match": None,
            }
            # 医疗备注：本人或队医；紧急联系人：本人、领队、运营、队医（无内容则省略）
            if (target_id == viewer_id or viewer.role == Role.MEDICAL) \
                    and target.medical_notes is not None:
                view["person"]["medical_notes"] = target.medical_notes
            if target_id == viewer_id or viewer.role in (
                    Role.MANAGER, Role.OPS, Role.MEDICAL):
                if target.emergency_contact is not None:
                    view["person"]["emergency_contact"] = target.emergency_contact

            match = self.matches[trip.match_id]
            view["match"] = {
                "opponent": match.opponent, "city": match.city, "venue": match.venue,
                "kickoff": match.kickoff.isoformat(),
                "report_deadline": match.report_deadline.isoformat(),
            }
            for seg in self._trip_segments(trip_id):
                if target_id not in seg.member_ids:
                    continue
                if seg.status == SegmentStatus.CANCELLED:
                    continue
                item = {"kind": seg.kind, "status": seg.status,
                        "vendor": seg.vendor, "detail": seg.detail,
                        "resource_id": seg.resource_id}
                if seg.start:
                    item["start_local"] = seg.start.isoformat()
                    item["start_utc"] = _utc(seg.start).isoformat()
                if seg.end:
                    item["end_local"] = seg.end.isoformat()
                    item["end_utc"] = _utc(seg.end).isoformat()
                if seg.night:
                    item["night"] = seg.night.isoformat()
                view["segments"].append(item)
            return view

    def list_roster(self, viewer_id: str, trip_id: str) -> list[dict]:
        """名单视图：队医可见医疗备注，领队/运营/队医可见紧急联系人。"""
        with self._lock:
            viewer = self._require_person(viewer_id)
            trip = self._require_trip(trip_id)
            result = []
            for pid in trip.member_ids:
                p = self.persons[pid]
                row = {"id": p.id, "name": p.name, "role": p.role,
                       "injured": p.injured,
                       "doc_expiry": p.doc_expiry.isoformat() if p.doc_expiry else None,
                       "status": "withdrawn" if pid in trip.withdrawn_ids else "active"}
                if viewer.role == Role.MEDICAL:
                    row["medical_notes"] = p.medical_notes
                if viewer.role in (Role.MANAGER, Role.OPS, Role.MEDICAL):
                    row["emergency_contact"] = p.emergency_contact
                result.append(row)
            return result

    # ---- 费用汇总（已发生费用保留，支持报销）----------------------------

    def cost_summary(self, trip_id: str) -> dict:
        with self._lock:
            self._require_trip(trip_id)
            by_vendor: dict[str, dict[str, float]] = {}
            gross = 0.0
            for seg in self.segments.values():
                if seg.trip_id != trip_id:
                    continue
                if seg.status == SegmentStatus.PROPOSED:
                    continue  # 草稿未落位，不计费用
                bucket = by_vendor.setdefault(
                    seg.vendor or "未知供应商",
                    {"gross": 0.0, "refunded": 0.0, "cancelled_cost": 0.0})
                bucket["gross"] += seg.cost
                gross += seg.cost
                if seg.status == SegmentStatus.CANCELLED:
                    bucket["cancelled_cost"] += seg.cost
            refunds_total = 0.0
            deltas = 0.0
            for r in self.records:
                if r.trip_id != trip_id:
                    continue
                refund = sum(r.refunds)
                refunds_total += refund
                if r.action == "replace":
                    deltas += r.cost_delta
                if refund and r.vendor:
                    by_vendor.setdefault(
                        r.vendor, {"gross": 0.0, "refunded": 0.0,
                                   "cancelled_cost": 0.0})
                    by_vendor[r.vendor]["refunded"] += refund
            return {
                "currency": "CNY",
                "gross_incurred": round(gross, 2),       # 已发生（含取消未退）
                "refunds": round(refunds_total, 2),
                "net_payable": round(gross - refunds_total, 2),
                "rebooking_delta": round(deltas, 2),     # 替换段费用差额合计
                "by_vendor": {k: {kk: round(vv, 2) for kk, vv in v.items()}
                              for k, v in sorted(by_vendor.items())},
                "reimbursable_records": [
                    r.id for r in self.records if r.trip_id == trip_id],
            }

    def change_history(self, trip_id: str) -> list[ChangeRecord]:
        with self._lock:
            return [r for r in self.records if r.trip_id == trip_id]

    # ---- 快照与恢复 -------------------------------------------------------

    def save(self, path: str) -> None:
        with self._lock:
            snapshot = {
                "version": 1,
                "persons": {k: _encode(asdict(v)) for k, v in self.persons.items()},
                "transports": {k: _encode(asdict(v)) for k, v in self.transports.items()},
                "vehicles": {k: asdict(v) for k, v in self.vehicles.items()},
                "rooms": {k: asdict(v) for k, v in self.rooms.items()},
                "training_slots": {k: _encode(asdict(v))
                                   for k, v in self.training_slots.items()},
                "matches": {k: _encode(asdict(v)) for k, v in self.matches.items()},
                "trips": {k: asdict(v) for k, v in self.trips.items()},
                "segments": {k: _encode(asdict(v)) for k, v in self.segments.items()},
                "proposals": {k: self._proposal_dict(v)
                              for k, v in self.proposals.items()},
                "records": [_encode(self._record_dict(r)) for r in self.records],
                "callbacks": self.callbacks,
            }
            data = json.dumps(snapshot, ensure_ascii=False, indent=2)
            directory = os.path.dirname(os.path.abspath(path)) or "."
            fd, tmp = tempfile.mkstemp(prefix=".travel-", suffix=".json", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, path)
            except BaseException:
                os.unlink(tmp)
                raise

    @classmethod
    def restore(cls, path: str) -> "TravelService":
        with open(path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
        svc = cls()
        data = _decode(snapshot)
        svc.persons = {k: Person(**v) for k, v in data["persons"].items()}
        svc.transports = {k: TransportOption(**v) for k, v in data["transports"].items()}
        svc.vehicles = {k: Vehicle(**v) for k, v in data["vehicles"].items()}
        svc.rooms = {k: Room(**v) for k, v in data["rooms"].items()}
        svc.training_slots = {k: TrainingSlot(**v)
                              for k, v in data["training_slots"].items()}
        svc.matches = {k: Match(**v) for k, v in data["matches"].items()}
        svc.trips = {k: Trip(**v) for k, v in data["trips"].items()}
        svc.segments = {k: ItinerarySegment(**v) for k, v in data["segments"].items()}
        svc.proposals = {}
        for k, v in data["proposals"].items():
            ops = [ChangeOp(action=o["action"], segment_id=o["segment_id"],
                            new=SegmentSpec(**o["new"]) if o["new"] else None,
                            notify=o["notify"]) for o in v["ops"]]
            v = dict(v)
            v["ops"] = ops
            svc.proposals[k] = ChangeProposal(**v)
        svc.records = [ChangeRecord(**v) for v in data["records"]]
        svc.callbacks = dict(data["callbacks"])
        svc.audit_consistency()  # 重启后复核资源一致性
        return svc

    def audit_consistency(self) -> dict:
        """复核派生状态：对所有行程做一次占用一致性检查。"""
        problems: list[str] = []
        with self._lock:
            for trip_id in self.trips:
                try:
                    self._check_occupancy(trip_id, [], cancelling=set())
                except TravelError as exc:
                    problems.append(f"{trip_id}: {exc}")
        return {"ok": not problems, "problems": problems}

    # ---- 查询辅助 ---------------------------------------------------------

    def get_segment(self, segment_id: str) -> ItinerarySegment:
        with self._lock:
            return self._require_segment(segment_id)

    def trip_segments(self, trip_id: str) -> list[ItinerarySegment]:
        with self._lock:
            return list(self._trip_segments(trip_id))

    # ---- 内部：建模与占用校验 --------------------------------------------

    def _build_segment(self, trip_id: str, spec: SegmentSpec, *,
                       status: str = SegmentStatus.PROPOSED,
                       segment_id: Optional[str] = None) -> ItinerarySegment:
        self._require_trip(trip_id)
        kind = spec.kind
        rid = spec.resource_id
        members = list(dict.fromkeys(spec.member_ids))
        for pid in members:
            if pid not in self.persons:
                raise TravelError(f"未知成员 {pid}")
        start = end = None
        night = spec.night
        vendor = spec.vendor
        cost = spec.cost if spec.cost is not None else 0.0
        detail = spec.detail

        if kind == SegmentKind.TRANSPORT:
            opt = self.transports.get(rid)
            if opt is None:
                raise TravelError(f"未知交通班次 {rid}")
            if opt.cancelled:
                raise BookingConflictError(f"班次 {rid} 已被供应商取消")
            start, end = opt.departure, opt.arrival
            vendor = spec.vendor if spec.vendor is not None else opt.vendor
            cost = spec.cost if spec.cost is not None else opt.cost
            detail = detail or f"{opt.origin}->{opt.destination}"
            if len(members) > opt.capacity:
                raise BookingConflictError(
                    f"班次 {rid} 容量 {opt.capacity}，分配 {len(members)} 人")
        elif kind == SegmentKind.VEHICLE:
            veh = self.vehicles.get(rid)
            if veh is None:
                raise TravelError(f"未知车辆 {rid}")
            if spec.start is None or spec.end is None:
                raise TravelError("车辆段必须给出派车起止时间")
            start, end = spec.start, spec.end
            vendor = spec.vendor if spec.vendor is not None else veh.vendor
            cost = spec.cost if spec.cost is not None else veh.cost
            detail = detail or veh.name
            if len(members) > veh.capacity:
                raise BookingConflictError(
                    f"车辆 {rid} 容量 {veh.capacity}，分配 {len(members)} 人")
        elif kind == SegmentKind.HOTEL:
            room = self.rooms.get(rid)
            if room is None:
                raise TravelError(f"未知房间 {rid}")
            if night is None:
                raise TravelError("酒店段必须给出入住夜")
            vendor = spec.vendor if spec.vendor is not None else room.hotel
            cost = spec.cost if spec.cost is not None else room.cost
            detail = detail or room.room_type
            if len(members) > room.capacity:
                raise BookingConflictError(
                    f"房间 {rid} 容量 {room.capacity}，分配 {len(members)} 人")
            injured = [self.persons[m].name for m in members
                       if self.persons[m].injured]
            if injured and room.capacity > 1:
                raise BookingConflictError(
                    f"伤员 {'、'.join(injured)} 按队医要求必须住单人房")
        elif kind == SegmentKind.TRAINING:
            slot = self.training_slots.get(rid)
            if slot is None:
                raise TravelError(f"未知训练场时段 {rid}")
            start, end = slot.start, slot.end
            vendor = spec.vendor if spec.vendor is not None else slot.venue
            cost = spec.cost if spec.cost is not None else slot.cost
            detail = detail or slot.venue
            if len(members) > slot.capacity:
                raise BookingConflictError(
                    f"训练场时段 {rid} 容量 {slot.capacity}，分配 {len(members)} 人")
        else:
            raise TravelError(f"未知行程段类型 {kind}")

        if start is not None and end is not None and _utc(start) >= _utc(end):
            raise TravelError("行程段结束时间必须晚于开始时间（注意跨日/跨时区）")

        return ItinerarySegment(
            id=segment_id or _new_id(), trip_id=trip_id, kind=kind,
            resource_id=rid, member_ids=members, start=start, end=end,
            night=night, vendor=vendor or "", cost=cost, status=status,
            detail=detail)

    def _check_occupancy(self, trip_id: str,
                         extra: list[ItinerarySegment],
                         cancelling: set[str]) -> None:
        """对时间型资源（班次/车辆/训练场/人员）与按夜资源（房间）做占用校验。"""
        existing = [s for s in self._trip_segments(trip_id)
                    if s.status in ACTIVE_STATUSES and s.id not in cancelling]
        allsegs = existing + list(extra)

        def capacity_of(kind: str, rid: str) -> int:
            if kind == SegmentKind.TRANSPORT:
                return self.transports[rid].capacity
            if kind == SegmentKind.VEHICLE:
                return self.vehicles[rid].capacity
            if kind == SegmentKind.TRAINING:
                return self.training_slots[rid].capacity
            raise KeyError(rid)

        # 固定时刻的资源：同班次/同时段内并集人数不得超容量
        for kind in (SegmentKind.TRANSPORT, SegmentKind.TRAINING):
            groups: dict[str, set[str]] = {}
            for s in allsegs:
                if s.kind == kind:
                    groups.setdefault(s.resource_id, set()).update(s.member_ids)
            for rid, members in groups.items():
                cap = capacity_of(kind, rid)
                if len(members) > cap:
                    raise BookingConflictError(
                        f"资源 {rid} 被重复占用：{len(members)} > {cap}")

        # 车辆：同一辆车的不同派车时段不得重叠（一辆车不能同时跑两趟），
        # 与是否坐满无关；单趟超员在 _build_segment 中拦截
        by_vehicle: dict[str, list[ItinerarySegment]] = {}
        for s in allsegs:
            if s.kind == SegmentKind.VEHICLE:
                by_vehicle.setdefault(s.resource_id, []).append(s)
        for rid, duties in by_vehicle.items():
            events = []
            for d in duties:
                events.append((_utc(d.start), 1, d))
                events.append((_utc(d.end), 0, d))
            events.sort(key=lambda e: (e[0], e[1]))
            active: list[ItinerarySegment] = []
            for t, is_start, seg in events:
                active = [d for d in active if _utc(d.end) > t]
                if is_start:
                    if active:
                        raise BookingConflictError(
                            f"车辆 {rid} 在 {t.isoformat()} 被重复派车"
                            f"（{seg.detail} 与 {active[0].detail} 时段重叠）")
                    active.append(seg)

        # 房间：同房间同夜不超容量；同一人同夜不能住两间房
        room_nights: dict[tuple[str, date], set[str]] = {}
        person_nights: dict[tuple[str, date], set[str]] = {}
        for s in allsegs:
            if s.kind != SegmentKind.HOTEL:
                continue
            room_nights.setdefault((s.resource_id, s.night), set()).update(s.member_ids)
            for m in s.member_ids:
                person_nights.setdefault((m, s.night), set()).add(s.resource_id)
        for (rid, night), members in room_nights.items():
            room = self.rooms[rid]
            if len(members) > room.capacity:
                raise BookingConflictError(
                    f"房间 {rid} 在 {night} 被重复占用：{len(members)} > {room.capacity}")
            injured = [self.persons[m].name for m in members
                       if self.persons[m].injured]
            if injured and room.capacity > 1:
                raise BookingConflictError(
                    f"房间 {rid} 在 {night} 住有伤员，必须为单人房")
        for (pid, night), rooms_set in person_nights.items():
            if len(rooms_set) > 1:
                raise BookingConflictError(
                    f"{self.persons[pid].name} 在 {night} 同时占用多间房")

        # 人员：时间型段之间不得重叠（航班/大巴/训练场）
        by_person: dict[str, list[ItinerarySegment]] = {}
        for s in allsegs:
            if s.kind == SegmentKind.HOTEL or not s.start:
                continue
            for m in s.member_ids:
                by_person.setdefault(m, []).append(s)
        for pid, segs_p in by_person.items():
            intervals = sorted((_utc(s.start), _utc(s.end), s.resource_id)
                               for s in segs_p)
            for i in range(1, len(intervals)):
                if intervals[i][0] < intervals[i - 1][1]:
                    raise BookingConflictError(
                        f"{self.persons[pid].name} 的行程时间重叠: "
                        f"{intervals[i - 1][2]} 与 {intervals[i][2]}")

    # ---- 小工具 -----------------------------------------------------------

    def _require_person(self, pid: str) -> Person:
        if pid not in self.persons:
            raise TravelError(f"未知成员 {pid}")
        return self.persons[pid]

    def _require_trip(self, trip_id: str) -> Trip:
        if trip_id not in self.trips:
            raise TravelError(f"未知行程 {trip_id}")
        return self.trips[trip_id]

    def _require_segment(self, seg_id: str) -> ItinerarySegment:
        if seg_id not in self.segments:
            raise TravelError(f"未知行程段 {seg_id}")
        return self.segments[seg_id]

    def _trip_segments(self, trip_id: str) -> list[ItinerarySegment]:
        return [s for s in self.segments.values() if s.trip_id == trip_id]

    def _active_members(self, trip: Trip) -> list[str]:
        return [m for m in trip.member_ids if m not in trip.withdrawn_ids]

    @staticmethod
    def _proposal_dict(p: ChangeProposal) -> dict:
        return {
            "id": p.id, "trip_id": p.trip_id, "proposer_id": p.proposer_id,
            "reason": p.reason,
            "ops": [{"action": o.action, "segment_id": o.segment_id,
                     "new": _encode(asdict(o.new)) if o.new else None,
                     "notify": list(o.notify)} for o in p.ops],
            "status": p.status, "created_at": _encode(p.created_at),
            "decided_by": p.decided_by,
            "decided_at": _encode(p.decided_at) if p.decided_at else None,
        }

    @staticmethod
    def _record_dict(r: ChangeRecord) -> dict:
        return {
            "id": r.id, "trip_id": r.trip_id, "proposal_id": r.proposal_id,
            "action": r.action, "old_segment_id": r.old_segment_id,
            "new_segment_id": r.new_segment_id, "vendor": r.vendor,
            "old_cost": r.old_cost, "new_cost": r.new_cost,
            "cost_delta": r.cost_delta, "reason": r.reason,
            "notified": list(r.notified), "actor_id": r.actor_id,
            "at": r.at, "refunds": list(r.refunds),
        }
