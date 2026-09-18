"""改签方案：领队提出、运营审核、原子生效，取消/替换全程留痕。"""

from app.errors import (
    ConflictError,
    DeadlineError,
    NotFoundError,
    SegmentLockedError,
    ValidationError,
)
from app.guards import locked, persisted
from app.models import (
    TERMINAL_SEGMENT_STATUSES,
    AddSegmentAction,
    CancelSegmentAction,
    ChangeProposal,
    ChangeRecord,
    CostKind,
    ItineraryStatus,
    ProposalStatus,
    ReplaceSegmentAction,
    Role,
    Segment,
    SegmentDraft,
    SegmentKind,
    SegmentStatus,
    money,
)

_TRANSPORT_KINDS = (SegmentKind.FLIGHT, SegmentKind.GROUND)


class ChangeMixin:
    # -- 提出与审核 ---------------------------------------------------------

    @locked
    @persisted
    def propose_change(self, itinerary_id, proposer_id, actions, reason=""):
        """领队对已确认的行程提出改签方案，审核通过前不生效。"""
        self._require_role(proposer_id, Role.LEADER)
        itinerary = self._itinerary(itinerary_id)
        if itinerary.status != ItineraryStatus.CONFIRMED:
            raise ValidationError("仅已确认的行程需要提交改签方案")
        coerced = [self._coerce_action(a) for a in actions]
        if not coerced:
            raise ValidationError("改签方案不能为空")
        proposal = ChangeProposal(
            proposal_id=self._new_id("prop"),
            itinerary_id=itinerary_id,
            proposer_id=proposer_id,
            reason=reason,
            actions=coerced,
            base_version=itinerary.version,
            created_at=self._now(),
        )
        self.state.proposals[proposal.proposal_id] = proposal
        return proposal.proposal_id

    @locked
    @persisted
    def approve_change(self, proposal_id, by_member):
        """运营审核通过：校验版本与全部动作后原子生效。"""
        self._require_role(by_member, Role.OPS)
        proposal = self._proposal(proposal_id)
        if proposal.status != ProposalStatus.PENDING:
            raise ValidationError(f"方案状态为 {proposal.status.value}，无法审核")
        itinerary = self._itinerary(proposal.itinerary_id)
        if itinerary.version != proposal.base_version:
            proposal.status = ProposalStatus.CONFLICT
            proposal.decided_by = by_member
            proposal.decided_at = self._now()
            raise ConflictError("行程在方案提交后已变化，请重新评估后再提交")
        for action in proposal.actions:
            self._validate_action(itinerary, action)
        records = [
            self._apply_action(itinerary, proposal, action) for action in proposal.actions
        ]
        itinerary.version += 1
        proposal.status = ProposalStatus.APPLIED
        proposal.decided_by = by_member
        proposal.decided_at = self._now()
        return {
            "proposal_id": proposal_id,
            "records": [r.record_id for r in records],
            "version": itinerary.version,
        }

    @locked
    @persisted
    def reject_change(self, proposal_id, by_member, note=""):
        self._require_role(by_member, Role.OPS)
        proposal = self._proposal(proposal_id)
        if proposal.status != ProposalStatus.PENDING:
            raise ValidationError(f"方案状态为 {proposal.status.value}，无法驳回")
        proposal.status = ProposalStatus.REJECTED
        proposal.decided_by = by_member
        proposal.decided_at = self._now()
        proposal.decision_note = note

    # -- 查询 ---------------------------------------------------------------

    @locked
    def list_proposals(self, itinerary_id=None):
        proposals = [
            p
            for p in self.state.proposals.values()
            if itinerary_id is None or p.itinerary_id == itinerary_id
        ]
        return [
            self._proposal_dict(p)
            for p in sorted(proposals, key=lambda p: (p.created_at, p.proposal_id))
        ]

    @locked
    def change_records(self, itinerary_id=None):
        records = [
            r
            for r in self.state.change_records.values()
            if itinerary_id is None or r.itinerary_id == itinerary_id
        ]
        return [
            self._record_dict(r)
            for r in sorted(records, key=lambda r: (r.at, r.record_id))
        ]

    # -- 内部：动作校验与执行 -------------------------------------------------

    def _proposal(self, proposal_id) -> ChangeProposal:
        proposal = self.state.proposals.get(proposal_id)
        if proposal is None:
            raise NotFoundError(f"改签方案不存在: {proposal_id}")
        return proposal

    def _coerce_action(self, action):
        if isinstance(action, (CancelSegmentAction, ReplaceSegmentAction, AddSegmentAction)):
            return action
        if isinstance(action, dict):
            kind = action.get("type")
            if kind == "cancel":
                return CancelSegmentAction(
                    segment_id=action["segment_id"],
                    notify=list(action.get("notify") or []),
                    reason=action.get("reason", ""),
                )
            if kind == "replace":
                return ReplaceSegmentAction(
                    segment_id=action["segment_id"],
                    replacement=self._coerce_draft(action["replacement"]),
                    notify=list(action.get("notify") or []),
                )
            if kind == "add":
                return AddSegmentAction(
                    draft=self._coerce_draft(action["segment"]),
                    notify=list(action.get("notify") or []),
                )
        raise ValidationError(f"无法识别的变更动作: {action!r}")

    def _validate_action(self, itinerary, action):
        if isinstance(action, CancelSegmentAction):
            segment = self._segment(itinerary, action.segment_id)
            self._require_segment_changeable(segment, "取消")
            if not action.notify:
                raise ValidationError("取消行程段必须记录通知对象")
        elif isinstance(action, ReplaceSegmentAction):
            segment = self._segment(itinerary, action.segment_id)
            self._require_segment_changeable(segment, "替换")
            if not action.notify:
                raise ValidationError("替换行程段必须记录通知对象")
            members = (
                set(action.replacement.member_ids)
                if action.replacement.member_ids
                else set(segment.member_ids)
            )
            prospective = Segment(
                segment_id="__prospective__",
                kind=action.replacement.kind,
                supplier=action.replacement.supplier,
                origin=action.replacement.origin,
                destination=action.replacement.destination,
                starts_at=action.replacement.starts_at,
                ends_at=action.replacement.ends_at,
                member_ids=members,
                option_id=action.replacement.option_id,
                slot_id=action.replacement.slot_id,
            )
            self._validate_new_segment(
                prospective, exclude_segment_id=segment.segment_id
            )
            self._validate_transport_deadline(itinerary, action.replacement)
        elif isinstance(action, AddSegmentAction):
            prospective = self._build_segment(action.draft, segment_id="__prospective__")
            self._validate_new_segment(prospective, exclude_segment_id=None)
            self._validate_transport_deadline(itinerary, action.draft)
        else:  # pragma: no cover - 防御未知动作类型
            raise ValidationError(f"未知的变更动作: {action!r}")

    def _require_segment_changeable(self, segment, verb):
        if segment.status == SegmentStatus.COMPLETED:
            raise SegmentLockedError(f"已完成的行程段不可重写，无法{verb}")
        if segment.status in TERMINAL_SEGMENT_STATUSES:
            raise SegmentLockedError(
                f"行程段状态为 {segment.status.value}，无法{verb}"
            )

    def _validate_transport_deadline(self, itinerary, draft: SegmentDraft):
        """替换/新增的交通段不能晚于最晚报到时间。"""
        if not itinerary.match_id or draft.kind not in _TRANSPORT_KINDS:
            return
        report_by = self._match(itinerary.match_id).report_by
        if draft.ends_at > report_by:
            raise DeadlineError(
                f"变更后到达 {draft.ends_at.isoformat()} "
                f"晚于最晚报到时间 {report_by.isoformat()}"
            )

    def _apply_action(self, itinerary, proposal, action) -> ChangeRecord:
        now = self._now()
        if isinstance(action, CancelSegmentAction):
            segment = self._segment(itinerary, action.segment_id)
            retained = self._booked_amount(segment.segment_id)
            segment.status = SegmentStatus.CANCELLED
            self._release_segment_assignments(segment.segment_id)
            record = ChangeRecord(
                record_id=self._new_id("chg"),
                itinerary_id=itinerary.itinerary_id,
                segment_id=segment.segment_id,
                action="cancelled",
                old_supplier=segment.supplier,
                new_supplier=None,
                cost_delta=money(-segment.cost),
                currency=segment.currency,
                notify=list(action.notify),
                retained_cost=retained,
                proposal_id=proposal.proposal_id,
                at=now,
                detail=action.reason or proposal.reason,
            )
        elif isinstance(action, ReplaceSegmentAction):
            old = self._segment(itinerary, action.segment_id)
            draft = action.replacement
            members = set(draft.member_ids) if draft.member_ids else set(old.member_ids)
            new_segment = Segment(
                segment_id=self._new_id("seg"),
                kind=draft.kind,
                supplier=draft.supplier,
                origin=draft.origin,
                destination=draft.destination,
                starts_at=draft.starts_at,
                ends_at=draft.ends_at,
                member_ids=members,
                status=old.status,
                cost=money(draft.cost),
                currency=draft.currency,
                option_id=draft.option_id,
                slot_id=draft.slot_id,
                note=draft.note or f"替换自 {old.segment_id}",
            )
            retained = self._booked_amount(old.segment_id)
            old.status = SegmentStatus.REPLACED
            self._release_segment_assignments(old.segment_id)
            itinerary.segments.append(new_segment)
            if new_segment.status == SegmentStatus.CONFIRMED and new_segment.cost > 0:
                self._add_ledger(
                    itinerary_id=itinerary.itinerary_id,
                    segment_id=new_segment.segment_id,
                    supplier=new_segment.supplier,
                    kind=CostKind.BOOKING,
                    amount=new_segment.cost,
                    currency=new_segment.currency,
                    reimbursable=True,
                    note="改签后重新预订",
                )
            record = ChangeRecord(
                record_id=self._new_id("chg"),
                itinerary_id=itinerary.itinerary_id,
                segment_id=old.segment_id,
                action="replaced",
                old_supplier=old.supplier,
                new_supplier=new_segment.supplier,
                cost_delta=money(new_segment.cost - old.cost),
                currency=old.currency,
                notify=list(action.notify),
                retained_cost=retained,
                proposal_id=proposal.proposal_id,
                at=now,
                detail=proposal.reason,
            )
        elif isinstance(action, AddSegmentAction):
            status = (
                SegmentStatus.CONFIRMED
                if itinerary.status == ItineraryStatus.CONFIRMED
                else SegmentStatus.DRAFT
            )
            new_segment = self._build_segment(action.draft, status=status)
            itinerary.segments.append(new_segment)
            if status == SegmentStatus.CONFIRMED and new_segment.cost > 0:
                self._add_ledger(
                    itinerary_id=itinerary.itinerary_id,
                    segment_id=new_segment.segment_id,
                    supplier=new_segment.supplier,
                    kind=CostKind.BOOKING,
                    amount=new_segment.cost,
                    currency=new_segment.currency,
                    reimbursable=True,
                    note="审核通过后新增预订",
                )
            record = ChangeRecord(
                record_id=self._new_id("chg"),
                itinerary_id=itinerary.itinerary_id,
                segment_id=new_segment.segment_id,
                action="added",
                old_supplier=None,
                new_supplier=new_segment.supplier,
                cost_delta=money(new_segment.cost),
                currency=new_segment.currency,
                notify=list(action.notify),
                retained_cost=money(0),
                proposal_id=proposal.proposal_id,
                at=now,
                detail=proposal.reason,
            )
        else:  # pragma: no cover - 防御未知动作类型
            raise ValidationError(f"未知的变更动作: {action!r}")
        self.state.change_records[record.record_id] = record
        return record

    def _release_segment_assignments(self, segment_id):
        self.state.vehicle_assignments.pop(segment_id, None)
        self.state.room_assignments.pop(segment_id, None)

    # -- 内部：视图组装 -------------------------------------------------------

    def _proposal_dict(self, proposal: ChangeProposal):
        return {
            "proposal_id": proposal.proposal_id,
            "itinerary_id": proposal.itinerary_id,
            "proposer_id": proposal.proposer_id,
            "reason": proposal.reason,
            "status": proposal.status.value,
            "base_version": proposal.base_version,
            "created_at": proposal.created_at.isoformat() if proposal.created_at else None,
            "decided_at": proposal.decided_at.isoformat() if proposal.decided_at else None,
            "decided_by": proposal.decided_by,
            "decision_note": proposal.decision_note,
            "actions": [self._action_dict(a) for a in proposal.actions],
        }

    def _action_dict(self, action):
        if isinstance(action, CancelSegmentAction):
            return {
                "type": "cancel",
                "segment_id": action.segment_id,
                "notify": list(action.notify),
                "reason": action.reason,
            }
        if isinstance(action, ReplaceSegmentAction):
            return {
                "type": "replace",
                "segment_id": action.segment_id,
                "replacement": self._draft_dict(action.replacement),
                "notify": list(action.notify),
            }
        if isinstance(action, AddSegmentAction):
            return {
                "type": "add",
                "segment": self._draft_dict(action.draft),
                "notify": list(action.notify),
            }
        return {"type": "unknown"}

    def _draft_dict(self, draft: SegmentDraft):
        return {
            "kind": draft.kind.value,
            "supplier": draft.supplier,
            "origin": draft.origin,
            "destination": draft.destination,
            "starts_at": draft.starts_at.isoformat(),
            "ends_at": draft.ends_at.isoformat(),
            "cost": str(draft.cost),
            "currency": draft.currency,
            "member_ids": sorted(draft.member_ids),
            "option_id": draft.option_id,
            "slot_id": draft.slot_id,
            "note": draft.note,
        }

    def _record_dict(self, record: ChangeRecord):
        return {
            "record_id": record.record_id,
            "itinerary_id": record.itinerary_id,
            "segment_id": record.segment_id,
            "action": record.action,
            "old_supplier": record.old_supplier,
            "new_supplier": record.new_supplier,
            "cost_delta": str(record.cost_delta),
            "currency": record.currency,
            "retained_cost": str(record.retained_cost),
            "notify": list(record.notify),
            "proposal_id": record.proposal_id,
            "at": record.at.isoformat(),
            "detail": record.detail,
        }
