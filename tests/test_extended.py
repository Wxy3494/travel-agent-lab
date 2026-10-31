"""Behavioral acceptance for the completed MVP, using isolated databases and fixed tools."""
import json
from datetime import timedelta

import httpx
import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agents import AutoGenAgents
from app.audit import audit_result
from app.config import Settings
from app.main import create_app
from app.planning import optimize_order
from app.providers import AmapProvider, FixtureProvider
from app.schemas import Constraints, Place, RevisionRequest, TripRequest
from app.service import TravelService
from app.storage import Repository, VersionConflict


class AmbiguousFixture(FixtureProvider):
    async def city(self, city, constraints):
        f = await super().city(city, constraints)
        if city == "广州" and not any(p.id == "branch-east" for p in f.places):
            f.places.extend([Place(id="branch-east", name="演示馆（东馆）", city=city, location="113.3,23.1",
                                  evidence_id=f.evidence_ids[0], address="东侧"),
                             Place(id="branch-west", name="演示馆（西馆）", city=city, location="113.2,23.1",
                                   evidence_id=f.evidence_ids[0], address="西侧")])
        return f


async def test_full_city_name_and_fixture_alias_preserve_raw_query(service, constraints):
    constraints.origin, constraints.cities = "广州市", ["广州市"]
    constraints.must_visit = ["广东博物馆"]
    r = await service.create(TripRequest(constraints=constraints))
    assert r.status == "ready", r.issues
    assert r.constraints.cities == ["广州"] and r.constraints.origin == "广州"
    assert r.constraints.must_visit == ["广东博物馆"]
    assert r.name_normalizations[0]["submitted"] == "广州市"
    b = r.constraints.bindings[0]
    assert b.place_id == "440100_0" and "演示" in b.basis
    assert any(e.place_id == b.place_id for d in r.days for e in d.events)
    assert not audit_result(r)


async def test_ambiguous_candidates_require_confirmation_and_resume(service, constraints):
    service.provider_factory = AmbiguousFixture
    constraints.must_visit = ["演示馆"]
    r = await service.create(TripRequest(constraints=constraints))
    assert r.status == "needs_input" and not r.days
    assert len(r.clarifications[0].options) == 2
    assert not any(t["step"] == "fixture_planner" for t in r.trace)
    assert service.repository.get(r.id).clarifications
    wrong = await service.revise(r.id, RevisionRequest(base_version=1,
        changes={"place_choices": {"must_visit:演示馆": "not-a-candidate"}}))
    assert wrong.status == "needs_input"
    fixed = await service.revise(r.id, RevisionRequest(base_version=2,
        changes={"place_choices": {"must_visit:演示馆": "branch-west"}}))
    assert fixed.status == "ready" and fixed.version == 3
    assert any(e.place_id == "branch-west" for d in fixed.days for e in d.events)
    assert "用户确认" in fixed.constraints.bindings[0].basis
    assert not audit_result(fixed)


async def test_missing_place_does_not_route_or_repeat(service, constraints):
    constraints.must_visit = ["查无此地点"]
    r = await service.create(TripRequest(constraints=constraints))
    assert r.status == "needs_input" and not r.clarifications[0].options
    assert not any(t["step"] == "validation" for t in r.trace)


async def test_aliases_cannot_hide_identity_conflict(service, constraints):
    constraints.must_visit, constraints.excluded = ["广东博物馆"], ["广东省博物馆"]
    r = await service.create(TripRequest(constraints=constraints))
    assert r.status == "needs_input" and any(q.key == "identity_conflict" for q in r.clarifications)


async def test_afternoon_relaxed_text_schedules_and_preserves_other_date(service, constraints):
    old = await service.create(TripRequest(constraints=constraints))
    revised = await service.revise(old.id, RevisionRequest(base_version=1,
        instruction="第二天轻松一点，只安排下午的活动"))
    assert revised.status == "ready", revised.issues
    assert revised.days[0] == old.days[0]
    assert all(e.start >= "13:00" for e in revised.days[1].events)
    assert len([e for e in revised.days[1].events if e.kind == "visit"]) == 1
    assert revised.revision_scope["preserved_days"] == [1]
    assert not audit_result(revised)


