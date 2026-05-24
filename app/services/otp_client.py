from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import polyline


def _graphql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _decode_geometry(encoded: str | None, fallback_coords: list[list[float]]) -> dict[str, Any]:
    if encoded:
        try:
            decoded = polyline.decode(encoded)
            if decoded:
                return {
                    "type": "LineString",
                    "coordinates": [[lon, lat] for lat, lon in decoded],
                }
        except (TypeError, ValueError):
            pass
    return {"type": "LineString", "coordinates": fallback_coords}


@dataclass(slots=True)
class OTPClient:
    graphql_url: str
    timeout_sec: float

    async def plan_walk_transit(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        depart_at: datetime,
        first: int,
    ) -> list[dict[str, Any]]:
        query = self._build_plan_query(origin, destination, depart_at, first, direct_modes=["WALK"], transit_modes=["SUBWAY"])
        return await self._execute_and_parse(query)

    async def plan_walk_direct(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        depart_at: datetime,
        first: int = 1,
    ) -> list[dict[str, Any]]:
        query = self._build_plan_query(origin, destination, depart_at, first, direct_modes=["WALK"], transit_modes=[])
        return await self._execute_and_parse(query)



    async def _execute_and_parse(self, query: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
            response = await client.post(self.graphql_url, json={"query": query})
            response.raise_for_status()
            payload = response.json()

        if payload.get("errors"):
            raise RuntimeError(str(payload["errors"]))

        edges = payload.get("data", {}).get("planConnection", {}).get("edges", [])
        itineraries: list[dict[str, Any]] = []
        for edge in edges:
            node = edge.get("node") or {}
            legs = []
            for leg in node.get("legs", []):
                from_place = leg.get("from") or {}
                to_place = leg.get("to") or {}
                fallback_coords = [
                    [from_place.get("lon"), from_place.get("lat")],
                    [to_place.get("lon"), to_place.get("lat")],
                ]
                legs.append(
                    {
                        "mode": leg.get("mode"),
                        "duration": int(round(leg.get("duration", 0))),
                        "distance": leg.get("distance"),
                        "from": from_place,
                        "to": to_place,
                        "route": leg.get("route"),
                        "geometry": _decode_geometry((leg.get("legGeometry") or {}).get("points"), fallback_coords),
                    }
                )
            itineraries.append(
                {
                    "start": self._parse_time(node.get("start")),
                    "end": self._parse_time(node.get("end")),
                    "duration": int(round(node.get("duration", 0))),
                    "generalized_cost": node.get("generalizedCost"),
                    "legs": legs,
                }
            )
        return itineraries

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _build_plan_query(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        depart_at: datetime,
        first: int,
        direct_modes: list[str],
        transit_modes: list[str],
    ) -> str:
        direct = ", ".join(direct_modes)
        transit = ", ".join(f"{{ mode: {mode} }}" for mode in transit_modes)
        modes_block = f"direct: [{direct}]"
        if transit_modes:
            modes_block += f"\n          transit: {{ transit: [{transit}] }}"
        else:
            modes_block += "\n          directOnly: true"
        depart_str = _graphql_escape(depart_at.isoformat())
        return f"""
        {{
          planConnection(
            first: {first}
            origin: {{
              location: {{ coordinate: {{ latitude: {origin[0]:.8f}, longitude: {origin[1]:.8f} }} }}
            }}
            destination: {{
              location: {{ coordinate: {{ latitude: {destination[0]:.8f}, longitude: {destination[1]:.8f} }} }}
            }}
            dateTime: {{ earliestDeparture: "{depart_str}" }}
            modes: {{
              {modes_block}
            }}
          ) {{
            edges {{
              node {{
                start
                end
                duration
                generalizedCost
                legs {{
                  mode
                  duration
                  distance
                  from {{
                    name
                    lat
                    lon
                    departure {{
                      scheduledTime
                      estimated {{
                        time
                        delay
                      }}
                    }}
                  }}
                  to {{
                    name
                    lat
                    lon
                    arrival {{
                      scheduledTime
                      estimated {{
                        time
                        delay
                      }}
                    }}
                  }}
                  route {{
                    gtfsId
                    longName
                    shortName
                    mode
                  }}
                  legGeometry {{
                    points
                  }}
                }}
              }}
            }}
          }}
        }}
        """
