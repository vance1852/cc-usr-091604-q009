"""费用台账与汇总：已发生费用保留在账上以便报销。"""

from collections import defaultdict
from decimal import Decimal

from app.guards import locked
from app.models import CostEntry, CostKind, SegmentStatus, money


class CostMixin:
    def _add_ledger(
        self, itinerary_id, segment_id, supplier, kind, amount, currency, reimbursable, note=""
    ):
        entry = CostEntry(
            entry_id=self._new_id("cost"),
            itinerary_id=itinerary_id,
            segment_id=segment_id,
            supplier=supplier,
            kind=kind,
            amount=money(amount),
            currency=currency,
            reimbursable=reimbursable,
            at=self._now(),
            note=note,
        )
        self.state.ledger.append(entry)
        return entry

    def _booked_amount(self, segment_id) -> Decimal:
        """某行程段已入账的预订费用（取消/替换后仍保留用于报销）。"""
        total = Decimal("0.00")
        for entry in self.state.ledger:
            if entry.segment_id == segment_id and entry.kind == CostKind.BOOKING:
                total += entry.amount
        return money(total)

    @locked
    def cost_summary(self, itinerary_id=None):
        """费用汇总：预订总额、按供应商、以及已发生待报销金额。"""
        entries = [
            e
            for e in self.state.ledger
            if itinerary_id is None or e.itinerary_id == itinerary_id
        ]
        segment_status = {}
        for itinerary in self.state.itineraries.values():
            for segment in itinerary.segments:
                segment_status[segment.segment_id] = segment.status
        currencies: dict[str, dict] = {}

        def bucket(currency):
            return currencies.setdefault(
                currency,
                {
                    "booked": Decimal("0.00"),
                    "retained_reimbursable": Decimal("0.00"),
                    "by_supplier": defaultdict(lambda: Decimal("0.00")),
                },
            )

        for entry in entries:
            target = bucket(entry.currency)
            if entry.kind == CostKind.BOOKING:
                target["booked"] += entry.amount
                target["by_supplier"][entry.supplier] += entry.amount
                if entry.reimbursable and segment_status.get(entry.segment_id) in (
                    SegmentStatus.CANCELLED,
                    SegmentStatus.REPLACED,
                ):
                    target["retained_reimbursable"] += entry.amount
        return {
            "itinerary_id": itinerary_id,
            "entry_count": len(entries),
            "currencies": {
                currency: {
                    "booked": str(data["booked"]),
                    "retained_reimbursable": str(data["retained_reimbursable"]),
                    "by_supplier": {
                        supplier: str(amount)
                        for supplier, amount in sorted(data["by_supplier"].items())
                    },
                }
                for currency, data in sorted(currencies.items())
            },
        }
