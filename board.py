#!/usr/bin/env python3


from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Dict, Any

import requests
from flask import Flask, jsonify, request, abort, make_response, render_template
print("Hello")
APP_TITLE = "Shelly Panel"
CONFIG_PATH = Path(__file__).with_name("config.json")

app = Flask(__name__)

# ------------------------
# Config loading utilities
# ------------------------


def load_config() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
            # Basic validation
            if not isinstance(data, dict) or "devices" not in data or not isinstance(data["devices"], list):
                raise ValueError("config.json must contain a top-level 'devices' array")
            return data
    # If no config present, provide a friendly default with instructions
    return {
        "devices": [
            {"ip": "192.168.1.50", "name": "Sample Device", "color": "#FF6A6A", "symbol": "\ud83d\udd26"}
        ]
    }

# Cache config in memory; modify here if you want hot-reload via file watcher
CONFIG: Dict[str, Any] = load_config()

# ------------------------
# Weather helpers
# ------------------------
from datetime import datetime, timedelta, timezone

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def geocode_location(query: str) -> Dict[str, Any] | None:
    """Return a dict with latitude, longitude and name for the given query or None on failure."""
    if not query:
        return None
    try:
        r = requests.get(GEOCODING_URL, params={"name": query, "count": 1}, timeout=5)
        r.raise_for_status()
        j = r.json()
        results = j.get("results") or []
        if not results:
            return None
        top = results[0]
        return {"name": top.get("name"), "latitude": top.get("latitude"), "longitude": top.get("longitude"), "country": top.get("country")}
    except Exception:
        return None


def get_weather_for_location(query: str, hours: int = 48) -> Dict[str, Any]:
    """Fetch hourly temperature and precipitation for the next `hours` hours and current weather.

    Uses Open-Meteo APIs (geocoding + forecast). Returns a dict with keys:
      - location_name
      - times (list of ISO strings) limited to `hours`
      - temperature (list)
      - precipitation (list)
      - current_weather (object) if available: {temperature, windspeed, winddirection, weathercode, time}
    On failure returns empty arrays and an 'error' key.
    """
    ge = geocode_location(query)
    if not ge:
        return {"location_name": query or "(unknown)", "times": [], "temperature": [], "precipitation": [], "current_weather": None, "error": "geocoding_failed"}

    lat = ge["latitude"]
    lon = ge["longitude"]
    location_name = f"{ge.get('name')}, {ge.get('country') or ''}".strip(', ')

    # Build start/end anchored to the current UTC hour to guarantee the payload starts at "now".
    now_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now_utc.isoformat()
    end = (now_utc + timedelta(hours=hours)).isoformat()

    # Request hourly temperature, precipitation and cloud cover. Use UTC timezone so times are consistent.
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation,cloudcover",
        "start": start,
        "end": end,
        "current_weather": True,
        "timezone": "UTC",
    }

    try:
        r = requests.get(FORECAST_URL, params=params, timeout=10)
        r.raise_for_status()
        j = r.json()
        hourly = j.get("hourly", {})
        # API should already limit to the requested range via start/end. Still slice defensively.
        times = hourly.get("time", [])[:hours]
        temps = hourly.get("temperature_2m", [])[:hours]
        prec = hourly.get("precipitation", [])[:hours]
        clouds = hourly.get("cloudcover", [])[:hours]
        current = j.get("current_weather") or None
        return {
            "location_name": location_name,
            "times": times,
            "temperature": temps,
            "precipitation": prec,
            "cloudcover": clouds,
            "current_weather": current,
        }
    except Exception as e:
        return {"location_name": location_name, "times": [], "temperature": [], "precipitation": [], "cloudcover": [], "current_weather": None, "error": str(e)}

# ------------------------
# Shelly helpers
# ------------------------

TIMEOUT = 3  # seconds