async def test_only_afternoon_recalculates_legs_keeps_morning_and_other_day(service, constraints):
    old = await service.create(TripRequest(constraints=constraints))
    new = await service.revise(old.id, RevisionRequest(base_version=1, target_day=2, target_period="afternoon",
        changes={"day_rules": [{"day": 2, "period": "afternoon", "start": "13:00", "end": "18:00", "visit_minutes": 30}]}))
    assert new.status == "ready", new.issues
    assert new.days[0] == old.days[0]
    assert [e for e in new.days[1].events if e.end <= "13:00"] == [e for e in old.days[1].events if e.end <= "13:00"]
    assert any(e.kind == "visit" and e.start >= "13:00" and int(e.end[:2]) * 60 + int(e.end[3:]) -
               int(e.start[:2]) * 60 - int(e.start[3:]) == 30 for e in new.days[1].events)
    assert not audit_result(new)


async def test_arrival_departure_and_meal_rest_are_enforced(service, constraints):
    c = Constraints.model_validate({**constraints.model_dump(), "arrival_time": "17:00", "departure_time": "13:00"})
    r = await service.create(TripRequest(constraints=c))
    assert r.status == "ready", r.issues
    assert min(e.start for e in r.days[0].events) >= "17:00"
    assert max(e.end for e in r.days[-1].events) <= "13:00"
    assert any(e.kind == "rest" for d in r.days for e in d.events)
    ordinary = await service.create(TripRequest(constraints=constraints))
    assert any(e.kind == "meal" for d in ordinary.days for e in d.events)
    assert not audit_result(r)


@pytest.mark.parametrize("patch", [{"daily_start": "23:00", "daily_end": "02:00"},
    {"days": 1, "arrival_time": "17:00", "departure_time": "10:00"},
    {"day_rules": [{"day": 2, "start": "18:00", "end": "13:00"}]},
    {"day_rules": [{"day": 3, "start": "13:00"}]}])
def test_cross_midnight_and_impossible_order_not_silently_wrapped(constraints, patch):
    with pytest.raises(ValidationError):
        Constraints.model_validate({**constraints.model_dump(), **patch})


async def test_closed_must_visit_and_unknown_hours_are_distinguished(service, constraints):
    c = Constraints.model_validate({**constraints.model_dump(), "days": 1, "must_visit": ["广州塔"],
        "opening_rules": [{"place_id": "440100_1", "on_date": str(constraints.start_date),
                           "closed": True, "source": "测试提供的闭馆公告；非真实营业信息"}]})
    r = await service.create(TripRequest(constraints=c))
    assert r.status == "needs_review" and any(i.code == "opening_conflict" for i in r.issues)
    assert not any(e.place_id == "440100_1" for d in r.days for e in d.events)
    assert any(e.kind == "user" for e in r.evidence)
    assert len({a.id for a in r.adjustments}) == len(r.adjustments)
    normal = await service.create(TripRequest(constraints=constraints))
    assert any(i.code == "opening_hours_unknown" for i in normal.issues)


async def test_budget_suggestion_requires_explicit_apply_and_preserves_hard_constraints(service, constraints):
    constraints.budget, constraints.must_visit = 1, ["广州塔"]
    old = await service.create(TripRequest(constraints=constraints))
    assert old.status == "needs_review" and old.constraints.budget == 1
    assert len([t for t in old.trace if t["step"] == "validation"]) == 1
    new = await service.revise(old.id, RevisionRequest(base_version=1, adjustment_id="increase_budget"))
    assert new.status == "ready" and new.version == 2
    assert new.constraints.people == old.constraints.people
    assert new.constraints.must_visit == old.constraints.must_visit
    for field in ["hotel_per_room_night", "food_per_person_day", "ticket_per_person_visit"]:
        assert getattr(new.constraints, field) == getattr(old.constraints, field)
    assert service.repository.get(old.id, 1).constraints.budget == 1
    with pytest.raises(VersionConflict):
        await service.revise(old.id, RevisionRequest(base_version=1, adjustment_id="increase_budget"))


async def test_too_many_required_places_offers_extra_day_without_dropping_any(service, constraints):
    constraints.days = 1
    constraints.must_visit = ["广东省博物馆", "广州塔", "陈家祠", "沙面"]
    r = await service.create(TripRequest(constraints=constraints))
    assert r.status == "needs_review" and r.issues[0].code == "too_many_activities"
    assert not any(t["step"] == "validation" for t in r.trace)
    new = await service.revise(r.id, RevisionRequest(base_version=1, adjustment_id="extra_day"))
    assert new.status == "ready" and new.constraints.days == 2
    assert new.constraints.must_visit == constraints.must_visit


