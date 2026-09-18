"""球队出行领域模型。"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from app.codec import register


def money(value) -> Decimal:
    """统一金额精度到分，避免跨快照出现不同的字符串表示。"""
    return Decimal(str(value)).quantize(Decimal("0.01"))



@register
class Role(str, Enum):
    PLAYER = "player"
    COACH = "coach"
    MEDIC = "medic"
    LEADER = "leader"
    OPS = "ops"



@register
class DocType(str, Enum):
    PASSPORT = "passport"
    VISA = "visa"
    ID_CARD = "id_card"



@register
class SegmentKind(str, Enum):
    FLIGHT = "flight"
    GROUND = "ground"
    HOTEL = "hotel"
    TRAINING = "training"



@register
class SegmentStatus(str, Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REPLACED = "replaced"
    SPLIT = "split"


#: 已终结状态：不可再被任何变更重写
TERMINAL_SEGMENT_STATUSES = frozenset(
    {
        SegmentStatus.COMPLETED,
        SegmentStatus.CANCELLED,
        SegmentStatus.REPLACED,
        SegmentStatus.SPLIT,
    }
)

#: 仍占用资源（座位、房间、班次容量、训练场时段）的状态
ACTIVE_SEGMENT_STATUSES = frozenset(
    {
        SegmentStatus.DRAFT,
        SegmentStatus.CONFIRMED,
        SegmentStatus.IN_PROGRESS,
    }
)



@register
class ItineraryStatus(str, Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"



@register
class ProposalStatus(str, Enum):
    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"
    CONFLICT = "conflict"



@register
class RoomType(str, Enum):
    SINGLE = "single"
    TWIN = "twin"
    SUITE = "suite"



@register
class CostKind(str, Enum):
    BOOKING = "booking"
    ADJUSTMENT = "adjustment"


@register
@dataclass
class Document:
    """证件：类型、号码与有效期。"""

    doc_type: DocType
    number: str
    expires_on: date

    def valid_through(self, on: date) -> bool:
        return self.expires_on >= on


@register
@dataclass
class EmergencyContact:
    """紧急联系人（按角色保护）。"""

    name: str
    phone: str
    relation: str = ""


@register
@dataclass
class Member:
    """球员或工作人员。"""

    member_id: str
    name: str
    role: Role
    documents: list[Document] = field(default_factory=list)
    emergency_contact: EmergencyContact | None = None
    medical_notes: str = ""
    requires_single_room: bool = False
    squad_number: int | None = None


@register
@dataclass
class Vehicle:
    """车辆与座位容量。"""

    vehicle_id: str
    kind: str
    capacity: int
    supplier: str


@register
@dataclass
class Room:
    """酒店房间：房型与床位数。"""

    room_id: str
    hotel: str
    room_type: RoomType
    capacity: int
    supplier: str


@register
@dataclass
class TransportOption:
    """交通班次（航班或地面班次），带容量与成本。"""

    option_id: str
    kind: SegmentKind
    supplier: str
    code: str
    origin: str
    destination: str
    departs_at: datetime
    arrives_at: datetime
    capacity: int
    cost: Decimal
    currency: str = "CNY"


@register
@dataclass
class TrainingSlot:
    """训练场时段。"""

    slot_id: str
    venue: str
    starts_at: datetime
    ends_at: datetime
    supplier: str
    cost: Decimal = Decimal("0.00")
    currency: str = "CNY"


@register
@dataclass
class Match:
    """比赛日程：开球时间与最晚报到时间。"""

    match_id: str
    opponent: str
    venue: str
    kicks_off_at: datetime
    report_by: datetime


@register
@dataclass
class Segment:
    """行程段：可拆分、有状态机、携带时区感知的起止时间。"""

    segment_id: str
    kind: SegmentKind
    supplier: str
    origin: str | None
    destination: str | None
    starts_at: datetime
    ends_at: datetime
    member_ids: set[str] = field(default_factory=set)
    status: SegmentStatus = SegmentStatus.DRAFT
    cost: Decimal = Decimal("0.00")
    currency: str = "CNY"
    option_id: str | None = None
    slot_id: str | None = None
    note: str = ""
    supplier_confirmed: bool = False

    @property
    def duration(self):
        """按绝对时刻计算的时长，跨时区也准确。"""
        return self.ends_at - self.starts_at

    @property
    def crosses_midnight(self) -> bool:
        """出发地与目的地各自本地日期不同即为夜间跨日。"""
        return self.starts_at.date() != self.ends_at.date()

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_SEGMENT_STATUSES


@register
@dataclass
class SegmentDraft:
    """新增或替换行程段时的规格说明。"""

    kind: SegmentKind
    supplier: str
    origin: str | None
    destination: str | None
    starts_at: datetime
    ends_at: datetime
    cost: Decimal = Decimal("0.00")
    currency: str = "CNY"
    member_ids: set[str] = field(default_factory=set)
    option_id: str | None = None
    slot_id: str | None = None
    note: str = ""


@register
@dataclass
class Itinerary:
    """一趟客场行程：若干行程段 + 版本号（乐观并发令牌）。"""

    itinerary_id: str
    name: str
    match_id: str | None
    status: ItineraryStatus
    segments: list[Segment] = field(default_factory=list)
    version: int = 0
    created_at: datetime | None = None


@register
@dataclass
class CancelSegmentAction:
    segment_id: str
    notify: list[str] = field(default_factory=list)
    reason: str = ""


@register
@dataclass
class ReplaceSegmentAction:
    segment_id: str
    replacement: SegmentDraft = None  # type: ignore[assignment]
    notify: list[str] = field(default_factory=list)


@register
@dataclass
class AddSegmentAction:
    draft: SegmentDraft = None  # type: ignore[assignment]
    notify: list[str] = field(default_factory=list)


@register
@dataclass
class ChangeProposal:
    """改签方案：领队提出，运营审核后才生效。"""

    proposal_id: str
    itinerary_id: str
    proposer_id: str
    reason: str
    actions: list = field(default_factory=list)
    base_version: int = 0
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str = ""


@register
@dataclass
class ChangeRecord:
    """变更留痕：供应商、费用差额、通知对象、保留的已发生费用。"""

    record_id: str
    itinerary_id: str
    segment_id: str | None
    action: str
    old_supplier: str | None
    new_supplier: str | None
    cost_delta: Decimal
    currency: str
    notify: list[str]
    retained_cost: Decimal
    proposal_id: str | None
    at: datetime
    detail: str = ""


@register
@dataclass
class CostEntry:
    """费用台账条目：已发生费用保留以便报销。"""

    entry_id: str
    itinerary_id: str
    segment_id: str | None
    supplier: str
    kind: CostKind
    amount: Decimal
    currency: str
    reimbursable: bool
    at: datetime
    note: str = ""


@register
@dataclass
class CallbackReceipt:
    """供应商回调回执：按 callback_id 幂等。"""

    callback_id: str
    supplier: str
    segment_id: str
    event: str
    applied: bool
    at: datetime


@register
@dataclass
class TravelState:
    """服务的全部可持久化状态。"""

    members: dict[str, Member] = field(default_factory=dict)
    vehicles: dict[str, Vehicle] = field(default_factory=dict)
    rooms: dict[str, Room] = field(default_factory=dict)
    transport_options: dict[str, TransportOption] = field(default_factory=dict)
    training_slots: dict[str, TrainingSlot] = field(default_factory=dict)
    matches: dict[str, Match] = field(default_factory=dict)
    itineraries: dict[str, Itinerary] = field(default_factory=dict)
    proposals: dict[str, ChangeProposal] = field(default_factory=dict)
    change_records: dict[str, ChangeRecord] = field(default_factory=dict)
    ledger: list[CostEntry] = field(default_factory=list)
    callbacks: dict[str, CallbackReceipt] = field(default_factory=dict)
    # segment_id -> vehicle_id -> 成员集合
    vehicle_assignments: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    # segment_id -> room_id -> 成员集合
    room_assignments: dict[str, dict[str, set[str]]] = field(default_factory=dict)
