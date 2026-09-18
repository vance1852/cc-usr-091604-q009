"""行程与行程段：生成、拆分、确认、跨时区与最晚报到时限。"""

from app.checks import require_aware
from app.errors import (
    CapacityError,
    DeadlineError,
    DocumentExpiredError,
    DoubleBookingError,
    NotFoundError,
    SegmentLockedError,
    ValidationError,
)
from app.guards import locked, persisted
from app.models import (
    ACTIVE_SEGMENT_STATUSES,
    CostKind,
    DocType,
    Itinerary,
    ItineraryStatus,
    Role,
    Segment,
    SegmentDraft,
    SegmentKind,
    SegmentStatus,
    money,
)

_TRANSPORT_KINDS = (SegmentKind.FLIGHT, SegmentKind.GROUND)


class ItineraryMixin:
    """行程编排：草稿期可直接编辑，确认后只能走改签审批。"""

    # -- 创建 -------------------------------------------------------------

    @locked
    @persisted
    def create_itinerary(self, name, match_id=None, itinerary_id=None):
        if match_id is not None:
            self._match(match_id)
        itinerary_id = itinerary_id or self._new_id("itn")
        if itinerary_id in self.state.itineraries:
            raise ValidationError(f"行程编号已存在: {itinerary_id}")
        itinerary = Itinerary(
            itinerary_id=itinerary_id,
            name=name,
            match_id=match_id,
            status=ItineraryStatus.DRAFT,
            created_at=self._now(),
        )
        self.state.itineraries[itinerary_id] = itinerary
        return itinerary_id

    @locked
    @persisted
    def add_segment(self, itinerary_id, draft, segment_id=None):
        itinerary = self._itinerary(itinerary_id)
        self._require_itinerary_editable(itinerary)
        segment = self._build_segment(self._coerce_draft(draft), segment_id=segment_id)
        self._validate_new_segment(segment, exclude_segment_id=None)
        itinerary.segments.append(segment)
        itinerary.version += 1
        return segment.segment_id

    @locked
    @persisted
    def add_segment_from_option(self, itinerary_id, option_id, member_ids, segment_id=None):
        itinerary = self._itinerary(itinerary_id)
        self._require_itinerary_editable(itinerary)
        option = self._option(option_id)
        draft = SegmentDraft(
            kind=option.kind,
            supplier=option.supplier,
            origin=option.origin,
            destination=option.destination,
            starts_at=option.departs_at,
            ends_at=option.arrives_at,
            cost=option.cost,
            currency=option.currency,
            member_ids=set(member_ids),
            option_id=option.option_id,
            note=option.code,
        )
        segment = self._build_segment(draft, segment_id=segment_id)
        self._validate_new_segment(segment, exclude_segment_id=None)
        itinerary.segments.append(segment)
        itinerary.version += 1
        return segment.segment_id

    @locked
    @persisted
    def split_segment(self, itinerary_id, segment_id, groups):
        """把一个行程段按成员分组拆成多段，成本按人数比例拆分。"""
        itinerary = self._itinerary(itinerary_id)
        self._require_itinerary_editable(itinerary)
        segment = self._segment(itinerary, segment_id)
        if segment.status != SegmentStatus.DRAFT:
            raise SegmentLockedError("仅草稿状态的行程段可拆分")
        if segment.slot_id:
            raise ValidationError("含训练场时段的行程段不可拆分")
        groups = [set(g) for g in groups]
        if len(groups) < 2:
            raise ValidationError("拆分至少需要两个分组")
        if any(not g for g in groups):
            raise ValidationError("拆分分组不能为空")
        union = set().union(*groups)
        if union != set(segment.member_ids):
            raise ValidationError("拆分分组必须恰好覆盖原行程段的全部成员")
        seen: set[str] = set()
        for group in groups:
            clash = seen & group
            if clash:
                raise ValidationError(f"成员重复出现在多个分组: {sorted(clash)}")
            seen |= group
        total = len(segment.member_ids)
        remaining = segment.cost
        new_ids = []
        for index, group in enumerate(groups):
            if index < len(groups) - 1:
                share = money(segment.cost * len(group) / total)
                remaining -= share
            else:
                share = remaining
            child = Segment(
                segment_id=self._new_id("seg"),
                kind=segment.kind,
                supplier=segment.supplier,
                origin=segment.origin,
                destination=segment.destination,
                starts_at=segment.starts_at,
                ends_at=segment.ends_at,
                member_ids=set(group),
                status=SegmentStatus.DRAFT,
                cost=share,
                currency=segment.currency,
                option_id=segment.option_id,
                note=f"拆分自 {segment.segment_id}",
            )
            itinerary.segments.append(child)
            new_ids.append(child.segment_id)
        segment.status = SegmentStatus.SPLIT
        # 父段终结，释放其占用的车辆/房间，子段重新分配
        self._release_segment_assignments(segment.segment_id)
        itinerary.version += 1
        return new_ids

    # -- 确认 -------------------------------------------------------------

    @locked
    @persisted
    def confirm_itinerary(self, itinerary_id, by_member):
        """确认行程：校验证件、报到时限与资源分配，并登记预订费用。"""
        self._require_role(by_member, Role.LEADER, Role.OPS)
        itinerary = self._itinerary(itinerary_id)
        if itinerary.status != ItineraryStatus.DRAFT:
            raise ValidationError("仅草稿行程可确认")
        pending = [s for s in itinerary.segments if s.status == SegmentStatus.DRAFT]
        if not pending:
            raise ValidationError("行程中没有待确认的行程段")
        self._validate_documents(itinerary)
        self._validate_deadlines(itinerary)
        for segment in pending:
            self._validate_segment_readiness(segment)
        for segment in pending:
            segment.status = SegmentStatus.CONFIRMED
            if segment.cost > 0:
                self._add_ledger(
                    itinerary_id=itinerary.itinerary_id,
                    segment_id=segment.segment_id,
                    supplier=segment.supplier,
                    kind=CostKind.BOOKING,
                    amount=segment.cost,
                    currency=segment.currency,
                    reimbursable=True,
                    note="预订确认",
                )
        itinerary.status = ItineraryStatus.CONFIRMED
        itinerary.version += 1
        return {
            "itinerary_id": itinerary.itinerary_id,
            "confirmed_segments": [s.segment_id for s in pending],
        }

    # -- 视图 -------------------------------------------------------------

    @locked
    def itinerary_view(self, itinerary_id, viewer_id):
        viewer = self._member(viewer_id)
        itinerary = self._itinerary(itinerary_id)
        return {
            "itinerary_id": itinerary.itinerary_id,
            "name": itinerary.name,
            "status": itinerary.status.value,
            "version": itinerary.version,
            "match": self._match_dict(itinerary.match_id) if itinerary.match_id else None,
            "deadlines": self._deadline_dict(itinerary),
            "segments": [
                self._segment_view(segment, viewer) for segment in itinerary.segments
            ],
        }

    @locked
    def member_schedule(self, itinerary_id, member_id):
        """成员视图：只看得到自己参加的行程段与自己的受保护信息。"""
        member = self._member(member_id)
        itinerary = self._itinerary(itinerary_id)
        segments = [
            self._segment_view(s, member)
            for s in itinerary.segments
            if member_id in s.member_ids
        ]
        return {
            "itinerary_id": itinerary.itinerary_id,
            "name": itinerary.name,
            "member": self._profile_dict(member, member),
            "deadlines": self._deadline_dict(itinerary),
            "segments": segments,
        }

    # -- 内部：查找 ---------------------------------------------------------

    def _itinerary(self, itinerary_id) -> Itinerary:
        itinerary = self.state.itineraries.get(itinerary_id)
        if itinerary is None:
            raise NotFoundError(f"行程不存在: {itinerary_id}")
        return itinerary

    def _segment(self, itinerary, segment_id) -> Segment:
        for segment in itinerary.segments:
            if segment.segment_id == segment_id:
                return segment
        raise NotFoundError(f"行程段不存在: {segment_id}")

    def _find_segment(self, segment_id):
        found = self._find_segment_or_none(segment_id)
        if found is None:
            raise NotFoundError(f"行程段不存在: {segment_id}")
        return found

    def _find_segment_or_none(self, segment_id):
        for itinerary in self.state.itineraries.values():
            for segment in itinerary.segments:
                if segment.segment_id == segment_id:
                    return itinerary, segment
        return None

    def _segment_status(self, segment_id):
        found = self._find_segment_or_none(segment_id)
        return found[1].status.value if found else None

    # -- 内部：构造与校验 ---------------------------------------------------

    def _require_itinerary_editable(self, itinerary):
        if itinerary.status != ItineraryStatus.DRAFT:
            raise ValidationError("行程已确认，变更需提交改签方案并由运营审核")

    def _coerce_draft(self, draft) -> SegmentDraft:
        if isinstance(draft, SegmentDraft):
            result = draft
        elif isinstance(draft, dict):
            result = SegmentDraft(
                kind=SegmentKind(draft["kind"]),
                supplier=draft["supplier"],
                origin=draft.get("origin"),
                destination=draft.get("destination"),
                starts_at=draft["starts_at"],
                ends_at=draft["ends_at"],
                cost=money(draft.get("cost", 0)),
                currency=draft.get("currency", "CNY"),
                member_ids=set(draft.get("member_ids") or []),
                option_id=draft.get("option_id"),
                slot_id=draft.get("slot_id"),
                note=draft.get("note", ""),
            )
        else:
            raise ValidationError(f"无法识别的行程段规格: {draft!r}")
        self._validate_draft(result)
        return result

    def _validate_draft(self, draft: SegmentDraft):
        require_aware(draft.starts_at, "starts_at")
        require_aware(draft.ends_at, "ends_at")
        if draft.ends_at <= draft.starts_at:
            raise ValidationError("行程段结束时间必须晚于开始时间")
        for member_id in draft.member_ids:
            self._member(member_id)

    def _build_segment(self, draft: SegmentDraft, segment_id=None, status=SegmentStatus.DRAFT):
        return Segment(
            segment_id=segment_id or self._new_id("seg"),
            kind=draft.kind,
            supplier=draft.supplier,
            origin=draft.origin,
            destination=draft.destination,
            starts_at=draft.starts_at,
            ends_at=draft.ends_at,
            member_ids=set(draft.member_ids),
            status=status,
            cost=money(draft.cost),
            currency=draft.currency,
            option_id=draft.option_id,
            slot_id=draft.slot_id,
            note=draft.note,
        )

    def _validate_new_segment(self, segment, exclude_segment_id):
        if segment.option_id:
            option = self._option(segment.option_id)
            load = self._option_load(
                segment.option_id, exclude_segment_id=exclude_segment_id
            ) + len(segment.member_ids)
            if load > option.capacity:
                raise CapacityError(
                    f"班次 {option.code} 容量不足: 需要 {load}，可用 {option.capacity}"
                )
        if segment.slot_id:
            self._slot(segment.slot_id)
            if self._slot_in_use(segment.slot_id, exclude_segment_id=exclude_segment_id):
                raise DoubleBookingError(f"训练场时段 {segment.slot_id} 已被预订")

    def _option_load(self, option_id, exclude_segment_id=None):
        load = 0
        for itinerary in self.state.itineraries.values():
            for segment in itinerary.segments:
                if (
                    segment.option_id == option_id
                    and segment.status in ACTIVE_SEGMENT_STATUSES
                    and segment.segment_id != exclude_segment_id
                ):
                    load += len(segment.member_ids)
        return load

    def _slot_in_use(self, slot_id, exclude_segment_id=None):
        for itinerary in self.state.itineraries.values():
            for segment in itinerary.segments:
                if (
                    segment.slot_id == slot_id
                    and segment.status in ACTIVE_SEGMENT_STATUSES
                    and segment.segment_id != exclude_segment_id
                ):
                    return True
        return False

    def _validate_documents(self, itinerary, required=(DocType.PASSPORT,)):
        members: set[str] = set()
        last_day = None
        for segment in itinerary.segments:
            if segment.status in ACTIVE_SEGMENT_STATUSES:
                members |= segment.member_ids
                if last_day is None or segment.ends_at.date() > last_day:
                    last_day = segment.ends_at.date()
        if not members:
            return
        problems = []
        for member_id in sorted(members):
            member = self._member(member_id)
            for doc_type in required:
                docs = [d for d in member.documents if d.doc_type == doc_type]
                if not docs:
                    problems.append(f"{member.name} 缺少{doc_type.value}")
                elif all(d.expires_on < last_day for d in docs):
                    problems.append(f"{member.name} 的{doc_type.value}在行程结束前已过期")
        if problems:
            raise DocumentExpiredError("；".join(problems))

    def _deadline_info(self, itinerary):
        report_by = None
        last_arrival = None
        if itinerary.match_id:
            report_by = self._match(itinerary.match_id).report_by
            arrivals = [
                s.ends_at
                for s in itinerary.segments
                if s.kind in _TRANSPORT_KINDS and s.status in ACTIVE_SEGMENT_STATUSES
            ]
            if arrivals:
                last_arrival = max(arrivals)
        ok = report_by is None or last_arrival is None or last_arrival <= report_by
        return {"report_by": report_by, "last_arrival": last_arrival, "ok": ok}

    def _validate_deadlines(self, itinerary):
        info = self._deadline_info(itinerary)
        if not info["ok"]:
            raise DeadlineError(
                f"最晚到达 {info['last_arrival'].isoformat()} "
                f"晚于最晚报到时间 {info['report_by'].isoformat()}"
            )

    def _validate_segment_readiness(self, segment):
        if segment.kind == SegmentKind.GROUND:
            assigned = set()
            for occupants in self.state.vehicle_assignments.get(segment.segment_id, {}).values():
                assigned |= occupants
            missing = sorted(segment.member_ids - assigned)
            if missing:
                raise ValidationError(f"地面交通段缺少车辆分配: {missing}")
        elif segment.kind == SegmentKind.HOTEL:
            assigned = set()
            for occupants in self.state.room_assignments.get(segment.segment_id, {}).values():
                assigned |= occupants
            missing = sorted(segment.member_ids - assigned)
            if missing:
                raise ValidationError(f"住宿段缺少房间分配: {missing}")
        issues = self._segment_assignment_issues(segment)
        if issues:
            raise ValidationError("；".join(issues))

    # -- 内部：视图组装 -----------------------------------------------------

    def _match_dict(self, match_id):
        match = self._match(match_id)
        return {
            "match_id": match.match_id,
            "opponent": match.opponent,
            "venue": match.venue,
            "kicks_off_at": match.kicks_off_at.isoformat(),
            "report_by": match.report_by.isoformat(),
        }

    def _deadline_dict(self, itinerary):
        info = self._deadline_info(itinerary)
        return {
            "report_by": info["report_by"].isoformat() if info["report_by"] else None,
            "last_arrival": (
                info["last_arrival"].isoformat() if info["last_arrival"] else None
            ),
            "ok": info["ok"],
        }

    def _segment_view(self, segment, viewer):
        return {
            "segment_id": segment.segment_id,
            "kind": segment.kind.value,
            "status": segment.status.value,
            "supplier": segment.supplier,
            "origin": segment.origin,
            "destination": segment.destination,
            "starts_at": segment.starts_at.isoformat(),
            "ends_at": segment.ends_at.isoformat(),
            "duration_minutes": int(segment.duration.total_seconds() // 60),
            "crosses_midnight": segment.crosses_midnight,
            "cost": str(segment.cost),
            "currency": segment.currency,
            "supplier_confirmed": segment.supplier_confirmed,
            "members": [
                self._profile_dict(self._member(mid), viewer)
                for mid in sorted(segment.member_ids)
            ],
            "vehicles": {
                vid: sorted(mids)
                for vid, mids in sorted(
                    self.state.vehicle_assignments.get(segment.segment_id, {}).items()
                )
            },
            "rooms": {
                rid: sorted(mids)
                for rid, mids in sorted(
                    self.state.room_assignments.get(segment.segment_id, {}).items()
                )
            },
        }
