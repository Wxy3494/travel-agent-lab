import asyncio
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from app.assistant import AssistantDecision, AssistantRequest, AssistantWireDecision, TourAssistant
from app.main import create_app
from app.planning import light_day_order
from app.schemas import TripRequest


async def test_real_model_aliases_trigger_local_replan_with_concrete_reply(service, constraints):
    trip = await service.create(TripRequest(constraints=constraints))
    async def model(*args):
        return AssistantDecision(action="revise", target_day=2, changes={"pace": "relaxed", "walking": "less"},
                                 reply="好的，我会减少第二天的景点，把路线安排得集中些。")
    events = []
    result = await TourAssistant(service, model).handle(trip.id,
        AssistantRequest(base_version=trip.version, request_id=uuid4().hex, message="第二天轻松点，少走路"), events.append)
    saved = service.repository.get(trip.id)
    assert result["changed"] and saved.version == 2 and saved.days[0] == trip.days[0]
    assert saved.constraints.budget == trip.constraints.budget and saved.constraints.day_rules[0].reduce_walking
    assert len([e for e in saved.days[1].events if e.kind == "visit"]) < len([e for e in trip.days[1].events if e.kind == "visit"])
    assert "第2天" in result["reply"] and "全团预算仍为6000元" in result["reply"] and "其他日期保留" in result["reply"]
    assert [e["stage"] for e in events] == ["understanding", "planning", "checking"]
    assert "减少第二天" in events[1]["message"]


async def test_confirmation_after_success_is_answer_only(service, constraints):
    trip = await service.create(TripRequest(constraints=constraints))
    async def bad_model(*args):
        return AssistantDecision(action="revise", changes={"pace": "relaxed", "walking": "less"})
    result = await TourAssistant(service, bad_model).handle(trip.id,
        AssistantRequest(base_version=1, request_id=uuid4().hex, message="就这样"))
    assert not result["changed"] and result["trip"]["version"] == 1
    assert "当前这版" in result["reply"]


async def test_legacy_rejected_preference_can_be_confirmed_without_retyping(service, constraints):
    trip = await service.create(TripRequest(constraints=constraints))
    repo = service.repository
    old = repo.begin_chat(trip.id, uuid4().hex, "第二天轻松点，少走路", 1)
    repo.finish_chat(old["claim"], {"reply": "我需要先核对相关地点或预约信息，再进行调整。请直接说明想改变的条件。",
        "trip": trip.model_dump(mode="json"), "changed": False})
    seen = []
    async def model(message, *args):
        seen.append(message)
        return AssistantDecision(action="revise", changes={"pace": "relaxed", "walking": "less"}, target_day=2)
    result = await TourAssistant(service, model).handle(trip.id,
        AssistantRequest(base_version=1, request_id=uuid4().hex, message="就这样"))
    assert seen == ["第二天轻松点，少走路"] and result["changed"]


async def test_stream_sends_model_acknowledgement_and_final_saved_result(service, constraints):
    trip = await service.create(TripRequest(constraints=constraints))
    api = create_app(service.settings, service)
    async def model(*args):
        await asyncio.sleep(.02)
        return AssistantDecision(action="revise", changes={"pace": "relaxed", "walking": "less"}, target_day=2,
            reply="我会把第二天安排得轻松些，优先减少来回步行。")
    api.state.tour_assistant = TourAssistant(service, model)
    request = {"base_version": 1, "request_id": uuid4().hex, "message": "第二天轻松点，少走路"}
    with TestClient(api).stream("POST", f"/trips/{trip.id}/chat/stream", json=request) as response:
        events = [json.loads(line) for line in response.iter_lines() if line]
    assert events[0]["stage"] == "understanding" and events[1]["stage"] == "planning"
    assert "轻松些" in events[1]["message"] and events[-1]["type"] == "result"
    assert events[-1]["data"]["changed"] and events[-1]["data"]["trip"]["version"] == 2
    with TestClient(api).stream("POST", f"/trips/{trip.id}/chat/stream", json=request) as response:
        replay = [json.loads(line) for line in response.iter_lines() if line]
    assert replay == [events[-1]] and len(service.repository.versions(trip.id)) == 2


def test_model_schema_exposes_allowed_preferences_and_day_rule_shape():
    schema = AssistantWireDecision.model_json_schema()
    props = schema["$defs"]["AssistantPreferences"]["properties"]
    assert "day_rules" in props and "reservations" not in props and "opening_rules" not in props
    assert "reduce_walking" in schema["$defs"]["DayRule"]["properties"]


async def test_reduce_walking_compares_map_routes_and_keeps_mandatory_place(service, constraints):
    from app.providers import FixtureProvider
    provider = FixtureProvider()
    facts = await provider.city("广州", constraints)
    places = facts.places[:3]
    constraints.must_visit = [places[2].name]
    # Each supplied route has actual walking segments; expensive optional place must be dropped.
    class WalkingProvider:
        max_calls, calls = 100, 0
        async def route(self, a, b, *args):
            self.calls += 1
            walk = 1000 if places[0].location in {a, b} else 10
            return {"minutes": 10, "steps": [{"kind": "walk", "distance": walk}]}
    ids, comparison = await light_day_order([p.id for p in places], {p.id: p for p in places},
        facts.hotels[0].location, facts.hotels[0].location, facts, constraints, WalkingProvider())
    assert places[2].id in ids and places[0].id not in ids
    assert comparison["after_walk_m"] < comparison["before_walk_m"]


async def test_live_style_unknown_admissions_do_not_discard_local_revision_scope(service, constraints, monkeypatch):
    from app.schemas import VisitCheck
    trip = await service.create(TripRequest(constraints=constraints, autonomous=True))
    async def unknown(point, on_date, *args):
        return VisitCheck(place_id=point.id, place_name=point.name, city=point.city, on_date=on_date, enforced=True)
    monkeypatch.setattr(service.admission_service, "lookup", unknown)
    async def model(*args):
        return AssistantDecision(action="revise", changes={"pace": "relaxed", "walking": "less"}, target_day=2)
    result = await TourAssistant(service, model).handle(trip.id,
        AssistantRequest(base_version=1, request_id=uuid4().hex, message="第二天轻松点，少走路"))
    assert result["changed"] and result["trip"]["status"] == "needs_review"
    assert result["trip"]["days"][0] == trip.days[0].model_dump(mode="json")
    assert result["trip"]["revision_scope"]["preserved_days"] == [1]
    assert "具体提示" in result["reply"]
