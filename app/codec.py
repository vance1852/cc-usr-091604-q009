"""状态对象的 JSON 序列化编解码。

所有需要持久化的领域对象用 ``@register`` 登记，快照恢复时按类型名找回。
"""

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

_REGISTRY: dict[str, type] = {}


def register(cls: type) -> type:
    """把类登记进编解码表，供反序列化时按名字找回。"""
    _REGISTRY[cls.__name__] = cls
    return cls


def encode(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        # str 枚举必须先于 str 判断，否则会被当成普通字符串
        return {"__enum__": f"{type(value).__name__}:{value.value}"}
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, (set, frozenset)):
        return {"__set__": [encode(v) for v in sorted(value, key=repr)]}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        payload = {f.name: encode(getattr(value, f.name)) for f in fields(value)}
        payload["__type__"] = type(value).__name__
        return payload
    raise TypeError(f"无法序列化类型: {type(value)!r}")


def decode(value: Any) -> Any:
    if isinstance(value, list):
        return [decode(v) for v in value]
    if isinstance(value, dict):
        if "__decimal__" in value:
            return Decimal(value["__decimal__"])
        if "__datetime__" in value:
            return datetime.fromisoformat(value["__datetime__"])
        if "__date__" in value:
            return date.fromisoformat(value["__date__"])
        if "__enum__" in value:
            name, _, raw = value["__enum__"].partition(":")
            return _REGISTRY[name](raw)
        if "__set__" in value:
            return {decode(v) for v in value["__set__"]}
        if "__type__" in value:
            cls = _REGISTRY[value["__type__"]]
            kwargs = {k: decode(v) for k, v in value.items() if k != "__type__"}
            return cls(**kwargs)
        return {k: decode(v) for k, v in value.items()}
    return value
