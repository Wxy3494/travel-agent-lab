"""One explicitly invoked live Meituan query through the actual API; maps/models stay fixed."""
import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.agents import DemoAgents
from app.config import Settings
from app.main import create_app
from app.providers import FixtureProvider
from app.schemas import Constraints
from app.service import TravelService
from app.storage import Repository


def check():
    folder = Path("artifacts/meituan-20261031")
    folder.mkdir(parents=True, exist_ok=True)
    configured = Settings.from_env()
    if not configured.meituan_token:
        raise SystemExit("Meituan credential is not configured; no request sent.")
    settings = replace(configured, mode="live", meituan_enabled=True,
        db_path=str(folder / ("isolated-" + uuid4().hex + ".sqlite3")))
    service = TravelService(settings, Repository(settings.db_path), provider_factory=FixtureProvider, agents_factory=DemoAgents)
    c = Constraints(origin="广州", cities=["广州"], start_date=datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=3),
        days=1, people=1, budget=2000, must_visit=["广东省博物馆"], day_place_ids={1: ["440100_0"]})
    api = TestClient(create_app(settings, service))
    started = time.monotonic()
    response = api.post("/trips", json={"constraints": c.model_dump(mode="json")})
    data = response.json()
    assert settings.meituan_token not in response.text, "Credential must not appear in API data"
    info = data.get("travel_info") or {}
    passed = response.status_code == 200 and info.get("status") == "success" and bool(info.get("content"))
    saved = api.get(f"/trips/{data['id']}").json() if passed else {}
    exported = api.get(f"/trips/{data['id']}/export").text if passed else ""
    passed = passed and saved.get("travel_info") == info and "美团景点与门票参考" in exported
    report = {"passed": passed, "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "scope": "live_Meituan_CLI_via_FastAPI_with_fixture_maps_and_demo_agents",
        "http_status": response.status_code, "status": info.get("status"), "message": info.get("message"),
        "duration_s": round(time.monotonic() - started, 2), "content_chars": len(info.get("content", "")),
        "content_sha256": hashlib.sha256(info.get("content", "").encode()).hexdigest(),
        "saved_and_exported": bool(passed), "paid_DeepSeek_or_AMap_calls": 0,
        "booking_or_orders": 0, "credential_in_response": False,
        "limits": "Consultation text; not verified live inventory, exact ticket price or actual admission."}
    (folder / "api-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # Public test case only, already redacted at the API boundary.
    (folder / "sample-trip.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    return bool(passed)


if __name__ == "__main__":
    raise SystemExit(0 if check() else 1)
