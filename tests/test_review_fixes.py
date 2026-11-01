"""Regression tests for the six 2026-10-31 review findings."""
import asyncio
from collections import Counter
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from app.adjustments import suggestions
from app.audit import audit_result
from app.config import client_read_timeout
from app.day_input import day_references
from app.input_parser import demo_patch
from app.main import create_app
from app.planning import allocate_days
from app.providers import FixtureProvider, ProviderError
from app.schemas import Constraints, Issue, Place, PlaceBinding, RevisionRequest, TripRequest, TripResult
from scripts.check_release import smoke_extracted_ui, verify_archive
from scripts.package_public import ROOT, build_package


def test_release_contains_and_loads_ui_assets_outside_checkout(tmp_path):
    output = tmp_path / "release"
    package = build_package(output=output)
    assert verify_archive(output=output)["required_ui_assets"] == "present"
    result = smoke_extracted_ui(Path(package["archive"]), parent=tmp_path)
    assert result["css"] == "loaded" and result["jpg"] == "embedded"
    assert not result["original_checkout_imported"]


@pytest.mark.parametrize("text", ["第0天轻松一点", "第 0 天轻松一点", "第-1天只安排下午", "第 -1 天只安排下午",
    "第-999天只安排下午", "第零天轻松一点", "第负一天轻松一点", "第31天只安排下午", "第一百天只安排下午"])
def test_invalid_text_days_rejected_before_create_or_revision(service, constraints, text):
    api = TestClient(create_app(service.settings, service))
    assert api.post("/trips", json={"prompt": text, "constraints": constraints.model_dump(mode="json")}).status_code == 422
    assert api.post("/trips/missing/revise", json={"base_version": 1, "instruction": text}).status_code == 422
    with pytest.raises(ProviderError, match="日序"):
        demo_patch(text)
    assert service.repository.list() == []


@pytest.mark.parametrize("day", [0, -1, -999, 31])
def test_invalid_structured_day_keys_are_422(service, constraints, day):
    api = TestClient(create_app(service.settings, service))
    for key, value in [("day_place_ids", {str(day): ["440100_0"]}), ("day_rules", [{"day": day}])]:
        response = api.post("/trips", json={"constraints": {**constraints.model_dump(mode="json"), key: value}})
        assert response.status_code == 422
        assert api.post("/trips/missing/revise", json={"base_version": 1, "changes": {key: value}}).status_code == 422
    assert api.post("/trips/missing/revise", json={"base_version": 1, "target_day": day}).status_code == 422


def test_valid_spaced_and_chinese_days_still_work():
    assert day_references("第 1 天和第三十天") == [1, 30]
    assert demo_patch("第 2 天只安排下午")["day_rules"] == [{"day": 2, "start": "13:00"}]
    with pytest.raises(ValidationError):
        Constraints(days=2, day_place_ids={3: []})


async def test_invalid_revision_keys_never_save_new_version(service, constraints):
    trip = await service.create(TripRequest(constraints=constraints))
    api = TestClient(create_app(service.settings, service))
    for day in (0, -1, -100, 3):
        response = api.post(f"/trips/{trip.id}/revise", json={"base_version": 1,
            "changes": {"day_place_ids": {str(day): ["440100_0"]}}})
        assert response.status_code == 422
    assert service.repository.get(trip.id).version == 1
    with pytest.raises(ValidationError):
        RevisionRequest(base_version=1, instruction="第 -9 天轻松一点")


def bindings(counts):
    return [PlaceBinding(field="must_visit", query=f"{city}{i}", place_id=f"{city}{i}", canonical_name=f"{city}{i}",
        city=city, basis="测试资料", evidence_id="test") for city, count in counts.items() for i in range(count)]


@pytest.mark.parametrize("cities,days,required,expected", [
    (["广州", "深圳"], 5, {"深圳": 7}, {"广州": 2, "深圳": 3}),
    (["广州", "深圳"], 5, {"广州": 7}, {"广州": 3, "深圳": 2}),
    (["广州", "深圳", "珠海"], 6, {"珠海": 9}, {"广州": 1, "深圳": 2, "珠海": 3}),
])
def test_allocation_respects_required_place_capacity(cities, days, required, expected):
    c = Constraints(cities=cities, days=days, bindings=bindings(required))
    assert Counter(allocate_days(c)) == expected


