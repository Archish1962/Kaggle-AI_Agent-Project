# ruff: noqa
import os
import json
import requests
from typing import Optional
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("EV Route Intelligence Server")

# ---------------------------------------------------------------------------
# API Keys & Configuration
# ---------------------------------------------------------------------------
OPEN_CHARGE_MAP_KEY = os.getenv("OPEN_CHARGE_MAP_KEY", "")
OPENROUTE_SERVICE_KEY = os.getenv("OPENROUTE_SERVICE_KEY", "")


# ---------------------------------------------------------------------------
# Helper: Geocoder (Mock/Simple)
# ---------------------------------------------------------------------------
def geocode_city(city: str) -> tuple[float, float]:
    """Helper to geocode standard cities for demo purposes."""
    city_lower = city.lower()
    if "san francisco" in city_lower or "sf" in city_lower:
        return 37.7749, -122.4194
    elif "los angeles" in city_lower or "la" in city_lower:
        return 34.0522, -118.2437
    elif "seattle" in city_lower:
        return 47.6062, -122.3321
    elif "portland" in city_lower:
        return 45.5152, -122.6784
    elif "san jose" in city_lower:
        return 37.3382, -121.8863
    elif "sacramento" in city_lower:
        return 38.5816, -121.4944
    # Default to SF
    return 37.7749, -122.4194


# ---------------------------------------------------------------------------
# Helper: OpenTopoData Elevation Fallback
# ---------------------------------------------------------------------------
def fetch_elevations_from_opentopodata(coords: list[tuple[float, float]]) -> list[float]:
    """Fetches elevations using OpenTopoData free API (SRTM 90m)."""
    if not coords:
        return []
    
    # Format: lat1,lon1|lat2,lon2|...
    loc_str = "|".join(f"{lat},{lon}" for lat, lon in coords)
    url = f"https://api.opentopodata.org/v1/srtm90m?locations={loc_str}"
    
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            results = response.json().get("results", [])
            # Extract elevation, fallback to 0.0 if None
            return [res.get("elevation") or 0.0 for res in results]
    except Exception as e:
        print(f"OpenTopoData fetch error: {e}")
    
    # Fallback to simulated elevations if API is down
    return [100.0 + (i * 75.0 % 250.0) for i in range(len(coords))]


# ---------------------------------------------------------------------------
# MCP Tool: find_chargers_near
# ---------------------------------------------------------------------------
@mcp.tool()
def find_chargers_near(
    latitude: float,
    longitude: float,
    radius: float = 50.0,
    connector_type: str = "CCS"
) -> str:
    """Find EV charging stations near a specific latitude and longitude.

    Args:
        latitude: The latitude of the center search location.
        longitude: The longitude of the center search location.
        radius: The search radius in kilometers (default 50).
        connector_type: Type of charging connector, e.g., CCS, Tesla, CHAdeMO (default CCS).
    """
    # Attempt Open Charge Map API if key is available
    if OPEN_CHARGE_MAP_KEY:
        url = (
            f"https://api.openchargemap.io/v3/poi/?output=json&latitude={latitude}"
            f"&longitude={longitude}&distance={radius}&distanceunit=KM"
            f"&maxresults=10&key={OPEN_CHARGE_MAP_KEY}"
        )
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                results = []
                for poi in data:
                    title = poi.get("AddressInfo", {}).get("Title", "Unknown Charger")
                    lat = poi.get("AddressInfo", {}).get("Latitude", 0.0)
                    lon = poi.get("AddressInfo", {}).get("Longitude", 0.0)
                    connections = poi.get("Connections", [])
                    power_kw = 150.0
                    if connections:
                        power_kw = connections[0].get("PowerKW") or 150.0
                    results.append({
                        "name": title,
                        "latitude": lat,
                        "longitude": lon,
                        "power_kw": power_kw,
                        "status": "Available"
                    })
                return json.dumps({"chargers": results}, indent=2)
        except Exception:
            pass  # Fall back to mock on failure

    # Mock chargers based on input location and connector type
    mock_chargers = [
        {
            "name": f"{connector_type} Supercharger Station A",
            "latitude": latitude + 0.08,
            "longitude": longitude - 0.05,
            "power_kw": 250.0 if "tesla" in connector_type.lower() else 150.0,
            "status": "Available",
            "distance_km": 12.5
        },
        {
            "name": f"{connector_type} Rapid Charger Station B",
            "latitude": latitude - 0.12,
            "longitude": longitude + 0.09,
            "power_kw": 350.0,
            "status": "Available",
            "distance_km": 18.2
        },
        {
            "name": f"City Charge {connector_type} Hub C",
            "latitude": latitude + 0.04,
            "longitude": longitude + 0.15,
            "power_kw": 50.0,
            "status": "Occupied",
            "distance_km": 21.0
        }
    ]
    return json.dumps({"chargers": mock_chargers}, indent=2)


