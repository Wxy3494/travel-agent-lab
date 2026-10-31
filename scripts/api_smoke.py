"""Behavioral acceptance against an actually running API, demo mode only."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.audit import audit_result
from app.schemas import TripResult


def run():
    records = []
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=200) as client:
        health = client.get("/health")
        health.raise_for_status()
        if health.json()["mode"] != "demo":
            raise SystemExit("Demo API required; this smoke does not authorize paid requests.")
        anchor = (datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=2)).isoformat()
        for cities, days in [(["广州"], 2), (["广州", "深圳"], 5), (["广州", "深圳", "珠海"], 7)]:
            body = {"constraints": {"origin": "广州", "cities": cities, "days": days,
                "start_date": anchor, "people": 2, "budget": 15000}}
            response = client.post("/trips", json=body)
            response.raise_for_status()
            trip = TripResult.model_validate(response.json())
            assert trip.status == "ready" and not audit_result(trip), trip.issues
            assert client.get(f"/trips/{trip.id}/export").text.startswith("# 旅行计划")
            changed = client.post(f"/trips/{trip.id}/revise", json={"base_version": 1, "changes": {"budget": 1}})
            assert changed.status_code == 200 and changed.json()["status"] == "needs_review"
            assert changed.json()["version"] == 2
            assert client.get(f"/trips/{trip.id}?version=1").json()["constraints"]["budget"] == 15000
            assert client.post(f"/trips/{trip.id}/revise", json={"base_version": 1, "changes": {}}).status_code == 409
            records.append({"cities": cities, "days": days, "id": trip.id, "create": "ready",
                "revision": "needs_review", "old_version": True, "conflict_http": 409,
                "export": True, "audit": "passed"})
        partial = client.post("/trips", json={}).json()
        assert partial["status"] == "needs_input" and len(partial["questions"]) == 1
        resumed = client.post(f"/trips/{partial['id']}/revise", json={"base_version": 1,
            "changes": {"origin": "广州", "cities": ["广州"], "days": 2, "start_date": anchor,
                        "people": 2, "budget": 6000}}).json()
        assert resumed["status"] == "ready" and resumed["version"] == 2
        blocked = client.post("/trips", json={"prompt": "帮我自动付款预订酒店"}).json()
        assert blocked["status"] == "blocked"
        assert client.get("/trips/missing").status_code == 404
        assert client.post("/trips", json={"constraints": {"days": 31}}).status_code == 422
        assert client.get("/trips?query=广州").json()
        choice_body = {"constraints": {"origin": "广州市", "cities": ["广州市"], "days": 2,
                                       "start_date": anchor, "people": 2, "budget": 6000, "must_visit": ["博物"]}}
        pending = client.post("/trips", json=choice_body).json()
        assert pending["status"] == "needs_input" and pending["clarifications"][0]["options"]
        point = pending["clarifications"][0]["options"][0]
        confirmed = client.post(f"/trips/{pending['id']}/revise", json={"base_version": 1,
            "changes": {"place_choices": {"must_visit:博物": point["id"]}}}).json()
        assert confirmed["status"] == "ready" and confirmed["version"] == 2
        revised = client.post(f"/trips/{pending['id']}/revise", json={"base_version": 2,
            "instruction": "第二天轻松一点，只安排下午的活动"}).json()
        assert revised["status"] == "ready" and revised["days"][0] == confirmed["days"][0]
        assert all(e["start"] >= "13:00" for e in revised["days"][1]["events"])
        assert not audit_result(TripResult.model_validate(revised))
        assert client.get(f"/trips/{pending['id']}/versions").json()[0]["version"] == 3
        diff = client.get(f"/trips/{pending['id']}/compare?from_version=2&to_version=3").json()
        assert diff["constraints"] and all(d["day"] == 2 for d in diff["days"])
        clone = client.post(f"/trips/{pending['id']}/clone", json={"version": 2}).json()
        assert clone["id"] != pending["id"] and clone["version"] == 1
        hotel = client.get("/places/search?city=广州&query=东侧&category=hotel").json()["options"][0]
        anchored = client.post(f"/trips/{clone['id']}/revise", json={"base_version": 1,
            "changes": {"anchors": {"广州": {"hotel_id": hotel["id"]}}}}).json()
        assert anchored["status"] == "ready"
        assert any("东侧" in e["title"] for d in anchored["days"] for e in d["events"])
    report = {"generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "execution": "running_api_demo", "passed": True, "trips": records,
        "clarification_resume": True, "transaction_blocked": True, "http_404_422": True}
    report["extended"] = {"candidate_confirmed": True, "scoped_revision_preserved_day": True,
                           "history_compare_clone": True, "selected_hotel_changes_route": True}
    out = Path("artifacts/acceptance")
    out.mkdir(parents=True, exist_ok=True)
    (out / "api.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "report": str(out / "api.json")}, ensure_ascii=False))


if __name__ == "__main__":
    run()
