from __future__ import annotations

import asyncio
import copy
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import permutations
from typing import Any, Literal

from zoneinfo import ZoneInfo

from shapely.geometry import LineString, Point

from app.models import Coordinate, RouteContext, RouteResponse, RouteSegment, RouteSummary, RouteTotals
from app.services.boundary import ChicagoBoundary
from app.services.contextual_factors import ContextualFactorsStore, segment_distance_m, segment_geometry
from app.services.otp_client import OTPClient
from app.services.rail_assets import RailAssetStore, haversine_meters


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
    profile: Literal["walk", "car"]
    strategy: Literal["walk_only", "walk_rail", "car_only", "car_park_rail"]
    segments: list[RouteSegment]
    depart_at: datetime
    arrive_at: datetime
    warnings: list[str] = field(default_factory=list)
    park_ride_station: str | None = None
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
        drive_sec = sum(segment.duration_sec for segment in self.segments if segment.kind == "drive")
        rail_sec = sum(segment.duration_sec for segment in self.segments if segment.kind == "rail")
        walk_distance_m = round(sum(float(segment.distance_m or 0) for segment in self.segments if segment.kind == "walk"), 1)
        drive_distance_m = round(sum(float(segment.distance_m or 0) for segment in self.segments if segment.kind == "drive"), 1)
        rail_distance_m = round(sum(float(segment.distance_m or 0) for segment in self.segments if segment.kind == "rail"), 1)
        return RouteTotals(
            total_sec=self.total_sec,
            walk_sec=walk_sec,
            drive_sec=drive_sec,
            rail_sec=rail_sec,
            wait_sec=max(0, self.total_sec - walk_sec - drive_sec - rail_sec),
            total_distance_m=round(walk_distance_m + drive_distance_m + rail_distance_m, 1),
            walk_distance_m=walk_distance_m,
            drive_distance_m=drive_distance_m,
            rail_distance_m=rail_distance_m,
            context_penalty_sec=self.context_penalty_sec,
            evaluated_sec=self.evaluated_sec or (self.total_sec + self.context_penalty_sec),
        )


