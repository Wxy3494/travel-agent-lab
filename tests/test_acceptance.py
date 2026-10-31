import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient
from fastapi.testclient import TestClient

from app.agents import AutoGenAgents
from app.audit import audit_result
from app.config import Settings
from app.main import create_app
from app.providers import AmapProvider, FixtureProvider, ProviderError
from app.schemas import Constraints, RevisionRequest, TripRequest
from app.service import TravelService
from app.storage import Repository, VersionConflict


def replay(responses):
    return ReplayChatCompletionClient(responses, model_info={
        "vision": False, "function_calling": True, "json_output": True,
        "family": "unknown", "structured_output": False})


def tool_call(city="广州"):
    return CreateResult(finish_reason="function_calls", content=[FunctionCall(
        id="read-city", name="collect_city", arguments=json.dumps({"city": city}))],
        usage=RequestUsage(prompt_tokens=10, completion_tokens=5), cached=False)


async def test_autogen_complete_cooperation_repairs_hallucinated_poi(tmp_path, constraints):
    bad = {"days": [{"day": 1, "city": "广州", "place_ids": ["made-up"], "reason": "草案"},
                    {"day": 2, "city": "广州", "place_ids": ["440100_2"], "reason": "草案"}]}
    good = {"days": [{"day": 1, "city": "广州", "place_ids": ["440100_0"], "reason": "文化"},
                     {"day": 2, "city": "广州", "place_ids": ["440100_2"], "reason": "文化"}]}
    client = replay([tool_call(), '{"status":"collected"}', json.dumps(bad),
        '{"approved":false,"issues":["地点ID缺少来源，请选择已查询地点"]}',
        json.dumps(good), '{"approved":true,"issues":[]}'])
    settings = Settings(mode="live", db_path=str(tmp_path / "replay.db"))
    service = TravelService(settings, Repository(settings.db_path),
        provider_factory=FixtureProvider, agents_factory=lambda s, t: AutoGenAgents(s, t, client=client))
    result = await service.create(TripRequest(constraints=constraints))
    assert result.status == "ready", result.issues
    validations = [t for t in result.trace if t["step"] == "validation"]
    assert len(validations) == 2 and "unknown_place" in validations[0]["error_codes"]
    assert result.metrics.model_calls == 6
    assert {t["step"] for t in result.trace} >= {"researcher", "planner", "reviewer"}
    assert not audit_result(result)
    assert "made-up" not in result.model_dump_json()


async def test_nlp_revision_patch_preserves_every_unmodified_constraint(constraints):
    constraints = Constraints.model_validate({**constraints.model_dump(), "transport": "driving", "rooms": 2,
        "excluded": ["沙面"], "return_to_origin": True, "interests": ["公园"]})
    client = ReplayChatCompletionClient(['{"budget":4500}'])
    agents = AutoGenAgents(Settings(), [], client=client)
    actual = await agents.extract("预算改成4500元", constraints, revision=True)
    assert actual.model_dump() == {**constraints.model_dump(), "budget": 4500}
    await agents.close()


async def test_nlp_missing_fields_remain_missing():
    client = ReplayChatCompletionClient(['{"cities":["广州"],"days":2}'])
    agents = AutoGenAgents(Settings(), [], client=client)
    actual = await agents.extract("广州玩两天", Constraints())
    assert actual.missing() == ["出发城市", "出发日期", "人数", "全团总预算（人民币）"]
    await agents.close()


async def test_model_budget_stops_tool_reflection(constraints):
    client = replay([tool_call(), '{"status":"collected"}'])
    agents = AutoGenAgents(Settings(max_model_calls=1), [], client=client)
    with pytest.raises(ProviderError) as exc:
        await agents.research(constraints, FixtureProvider(), "multi")
    assert exc.value.code == "model_limit" and agents.client.calls == 1
    await agents.close()


async def test_tool_errors_survive_autogen_tool_boundary(constraints):
    client = replay([tool_call(), '{"status":"collected"}'])
    agents = AutoGenAgents(Settings(), [], client=client)
    with pytest.raises(ProviderError) as exc:
        await agents.research(constraints, FixtureProvider(scenario="rate_limit"), "multi")
    assert exc.value.code == "upstream_rate_limit"
    await agents.close()


async def test_injected_poi_cannot_add_unapproved_tools(tmp_path, constraints):
    # Exercise real AutoGen on adversarial tool content; replay is not model resistance evidence.
    client = replay([tool_call(), '{"status":"collected"}'])
    trace = []
    agents = AutoGenAgents(Settings(), trace, client=client)
    facts = await agents.research(constraints, FixtureProvider(scenario="injection"), "multi")
    assert "自动付款" in facts["广州"].places[0].name
    assert trace[0]["events"].count("ToolCallExecutionEvent") == 1
    await agents.close()


async def test_output_audit_detects_mutated_date_money_and_source(service, constraints):
    result = await service.create(TripRequest(constraints=constraints))
    result.days[1].date = result.days[0].date
    result.budget.total.high += 100
    result.days[0].events[0].evidence_ids = ["unknown"]
    codes = {i.code for i in audit_result(result)}
    assert {"audit_date_sequence", "audit_budget_arithmetic", "audit_source_missing"} <= codes


async def test_concurrent_revision_only_one_version_wins(service, constraints):
    original = await service.create(TripRequest(constraints=constraints))
    results = await asyncio.gather(*[service.revise(original.id, RevisionRequest(
        base_version=1, changes={"budget": amount})) for amount in (5000, 7000)], return_exceptions=True)
    assert sum(isinstance(r, VersionConflict) for r in results) == 1
    assert service.repository.get(original.id).version == 2
    assert service.repository.get(original.id, 1).constraints.budget == 6000


@pytest.mark.parametrize("duration,distance", [("NaN", "1000"), ("Infinity", "1000"), ("600", None)])
async def test_malformed_route_data_cannot_be_validated(duration, distance):
    body = {"status": "1", "route": {"transits": [{"cost": {"duration": duration}, "distance": distance}]}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
        provider = AmapProvider("test", client=client)
        with pytest.raises(ProviderError):
            await provider.route("113,23", "114,22", "020", "0755", "public")


def test_invalid_input_not_echoed_in_http_error(service):
    api = TestClient(create_app(service.settings, service))
    secret = "fake-private-value"
    response = api.post("/trips", json={"constraints": {"people": secret}})
    assert response.status_code == 422 and secret not in response.text


async def test_bare_sensitive_number_not_stored(service, constraints):
    private = "6222000000000000000"
    result = await service.create(TripRequest(prompt=private, constraints=constraints))
    assert result.status == "blocked"
    assert private not in service.repository.get(result.id).model_dump_json()


async def test_three_city_low_budget_retains_constraint_failure(service, constraints):
    c = Constraints.model_validate({**constraints.model_dump(), "cities": ["广州", "深圳", "珠海"],
                                   "days": 7, "budget": 1})
    result = await service.create(TripRequest(constraints=c))
    assert result.status == "needs_review"
    assert any(i.code == "over_budget" for i in result.issues)
    assert result.metrics.tool_calls < 60


@pytest.mark.parametrize("changes", [{"timeout": float("inf")}, {"max_model_calls": 0}, {"input_price": -1}])
def test_config_execution_limits_validated(changes):
    with pytest.raises(ValueError):
        replace(Settings(), **changes)