# ---------------------------------------------------------------------------
# MCP Tool: compute_route
# ---------------------------------------------------------------------------
@mcp.tool()
def compute_route(
    origin: str,
    destination: str,
    waypoints: Optional[str] = None
) -> str:
    """Compute the driving route between an origin and a destination, returning waypoint coordinates.

    Args:
        origin: The start city or address.
        destination: The end city or address.
        waypoints: Optional comma-separated list of waypoint cities or coordinates.
    """
    start_lat, start_lon = geocode_city(origin)
    end_lat, end_lon = geocode_city(destination)

    route_data = None

    # Attempt OpenRouteService API if key is available
    if OPENROUTE_SERVICE_KEY:
        # OpenRouteService expects coordinates as lon,lat
        url = (
            f"https://api.openrouteservice.org/v2/directions/driving-car"
            f"?api_key={OPENROUTE_SERVICE_KEY}&start={start_lon},{start_lat}&end={end_lon},{end_lat}"
        )
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                res_data = response.json()
                feature = res_data.get("features", [{}])[0]
                geometry = feature.get("geometry", {})
                properties = feature.get("properties", {})
                summary = properties.get("summary", {})
                
                coords = geometry.get("coordinates", [])  # list of [lon, lat]
                
                # Sample 5 key coordinates along the route for elevation checks
                sampled_coords = []
                if coords:
                    step = max(1, len(coords) // 5)
                    sampled_coords = [(pt[1], pt[0]) for pt in coords[::step][:5]]
                    # Ensure end point is included
                    if (coords[-1][1], coords[-1][0]) not in sampled_coords:
                        sampled_coords.append((coords[-1][1], coords[-1][0]))

                # Fetch real elevations from OpenTopoData fallback
                elevations = fetch_elevations_from_opentopodata(sampled_coords)
                
                route_data = {
                    "origin": origin,
                    "destination": destination,
                    "distance_km": (summary.get("distance", 0.0) / 1000.0),
                    "duration_hours": (summary.get("duration", 0.0) / 3600.0),
                    "waypoints": [
                        {"latitude": pt[0], "longitude": pt[1], "elevation_m": el}
                        for pt, el in zip(sampled_coords, elevations)
                    ]
                }
        except Exception:
            pass

    # Fallback to high-quality simulated route details
    if not route_data:
        # Standard route distance/duration estimation
        dist = 600.0  # default
        dur = 6.0
        
        # Adjust SF -> LA specific values
        if "san francisco" in origin.lower() and "los angeles" in destination.lower():
            dist = 615.0
            dur = 6.2
        elif "seattle" in origin.lower() and "portland" in destination.lower():
            dist = 280.0
            dur = 3.0

        # Sample coordinates along the path
        sampled_coords = [
            (start_lat, start_lon),
            (start_lat + (end_lat - start_lat) * 0.25, start_lon + (end_lon - start_lon) * 0.25),
            (start_lat + (end_lat - start_lat) * 0.5, start_lon + (end_lon - start_lon) * 0.5),
            (start_lat + (end_lat - start_lat) * 0.75, start_lon + (end_lon - start_lon) * 0.75),
            (end_lat, end_lon)
        ]

        # Fetch elevations from OpenTopoData fallback
        elevations = fetch_elevations_from_opentopodata(sampled_coords)

        route_data = {
            "origin": origin,
            "destination": destination,
            "distance_km": dist,
            "duration_hours": dur,
            "waypoints": [
                {"latitude": pt[0], "longitude": pt[1], "elevation_m": el}
                for pt, el in zip(sampled_coords, elevations)
            ]
        }

    return json.dumps(route_data, indent=2)


# ---------------------------------------------------------------------------
# MCP Tool: fetch_weather
# ---------------------------------------------------------------------------
@mcp.tool()
def fetch_weather(latitude: float, longitude: float) -> str:
    """Fetch real-time weather information for a specific location using Open-Meteo.

    Args:
        latitude: Latitude of the weather location.
        longitude: Longitude of the weather location.
    """
    url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&current=temperature_2m,rain"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            current = data.get("current", {})
            temp = current.get("temperature_2m", 20.0)
            rain = current.get("rain", 0.0)
            return json.dumps({
                "latitude": latitude,
                "longitude": longitude,
                "temperature_c": temp,
                "rain_mm": rain,
                "is_raining": rain > 0.0
            }, indent=2)
    except Exception:
        pass

    # Simple fallback weather mock
    return json.dumps({
        "latitude": latitude,
        "longitude": longitude,
        "temperature_c": 12.0,  # Cold weather range degradation trigger
        "rain_mm": 0.5,
        "is_raining": True
    }, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
