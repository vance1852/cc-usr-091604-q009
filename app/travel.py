"""球队出行领域的最小起点。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TripSegment:
    """保存一段行程的起点和终点。"""

    origin: str
    destination: str


class TravelService:
    """提供出行服务的基础健康状态。"""

    def health(self) -> dict[str, str]:
        return {"service": "travel", "status": "ok"}

