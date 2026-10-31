import json

import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from app.agents import AutoGenAgents
from app.config import Settings
from app.providers import FixtureProvider, ProviderError


async def test_real_autogen_executes_research_tool(constraints):
    calls = CreateResult(finish_reason="function_calls", content=[
        FunctionCall(id="call_1", name="collect_city", arguments=json.dumps({"city": "广州"}, ensure_ascii=False))],
        usage=RequestUsage(prompt_tokens=10, completion_tokens=5), cached=False)
    client = ReplayChatCompletionClient([calls, '{"status":"collected"}'], model_info={
        "vision": False, "function_calling": True, "json_output": True, "family": "unknown", "structured_output": False})
    trace = []
    agents = AutoGenAgents(Settings(), trace, client=client)
    facts = await agents.research(constraints, FixtureProvider(), "multi")
    assert list(facts) == ["广州"]
    assert "ToolCallExecutionEvent" in trace[0]["events"]
    await agents.close()


async def test_autogen_schema_repair_is_bounded(constraints):
    client = ReplayChatCompletionClient(["not-json", "still-not-json"])
    agents = AutoGenAgents(Settings(), [], client=client)
    with pytest.raises(ProviderError) as caught:
        await agents.select(constraints, {}, [])
    assert caught.value.code == "model_schema_invalid"
    assert agents.calls == 2
    await agents.close()


async def test_autogen_schema_repair_can_succeed(constraints):
    valid = {"days": [{"day": 1, "city": "广州", "place_ids": ["a"], "reason": "示例"}]}
    client = ReplayChatCompletionClient(["bad", json.dumps(valid)])
    agents = AutoGenAgents(Settings(), [], client=client)
    result = await agents.select(constraints, {}, [])
    assert result.days[0].place_ids == ["a"]
    await agents.close()
