"""球队出行编排服务入口。"""

import threading
import uuid
from datetime import datetime, timezone

from app.assignments import AssignmentMixin
from app.callbacks import CallbackMixin
from app.changes import ChangeMixin
from app.costs import CostMixin
from app.inventory import InventoryMixin
from app.itinerary import ItineraryMixin
from app.models import TravelState
from app.recovery import RecoveryMixin
from app.roster import RosterMixin
from app.store import save_snapshot


class TravelService(
    RosterMixin,
    InventoryMixin,
    ItineraryMixin,
    AssignmentMixin,
    ChangeMixin,
    CallbackMixin,
    CostMixin,
    RecoveryMixin,
):
    """职业足球队客场出行编排服务。

    一个实例持有一份完整状态；所有公开变更方法在内部可重入锁下串行执行，
    配置 ``storage_path`` 后每次成功变更都会原子写入快照，可用
    :meth:`recover` 在重启后恢复并校验资源一致性。
    """

    def __init__(self, storage_path=None, now=None):
        self.state = TravelState()
        self._lock = threading.RLock()
        self._storage_path = storage_path
        self._now_fn = now or (lambda: datetime.now(timezone.utc))

    # -- 基础工具 -----------------------------------------------------------

    def health(self):
        return {"service": "travel", "status": "ok"}

    def _now(self):
        return self._now_fn()

    def _new_id(self, prefix):
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def _persist(self):
        if self._storage_path:
            save_snapshot(self.state, self._storage_path)
