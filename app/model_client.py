"""Budget each actual SDK request, including tool reflection and JSON repair.

SDK retries are disabled in production so this count also bounds outbound requests.
No messages or model thought content are retained by this wrapper.
"""
from autogen_core.models import ChatCompletionClient

from app.providers import ProviderError


class LimitedClient(ChatCompletionClient):
    def __init__(self, delegate, limit):
        self.delegate, self.limit, self.calls = delegate, limit, 0

    def tick(self):
        if self.calls >= self.limit:
            raise ProviderError("model_limit", "达到本次模型请求次数上限")
        self.calls += 1

    async def create(self, messages, **kwargs):
        self.tick()
        if not kwargs.get("tools") and kwargs.get("json_output") is None:
            kwargs["json_output"] = True
        return await self.delegate.create(messages, **kwargs)

    async def create_stream(self, messages, **kwargs):
        self.tick()
        async for chunk in self.delegate.create_stream(messages, **kwargs):
            yield chunk

    async def close(self):
        await self.delegate.close()

    def actual_usage(self):
        return self.delegate.actual_usage()

    def total_usage(self):
        return self.delegate.total_usage()

    def count_tokens(self, messages, **kwargs):
        return self.delegate.count_tokens(messages, **kwargs)

    def remaining_tokens(self, messages, **kwargs):
        return self.delegate.remaining_tokens(messages, **kwargs)

    @property
    def capabilities(self):
        return self.delegate.capabilities

    @property
    def model_info(self):
        return self.delegate.model_info
