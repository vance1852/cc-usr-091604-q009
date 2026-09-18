"""快照恢复与重启后的资源一致性校验。"""

from app.errors import ConsistencyError, ValidationError
from app.guards import locked
from app.models import (
    ACTIVE_SEGMENT_STATUSES,
    TERMINAL_SEGMENT_STATUSES,
    SegmentStatus,
)
from app.store import load_snapshot, save_snapshot


class RecoveryMixin:
    @locked
    def snapshot(self, path=None):
        """把当前状态原子写入快照文件，返回路径。"""
        target = path or self._storage_path
        if not target:
            raise ValidationError("未配置快照路径")
        save_snapshot(self.state, target)
        return target

    @classmethod
    def recover(cls, path, strict=True):
        """从快照恢复服务实例，并校验资源一致性。"""
        service = cls(storage_path=path)
        service.state = load_snapshot(path)
        issues = service.validate_consistency()
        if issues and strict:
            raise ConsistencyError("快照恢复后一致性校验失败: " + "；".join(issues))
        return service

    @locked
    def validate_consistency(self):
        """结构一致性校验：容量、重复占用、时段冲突、终结段留痕。"""
        issues = []
        for itinerary in self.state.itineraries.values():
            for segment in itinerary.segments:
                if segment.ends_at <= segment.starts_at:
                    issues.append(f"行程段 {segment.segment_id} 时间顺序异常")
                for member_id in segment.member_ids:
                    if member_id not in self.state.members:
                        issues.append(
                            f"行程段 {segment.segment_id} 含有未知成员 {member_id}"
                        )
                issues.extend(self._segment_assignment_issues(segment))
                if segment.status in TERMINAL_SEGMENT_STATUSES:
                    if self.state.vehicle_assignments.get(segment.segment_id):
                        issues.append(
                            f"已终结行程段 {segment.segment_id} 仍持有车辆分配"
                        )
                    if self.state.room_assignments.get(segment.segment_id):
                        issues.append(
                            f"已终结行程段 {segment.segment_id} 仍持有房间分配"
                        )
                if segment.status in (SegmentStatus.CANCELLED, SegmentStatus.REPLACED):
                    if not any(
                        r.segment_id == segment.segment_id
                        for r in self.state.change_records.values()
                    ):
                        issues.append(
                            f"行程段 {segment.segment_id} 已{segment.status.value}但缺少变更记录"
                        )
        for option in self.state.transport_options.values():
            load = self._option_load(option.option_id)
            if load > option.capacity:
                issues.append(
                    f"班次 {option.code} 超售: 占用 {load}，容量 {option.capacity}"
                )
        slot_users: dict[str, list[str]] = {}
        for itinerary in self.state.itineraries.values():
            for segment in itinerary.segments:
                if segment.slot_id and segment.status in ACTIVE_SEGMENT_STATUSES:
                    slot_users.setdefault(segment.slot_id, []).append(segment.segment_id)
        for slot_id, users in slot_users.items():
            if len(users) > 1:
                issues.append(f"训练场时段 {slot_id} 被重复预订: {users}")
        for proposal in self.state.proposals.values():
            if proposal.itinerary_id not in self.state.itineraries:
                issues.append(f"改签方案 {proposal.proposal_id} 引用了不存在的行程")
        for entry in self.state.ledger:
            if entry.itinerary_id not in self.state.itineraries:
                issues.append(f"费用记录 {entry.entry_id} 引用了不存在的行程")
        return issues
