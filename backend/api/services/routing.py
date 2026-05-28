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

from backend.api.models import Coordinate, RouteContext, RouteResponse, RouteSegment, RouteSummary, RouteTotals
from backend.api.services.boundary import ChicagoBoundary
from backend.api.services.contextual_factors import ContextualFactorsStore, segment_distance_m, segment_geometry
from backend.api.services.rail_assets import RailAssetStore, haversine_meters


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
        
        executable = str(Path(__file__).resolve().parent.parent.parent / "backend" / "router")
        graph_file = str(Path(__file__).resolve().parent.parent.parent / "data" / "assets" / "data_graph.txt")
        
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
        profile: Literal["walk"],
        depart_at: datetime,
        stop_order_mode: Literal["none", "ordered", "optimize"],
        blocked_roads: list[BlockedRoad],
        leg_cache: dict[tuple[Any, ...], Candidate | None],
    ) -> Candidate | None:
        sequence = [origin, *ordered_stops, destination]
        combined_segments: list[RouteSegment] = []
        combined_warnings: list[str] = []
        
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
                
            current_depart = leg_candidate.arrive_at + time_shift

        strategy = self._derive_strategy(profile, combined_segments)
        candidate = Candidate(
            profile=profile,
            strategy=strategy,
            segments=combined_segments,
            depart_at=depart_at,
            arrive_at=current_depart,
            warnings=dedupe_keep_order(combined_warnings),
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

    async def _heal_candidate(self, candidate: Candidate, blocked_roads: list[BlockedRoad], depart_at: datetime) -> Candidate | None:
        from datetime import timedelta
        import copy
        healed_segments = []
        for segment in candidate.segments:
            if segment.kind != "walk":
                healed_segments.append(segment)
                continue
                
            geom = segment_geometry(segment)
            intersecting_blocks = [br for br in blocked_roads if geom.intersects(br.buffered_geometry)]
            if not intersecting_blocks:
                healed_segments.append(segment)
                continue

            block = intersecting_blocks[0]
            origin = (segment.start.lat, segment.start.lon)
            dest = (segment.end.lat, segment.end.lon)
            segment_time = segment.departure_time or depart_at
            
            all_lats = [pt[0] for br in blocked_roads for pt in (br.start, br.end)]
            all_lons = [pt[1] for br in blocked_roads for pt in (br.start, br.end)]
            
            import asyncio
            healed = False
            for expand in [0.001, 0.003, 0.008]:
                min_lat, max_lat = min(all_lats) - expand, max(all_lats) + expand
                min_lon, max_lon = min(all_lons) - expand, max(all_lons) + expand
                
                WEST_skirt_1 = (origin[0], min_lon - expand)
                WEST_skirt_2 = (dest[0], min_lon - expand)
                EAST_skirt_1 = (origin[0], max_lon + expand)
                EAST_skirt_2 = (dest[0], max_lon + expand)
                NORTH_skirt_1 = (max_lat + expand, origin[1])
                NORTH_skirt_2 = (max_lat + expand, dest[1])
                SOUTH_skirt_1 = (min_lat - expand, origin[1])
                SOUTH_skirt_2 = (min_lat - expand, dest[1])
                
                skirt_paths = [
                    [WEST_skirt_1, WEST_skirt_2],
                    [EAST_skirt_1, EAST_skirt_2],
                    [NORTH_skirt_1, NORTH_skirt_2],
                    [SOUTH_skirt_1, SOUTH_skirt_2],
                ]
                
                for path in skirt_paths:
                    wp1, wp2 = path
                    
                    its_res = await asyncio.gather(
                        self._otp_plan_walk_direct(origin, wp1, segment_time, 1),
                        self._otp_plan_walk_direct(wp1, wp2, segment_time, 1) if wp1 != wp2 else asyncio.sleep(0),
                        self._otp_plan_walk_direct(wp2, dest, segment_time, 1)
                    )
                        
                    c1_its, c2_its, c3_its = its_res
                    if not c1_its or not c3_its or (wp1 != wp2 and not c2_its):
                        continue
                        
                    c1_segs = self._candidate_from_itinerary(candidate.profile, candidate.strategy, c1_its[0]).segments
                    c2_segs = self._candidate_from_itinerary(candidate.profile, candidate.strategy, c2_its[0]).segments if wp1 != wp2 else []
                    c3_segs = self._candidate_from_itinerary(candidate.profile, candidate.strategy, c3_its[0]).segments
                    
                    test_candidate = copy.deepcopy(candidate)
                    test_candidate.segments = c1_segs + c2_segs + c3_segs
                    if self._candidate_avoids_blocked_roads(test_candidate, blocked_roads):
                        healed_segments.extend(test_candidate.segments)
                        healed = True
                        break
                
                if healed:
                    break
            
            if not healed:
                return None
                
        healed_candidate = copy.deepcopy(candidate)
        healed_candidate.segments = healed_segments
        current_time = depart_at
        for seg in healed_candidate.segments:
            seg.departure_time = current_time
            current_time += timedelta(seconds=seg.duration_sec)
            seg.arrival_time = current_time
        healed_candidate.arrive_at = current_time
        healed_candidate.warnings.append("Lộ trình đã được tự động bẻ lái qua các con đường lân cận để tránh đoạn chặn đường.")
        if self._candidate_avoids_blocked_roads(healed_candidate, blocked_roads):
            return healed_candidate
        return None

    async def _plan_point_pair(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        profile: Literal["walk"],
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

        candidates = await self._plan_walk_profile(origin, destination, depart_at, blocked_roads)

        filtered = [candidate for candidate in candidates if self._candidate_stays_inside_city(candidate)]
        
        if not filtered:
            import uuid
            from shapely.geometry import LineString
            
            p1_coords = (origin[0], origin[1])
            p2_coords = (destination[0], destination[1])
            
            fallback_seg = RouteSegment(
                kind="walk",
                start=LocationLabel(lat=p1_coords[0], lon=p1_coords[1], name="Origin"),
                end=LocationLabel(lat=p2_coords[0], lon=p2_coords[1], name="Destination"),
                duration_sec=3600,
                distance_m=1000.0,
                path="x",
                departure_time=depart_at,
                arrival_time=depart_at + timedelta(seconds=3600)
            )
            fallback_seg.set_geometry(LineString([p1_coords, p2_coords]))
            
            fallback_c = Candidate(
                profile=profile,
                strategy="walk_only",
                segments=[fallback_seg],
                total_sec=3600,
                depart_at=depart_at,
                arrive_at=depart_at + timedelta(seconds=3600)
            )
            fallback_c.warnings.append("Toàn bộ ngõ ngách xung quanh đã bị cấm hoặc đường lưới cục bộ không khả dụng, hệ thống chuyển sang chỉ báo định tuyến thẳng tóm lược.")
            filtered.append(fallback_c)
            
        valid_filtered = []
        if blocked_roads:
            for c in filtered:
                if self._candidate_avoids_blocked_roads(c, blocked_roads):
                    valid_filtered.append(c)
            
            if not valid_filtered and filtered:
                for c in filtered:
                    healed = await self._heal_candidate(c, blocked_roads, depart_at)
                    if healed:
                        valid_filtered.append(healed)
                        
                if not valid_filtered:
                    def count_intersections(c: Candidate) -> int:
                        overlaps = 0
                        for seg in c.segments:
                            geom = segment_geometry(seg)
                            overlaps += sum(1 for br in blocked_roads if geom.intersects(br.buffered_geometry))
                        return overlaps
                        
                    best_fallback = min(filtered, key=lambda c: (count_intersections(c), c.total_sec))
                    best_fallback = copy.deepcopy(best_fallback)
                    best_fallback.warnings.append("Cảnh báo: Đoạn đường cấm quá lớn và phức tạp, mạng lưới giao thông xung quanh không có đường vòng tránh tối ưu. Hiển thị lộ trình đi xuyên qua ranh giới.")
                    valid_filtered.append(best_fallback)
                    
            filtered = valid_filtered

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
        blocked_roads: list[BlockedRoad] | None = None,
    ) -> list[Candidate]:
        request_count = max(self.otp_first_itineraries, self.candidate_limit * 2)
        if blocked_roads:
            request_count = min(request_count, 3)
            
        transit_itineraries, direct_walk_itineraries = await asyncio.gather(
            self.otp.plan_walk_transit(origin, destination, depart_at, request_count),
            self._otp_plan_walk_direct(origin, destination, depart_at, request_count),
        )
        candidates = [self._candidate_from_itinerary("walk", "walk_rail", itinerary) for itinerary in transit_itineraries]
        for itinerary in direct_walk_itineraries:
            walk_only = self._candidate_from_itinerary("walk", "walk_only", itinerary)
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

    def _candidate_from_itinerary(
        self,
        profile: Literal["walk"],
        strategy: Literal["walk_only", "walk_rail"],
        itinerary: dict[str, Any],
    ) -> Candidate:
        itinerary_start = itinerary["start"] or datetime.now(self.timezone)
        itinerary_end = itinerary["end"] or itinerary_start
        segments: list[RouteSegment] = []

        for leg in itinerary.get("legs", []):
            route = leg.get("route") or {}
            mode = str(leg.get("mode") or "").upper()
            line_id = route_id_tail(route.get("gtfsId"))
            if route:
                kind: Literal["walk", "rail"] = "rail"
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
                candidate = self._candidate_from_itinerary("walk", "walk_only", walk_itineraries[0])
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
        profile: Literal["walk"],
        segments: list[RouteSegment],
    ) -> Literal["walk_only", "walk_rail"]:
        has_rail = any(segment.kind == "rail" for segment in segments)
        return "walk_rail" if has_rail else "walk_only"

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
