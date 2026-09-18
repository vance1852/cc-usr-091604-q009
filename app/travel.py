"""球队出行领域的对外入口。"""

from dataclasses import dataclass

from app.service import TravelService

__all__ = ["TravelService", "TripSegment"]


@dataclass(frozen=True)
class TripSegment:
    """保存一段行程的起点和终点。"""

    origin: str
    destination: str
