import asyncio
from datetime import datetime
from app.services.routing import RoutePlanner, BlockedRoad
from app.services.otp_client import OTPClient

class MockBoundary: 
    def __init__(self):
        from shapely.geometry import Polygon
        self.geom = Polygon([(-88, 41), (-87, 41), (-87, 42), (-88, 42)])
    def contains(self, lat, lon): return True
class MockRail: pass
class MockContext:
    def evaluate_candidate(self, c): 
        return {
            "traffic_bucket_id": "", "traffic_bucket_label": "", 
            "context_penalty_sec": 0, "traffic_penalty_sec": 0, "weather_penalty_sec": 0,
            "congestion_alerts": [], "hazard_alerts": [], "warning_areas": [], "one_way_compliant": True
        }

async def main():
    otp = OTPClient("http://127.0.0.1:8080/otp/gtfs/v1", 20.0)
    planner = RoutePlanner(MockBoundary(), MockRail(), otp, "America/Chicago")
    planner.contextual_factors = MockContext()
    
    A = (41.815, -87.69)
    B = (41.810, -87.69)
    blocked = [BlockedRoad((41.812, -87.70), (41.812, -87.68), "Test", 35)]
    
    cands = await planner._plan_walk_profile(A, B, datetime.now(), blocked)
    print("Candidates:", len(cands))
    
    best = await planner._plan_point_pair(A, B, "walk", datetime.now(), blocked, {})
    if best:
        print("Point Pair fallback successful, warnings:", best.warnings)
    else:
        print("POINT PAIR COMPLETELY FAILED (returned None)")

asyncio.run(main())