def shelly_toggle(ip: str) -> Dict[str, Any]:
    """Attempt to toggle a Shelly device. Tries Gen1, then falls back to Gen2 RPC."""
    # Gen1 style: http://<ip>/relay/0?turn=toggle
    url_gen1 = f"http://{ip}/relay/0?turn=toggle"
    try:
        r = requests.get(url_gen1, timeout=TIMEOUT)
        if r.ok:
            return {"ok": True, "endpoint": url_gen1, "status_code": r.status_code, "error": None}
    except Exception as e:
        err1 = str(e)
    else:
        err1 = f"HTTP {r.status_code}"

    # Gen2 style: http://{ip}/rpc/Switch.Toggle?id=0
    url_gen2 = f"http://{ip}/rpc/Switch.Toggle?id=0"
    try:
        r2 = requests.get(url_gen2, timeout=TIMEOUT)
        if r2.ok:
            return {"ok": True, "endpoint": url_gen2, "status_code": r2.status_code, "error": None}
    except Exception as e:
        err2 = str(e)
    else:
        err2 = f"HTTP {r2.status_code}"

    return {"ok": False, "endpoint": url_gen2, "status_code": None, "error": f"Gen1 failed: {err1}; Gen2 failed: {err2}"}

# ------------------------
# API routes
# ------------------------

@app.get("/api/devices")
def api_devices():
    # Return devices with an additional 'state' field (True=on, False=off, None=unknown)
    devices = []
    for d in CONFIG.get("devices", []):
        ip = d.get("ip")
        state = None
        if ip:
            try:
                state = shelly_get_state(ip)
            except Exception:
                state = None
        dd = dict(d)
        dd["state"] = state
        devices.append(dd)
    return jsonify(devices)


def shelly_get_state(ip: str) -> bool | None:
    """Try to determine whether relay 0 is on for a Shelly device.

    Returns True, False, or None if unknown/error.
    This tries several common endpoints for Shelly Gen1/Gen2 devices.
    """
    # Try Gen1 status endpoint
    urls = [f"http://{ip}/status", f"http://{ip}/relay/0", f"http://{ip}/rpc/Switch.GetStatus?id=0", f"http://{ip}/rpc/Switch.GetStatus"]
    for url in urls:
        try:
            r = requests.get(url, timeout=TIMEOUT)
            if not r.ok:
                continue
            try:
                j = r.json()
            except Exception:
                # some endpoints might return plain text
                text = r.text.strip().lower()
                if text in ("on", "true", "1"):
                    return True
                if text in ("off", "false", "0"):
                    return False
                continue

            # parse common shapes
            # shape: { "relays": [ { "ison": true } ] }
            relays = j.get("relays") if isinstance(j, dict) else None
            if isinstance(relays, list) and relays:
                first = relays[0]
                if isinstance(first, dict) and "ison" in first:
                    return bool(first.get("ison"))

            # shape: { "ison": true }
            if isinstance(j, dict) and "ison" in j:
                return bool(j.get("ison"))

            # Gen2 rpc may return { "ison": true } or nested
            if isinstance(j, dict) and "power" in j:
                # sometimes 'power' is numeric >0
                try:
                    return float(j.get("power", 0)) > 0
                except Exception:
                    pass

            # Try other common keys
            if isinstance(j, dict) and "output" in j:
                return bool(j.get("output"))

            # Fallback: search recursively for a boolean 'ison' key
            def find_ison(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k == "ison" and isinstance(v, bool):
                            return v
                        res = find_ison(v)
                        if res is not None:
                            return res
                if isinstance(obj, list):
                    for item in obj:
                        res = find_ison(item)
                        if res is not None:
                            return res
                return None

            res = find_ison(j)
            if res is not None:
                return bool(res)

        except Exception:
            continue
    return None

@app.post("/api/toggle")
def api_toggle():
    data = request.get_json(silent=True) or {}
    ip = data.get("ip")
    if not ip:
        abort(make_response(jsonify({"error": "Missing 'ip'"}), 400))
    result = shelly_toggle(ip)
    status = 200 if result["ok"] else 502
    return make_response(jsonify(result), status)

# ------------------------
# Frontend route
# ------------------------

@app.get("/")
def index():
    # read location from config (default to Munich, Germany if not provided)
    location = CONFIG.get("location") or "Munich, Germany"
    # We'll render the page and let the client fetch `/api/weather` for live updates.
    return render_template("index.html", title=APP_TITLE)


@app.get("/api/weather")
def api_weather():
    """Return weather JSON for the configured location (next 48 hours)."""
    location = CONFIG.get("location") or "Munich, Germany"
    data = get_weather_for_location(location, hours=48)
    return jsonify(data)

# ------------------------
# Entrypoint
# ------------------------

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "5000")), debug=bool(os.environ.get("DEBUG", "0") == "1"))
