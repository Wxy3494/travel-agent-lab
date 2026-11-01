"""Compact route descriptions and key-free AMap walking links, including legacy trips."""
import math
import re
from itertools import pairwise
from urllib.parse import urlencode


def coordinate(value):
    try:
        lon, lat = map(float, value.split(","))
        if not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
            return None
        return f"{lon:.6f},{lat:.6f}"
    except (AttributeError, TypeError, ValueError):
        return None


def walking_link(start, end):
    start, end = coordinate(start), coordinate(end)
    if not start or not end or start == end:
        return None
    return "https://uri.amap.com/navigation?" + urlencode({"from": start + ",步行起点", "to": end + ",步行终点",
        "mode": "walk", "src": "travel-agent-lab", "callnative": 1})


def walking_step(value, start=None, end=None):
    try:
        distance = float(value["distance"])
        if not math.isfinite(distance) or not 0 < distance <= 10_000_000:
            return None
    except (KeyError, ValueError, TypeError):
        return None
    points = [p for step in value.get("steps", []) if isinstance(step, dict)
              for p in str(step.get("polyline", "")).split(";") if coordinate(p)]
    return {"kind": "walk", "distance": distance,
        "start_location": coordinate(value.get("origin")) or coordinate(start) or (coordinate(points[0]) if points else None),
        "end_location": coordinate(value.get("destination")) or coordinate(end) or (coordinate(points[-1]) if points else None)}


def route_lines(value):
    """Never turn a whole bus/train journey into a walk when old leg endpoints are absent."""
    data = value if isinstance(value, dict) else value.model_dump(mode="json")
    steps = data.get("route_steps", [])
    if not steps:
        steps = []
        for text in data.get("route_segments", []):
            match = re.fullmatch(r"步行\s*(\d+(?:\.\d+)?)\s*米", text.strip())
            steps.append({"kind": "walk", "distance": float(match[1])} if match else {"kind": "transit", "text": text})
        if steps and all(s["kind"] == "walk" for s in steps) and "实际步行路线" in data.get("note", ""):
            steps = [{"kind": "walk", "distance": sum(s["distance"] for s in steps),
                      "start_location": data.get("start_location"), "end_location": data.get("end_location")}]
    result, walks = [], []

    def flush():
        if not walks:
            return
        distance = round(sum(s.get("distance") or 0 for s in walks))
        connected = all(coordinate(a.get("end_location")) and coordinate(a.get("end_location")) == coordinate(b.get("start_location"))
                        for a, b in pairwise(walks))
        joined = walking_link(walks[0].get("start_location"), walks[-1].get("end_location")) if connected else None
        links = [joined] if joined else [walking_link(s.get("start_location"), s.get("end_location")) for s in walks]
        links = list(dict.fromkeys(link for link in links if link))
        result.append({"text": f"步行{'共' if len(walks) > 1 else ''} {distance} 米", "links": links})
        walks.clear()

    for step in steps:
        step = step if isinstance(step, dict) else step.model_dump(mode="json")
        if step["kind"] == "walk":
            walks.append(step)
        else:
            flush()
            if step.get("text"):
                result.append({"text": step["text"], "links": []})
    flush()
    return result
