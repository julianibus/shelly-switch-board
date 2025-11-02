#!/usr/bin/env python3


from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Dict, Any

import requests
from flask import Flask, jsonify, request, abort, make_response, render_template

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
    return jsonify(CONFIG["devices"])

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
    return render_template("index.html", title=APP_TITLE)

# ------------------------
# Entrypoint
# ------------------------

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "5000")), debug=bool(os.environ.get("DEBUG", "0") == "1"))
