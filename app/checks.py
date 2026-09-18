"""共享入参校验。"""

from datetime import datetime

from app.errors import ValidationError


def require_aware(value: datetime, field: str) -> datetime:
    """跨时区行程必须携带时区信息，拒绝朴素时间。"""
    if not isinstance(value, datetime):
        raise ValidationError(f"{field} 必须是日期时间")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} 必须携带时区信息")
    return value