async def test_five_day_seven_must_visits_is_materialized_and_audited(service, constraints):
    names = [f"深圳必去点{i}" for i in range(7)]

    class SevenPlaces(FixtureProvider):
        async def city(self, city, c):
            facts = await super().city(city, c)
            if city == "深圳":
                facts.places = [Place(id=f"mandatory-{i}", name=name, city=city,
                    location=f"114.{i + 10},22.5", evidence_id=facts.evidence_ids[0]) for i, name in enumerate(names)]
            return facts

        async def route(self, *args):
            return {**await super().route(*args), "minutes": 10}

    service.provider_factory = SevenPlaces
    c = Constraints.model_validate({**constraints.model_dump(), "cities": ["广州", "深圳"], "days": 5,
                                   "must_visit": names, "budget": 10000})
    trip = await service.create(TripRequest(constraints=c))
    assert trip.status == "ready", trip.issues
    assert Counter(d.city for d in trip.days) == {"广州": 2, "深圳": 3}
    assert set(names) <= {e.title for d in trip.days for e in d.events if e.kind == "visit"}
    assert not audit_result(trip)
    assert any(t["step"] == "city_allocation" for t in trip.trace)
    c = Constraints.model_validate({**c.model_dump(), "days": 3})
    failed = await service.create(TripRequest(constraints=c))
    assert failed.status == "needs_review" and failed.issues[0].code == "too_many_activities"


@pytest.mark.parametrize("days", [7, 8, 29, 30])
def test_extra_day_suggestion_uses_thirty_day_limit(constraints, days):
    c = constraints.model_copy(update={"days": days})
    result = TripResult(id="test", version=1, mode="demo", strategy="multi", status="needs_review", constraints=c,
                        issues=[Issue(code="missing_must_visit", message="必去地点未安排")])
    option = next((a for a in suggestions(result) if a.id == "extra_day"), None)
    assert bool(option) == (days < 30)
    if option:
        assert option.changes["days"] == days + 1


async def test_extra_day_time_conflict_suggestion_also_uses_thirty_days(service, constraints):
    trip = await service.create(TripRequest(constraints=constraints))
    for day_count in (7, 29, 30):
        trip.constraints.days = day_count
        trip.issues = [Issue(code="time_window", message="活动超时")]
        options = [a for a in suggestions(trip) if a.id == "extra_day"]
        assert len(options) == int(day_count < 30)
        if options:
            assert options[0].changes["days"] == day_count + 1


async def test_backend_deadline_returns_before_client_waits_out(service, constraints):
    class SlowResearch(FixtureProvider):
        async def city(self, city, c):
            await asyncio.sleep(0.1)
            return await super().city(city, c)
    service.settings = replace(service.settings, timeout=0.02)
    service.provider_factory = SlowResearch
    api = TestClient(create_app(service.settings, service))
    response = api.post("/trips", json={"constraints": constraints.model_dump(mode="json")})
    assert response.status_code == 200 and response.json()["status"] == "failed"
    assert response.json()["issues"][0]["code"] == "request_timeout"
    assert api.get("/health").json()["request_timeout_seconds"] == 0.02
    assert client_read_timeout(0.02) > 0.02


def test_frontend_uses_server_timeout_even_with_different_local_environment(monkeypatch, service):
    calls = []
    def local(method, url, **kwargs):
        calls.append((url, kwargs["timeout"]))
        body = {"mode": "demo", "status": "ok", "request_timeout_seconds": 600} if url.endswith("/health") else {"items": [], "total": 0}
        return httpx.Response(200, json=body)
    monkeypatch.setenv("TRAVEL_TIMEOUT_SECONDS", "200")
    monkeypatch.setattr(httpx, "request", local)
    ui = AppTest.from_file(str(ROOT / "app/ui.py"), default_timeout=10).run()
    assert not ui.exception
    assert any(url.endswith("/trips/history?limit=5") and timeout.read == 630 for url, timeout in calls)
    assert all(timeout.connect == 10 for _, timeout in calls)


def test_frontend_timeout_explains_history_recovery(monkeypatch):
    def slow(method, url, **kwargs):
        raise httpx.ReadTimeout("test only")
    monkeypatch.setattr(httpx, "request", slow)
    ui = AppTest.from_file(str(ROOT / "app/ui.py"), default_timeout=10).run()
    assert not ui.exception
    assert any("历史旅行列表" in m.value and "避免立即重复提交" in m.value for m in ui.warning)
