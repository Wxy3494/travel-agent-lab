import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents import DemoAgents
from app.config import Settings
from app.main import create_app
from app.providers import FixtureProvider
from app.schemas import Review, RevisionRequest, TripRequest
from app.service import TravelService
from app.storage import Repository, VersionConflict

CASES = json.loads((Path(__file__).parents[1] / "evals/cases-v3.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_contract_cases(case, tmp_path, constraints):
    settings = Settings(db_path=str(tmp_path / "cases.db"))
    service = TravelService(settings, Repository(settings.db_path),
                            provider_factory=lambda: FixtureProvider(scenario=case.get("scenario", "normal")))
    api = TestClient(create_app(settings, service))
    body = {"constraints": {**constraints.model_dump(mode="json"), **case.get("changes", {})},
            "prompt": case.get("prompt", "")}
    response = api.post("/trips", json=body)
    assert response.status_code == case.get("expected_http", 200)
    if response.status_code != 200:
        return
    result = response.json()
    if "revision" in case:
        original = result
        result = api.post(f"/trips/{original['id']}/revise",
                          json={"base_version": 1, "changes": case["revision"]}).json()
        assert result["version"] == 2
        assert result["constraints"]["cities"] == original["constraints"]["cities"]
        assert result["constraints"]["people"] == original["constraints"]["people"]
        assert api.get(f"/trips/{original['id']}?version=1").json()["constraints"]["budget"] == 6000
    assert result["status"] == case["expected"], result["issues"]
    if "issue" in case:
        assert case["issue"] in [x["code"] for x in result["issues"]]
    names = [e["title"] for day in result["days"] for e in day["events"] if e["kind"] == "visit"]
    if "includes" in case:
        assert case["includes"] in names
    if "excludes" in case:
        assert case["excludes"] not in names
    if "hotel_quantity" in case:
        assert next(i["quantity"] for i in result["budget"]["items"] if i["category"] == "住宿") == case["hotel_quantity"]
    assert result["metrics"]["model_calls"] == 0
    if result["status"] == "ready":
        assert not any(i["severity"] == "error" for i in result["issues"])
        known = {e["id"] for e in result["evidence"]}
        assert all(set(e["evidence_ids"]) <= known for d in result["days"] for e in d["events"])


async def test_versions_and_persistence(service, constraints):
    result = await service.create(TripRequest(constraints=constraints))
    changed = await service.revise(result.id, RevisionRequest(base_version=1, changes={"excluded": ["广东省博物馆"]}))
    assert changed.version == 2
    assert changed.status == "ready"
    with pytest.raises(VersionConflict):
        await service.revise(result.id, RevisionRequest(base_version=1, changes={"budget": 4000}))
    reopened = Repository(service.settings.db_path)
    assert reopened.get(result.id).version == 2
    assert reopened.get(result.id, 1).constraints.excluded == []


async def test_sqlite_handles_are_closed_on_windows(service, constraints):
    await service.create(TripRequest(constraints=constraints))
    path = Path(service.settings.db_path)
    path.rename(path.with_suffix(".closed.db"))


async def test_lower_budget_cannot_be_approved_by_model(service, constraints):
    class ApprovesEverything(DemoAgents):
        async def review(self, c, days, issues):
            return Review(approved=True)
    service.agents_factory = ApprovesEverything
    constraints.budget = 1
    result = await service.create(TripRequest(constraints=constraints))
    assert result.status == "needs_review"
    # A new explicit requirement stops repeated identical, unfixable budget repairs.
    assert len([t for t in result.trace if t["step"] == "validation"]) == 1
    assert any(a.id == "increase_budget" for a in result.adjustments)


async def test_different_users_are_isolated(service, constraints):
    second = constraints.model_copy(update={"people": 5, "budget": 20000})
    a, b = await asyncio.gather(service.create(TripRequest(constraints=constraints)),
                                service.create(TripRequest(constraints=second)))
    assert a.id != b.id
    assert service.repository.get(a.id).constraints.people == 2
    assert service.repository.get(b.id).constraints.people == 5


async def test_deadline_cancels_work(tmp_path, constraints):
    class Slow(DemoAgents):
        async def research(self, *args):
            await asyncio.sleep(1)
    settings = Settings(timeout=0.01, db_path=str(tmp_path / "timeout.db"))
    service = TravelService(settings, Repository(settings.db_path), agents_factory=Slow)
    result = await service.create(TripRequest(constraints=constraints))
    assert result.status == "failed"
    assert result.issues[0].code == "request_timeout"


async def test_tool_budget_is_enforced(service, constraints):
    service.provider_factory = lambda: FixtureProvider(max_calls=1)
    result = await service.create(TripRequest(constraints=constraints))
    assert result.status == "failed"
    assert result.issues[0].code == "tool_limit"


async def test_long_travel_is_not_success(service, constraints):
    service.provider_factory = lambda: FixtureProvider(scenario="slow_route")
    result = await service.create(TripRequest(constraints=constraints))
    assert result.status == "needs_review"
    assert any(i.code == "day_too_long" for i in result.issues)


async def test_demo_does_not_pretend_to_parse_text(service, constraints):
    result = await service.create(TripRequest(prompt="第二天不要去广州塔", constraints=constraints))
    assert result.status == "needs_input"
    assert result.metrics.model_calls == 0


async def test_sensitive_request_is_not_persisted(service, constraints):
    secret = "fake-sensitive-value"
    constraints.pricing_note = "保存银行卡号 " + secret
    result = await service.create(TripRequest(constraints=constraints))
    assert result.status == "blocked"
    assert secret not in service.repository.get(result.id).model_dump_json()


def test_api_revision_validation_and_export(service, constraints):
    api = TestClient(create_app(service.settings, service))
    result = api.post("/trips", json={"constraints": constraints.model_dump(mode="json")}).json()
    assert api.get(f"/trips/{result['id']}/export").text.startswith("# 旅行计划")
    assert api.post(f"/trips/{result['id']}/revise", json={"base_version": 1, "changes": {"unknown": "x"}}).status_code == 422
    assert api.post(f"/trips/{result['id']}/revise", json={"base_version": 2, "changes": {}}).status_code == 409
    assert api.get("/trips/not-found").status_code == 404


async def test_clarification_can_resume(service):
    initial = await service.create(TripRequest())
    assert initial.status == "needs_input" and len(initial.questions) == 1
    complete = await service.revise(initial.id, RevisionRequest(base_version=1, changes={
        "origin": "广州", "cities": ["广州"], "start_date": str(date.today() + timedelta(days=2)),
        "days": 2, "people": 2, "budget": 6000}))
    assert complete.status == "ready"
