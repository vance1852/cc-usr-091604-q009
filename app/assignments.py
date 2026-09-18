"""车辆与房间分配：防重复占用，部分成员退出只释放其对应资源。"""

from app.errors import (
    CapacityError,
    DoubleBookingError,
    SegmentLockedError,
    ValidationError,
)
from app.guards import locked, persisted
from app.models import (
    TERMINAL_SEGMENT_STATUSES,
    ChangeRecord,
    Role,
    RoomType,
    SegmentKind,
    SegmentStatus,
    money,
)

_MUTABLE_SEGMENT_STATUSES = (SegmentStatus.DRAFT, SegmentStatus.CONFIRMED)


class AssignmentMixin:
    # -- 车辆 -------------------------------------------------------------

    @locked
    @persisted
    def assign_vehicle(self, segment_id, vehicle_id, member_ids):
        _, segment = self._find_segment(segment_id)
        self._assign_vehicle_core(segment, vehicle_id, set(member_ids))

    @locked
    @persisted
    def auto_assign_vehicles(self, segment_id):
        """按车辆编号顺序贪心排座，返回 车辆 -> 成员 的分配结果。"""
        _, segment = self._find_segment(segment_id)
        if segment.kind != SegmentKind.GROUND:
            raise ValidationError("仅地面交通段可分配车辆")
        self._require_segment_mutable(segment)
        current = self.state.vehicle_assignments.setdefault(segment_id, {})
        assigned = set().union(*current.values()) if current else set()
        pending = sorted(segment.member_ids - assigned)
        plan: dict[str, list[str]] = {}
        for vehicle in sorted(self.state.vehicles.values(), key=lambda v: v.vehicle_id):
            if not pending:
                break
            free = vehicle.capacity - len(current.get(vehicle.vehicle_id, set()))
            if free <= 0:
                continue
            take, pending = pending[:free], pending[free:]
            if take:
                plan[vehicle.vehicle_id] = take
        if pending:
            raise CapacityError(f"车辆座位不足，未分配成员: {pending}")
        for vehicle_id, members in plan.items():
            self._assign_vehicle_core(segment, vehicle_id, set(members))
        return {vid: sorted(m) for vid, m in sorted(plan.items())}

    def _assign_vehicle_core(self, segment, vehicle_id, members):
        if segment.kind != SegmentKind.GROUND:
            raise ValidationError("仅地面交通段可分配车辆")
        self._require_segment_mutable(segment)
        vehicle = self._vehicle(vehicle_id)
        members = self._check_members_subset(segment, members)
        current = self.state.vehicle_assignments.setdefault(segment.segment_id, {})
        for other_id, occupants in current.items():
            clash = occupants & members
            if clash:
                raise DoubleBookingError(
                    f"成员已分配到车辆 {other_id}: {sorted(clash)}"
                )
        occupants = current.setdefault(vehicle_id, set())
        if len(occupants) + len(members) > vehicle.capacity:
            raise CapacityError(
                f"车辆 {vehicle_id} 容量不足: 需要 {len(occupants) + len(members)}，"
                f"容量 {vehicle.capacity}"
            )
        occupants |= members

    # -- 房间 -------------------------------------------------------------

    @locked
    @persisted
    def assign_room(self, segment_id, room_id, member_ids):
        _, segment = self._find_segment(segment_id)
        self._assign_room_core(segment, room_id, set(member_ids))

    @locked
    @persisted
    def auto_assign_rooms(self, segment_id):
        """先满足伤员单间要求，再按房型顺序填充，返回 房间 -> 成员。"""
        _, segment = self._find_segment(segment_id)
        if segment.kind != SegmentKind.HOTEL:
            raise ValidationError("仅住宿段可分配房间")
        self._require_segment_mutable(segment)
        current = self.state.room_assignments.setdefault(segment_id, {})
        assigned = set().union(*current.values()) if current else set()
        pending = sorted(segment.member_ids - assigned)
        required = [m for m in pending if self._member(m).requires_single_room]
        others = [m for m in pending if m not in set(required)]
        rooms = sorted(
            self.state.rooms.values(),
            key=lambda r: (r.room_type != RoomType.SINGLE, r.room_id),
        )
        # 先在空房规划中验证可行性，再落库，避免半拉子分配
        plan: dict[str, list[str]] = {}
        planned_occupancy = {
            r.room_id: len(current.get(r.room_id, set())) for r in rooms
        }
        free_singles = [
            r for r in rooms if r.room_type == RoomType.SINGLE and planned_occupancy[r.room_id] == 0
        ]
        if len(required) > len(free_singles):
            raise CapacityError("单人间数量不足，无法满足伤员单间要求")
        for member_id, room in zip(required, free_singles):
            plan.setdefault(room.room_id, []).append(member_id)
            planned_occupancy[room.room_id] += 1
        for room in rooms:
            if not others:
                break
            occupants = current.get(room.room_id, set())
            if any(self._member(m).requires_single_room for m in occupants):
                continue
            free = room.capacity - planned_occupancy[room.room_id]
            if free <= 0:
                continue
            take, others = others[:free], others[free:]
            if take:
                plan.setdefault(room.room_id, []).extend(take)
                planned_occupancy[room.room_id] += len(take)
        if others:
            raise CapacityError(f"房间床位不足，未分配成员: {others}")
        for room_id, members in plan.items():
            self._assign_room_core(segment, room_id, set(members))
        return {rid: sorted(m) for rid, m in sorted(plan.items())}

    def _assign_room_core(self, segment, room_id, members):
        if segment.kind != SegmentKind.HOTEL:
            raise ValidationError("仅住宿段可分配房间")
        self._require_segment_mutable(segment)
        room = self._room(room_id)
        members = self._check_members_subset(segment, members)
        current = self.state.room_assignments.setdefault(segment.segment_id, {})
        for other_id, occupants in current.items():
            clash = occupants & members
            if clash:
                raise DoubleBookingError(
                    f"成员已分配到房间 {other_id}: {sorted(clash)}"
                )
        occupants = current.setdefault(room_id, set())
        merged = occupants | members
        if len(merged) > room.capacity:
            raise CapacityError(
                f"房间 {room_id} 床位不足: 需要 {len(merged)}，容量 {room.capacity}"
            )
        single_required = [m for m in merged if self._member(m).requires_single_room]
        if single_required and (room.room_type != RoomType.SINGLE or len(merged) > 1):
            raise ValidationError(f"成员 {sorted(single_required)} 需要单独房间")
        occupants |= members

    # -- 成员临时退出 -------------------------------------------------------

    @locked
    @persisted
    def withdraw_members(self, itinerary_id, member_ids, by_member, reason=""):
        """部分成员临时退出：只释放他们占用的座位与床位，其余成员不受影响。"""
        self._require_role(by_member, Role.LEADER, Role.OPS)
        itinerary = self._itinerary(itinerary_id)
        targets = set(member_ids)
        if not targets:
            raise ValidationError("退出成员列表不能为空")
        for member_id in targets:
            self._member(member_id)
        affected: set[str] = set()
        for segment in itinerary.segments:
            if segment.status in TERMINAL_SEGMENT_STATUSES:
                continue
            hit = segment.member_ids & targets
            if not hit:
                continue
            segment.member_ids -= hit
            affected |= hit
            for occupants in self.state.vehicle_assignments.get(segment.segment_id, {}).values():
                occupants -= hit
            for occupants in self.state.room_assignments.get(segment.segment_id, {}).values():
                occupants -= hit
        if not affected:
            raise ValidationError("所选成员不在该行程的任何行程段中")
        record = ChangeRecord(
            record_id=self._new_id("chg"),
            itinerary_id=itinerary.itinerary_id,
            segment_id=None,
            action="withdraw",
            old_supplier=None,
            new_supplier=None,
            cost_delta=money(0),
            currency="CNY",
            notify=sorted(affected),
            retained_cost=money(0),
            proposal_id=None,
            at=self._now(),
            detail=reason or "成员临时退出",
        )
        self.state.change_records[record.record_id] = record
        return {"withdrawn": sorted(affected), "record_id": record.record_id}

    # -- 内部 ---------------------------------------------------------------

    def _require_segment_mutable(self, segment):
        if segment.status not in _MUTABLE_SEGMENT_STATUSES:
            raise SegmentLockedError(
                f"行程段状态为 {segment.status.value}，不可调整资源分配"
            )

    def _check_members_subset(self, segment, members):
        members = set(members)
        if not members:
            raise ValidationError("分配成员列表不能为空")
        unknown = members - set(self.state.members)
        if unknown:
            raise ValidationError(f"成员不存在: {sorted(unknown)}")
        outsider = members - segment.member_ids
        if outsider:
            raise ValidationError(
                f"成员不在行程段 {segment.segment_id} 中: {sorted(outsider)}"
            )
        return members

    def _segment_assignment_issues(self, segment):
        """单个行程段的分配完整性检查，供确认与恢复校验共用。"""
        issues = []
        seen: dict[str, str] = {}
        for vehicle_id, occupants in self.state.vehicle_assignments.get(
            segment.segment_id, {}
        ).items():
            vehicle = self.state.vehicles.get(vehicle_id)
            if vehicle is None:
                issues.append(f"车辆 {vehicle_id} 不存在")
                continue
            if len(occupants) > vehicle.capacity:
                issues.append(f"车辆 {vehicle_id} 超载")
            for member_id in occupants:
                if member_id in seen:
                    issues.append(f"成员 {member_id} 被重复分配车辆")
                seen[member_id] = vehicle_id
                if member_id not in segment.member_ids:
                    issues.append(f"成员 {member_id} 不在行程段却被分配车辆 {vehicle_id}")
        seen = {}
        for room_id, occupants in self.state.room_assignments.get(
            segment.segment_id, {}
        ).items():
            room = self.state.rooms.get(room_id)
            if room is None:
                issues.append(f"房间 {room_id} 不存在")
                continue
            if len(occupants) > room.capacity:
                issues.append(f"房间 {room_id} 超员")
            singles = [
                m
                for m in occupants
                if m in self.state.members and self.state.members[m].requires_single_room
            ]
            if singles and (room.room_type != RoomType.SINGLE or len(occupants) > 1):
                issues.append(f"房间 {room_id} 未满足伤员单间要求")
            for member_id in occupants:
                if member_id in seen:
                    issues.append(f"成员 {member_id} 被重复分配房间")
                seen[member_id] = room_id
                if member_id not in segment.member_ids:
                    issues.append(f"成员 {member_id} 不在行程段却被分配房间 {room_id}")
        return issues