async def test_last_day_departure_ends_at_selected_station(service, constraints):
    c = Constraints.model_validate({**constraints.model_dump(), "departure_time": "18:00",
        "anchors": {"广州": {"departure_station_id": "440100_station"}}})
    r = await service.create(TripRequest(constraints=c))
    assert r.status == "ready", r.issues
    last = r.days[-1].events[-1]
    assert last.end_location == "113.321,23.117" and last.title.startswith("到达离开车站")
    assert last.end <= "18:00"


async def test_selected_hotel_and_stations_change_real_coordinates_and_reject_wrong_city(service, constraints):
    c = Constraints.model_validate({**constraints.model_dump(), "cities": ["广州", "深圳"], "days": 3,
        "anchors": {"广州": {"hotel_id": "440100_hotel_east", "departure_station_id": "440100_station"},
                    "深圳": {"arrival_station_id": "440300_station"}}})
    r = await service.create(TripRequest(constraints=c))
    assert r.status == "ready", r.issues
    assert any("东侧演示酒店" in e.title for d in r.days for e in d.events)
    assert any(e.title == "前往出发车站" for d in r.days for e in d.events)
    assert any(e.title.endswith("城际交通") and e.end_location == "114.061,22.548" for d in r.days for e in d.events)
    bad = Constraints.model_validate({**constraints.model_dump(), "anchors": {"广州": {"hotel_id": "440300_hotel"}}})
    failed = await service.create(TripRequest(constraints=bad))
    assert failed.status == "needs_input" and failed.issues[0].code == "anchor_invalid"


async def test_optimization_compares_actual_fixed_route_durations(constraints):
    provider = FixtureProvider()
    local = await provider.city("广州", constraints)
    available = {p.id: p for p in local.places}
    ids = ["440100_13", "440100_2", "440100_0"]
    order, measured = await optimize_order(ids, available, local.location, local.location, local, constraints, provider)
    assert set(order) == set(ids) and measured["after_minutes"] < measured["before_minutes"]
    assert provider.calls <= 9
    assert measured["after_ids"] == order


