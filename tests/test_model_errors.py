import httpx
import pytest
from autogen_ext.models.replay import ReplayChatCompletionClient
from openai import APIStatusError

from app.agents import AutoGenAgents
from app.config import Settings
from app.providers import ProviderError


@pytest.mark.parametrize("status", [402, 401, 500])
async def test_model_balance_error_is_specific_without_leaking_sdk_body(status):
    class ErrorClient(ReplayChatCompletionClient):
        async def create(self, *args, **kwargs):
            response = httpx.Response(status, request=httpx.Request("POST", "https://example.invalid/?key=private-key"))
            raise APIStatusError("private-key and raw provider body", response=response, body={"secret": "private-key"})

    trace = []
    agents = AutoGenAgents(Settings(), trace, client=ErrorClient([]))
    try:
        with pytest.raises(ProviderError) as caught:
            await agents.run("researcher", "test", {})
        assert caught.value.code == ("model_balance_insufficient" if status == 402 else "model_call_failed")
        if status == 402:
            assert "余额不足" in str(caught.value)
        assert trace[0]["status_code"] == status
        assert "private-key" not in str(caught.value) + str(trace)
    finally:
        await agents.close()
