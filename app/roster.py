"""名单、证件与按角色保护的个人信息。"""

from app.errors import NotAuthorizedError, NotFoundError, ValidationError
from app.guards import locked, persisted
from app.models import DocType, Document, EmergencyContact, Member, Role

#: 可查看紧急联系人与证件号码的角色
_PRIVILEGED_ROLES = (Role.MEDIC, Role.LEADER, Role.OPS)


class RosterMixin:
    """球员与工作人员名单、证件有效期、受保护信息的读写。"""

    # -- 写入 -------------------------------------------------------------

    @locked
    @persisted
    def add_member(self, name, role, member_id=None, squad_number=None):
        from app.errors import ValidationError

        role = Role(role)
        member_id = member_id or self._new_id("mem")
        if member_id in self.state.members:
            raise ValidationError(f"成员编号已存在: {member_id}")
        self.state.members[member_id] = Member(
            member_id=member_id, name=name, role=role, squad_number=squad_number
        )
        return member_id

    @locked
    @persisted
    def add_document(self, member_id, doc_type, number, expires_on):
        member = self._member(member_id)
        doc = Document(doc_type=DocType(doc_type), number=number, expires_on=expires_on)
        member.documents.append(doc)
        return doc

    @locked
    @persisted
    def set_emergency_contact(self, member_id, name, phone, relation="", by_member=None):
        self._require_role(by_member, Role.LEADER, Role.OPS)
        member = self._member(member_id)
        member.emergency_contact = EmergencyContact(name=name, phone=phone, relation=relation)

    @locked
    @persisted
    def set_medical_notes(self, member_id, notes, by_member):
        """医疗备注仅队医可写。"""
        self._require_role(by_member, Role.MEDIC)
        self._member(member_id).medical_notes = notes

    @locked
    @persisted
    def set_single_room_requirement(self, member_id, required, by_member):
        """队医标记伤员需要单独房间。"""
        self._require_role(by_member, Role.MEDIC)
        self._member(member_id).requires_single_room = bool(required)

    # -- 读取 -------------------------------------------------------------

    @locked
    def member_profile(self, member_id, viewer_id):
        viewer = self._member(viewer_id)
        target = self._member(member_id)
        return self._profile_dict(target, viewer)

    @locked
    def roster(self, viewer_id):
        viewer = self._member(viewer_id)
        return [
            self._profile_dict(m, viewer)
            for m in sorted(self.state.members.values(), key=lambda m: m.member_id)
        ]

    # -- 内部 -------------------------------------------------------------

    def _member(self, member_id) -> Member:
        member = self.state.members.get(member_id)
        if member is None:
            raise NotFoundError(f"成员不存在: {member_id}")
        return member

    def _require_role(self, member_id, *roles) -> Member:
        member = self._member(member_id)
        if member.role not in roles:
            allowed = "、".join(r.value for r in roles)
            raise NotAuthorizedError(
                f"{member.name}({member.role.value}) 无权执行该操作，需要角色: {allowed}"
            )
        return member

    def _profile_dict(self, target: Member, viewer: Member) -> dict:
        """按查看者角色过滤紧急联系人、医疗备注与证件号码。"""
        privileged = viewer.role in _PRIVILEGED_ROLES
        self_view = viewer.member_id == target.member_id
        today = self._now().date()
        documents = [
            {
                "doc_type": doc.doc_type.value,
                "number": doc.number if (privileged or self_view) else None,
                "expires_on": doc.expires_on.isoformat(),
                "valid": doc.expires_on >= today,
            }
            for doc in target.documents
        ]
        contact = target.emergency_contact
        return {
            "member_id": target.member_id,
            "name": target.name,
            "role": target.role.value,
            "squad_number": target.squad_number,
            "documents": documents,
            "emergency_contact": (
                {"name": contact.name, "phone": contact.phone, "relation": contact.relation}
                if contact and (privileged or self_view)
                else None
            ),
            "medical_notes": (
                target.medical_notes if (viewer.role == Role.MEDIC or self_view) else None
            ),
            "requires_single_room": (
                target.requires_single_room if (privileged or self_view) else None
            ),
        }