@dataclass(slots=True)
class RoutePlanner:
    boundary: ChicagoBoundary
    rail_assets: RailAssetStore
    otp: OTPClient
    timezone_name: str
    otp_first_itineraries: int
    candidate_limit: int
    park_ride_candidate_limit: int
    contextual_factors: ContextualFactorsStore | NullContextualFactors = field(default_factory=NullContextualFactors)

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    async def plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        profile: Literal["walk", "car"],
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
        for blocked in blocked_roads:
            if not (self.boundary.contains(*blocked.start) and self.boundary.contains(*blocked.end)):
                raise ValueError("Các đoạn đường cấm phải được vẽ hoàn toàn trong ranh giới Chicago.")

        stop_sequences = self._build_stop_sequences(origin, stops, stop_order_mode)
        leg_cache: dict[tuple[Any, ...], Candidate | None] = {}
        best_candidate: Candidate | None = None

        for stop_order_indices in stop_sequences:
            ordered_stops = [stops[index] for index in stop_order_indices]
            candidate = await self._plan_sequence(
                origin,
                destination,
                ordered_stops,
                stop_order_indices,
                profile,
                depart_local,
                stop_order_mode if ordered_stops else "none",
                blocked_roads,
                leg_cache,
            )
            if candidate is None:
                continue
            if best_candidate is None or candidate.evaluated_sec < best_candidate.evaluated_sec:
                best_candidate = candidate

        if best_candidate is None:
            if profile == "walk":
                raise RuntimeError("Không tìm thấy lộ trình đi bộ phù hợp. Hãy thử đổi sang chế độ 'Ô tô + CTA rail' để hệ thống gợi ý trạm Park & Ride nhé.")
            raise RuntimeError("Không tìm thấy lộ trình phù hợp cho kiểu di chuyển và ràng buộc đã chọn.")

        return self._serialize(best_candidate)

    def _build_stop_sequences(
        self,
        origin: tuple[float, float],
        stops: list[tuple[float, float]],
        stop_order_mode: Literal["none", "ordered", "optimize"],
    ) -> list[list[int]]:
        if not stops or stop_order_mode == "none":
            return [[]]
        if stop_order_mode == "ordered":
            if len(stops) > 100:
                raise ValueError("Chế độ giữ nguyên thứ tự chỉ hỗ trợ tối đa 100 điểm dừng.")
            return [list(range(len(stops)))]
        if len(stops) > 50:
            raise ValueError("Chế độ tối ưu thứ tự chỉ hỗ trợ tối đa 50 điểm dừng.")
            
        unvisited = set(range(len(stops)))
        current_point = origin
        sequence: list[int] = []
        
        while unvisited:
            next_stop = min(unvisited, key=lambda idx: haversine_meters(current_point[0], current_point[1], stops[idx][0], stops[idx][1]))
            sequence.append(next_stop)
            current_point = stops[next_stop]
            unvisited.remove(next_stop)
            
        return [sequence]

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

    async def _plan_sequence(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        ordered_stops: list[tuple[float, float]],
        stop_order_indices: list[int],
        profile: Literal["walk", "car"],
        depart_at: datetime,
        stop_order_mode: Literal["none", "ordered", "optimize"],
        blocked_roads: list[BlockedRoad],
        leg_cache: dict[tuple[Any, ...], Candidate | None],
    ) -> Candidate | None:
        sequence = [origin, *ordered_stops, destination]
        combined_segments: list[RouteSegment] = []
        combined_warnings: list[str] = []
        park_ride_labels: list[str] = []
        
        pairs = list(zip(sequence, sequence[1:]))
        semaphore = asyncio.Semaphore(50)
        
        async def fetch_leg(leg_origin: tuple[float, float], leg_destination: tuple[float, float], idx: int) -> tuple[int, Candidate | None]:
            async with semaphore:
                candidate = await self._plan_point_pair(
                    leg_origin,
                    leg_destination,
                    profile,
                    depart_at,
                    blocked_roads,
                    leg_cache,
                )
                return idx, candidate

        leg_results = await asyncio.gather(*(fetch_leg(start, end, i) for i, (start, end) in enumerate(pairs)))
        leg_results.sort(key=lambda x: x[0])
        
        current_depart = depart_at
        
        for _, leg_candidate in leg_results:
            if leg_candidate is None:
                return None
            
            time_shift = current_depart - leg_candidate.depart_at
            
            for segment in leg_candidate.segments:
                if segment.departure_time:
                    segment.departure_time += time_shift
                if segment.arrival_time:
                    segment.arrival_time += time_shift
            
            combined_segments.extend(copy.deepcopy(leg_candidate.segments))
            combined_warnings.extend(leg_candidate.warnings)
            if leg_candidate.park_ride_station:
                park_ride_labels.append(leg_candidate.park_ride_station)
                
            current_depart = leg_candidate.arrive_at + time_shift

        strategy = self._derive_strategy(profile, combined_segments)
        candidate = Candidate(
            profile=profile,
            strategy=strategy,
            segments=combined_segments,
            depart_at=depart_at,
            arrive_at=current_depart,
            warnings=dedupe_keep_order(combined_warnings),
            park_ride_station=", ".join(dedupe_keep_order(park_ride_labels)) or None,
            stop_order_mode=stop_order_mode,
            stop_order_indices=list(stop_order_indices),
            blocked_segment_count=len(blocked_roads),
        )
        self._apply_context(candidate)

        if blocked_roads:
            candidate.warnings.append(f"Đã áp dụng {len(blocked_roads)} đoạn đường cấm do người dùng chọn.")
        if ordered_stops:
            if stop_order_mode == "ordered":
                candidate.warnings.append("Lộ trình nhiều điểm dừng đang tuân theo đúng thứ tự đã nhập.")
            else:
                order_text = " → ".join(str(index + 1) for index in stop_order_indices)
                candidate.warnings.append(f"Hệ thống đã tối ưu thứ tự điểm dừng: {order_text}.")
        candidate.warnings = dedupe_keep_order(candidate.warnings)
        return candidate

    async def _plan_point_pair(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        profile: Literal["walk", "car"],
        depart_at: datetime,
        blocked_roads: list[BlockedRoad],
        leg_cache: dict[tuple[Any, ...], Candidate | None],
    ) -> Candidate | None:
        cache_key = (
            round(origin[0], 6),
            round(origin[1], 6),
            round(destination[0], 6),
            round(destination[1], 6),
            profile,
            depart_at.isoformat(timespec="minutes"),
            tuple(
                (
                    round(road.start[0], 6),
                    round(road.start[1], 6),
                    round(road.end[0], 6),
                    round(road.end[1], 6),
                    round(road.buffer_m, 1),
                )
                for road in blocked_roads
            ),
        )
        if cache_key in leg_cache:
            cached = leg_cache[cache_key]
            return copy.deepcopy(cached) if cached is not None else None

        if profile == "walk":
            candidates = await self._plan_walk_profile(origin, destination, depart_at)
        else:
            candidates = await self._plan_car_profile(origin, destination, depart_at)

        filtered = [candidate for candidate in candidates if self._candidate_stays_inside_city(candidate)]
        if blocked_roads:
            filtered = [candidate for candidate in filtered if self._candidate_avoids_blocked_roads(candidate, blocked_roads)]

        for candidate in filtered:
            self._apply_context(candidate)

        filtered.sort(key=lambda item: (item.evaluated_sec, item.total_sec))
        best = filtered[0] if filtered else None
        leg_cache[cache_key] = copy.deepcopy(best) if best is not None else None
        return copy.deepcopy(best) if best is not None else None

    def _normalize_depart_at(self, depart_at: datetime | None) -> datetime:
        if depart_at is None:
            return datetime.now(self.timezone)
        if depart_at.tzinfo is None:
            return depart_at.replace(tzinfo=self.timezone)
        return depart_at.astimezone(self.timezone)

    async def _plan_walk_profile(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        depart_at: datetime,
    ) -> list[Candidate]:
        request_count = max(self.otp_first_itineraries, self.candidate_limit * 2)
        transit_itineraries, direct_walk_itineraries = await asyncio.gather(
            self.otp.plan_walk_transit(origin, destination, depart_at, request_count),
            self._otp_plan_walk_direct(origin, destination, depart_at, max(2, self.candidate_limit)),
        )
        candidates = [self._candidate_from_itinerary("walk", "walk_rail", itinerary, None) for itinerary in transit_itineraries]
        for itinerary in direct_walk_itineraries:
            walk_only = self._candidate_from_itinerary("walk", "walk_only", itinerary, None)
            if all(segment.kind == "walk" for segment in walk_only.segments):
                if not transit_itineraries:
                    walk_only.warnings.append(
                        "Không tìm thấy phương án kết hợp tàu CTA phù hợp, hệ thống chuyển sang đi bộ toàn tuyến."
                    )
                candidates.append(walk_only)
        if not any(candidate.strategy == "walk_only" for candidate in candidates):
            chunked_walk = await self._plan_chunked_walk(origin, destination, depart_at, had_transit=bool(transit_itineraries))
            if chunked_walk is not None:
                candidates.append(chunked_walk)
        candidates.sort(key=lambda item: item.total_sec)
        return candidates[: max(self.candidate_limit, 1)]

    async def _plan_car_profile(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        depart_at: datetime,
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        direct_itineraries = await self._otp_plan_drive_direct(origin, destination, depart_at, max(2, self.candidate_limit))
        for itinerary in direct_itineraries:
            candidates.append(self._candidate_from_itinerary("car", "car_only", itinerary, None))

        for station in self.rail_assets.nearest_park_ride_stations(origin[0], origin[1], self.park_ride_candidate_limit):
            drive_itineraries = await self._otp_plan_drive_direct(
                origin,
                (station["lat"], station["lon"]),
                depart_at,
                max(2, self.candidate_limit),
            )
            if not drive_itineraries:
                continue

            for drive_itinerary in drive_itineraries:
                drive_candidate = self._candidate_from_itinerary("car", "car_only", drive_itinerary, None)
                transit_depart = drive_candidate.arrive_at + timedelta(minutes=2)
                transit_itineraries = await self.otp.plan_walk_transit(
                    (station["lat"], station["lon"]),
                    destination,
                    transit_depart,
                    min(2, self.otp_first_itineraries),
                )
                for transit_itinerary in transit_itineraries:
                    transit_candidate = self._candidate_from_itinerary(
                        "car",
                        "car_park_rail",
                        transit_itinerary,
                        station["stop_name"],
                    )
                    combined = Candidate(
                        profile="car",
                        strategy="car_park_rail",
                        segments=drive_candidate.segments + transit_candidate.segments,
                        depart_at=drive_candidate.depart_at,
                        arrive_at=transit_candidate.arrive_at,
                        park_ride_station=station["stop_name"],
                    )
                    candidates.append(combined)

        candidates.sort(key=lambda item: item.total_sec)
        return candidates[: max(self.candidate_limit, 1)]

    def _candidate_from_itinerary(
        self,
        profile: Literal["walk", "car"],
        strategy: Literal["walk_only", "walk_rail", "car_only", "car_park_rail"],
        itinerary: dict[str, Any],
        park_ride_station: str | None,
    ) -> Candidate:
        itinerary_start = itinerary["start"] or datetime.now(self.timezone)
        itinerary_end = itinerary["end"] or itinerary_start
        segments: list[RouteSegment] = []

        for leg in itinerary.get("legs", []):
            route = leg.get("route") or {}
            mode = str(leg.get("mode") or "").upper()
            line_id = route_id_tail(route.get("gtfsId"))
            if route:
                kind: Literal["walk", "drive", "rail"] = "rail"
            elif mode == "CAR":
                kind = "drive"
            else:
                kind = "walk"

            start_time = self._pick_leg_time(leg.get("from"), "departure")
            end_time = self._pick_leg_time(leg.get("to"), "arrival")
            from_name = (leg.get("from") or {}).get("name")
            to_name = (leg.get("to") or {}).get("name")
            station = self.rail_assets.resolve_station(from_name, line_id) if kind == "rail" and from_name else None
            line_color = self.rail_assets.line_colors.get(line_id or "", "").upper() or None
            line_name = localized_line_name(line_id, route.get("longName"))
            distance_m = float(leg.get("distance") or 0)
            if distance_m <= 0:
                distance_m = segment_distance_m(type("TempSeg", (), {
                    "geometry": leg["geometry"],
                    "start": to_coordinate((leg.get("from") or {}).get("lat"), (leg.get("from") or {}).get("lon")),
                    "end": to_coordinate((leg.get("to") or {}).get("lat"), (leg.get("to") or {}).get("lon")),
                    "distance_m": 0,
                })())

            segments.append(
                RouteSegment(
                    kind=kind,
                    duration_sec=int(leg.get("duration", 0)),
                    distance_m=round(distance_m, 1),
                    geometry=leg["geometry"],
                    start=to_coordinate((leg.get("from") or {}).get("lat"), (leg.get("from") or {}).get("lon")),
                    end=to_coordinate((leg.get("to") or {}).get("lat"), (leg.get("to") or {}).get("lon")),
                    from_name=from_name,
                    to_name=to_name,
                    departure_time=start_time,
                    arrival_time=end_time,
                    line_id=line_id,
                    line_name=line_name,
                    line_color=f"#{line_color}" if line_color and not line_color.startswith("#") else line_color,
                    station_id=station["stop_id"] if station else None,
                )
            )

        return Candidate(
            profile=profile,
            strategy=strategy,
            segments=segments,
            depart_at=itinerary_start,
            arrive_at=itinerary_end,
            park_ride_station=park_ride_station,
        )

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
            "car_only": "Lái xe trực tiếp trên mạng đường bộ trong Chicago.",
            "car_park_rail": "Lái xe đến ga CTA có bãi gửi xe chính thức rồi tiếp tục bằng tàu CTA.",
        }[candidate.strategy]

        return RouteResponse(
            summary=RouteSummary(
                profile=candidate.profile,
                selected_strategy=candidate.strategy,
                description=description,
                depart_at=candidate.depart_at,
                arrive_at=candidate.arrive_at,
                park_ride_station=candidate.park_ride_station,
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

    async def _plan_chunked_walk(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        depart_at: datetime,
        *,
        had_transit: bool,
    ) -> Candidate | None:
        distance_m = haversine_meters(origin[0], origin[1], destination[0], destination[1])
        if distance_m < 1:
            return None

        for max_chunk_m in (3_000, 2_000, 1_500):
            segment_count = max(2, math.ceil(distance_m / max_chunk_m))
            waypoints = [
                (
                    origin[0] + (destination[0] - origin[0]) * (index / segment_count),
                    origin[1] + (destination[1] - origin[1]) * (index / segment_count),
                )
                for index in range(segment_count + 1)
            ]
            if any(not self.boundary.contains(lat, lon) for lat, lon in waypoints):
                continue

            current_depart = depart_at
            combined_segments: list[RouteSegment] = []
            failed = False

            for start_point, end_point in zip(waypoints, waypoints[1:]):
                itineraries = await self._otp_plan_walk_direct(start_point, end_point, current_depart, max(2, self.candidate_limit))
                walk_itineraries = [itinerary for itinerary in itineraries]
                if not walk_itineraries:
                    failed = True
                    break
                candidate = self._candidate_from_itinerary("walk", "walk_only", walk_itineraries[0], None)
                if not candidate.segments or any(segment.kind != "walk" for segment in candidate.segments):
                    failed = True
                    break
                combined_segments.extend(candidate.segments)
                current_depart = candidate.arrive_at

            if failed or not combined_segments:
                continue

            warnings: list[str] = ["Lộ trình đi bộ được ghép từ nhiều chặng ngắn để bao phủ quãng đường dài trong Chicago."]
            if not had_transit:
                warnings.insert(0, "Không tìm thấy phương án kết hợp tàu CTA phù hợp, hệ thống chuyển sang đi bộ toàn tuyến.")
            return Candidate(
                profile="walk",
                strategy="walk_only",
                segments=combined_segments,
                depart_at=depart_at,
                arrive_at=current_depart,
                warnings=warnings,
            )

        return None

    def _candidate_stays_inside_city(self, candidate: Candidate) -> bool:
        city = getattr(self.boundary, "geometry", None)
        if city is None:
            return True
        tolerance = 1e-4
        for segment in candidate.segments:
            coords = (segment.geometry or {}).get("coordinates") or []
            if len(coords) < 2:
                point = Point(segment.start.lon, segment.start.lat)
                if not (city.buffer(tolerance).contains(point) or city.touches(point)):
                    return False
                continue
            line = LineString(coords)
            if not city.buffer(tolerance).covers(line):
                return False
        return True

    def _candidate_avoids_blocked_roads(self, candidate: Candidate, blocked_roads: list[BlockedRoad]) -> bool:
        if not blocked_roads:
            return True
        for segment in candidate.segments:
            geometry = segment_geometry(segment)
            for blocked in blocked_roads:
                if geometry.intersects(blocked.buffered_geometry):
                    return False
        return True

    def _derive_strategy(
        self,
        profile: Literal["walk", "car"],
        segments: list[RouteSegment],
    ) -> Literal["walk_only", "walk_rail", "car_only", "car_park_rail"]:
        has_rail = any(segment.kind == "rail" for segment in segments)
        if profile == "walk":
            return "walk_rail" if has_rail else "walk_only"
        return "car_park_rail" if has_rail else "car_only"

    async def _otp_plan_walk_direct(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        depart_at: datetime,
        first: int,
    ) -> list[dict[str, Any]]:
        try:
            return await self.otp.plan_walk_direct(origin, destination, depart_at, first)
        except TypeError:
            return await self.otp.plan_walk_direct(origin, destination, depart_at)

    async def _otp_plan_drive_direct(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        depart_at: datetime,
        first: int,
    ) -> list[dict[str, Any]]:
        try:
            return await self.otp.plan_drive_direct(origin, destination, depart_at, first)
        except TypeError:
            return await self.otp.plan_drive_direct(origin, destination, depart_at)

    @staticmethod
    def _pick_leg_time(place: dict[str, Any] | None, field: str) -> datetime | None:
        if not place:
            return None
        data = place.get(field) or {}
        estimated = data.get("estimated") or {}
        preferred = estimated.get("time") or data.get("scheduledTime")
        if not preferred:
            return None
        return datetime.fromisoformat(str(preferred).replace("Z", "+00:00"))
