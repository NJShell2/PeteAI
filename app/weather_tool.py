"""Weather forecasts without a browser.

A weather question answered by driving a browser at a weather site is slow,
fragile, and walks straight into CAPTCHAs and bot walls. Open-Meteo serves
forecasts as plain JSON with no API key and no signup, so ``get_weather``
answers "tomorrow's weather for West Lafayette" in one HTTP round trip.
"""

from typing import Any, Dict, List, Optional

import httpx


GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes -> plain English.
WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Foggy",
    48: "Foggy with icy deposits",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light rain showers",
    81: "Rain showers",
    82: "Violent rain showers",
    85: "Light snow showers",
    86: "Snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Thunderstorm with hail",
}


async def _get_json(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


async def geocode(location: str) -> Optional[Dict[str, Any]]:
    """Resolves a place name to latitude/longitude. None if not found."""
    data = await _get_json(
        GEOCODE_URL,
        {"name": location, "count": 1, "language": "en", "format": "json"},
    )
    results: List[Dict[str, Any]] = data.get("results") or []
    if not results:
        return None
    best = results[0]
    return {
        "name": best.get("name", location),
        "region": best.get("admin1") or "",
        "country": best.get("country") or "",
        "country_code": best.get("country_code") or "",
        "latitude": best.get("latitude"),
        "longitude": best.get("longitude"),
    }


def describe_code(code: Any) -> str:
    try:
        return WEATHER_CODES.get(int(code), "Unknown conditions")
    except (TypeError, ValueError):
        return "Unknown conditions"


async def get_weather(location: str, days_ahead: int = 1) -> str:
    """One-line-ish forecast for a place.

    ``days_ahead``: 0 = today, 1 = tomorrow (the default), up to 7.
    Returns a human-readable summary, or a clear error string.
    """
    location = (location or "").strip()
    if not location:
        return "No location given. Tell me a city or place name and I will look up its forecast."
    try:
        days_ahead = int(days_ahead)
    except (TypeError, ValueError):
        days_ahead = 1
    days_ahead = max(0, min(days_ahead, 7))

    try:
        place = await geocode(location)
    except Exception as e:
        return f"Could not reach the weather service: {e}"
    if not place:
        return f"Could not find a place called '{location}'. Try a nearby city."

    # Fahrenheit for the US, Celsius everywhere else -- the unit the asker
    # almost certainly expects, without having to be told.
    unit = "fahrenheit" if place.get("country_code") == "US" else "celsius"
    symbol = "F" if unit == "fahrenheit" else "C"

    try:
        data = await _get_json(
            FORECAST_URL,
            {
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "daily": "weathercode,temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max",
                "temperature_unit": unit,
                "timezone": "auto",
                "forecast_days": days_ahead + 1,
            },
        )
    except Exception as e:
        return f"Could not reach the weather service: {e}"

    daily = data.get("daily") or {}
    times = daily.get("time") or []
    if days_ahead >= len(times):
        return f"No forecast available that far out for {place['name']}."
    i = days_ahead
    date = times[i]
    desc = describe_code((daily.get("weathercode") or [None])[i])
    hi = (daily.get("temperature_2m_max") or [None])[i]
    lo = (daily.get("temperature_2m_min") or [None])[i]
    pop = (daily.get("precipitation_probability_max") or [None])[i]

    where = place["name"]
    if place.get("region"):
        where += f", {place['region']}"
    day_label = {0: "Today", 1: "Tomorrow"}.get(days_ahead, date)

    parts = [f"{day_label} in {where}: {desc}."]
    if hi is not None and lo is not None:
        parts.append(f"High {round(hi)}°{symbol}, low {round(lo)}°{symbol}.")
    if pop is not None:
        parts.append(f"{pop}% chance of precipitation.")
    return " ".join(parts)