async def test_amap_adapter_optimization_uses_http_route_durations(constraints):
    import math

    from app.schemas import CityFacts
    points = [Place(id=f"p{i}", name=f"模拟位置{i}", city="广州", location=f"{113 + x},23", evidence_id="ev")
              for i, x in enumerate((0.01, 0.03, 0.09))]
    def response(request):
        a = float(request.url.params["origin"].split(",")[0])
        b = float(request.url.params["destination"].split(",")[0])
        seconds = math.ceil(abs(a - b) * 36000 + 600)
        return httpx.Response(200, json={"status": "1", "route": {"transits": [{"distance": "1000",
            "cost": {"duration": str(seconds), "transit_fee": "3"}}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        provider = AmapProvider("test", client=client)
        f = CityFacts(city="广州", citycode="020", adcode="440100", location="113,23", places=points)
        order, measured = await optimize_order(["p2", "p0", "p1"], {p.id: p for p in points}, f.location, f.location, f, constraints, provider)
        assert order == ["p0", "p1", "p2"] and measured["after_minutes"] < measured["before_minutes"]
        assert provider.calls == 7  # One shared HTTP leg is served by the request cache.


async def test_opening_later_than_available_window_does_not_schedule_early(service, constraints):
    c = Constraints.model_validate({**constraints.model_dump(), "days": 1, "daily_end": "11:00", "visit_minutes": 30,
        "must_visit": ["广东省博物馆"], "opening_rules": [{"place_id": "440100_0", "on_date": str(constraints.start_date),
            "start": "15:00", "end": "17:00", "source": "测试提供的下午开放说明"}]})
    r = await service.create(TripRequest(constraints=c))
    assert r.status == "needs_review" and any(i.code == "opening_conflict" for i in r.issues)
    assert not any(e.place_id == "440100_0" for d in r.days for e in d.events)


def test_municipality_and_regular_city_membership_do_not_accept_other_cities():
    assert AmapProvider.valid_poi({"id": "a", "name": "b", "location": "116,40", "adcode": "110108"}, {"adcode": "110000"})
    assert not AmapProvider.valid_poi({"id": "a", "name": "b", "location": "113,23", "adcode": "440300"}, {"adcode": "440100"})


async def test_rain_replacement_keeps_other_days_and_recalculates_budget(service, constraints):
    service.provider_factory = lambda: FixtureProvider(scenario="rain")
    old = await service.create(TripRequest(constraints=constraints))
    visits = [e for e in old.days[0].events if e.kind == "visit"]
    used = {e.place_id for d in old.days for e in d.events if e.kind == "visit"}
    assert all(p.id not in used for d in old.days for p in d.indoor_options)
    option = next(p for p in old.days[0].indoor_options if p.id not in used)
    ids = [visits[0].place_id, option.id]
    new = await service.revise(old.id, RevisionRequest(base_version=1, target_day=1, changes={"day_place_ids": {1: ids}}))
    assert new.status == "ready", new.issues
    assert new.days[1] == old.days[1]
    assert option.id in [e.place_id for e in new.days[0].events if e.kind == "visit"]
    assert not audit_result(new)


async def test_global_change_expands_scope_instead_of_claiming_other_days_preserved(service, constraints):
    old = await service.create(TripRequest(constraints=constraints))
    new = await service.revise(old.id, RevisionRequest(base_version=1, target_day=2, changes={"people": 3}))
    assert new.revision_scope["expanded"] and new.revision_scope["preserved_days"] == []
    assert new.constraints.people == 3 and new.budget.total != old.budget.total


async def test_initial_afternoon_rule_changes_only_afternoon_duration(service, constraints):
    c = Constraints.model_validate({**constraints.model_dump(), "day_rules": [{"day": 1, "period": "afternoon",
        "start": "15:00", "end": "18:00", "visit_minutes": 30}]})
    r = await service.create(TripRequest(constraints=c))
    assert r.status == "ready", r.issues
    visits = [e for e in r.days[0].events if e.kind == "visit"]
    assert visits[0].start < "13:00" and visits[-1].start >= "15:00"
    assert not audit_result(r)


def test_history_list_compare_clone_and_reopen(service, constraints):
    api = TestClient(create_app(service.settings, service))
    old = api.post("/trips", json={"constraints": constraints.model_dump(mode="json")}).json()
    new = api.post(f"/trips/{old['id']}/revise", json={"base_version": 1, "changes": {"budget": 5000}}).json()
    assert api.get("/trips?query=广州").json()[0]["id"] == old["id"]
    assert api.get("/trips?query=深圳").json() == []
    assert [v["version"] for v in api.get(f"/trips/{old['id']}/versions").json()] == [2, 1]
    diff = api.get(f"/trips/{old['id']}/compare?from_version=1&to_version=2").json()
    assert any(r["field"] == "budget" and r["before"] == 6000 and r["after"] == 5000 for r in diff["constraints"])
    clone = api.post(f"/trips/{old['id']}/clone", json={"version": 1}).json()
    assert clone["version"] == 1 and clone["id"] != old["id"] and clone["constraints"]["budget"] == 6000
    assert Repository(service.settings.db_path).get(old["id"]).version == new["version"]
    assert api.get(f"/trips/{old['id']}/export?version=2").text.startswith("# 旅行计划")


async def test_live_adapter_and_autogen_full_pipeline_with_mock_http(tmp_path, constraints):
    poi = [{"id": "amap-museum", "name": "规范博物馆", "adcode": "440106", "typecode": "140100", "location": "113.32,23.11"},
           {"id": "amap-park", "name": "规范公园", "adcode": "440106", "typecode": "110100", "location": "113.30,23.12"}]
    hotel = {"id": "amap-hotel", "name": "已查坐标酒店", "adcode": "440106", "typecode": "100100", "location": "113.31,23.10"}

    def respond(request):
        path = request.url.path
        if "geocode" in path:
            body = {"geocodes": [{"location": "113.26,23.12", "citycode": "020", "adcode": "440100"}]}
        elif "weather" in path:
            body = {"forecasts": [{"casts": [{"date": str(constraints.start_date + timedelta(days=i)), "dayweather": "晴"} for i in range(2)]}]}
        elif "direction" in path:
            body = {"route": {"transits": [{"distance": "2000", "cost": {"duration": "600", "transit_fee": "3"},
                "segments": [{"bus": {"buslines": [{"name": "示例地铁", "departure_stop": {"name": "A站"}, "arrival_stop": {"name": "B站"}}]}}]}]}}
        elif "detail" in path:
            body = {"pois": [hotel]}
        else:
            body = {"pois": [hotel] if request.url.params.get("keywords") == "酒店" else poi}
        return httpx.Response(200, json={"status": "1", **body})

    call = CreateResult(finish_reason="function_calls", content=[FunctionCall(id="one", name="collect_city", arguments='{"city":"广州"}')],
                        usage=RequestUsage(prompt_tokens=10, completion_tokens=5), cached=False)
    selection = {"days": [{"day": 1, "city": "广州", "place_ids": ["amap-museum"], "reason": "固定回放"},
                          {"day": 2, "city": "广州", "place_ids": ["amap-park"], "reason": "固定回放"}]}
    model = ReplayChatCompletionClient([call, '{"status":"collected"}', json.dumps(selection), '{"approved":true,"issues":[]}'],
        model_info={"vision": False, "function_calling": True, "json_output": True, "family": "unknown", "structured_output": False})
    settings = Settings(mode="live", db_path=str(tmp_path / "mock-live.db"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = TravelService(settings, Repository(settings.db_path),
            provider_factory=lambda: AmapProvider("test-key-never-persist", client=client),
            agents_factory=lambda s, t: AutoGenAgents(s, t, client=model))
        c = Constraints.model_validate({**constraints.model_dump(), "anchors": {"广州": {"hotel_id": "amap-hotel"}}})
        r = await service.create(TripRequest(constraints=c))
        assert r.status == "ready", r.issues
        assert r.metrics.model_calls == 4 and r.metrics.tool_calls > 0
        assert any(e.route_segments == ["示例地铁 → A站 → B站"] for d in r.days for e in d.events)
        assert any(e.kind == "live" for e in r.evidence)
        assert "test-key-never-persist" not in r.model_dump_json()
        assert not any(p.indoor is True for d in r.days for p in d.indoor_options)
        assert not audit_result(r)


async def test_live_search_rejects_cross_city_and_same_name_branches_require_choice(constraints):
    records = [{"id": "east", "name": "同名馆", "adcode": "440106", "location": "113.3,23.1", "address": "东馆"},
               {"id": "west", "name": "同名馆", "adcode": "440104", "location": "113.2,23.1", "address": "西馆"},
               {"id": "other-city", "name": "同名馆", "adcode": "440300", "location": "114,22"}]
    def response(request):
        body = {"geocodes": [{"adcode": "440100", "citycode": "020", "location": "113,23"}]} if "geocode" in request.url.path else {"pois": records}
        return httpx.Response(200, json={"status": "1", **body})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        provider = AmapProvider("test", client=client)
        candidates = await provider.search("广州", "同名馆", constraints)
        assert {p.id for p in candidates} == {"east", "west"}
        from app.names import resolve_names
        from app.schemas import CityFacts
        facts = {"广州": CityFacts(city="广州", citycode="020", adcode="440100", location="113,23", places=candidates)}
        c = Constraints.model_validate({**constraints.model_dump(), "must_visit": ["同名馆"]})
        _, questions = await resolve_names(c, facts, provider)
        assert len(questions) == 1 and len(questions[0].options) == 2


async def test_scoped_legacy_payload_expands_when_day_costs_not_available(service, constraints):
    old = await service.create(TripRequest(constraints=constraints))
    for i in old.budget.items:
        i.day = None
    service.repository.get = lambda *args: old
    new = await service.revise(old.id, RevisionRequest(base_version=1, target_day=2,
        changes={"day_rules": [{"day": 2, "start": "13:00"}]}))
    assert new.revision_scope["expanded"] and "旧版" in new.revision_scope["explanation"]


async def test_extended_report_includes_each_workflow_step_and_unknown_cost(tmp_path, monkeypatch):
    from scripts.evaluate import evaluate
    monkeypatch.setenv("MODEL_INPUT_PRICE_PER_MILLION", "")
    monkeypatch.setenv("MODEL_OUTPUT_PRICE_PER_MILLION", "")
    assert await evaluate(output=str(tmp_path / "report"), suite="extended", max_cases=7)
    report = json.loads((tmp_path / "report/report.json").read_text(encoding="utf-8"))
    confirmed = next(r for r in report["results"] if r["id"] == "ext04")
    assert [r["status"] for r in confirmed["workflow_results"]] == ["needs_input", "ready"]
    assert confirmed["workflow_requests"] == 2
    assert confirmed["workflow_metrics"]["estimated_model_cost_cny"] is None
    assert confirmed["workflow_metrics"]["tool_calls"] == sum(r["metrics"]["tool_calls"] for r in confirmed["workflow_results"])
