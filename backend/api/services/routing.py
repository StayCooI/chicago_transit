from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from zoneinfo import ZoneInfo

from shapely.geometry import LineString

from backend.api.models import Coordinate, RouteContext, RouteResponse, RouteSegment, RouteSummary, RouteTotals
from backend.api.services.boundary import ChicagoBoundary
from backend.api.services.contextual_factors import ContextualFactorsStore
from backend.api.services.rail_assets import RailAssetStore


LINE_LABELS_VI = {
    "Red": "Tuyến Đỏ",
    "Blue": "Tuyến Xanh Dương",
    "Brn": "Tuyến Nâu",
    "G": "Tuyến Xanh Lá",
    "Org": "Tuyến Cam",
    "P": "Tuyến Tím",
    "Pink": "Tuyến Hồng",
    "Y": "Tuyến Vàng",
}


def localized_line_name(line_id: str | None, fallback: str | None) -> str | None:
    if line_id and line_id in LINE_LABELS_VI:
        return LINE_LABELS_VI[line_id]
    return fallback


def route_id_tail(gtfs_id: str | None) -> str | None:
    if not gtfs_id:
        return None
    return gtfs_id.split(":")[-1]


def to_coordinate(lat: float, lon: float) -> Coordinate:
    return Coordinate(lat=lat, lon=lon)


def dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def blocked_buffer_degrees(meters: float) -> float:
    return float(meters) / 111_000


@dataclass(slots=True)
class NullContextualFactors:
    generated_at: str = ""
    time_profiles: list[dict[str, Any]] = field(default_factory=list)
    congestion_corridors: dict[str, Any] = field(default_factory=lambda: {"type": "FeatureCollection", "features": []})
    hazard_zones: dict[str, Any] = field(default_factory=lambda: {"type": "FeatureCollection", "features": []})

    def evaluate_candidate(self, candidate: Any) -> dict[str, Any]:
        return {
            "traffic_bucket_id": "",
            "traffic_bucket_label": "",
            "context_penalty_sec": 0,
            "traffic_penalty_sec": 0,
            "weather_penalty_sec": 0,
            "congestion_alerts": [],
            "hazard_alerts": [],
            "warning_areas": [],
            "one_way_compliant": True,
        }


@dataclass(slots=True)
class BlockedRoad:
    start: tuple[float, float]
    end: tuple[float, float]
    label: str
    buffer_m: float
    line_coords: list[tuple[float, float]] | None = None

    @property
    def geometry(self) -> LineString:
        if self.line_coords and len(self.line_coords) >= 2:
            return LineString([(lon, lat) for lat, lon in self.line_coords])
        return LineString([(self.start[1], self.start[0]), (self.end[1], self.end[0])])

    @property
    def buffered_geometry(self):
        return self.geometry.buffer(blocked_buffer_degrees(self.buffer_m))


@dataclass(slots=True)
class Candidate:
    profile: Literal["walk"]
    strategy: Literal["walk_only", "walk_rail"]
    segments: list[RouteSegment]
    depart_at: datetime
    arrive_at: datetime
    warnings: list[str] = field(default_factory=list)
    context_penalty_sec: int = 0
    evaluated_sec: int = 0
    traffic_bucket_id: str = ""
    traffic_bucket_label: str = ""
    congestion_alerts: list[str] = field(default_factory=list)
    hazard_alerts: list[str] = field(default_factory=list)
    warning_areas: list[str] = field(default_factory=list)
    one_way_compliant: bool = True
    stop_order_mode: Literal["none", "ordered", "optimize"] = "none"
    stop_order_indices: list[int] = field(default_factory=list)
    blocked_segment_count: int = 0

    @property
    def total_sec(self) -> int:
        return max(0, int((self.arrive_at - self.depart_at).total_seconds()))

    @property
    def total_distance_m(self) -> float:
        return round(sum(float(segment.distance_m or 0) for segment in self.segments), 1)

    def totals(self) -> RouteTotals:
        walk_sec = sum(segment.duration_sec for segment in self.segments if segment.kind == "walk")
        rail_sec = sum(segment.duration_sec for segment in self.segments if segment.kind == "rail")
        walk_distance_m = round(sum(float(segment.distance_m or 0) for segment in self.segments if segment.kind == "walk"), 1)
        rail_distance_m = round(sum(float(segment.distance_m or 0) for segment in self.segments if segment.kind == "rail"), 1)
        return RouteTotals(
            total_sec=self.total_sec,
            walk_sec=walk_sec,
            rail_sec=rail_sec,
            wait_sec=max(0, self.total_sec - walk_sec - rail_sec),
            total_distance_m=round(walk_distance_m + rail_distance_m, 1),
            walk_distance_m=walk_distance_m,
            rail_distance_m=rail_distance_m,
            context_penalty_sec=self.context_penalty_sec,
            evaluated_sec=self.evaluated_sec or (self.total_sec + self.context_penalty_sec),
        )


