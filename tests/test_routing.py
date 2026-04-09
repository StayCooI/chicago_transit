from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.routing import RoutePlanner


TZ = ZoneInfo("America/Chicago")


class FakeBoundary:
    generated_at = "2026-04-07T00:00:00+00:00"

    def contains(self, lat: float, lon: float) -> bool:
        return 41.6 <= lat <= 42.1 and -87.9 <= lon <= -87.5


class FakeRailAssets:
    generated_at = "2026-04-07T00:00:00+00:00"
    line_colors = {"Red": "#C60C30", "Blue": "#00A1DE"}
    park_ride_stations = [{"stop_id": "40900", "stop_name": "Howard", "lat": 42.019063, "lon": -87.672892, "route_id": "Red"}]

    def resolve_station(self, stop_name: str, line_id: str | None = None):
        if stop_name == "Howard":
            return {"stop_id": "40900", "stop_name": "Howard", "routes": ["Red"]}
        return None

    def nearest_park_ride_stations(self, lat: float, lon: float, limit: int):
        return self.park_ride_stations[:limit]


class FakeOTP:
    def __init__(self):
        self.calls = []

    async def plan_walk_transit(self, origin, destination, depart_at, first):
        self.calls.append(("walk", origin, destination))
        if origin == (42.019063, -87.672892):
            return [make_itinerary(depart_at, [make_drive_or_walk_leg("WALK", depart_at, 120, "Howard", "Howard"), make_rail_leg("Red", "Howard", "Jackson", depart_at + timedelta(minutes=2), 900)])]
        return [make_itinerary(depart_at, [make_drive_or_walk_leg("WALK", depart_at, 300, "Origin", "Howard"), make_rail_leg("Red", "Howard", "Jackson", depart_at + timedelta(minutes=5), 1200)])]

    async def plan_walk_direct(self, origin, destination, depart_at, first=1):
        self.calls.append(("walk_direct", origin, destination))
        return [make_itinerary(depart_at, [make_drive_or_walk_leg("WALK", depart_at, 4200, "Origin", "Destination")])]

    async def plan_drive_direct(self, origin, destination, depart_at, first=1):
        self.calls.append(("car", origin, destination))
        if destination == (42.019063, -87.672892):
            return [make_itinerary(depart_at, [make_drive_or_walk_leg("CAR", depart_at, 600, "Origin", "Howard")])]
        return [make_itinerary(depart_at, [make_drive_or_walk_leg("CAR", depart_at, 3600, "Origin", "Destination")])]


def make_itinerary(start: datetime, legs: list[dict]):
    current = start
    for leg in legs:
        if not leg["from"]["departure"]["scheduledTime"]:
            leg["from"]["departure"]["scheduledTime"] = current.isoformat()
        current = datetime.fromisoformat(leg["to"]["arrival"]["scheduledTime"])
    return {"start": start, "end": current, "duration": int((current - start).total_seconds()), "legs": legs}


def make_drive_or_walk_leg(mode: str, start: datetime, duration_sec: int, from_name: str, to_name: str):
    end = start + timedelta(seconds=duration_sec)
    return {
        "mode": mode,
        "duration": duration_sec,
        "from": {
            "name": from_name,
            "lat": 41.88,
            "lon": -87.63,
            "departure": {"scheduledTime": start.isoformat(), "estimated": {}},
        },
        "to": {
            "name": to_name,
            "lat": 41.9,
            "lon": -87.62,
            "arrival": {"scheduledTime": end.isoformat(), "estimated": {}},
        },
        "route": None,
        "geometry": {"type": "LineString", "coordinates": [[-87.63, 41.88], [-87.62, 41.9]]},
    }


def make_point_to_point_leg(
    mode: str,
    start: datetime,
    duration_sec: int,
    origin: tuple[float, float],
    destination: tuple[float, float],
    *,
    path: list[list[float]] | None = None,
):
    end = start + timedelta(seconds=duration_sec)
    return {
        "mode": mode,
        "duration": duration_sec,
        "distance": duration_sec,
        "from": {
            "name": "Origin",
            "lat": origin[0],
            "lon": origin[1],
            "departure": {"scheduledTime": start.isoformat(), "estimated": {}},
        },
        "to": {
            "name": "Destination",
            "lat": destination[0],
            "lon": destination[1],
            "arrival": {"scheduledTime": end.isoformat(), "estimated": {}},
        },
        "route": None,
        "geometry": {
            "type": "LineString",
            "coordinates": path or [[origin[1], origin[0]], [destination[1], destination[0]]],
        },
    }


def make_rail_leg(route_id: str, from_name: str, to_name: str, start: datetime, duration_sec: int):
    end = start + timedelta(seconds=duration_sec)
    return {
        "mode": "SUBWAY",
        "duration": duration_sec,
        "from": {
            "name": from_name,
            "lat": 42.019063,
            "lon": -87.672892,
            "departure": {"scheduledTime": start.isoformat(), "estimated": {}},
        },
        "to": {
            "name": to_name,
            "lat": 41.878183,
            "lon": -87.629296,
            "arrival": {"scheduledTime": end.isoformat(), "estimated": {}},
        },
        "route": {"gtfsId": f"CTA:{route_id}", "longName": f"{route_id} Line"},
        "geometry": {"type": "LineString", "coordinates": [[-87.672892, 42.019063], [-87.629296, 41.878183]]},
    }


