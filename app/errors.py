"""领域异常定义。"""


class TravelError(Exception):
    """系统内所有领域异常的基类。"""


class NotFoundError(TravelError):
    """引用的实体不存在。"""


class NotAuthorizedError(TravelError):
    """当前角色无权执行该操作。"""


class ValidationError(TravelError):
    """输入或状态不满足业务规则。"""


class CapacityError(ValidationError):
    """车辆、房间或班次容量不足。"""


class DoubleBookingError(ValidationError):
    """资源被重复占用（车辆座位、房间、训练场时段）。"""


class SegmentLockedError(ValidationError):
    """行程段已终结（完成/取消/被替换/被拆分），不可重写。"""


class ConflictError(TravelError):
    """乐观并发冲突：行程在方案提交后已被其他变更修改。"""


class DocumentExpiredError(ValidationError):
    """成员证件在行程结束前已过期或缺失。"""


class DeadlineError(ValidationError):
    """到达时间晚于最晚报到时间。"""


class ConsistencyError(TravelError):
    """快照恢复后资源一致性校验失败。"""
