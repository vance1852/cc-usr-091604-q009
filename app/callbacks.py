"""供应商回调：按 callback_id 幂等，重复回调不产生二次影响。"""

from app.errors import ValidationError
from app.guards import locked, persisted
from app.models import (
    CallbackReceipt,
    ChangeRecord,
    SegmentStatus,
    money,
)

#: 事件 -> (允许的当前状态, 目标状态；None 表示仅打标记)
_TRANSITIONS = {
    "confirmed": ({SegmentStatus.CONFIRMED}, None),
    "departed": ({SegmentStatus.CONFIRMED}, SegmentStatus.IN_PROGRESS),
    "arrived": (
        {SegmentStatus.CONFIRMED, SegmentStatus.IN_PROGRESS},
        SegmentStatus.COMPLETED,
    ),
    "completed": (
        {SegmentStatus.CONFIRMED, SegmentStatus.IN_PROGRESS},
        SegmentStatus.COMPLETED,
    ),
    "cancelled": ({SegmentStatus.CONFIRMED}, SegmentStatus.CANCELLED),
}


class CallbackMixin:
    @locked
    @persisted
    def supplier_callback(self, callback_id, supplier, segment_id, event, at=None):
        """处理供应商回调；同一 callback_id 重放时返回首次回执，状态不变。"""
        existing = self.state.callbacks.get(callback_id)
        if existing is not None:
            return {
                "callback_id": callback_id,
                "duplicate": True,
                "applied": False,
                "original_event": existing.event,
                "segment_status": self._segment_status(segment_id),
            }
        if event not in _TRANSITIONS:
            raise ValidationError(f"未知的供应商事件: {event}")
        itinerary, segment = self._find_segment(segment_id)
        if segment.supplier != supplier:
            raise ValidationError(
                f"回调供应商 {supplier} 与行程段供应商 {segment.supplier} 不一致"
            )
        allowed, target = _TRANSITIONS[event]
        if segment.status not in allowed:
            raise ValidationError(
                f"行程段状态 {segment.status.value} 不接受事件 {event}"
            )
        now = at or self._now()
        if event == "confirmed":
            segment.supplier_confirmed = True
        elif target is not None:
            segment.status = target
        if event == "cancelled":
            # 供应商通知取消：释放资源、保留已发生费用、留痕并通知段内成员
            self._release_segment_assignments(segment.segment_id)
            record = ChangeRecord(
                record_id=self._new_id("chg"),
                itinerary_id=itinerary.itinerary_id,
                segment_id=segment.segment_id,
                action="supplier_cancelled",
                old_supplier=supplier,
                new_supplier=None,
                cost_delta=money(-segment.cost),
                currency=segment.currency,
                notify=sorted(segment.member_ids),
                retained_cost=self._booked_amount(segment.segment_id),
                proposal_id=None,
                at=now,
                detail="供应商通知取消",
            )
            self.state.change_records[record.record_id] = record
        itinerary.version += 1
        self.state.callbacks[callback_id] = CallbackReceipt(
            callback_id=callback_id,
            supplier=supplier,
            segment_id=segment_id,
            event=event,
            applied=True,
            at=now,
        )
        return {
            "callback_id": callback_id,
            "duplicate": False,
            "applied": True,
            "segment_status": segment.status.value,
        }
