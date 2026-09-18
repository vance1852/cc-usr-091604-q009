"""交通班次、车辆、房间、训练场时段与比赛日程。"""

from app.checks import require_aware
from app.errors import NotFoundError, ValidationError
from app.guards import locked, persisted
from app.models import (
    Match,
    Room,
    RoomType,
    SegmentKind,
    TrainingSlot,
    TransportOption,
    Vehicle,
    money,
)


class InventoryMixin:
    """出行资源库存的登记与查询。"""

    @locked
    @persisted
    def add_vehicle(self, kind, capacity, supplier, vehicle_id=None):
        if capacity < 1:
            raise ValidationError("车辆容量必须为正")
        vehicle_id = vehicle_id or self._new_id("veh")
        if vehicle_id in self.state.vehicles:
            raise ValidationError(f"车辆编号已存在: {vehicle_id}")
        self.state.vehicles[vehicle_id] = Vehicle(
            vehicle_id=vehicle_id, kind=kind, capacity=capacity, supplier=supplier
        )
        return vehicle_id

    @locked
    @persisted
    def add_room(self, hotel, room_type, capacity, supplier, room_id=None):
        if capacity < 1:
            raise ValidationError("房间床位数必须为正")
        room_id = room_id or self._new_id("room")
        if room_id in self.state.rooms:
            raise ValidationError(f"房间编号已存在: {room_id}")
        self.state.rooms[room_id] = Room(
            room_id=room_id,
            hotel=hotel,
            room_type=RoomType(room_type),
            capacity=capacity,
            supplier=supplier,
        )
        return room_id

    @locked
    @persisted
    def add_transport_option(
        self,
        kind,
        supplier,
        code,
        origin,
        destination,
        departs_at,
        arrives_at,
        capacity,
        cost,
        currency="CNY",
        option_id=None,
    ):
        kind = SegmentKind(kind)
        if kind not in (SegmentKind.FLIGHT, SegmentKind.GROUND):
            raise ValidationError("班次类型仅支持航班或地面交通")
        require_aware(departs_at, "departs_at")
        require_aware(arrives_at, "arrives_at")
        if arrives_at <= departs_at:
            raise ValidationError("班次到达时间必须晚于出发时间")
        if capacity < 1:
            raise ValidationError("班次容量必须为正")
        option_id = option_id or self._new_id("opt")
        if option_id in self.state.transport_options:
            raise ValidationError(f"班次编号已存在: {option_id}")
        self.state.transport_options[option_id] = TransportOption(
            option_id=option_id,
            kind=kind,
            supplier=supplier,
            code=code,
            origin=origin,
            destination=destination,
            departs_at=departs_at,
            arrives_at=arrives_at,
            capacity=capacity,
            cost=money(cost),
            currency=currency,
        )
        return option_id

    @locked
    @persisted
    def add_training_slot(self, venue, starts_at, ends_at, supplier, cost=0, slot_id=None):
        require_aware(starts_at, "starts_at")
        require_aware(ends_at, "ends_at")
        if ends_at <= starts_at:
            raise ValidationError("训练场时段结束必须晚于开始")
        slot_id = slot_id or self._new_id("slot")
        if slot_id in self.state.training_slots:
            raise ValidationError(f"训练场时段编号已存在: {slot_id}")
        self.state.training_slots[slot_id] = TrainingSlot(
            slot_id=slot_id,
            venue=venue,
            starts_at=starts_at,
            ends_at=ends_at,
            supplier=supplier,
            cost=money(cost),
        )
        return slot_id

    @locked
    @persisted
    def add_match(self, opponent, venue, kicks_off_at, report_by, match_id=None):
        require_aware(kicks_off_at, "kicks_off_at")
        require_aware(report_by, "report_by")
        if report_by >= kicks_off_at:
            raise ValidationError("最晚报到时间必须早于开球时间")
        match_id = match_id or self._new_id("match")
        if match_id in self.state.matches:
            raise ValidationError(f"比赛编号已存在: {match_id}")
        self.state.matches[match_id] = Match(
            match_id=match_id,
            opponent=opponent,
            venue=venue,
            kicks_off_at=kicks_off_at,
            report_by=report_by,
        )
        return match_id

    # -- 内部查找 ---------------------------------------------------------

    def _vehicle(self, vehicle_id) -> Vehicle:
        vehicle = self.state.vehicles.get(vehicle_id)
        if vehicle is None:
            raise NotFoundError(f"车辆不存在: {vehicle_id}")
        return vehicle

    def _room(self, room_id) -> Room:
        room = self.state.rooms.get(room_id)
        if room is None:
            raise NotFoundError(f"房间不存在: {room_id}")
        return room

    def _option(self, option_id) -> TransportOption:
        option = self.state.transport_options.get(option_id)
        if option is None:
            raise NotFoundError(f"交通班次不存在: {option_id}")
        return option

    def _slot(self, slot_id) -> TrainingSlot:
        slot = self.state.training_slots.get(slot_id)
        if slot is None:
            raise NotFoundError(f"训练场时段不存在: {slot_id}")
        return slot

    def _match(self, match_id) -> Match:
        match = self.state.matches.get(match_id)
        if match is None:
            raise NotFoundError(f"比赛不存在: {match_id}")
        return match
