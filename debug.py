import asyncio
from datetime import datetime
from app.services.routing import RoutePlanner, BlockedRoad
from app.services.boundary import ChicagoBoundary
from app.services.rail_assets import RailAssetStore
from app.services.otp_client import OTPClient
from app.services.contextual_factors import ContextualFactorsStore
from pathlib import Path

async def main():
    root = Path("/Users/huy/Documents/IT3160/Project IT3160/chicago_transit")
    assets = root / "data" / "assets"
    
    boundary = ChicagoBoundary(assets / "boundary.geojson")
    rail = RailAssetStore(assets / "cta_rail_lines.geojson", assets / "cta_rail_stations.json")
    context = ContextualFactorsStore(assets / "contextual_factors.json", "America/Chicago")
    otp = OTPClient("http://127.0.0.1:8080/otp/gtfs/v1", 20)
    
    planner = RoutePlanner(
        boundary=boundary,
        rail_assets=rail,
        contextual_factors=context,
        otp=otp,
        timezone_name="America/Chicago",
        otp_first_itineraries=3,
        candidate_limit=3
    )

    origin = (41.8781, -87.6298) # Somewhere in loop
    destination = (41.8826, -87.6226) # Millennium park
    depart_at = datetime.now()
    
    print("Fetching route without block...")
    c1 = await planner.plan(origin, destination, "walk", depart_at)
    print("Without block strategy:", c1.summary.selected_strategy)
    
    # We will put a large block right in the middle
    blocks = [{
        "start": {"lat": 41.880, "lon": -87.635},
        "end": {"lat": 41.880, "lon": -87.620},
        "label": "block1",
        "buffer_m": 50
    }]
    
    try:
        print("Fetching route with block...")
        c2 = await planner.plan(origin, destination, "walk", depart_at, blocked_segments=blocks)
        print("With block strategy:", c2.summary.selected_strategy)
        print("Warnings:", c2.warnings)
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