def build_planner() -> RoutePlanner:
    return RoutePlanner(
        boundary=FakeBoundary(),
        rail_assets=FakeRailAssets(),
        otp=FakeOTP(),
        timezone_name="America/Chicago",
        otp_first_itineraries=3,
        candidate_limit=3,
        park_ride_candidate_limit=3,
    )


def test_walk_profile_has_no_drive_segments():
    planner = build_planner()
    result = asyncio.run(planner.plan((41.88, -87.63), (41.79, -87.6), "walk", datetime(2026, 4, 7, 8, 0, tzinfo=TZ)))
    assert result.summary.selected_strategy == "walk_rail"
    assert all(segment.kind != "drive" for segment in result.segments)


def test_car_profile_prefers_park_and_ride_when_faster():
    planner = build_planner()
    result = asyncio.run(planner.plan((41.88, -87.63), (41.79, -87.6), "car", datetime(2026, 4, 7, 8, 0, tzinfo=TZ)))
    assert result.summary.selected_strategy == "car_park_rail"
    assert result.summary.park_ride_station == "Howard"


def test_outside_city_points_raise_value_error():
    planner = build_planner()
    try:
        asyncio.run(planner.plan((42.4, -87.63), (41.79, -87.6), "walk", datetime(2026, 4, 7, 8, 0, tzinfo=TZ)))
    except ValueError as exc:
        assert "ranh giới thành phố Chicago" in str(exc)
    else:
        raise AssertionError("Expected ValueError for outside-city point.")


class NoTransitOTP(FakeOTP):
    async def plan_walk_transit(self, origin, destination, depart_at, first):
        self.calls.append(("walk", origin, destination))
        return []

    async def plan_walk_direct(self, origin, destination, depart_at, first=1):
        self.calls.append(("walk_direct", origin, destination))
        return [make_itinerary(depart_at, [make_drive_or_walk_leg("WALK", depart_at, 1800, "Origin", "Destination")])]


def test_walk_profile_falls_back_to_walk_only_when_no_transit_route():
    planner = RoutePlanner(
        boundary=FakeBoundary(),
        rail_assets=FakeRailAssets(),
        otp=NoTransitOTP(),
        timezone_name="America/Chicago",
        otp_first_itineraries=3,
        candidate_limit=3,
        park_ride_candidate_limit=3,
    )
    result = asyncio.run(planner.plan((41.88, -87.63), (41.79, -87.6), "walk", datetime(2026, 4, 7, 8, 0, tzinfo=TZ)))
    assert result.summary.selected_strategy == "walk_only"
    assert all(segment.kind == "walk" for segment in result.segments)
    assert result.warnings


class DistanceOnlyOTP(FakeOTP):
    async def plan_walk_transit(self, origin, destination, depart_at, first):
        return []

    async def plan_walk_direct(self, origin, destination, depart_at, first=1):
        lat_delta = abs(origin[0] - destination[0])
        lon_delta = abs(origin[1] - destination[1])
        duration = int((lat_delta + lon_delta) * 120_000)
        return [make_itinerary(depart_at, [make_point_to_point_leg("WALK", depart_at, duration, origin, destination)])]


def test_optimize_stop_order_reorders_stops_for_shorter_total_time():
    planner = RoutePlanner(
        boundary=FakeBoundary(),
        rail_assets=FakeRailAssets(),
        otp=DistanceOnlyOTP(),
        timezone_name="America/Chicago",
        otp_first_itineraries=3,
        candidate_limit=3,
        park_ride_candidate_limit=3,
    )
    result = asyncio.run(
        planner.plan(
            (41.88, -87.63),
            (41.87, -87.63),
            "walk",
            datetime(2026, 4, 7, 8, 0, tzinfo=TZ),
            stops=[(41.871, -87.631), (41.88, -87.70)],
            stop_order_mode="optimize",
        )
    )
    assert result.summary.stop_order_mode == "optimize"
    assert result.summary.stop_order_indices == [1, 0]
    assert result.totals.total_distance_m > 0


class AvoidBlockedOTP(FakeOTP):
    async def plan_walk_transit(self, origin, destination, depart_at, first):
        return []

    async def plan_walk_direct(self, origin, destination, depart_at, first=1):
        blocked_path = [[-87.63, 41.88], [-87.62, 41.89]]
        safe_path = [[-87.64, 41.88], [-87.645, 41.885], [-87.65, 41.89]]
        return [
            make_itinerary(depart_at, [make_point_to_point_leg("WALK", depart_at, 600, origin, destination, path=blocked_path)]),
            make_itinerary(depart_at, [make_point_to_point_leg("WALK", depart_at, 900, origin, destination, path=safe_path)]),
        ]


def test_blocked_segment_filters_out_intersecting_route():
    planner = RoutePlanner(
        boundary=FakeBoundary(),
        rail_assets=FakeRailAssets(),
        otp=AvoidBlockedOTP(),
        timezone_name="America/Chicago",
        otp_first_itineraries=3,
        candidate_limit=3,
        park_ride_candidate_limit=3,
    )
    result = asyncio.run(
        planner.plan(
            (41.88, -87.63),
            (41.89, -87.62),
            "walk",
            datetime(2026, 4, 7, 8, 0, tzinfo=TZ),
            blocked_segments=[
                {
                    "start": {"lat": 41.88, "lon": -87.63},
                    "end": {"lat": 41.89, "lon": -87.62},
                    "label": "Chặn thử nghiệm",
                    "buffer_m": 40,
                }
            ],
        )
    )
    assert result.summary.blocked_segment_count == 1
    assert result.totals.total_sec == 900
    assert "đoạn đường cấm" in " ".join(result.warnings).lower()