@dataclass(slots=True)
class RoutePlanner:
    boundary: ChicagoBoundary
    rail_assets: RailAssetStore
    timezone_name: str
    otp_first_itineraries: int
    candidate_limit: int
    contextual_factors: ContextualFactorsStore | NullContextualFactors = field(default_factory=NullContextualFactors)

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    async def plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        profile: Literal["walk"],
        depart_at: datetime | None,
        *,
        stops: list[tuple[float, float]] | None = None,
        stop_order_mode: Literal["none", "ordered", "optimize"] = "none",
        blocked_segments: list[dict[str, Any]] | None = None,
    ) -> RouteResponse:
        depart_local = self._normalize_depart_at(depart_at)
        stops = stops or []
        blocked_roads = self._normalize_blocked_segments(blocked_segments or [])

        all_points = [origin, destination, *stops]
        for point in all_points:
            if not self.boundary.contains(*point):
                raise ValueError("Tất cả các điểm phải nằm trong ranh giới thành phố Chicago.")
                
        import subprocess
        import json
        from pathlib import Path
        from datetime import timedelta
        
        ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
        executable = str(ROOT_DIR / "backend" / "router")
        graph_file = str(ROOT_DIR / "data" / "assets" / "data_graph.txt")
        
        input_data = f"{origin[0]} {origin[1]} {destination[0]} {destination[1]}\n"
        input_data += f"{len(stops)}\n"
        for stop in stops:
            input_data += f"{stop[0]} {stop[1]}\n"
            
        try:
            result = subprocess.run([executable, graph_file], input=input_data, text=True, capture_output=True, check=True)
            output = result.stdout
            data = json.loads(output)
        except Exception as e:
            raise RuntimeError(f"Lỗi khi gọi C++ router: {e}")
            
        if "error" in data:
            raise RuntimeError(data["error"])
            
        segments = []
        path_nodes = data.get("path", [])
        total_dist = data.get("total_cost", 0.0)
        
        if path_nodes:
            coords = [(pt["lon"], pt["lat"]) for pt in path_nodes]
            segment = RouteSegment(
                kind="walk",
                distance_m=total_dist,
                duration_sec=total_dist / 1.3,
                geometry={"type": "LineString", "coordinates": coords},
                instruction="Đi theo lộ trình (A* & GA) tự code bằng C++."
            )
            segments.append(segment)
            
        candidate = Candidate(
            profile=profile,
            strategy="walk",
            segments=segments,
            depart_at=depart_local,
            arrive_at=depart_local + timedelta(seconds=total_dist / 1.3),
            warnings=["Lộ trình được tính toán bằng thuật toán A* và GA tự code (C++)!"],
            stop_order_mode=stop_order_mode,
            stop_order_indices=[],
            blocked_segment_count=len(blocked_roads)
        )
        self._apply_context(candidate)

        return self._serialize(candidate)


    def _normalize_blocked_segments(self, blocked_segments: list[dict[str, Any]]) -> list[BlockedRoad]:
        normalized: list[BlockedRoad] = []
        for index, item in enumerate(blocked_segments, start=1):
            start = item.get("start") or {}
            end = item.get("end") or {}
            label = str(item.get("label") or f"Đoạn đường cấm {index}")
            coords = item.get("geometry", {}).get("coordinates", []) if item.get("geometry") else []
            line_coords = [(float(pt[1]), float(pt[0])) for pt in coords] if coords else None
            normalized.append(
                BlockedRoad(
                    start=(float(start["lat"]), float(start["lon"])),
                    end=(float(end["lat"]), float(end["lon"])),
                    label=label,
                    buffer_m=float(item.get("buffer_m", 35)),
                    line_coords=line_coords,
                )
            )
        return normalized


    def _normalize_depart_at(self, depart_at: datetime | None) -> datetime:
        if depart_at is None:
            return datetime.now(self.timezone)
        if depart_at.tzinfo is None:
            return depart_at.replace(tzinfo=self.timezone)
        return depart_at.astimezone(self.timezone)


    def _apply_context(self, candidate: Candidate) -> None:
        context = self.contextual_factors.evaluate_candidate(candidate)
        candidate.context_penalty_sec = int(context["context_penalty_sec"])
        candidate.evaluated_sec = candidate.total_sec + candidate.context_penalty_sec
        candidate.traffic_bucket_id = str(context["traffic_bucket_id"])
        candidate.traffic_bucket_label = str(context["traffic_bucket_label"])
        candidate.congestion_alerts = list(context["congestion_alerts"])
        candidate.hazard_alerts = list(context["hazard_alerts"])
        candidate.warning_areas = list(context["warning_areas"])
        candidate.one_way_compliant = bool(context["one_way_compliant"])
        candidate.warnings.extend(candidate.congestion_alerts)
        candidate.warnings.extend(candidate.hazard_alerts)
        candidate.warnings = dedupe_keep_order(candidate.warnings)

    def _serialize(self, candidate: Candidate) -> RouteResponse:
        lines_used = []
        for segment in candidate.segments:
            if segment.kind == "rail" and segment.line_name and segment.line_name not in lines_used:
                lines_used.append(segment.line_name)

        description = {
            "walk_only": "Đi bộ toàn tuyến trên mạng đường bộ trong Chicago.",
            "walk_rail": "Kết hợp đi bộ và tàu CTA trong phạm vi Chicago.",
        }[candidate.strategy]

        return RouteResponse(
            summary=RouteSummary(
                profile=candidate.profile,
                selected_strategy=candidate.strategy,
                description=description,
                depart_at=candidate.depart_at,
                arrive_at=candidate.arrive_at,
                lines_used=lines_used,
                stop_order_mode=candidate.stop_order_mode,
                stop_order_indices=candidate.stop_order_indices,
                blocked_segment_count=candidate.blocked_segment_count,
            ),
            totals=candidate.totals(),
            segments=candidate.segments,
            context=RouteContext(
                traffic_bucket_id=candidate.traffic_bucket_id,
                traffic_bucket_label=candidate.traffic_bucket_label,
                one_way_compliant=candidate.one_way_compliant,
                congestion_alerts=candidate.congestion_alerts,
                hazard_alerts=candidate.hazard_alerts,
                warning_areas=candidate.warning_areas,
            ),
            warnings=candidate.warnings,
            data_timestamps={
                "boundary_generated_at": self.boundary.generated_at,
                "rail_generated_at": self.rail_assets.generated_at,
                "context_generated_at": self.contextual_factors.generated_at,
            },
            inside_city=True,
        )

