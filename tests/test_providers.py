import httpx
import pytest

from app.providers import AmapProvider, ProviderError


async def test_retries_transient_response_and_caches():
    attempts = []
    def respond(request):
        attempts.append(request)
        return httpx.Response(503) if len(attempts) == 1 else httpx.Response(200, json={"status": "1", "geocodes": []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AmapProvider("secret-value", client=client)
        await provider.get("/v3/geocode/geo", {"address": "广州"})
        await provider.get("/v3/geocode/geo", {"address": "广州"})
        assert len(attempts) == 2
        assert provider.calls == 2


async def test_provider_error_redacts_response_and_key():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"status": "0", "infocode": "10001", "info": "secret-value"}))) as client:
        provider = AmapProvider("secret-value", client=client)
        with pytest.raises(ProviderError) as caught:
            await provider.get("/v3/geocode/geo", {})
        assert "secret-value" not in str(caught.value)
        assert caught.value.code == "amap_10001"


@pytest.mark.parametrize("recovers", [True, False])
async def test_business_qps_limit_retries_once_and_counts_calls(recovers):
    calls = []

    def respond(request):
        calls.append(request)
        if recovers and len(calls) == 2:
            return httpx.Response(200, json={"status": "1", "geocodes": []})
        return httpx.Response(200, json={"status": "0", "infocode": "10021", "info": "secret-value"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AmapProvider("secret-value", max_calls=2, client=client)
        if recovers:
            assert (await provider.get("/v3/geocode/geo", {}))["status"] == "1"
        else:
            with pytest.raises(ProviderError) as caught:
                await provider.get("/v3/geocode/geo", {})
            assert caught.value.code == "amap_10021"
            assert "secret-value" not in str(caught.value)
        assert len(calls) == provider.calls == 2


@pytest.mark.parametrize("transport,body", [
    ("public", {"transits": [{"distance": "150000", "cost": {"duration": "7200", "transit_fee": "80"}}]}),
    ("driving", {"paths": [{"distance": "150000", "cost": {"duration": "7200", "tolls": "40"}}]}),
])
async def test_route_normalization_and_price_units(transport, body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"status": "1", "route": body}))) as client:
        provider = AmapProvider("test-key", client=client)
        result = await provider.route("113,23", "114,22", "020", "0755", transport)
        assert result["minutes"] == 120
        assert result["cost"].high > result["cost"].low
        if transport == "driving":
            assert result["cost"].low == 105  # must not report tolls as whole-trip expense
        assert "test-key" not in str(provider.evidence)


async def test_missing_duration_never_becomes_zero_travel():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            "status": "1", "route": {"transits": [{"distance": "1000"}]}}))) as client:
        provider = AmapProvider("test-key", client=client)
        with pytest.raises(ProviderError, match="耗时"):
            await provider.route("1,2", "3,4", "020", "0755", "public")


async def test_poi_queries_request_administrative_fields(constraints):
    """AMap base POI responses may omit adcode; all search paths need extended fields."""
    poi_requests = []

    def respond(request):
        if "geocode" in request.url.path:
            body = {"geocodes": [{"adcode": "440100", "citycode": "020", "location": "113.26,23.13"}]}
        elif "weather" in request.url.path:
            body = {"forecasts": []}
        else:
            poi_requests.append(request)
            row = {"id": "museum", "name": "博物馆", "location": "113.32,23.11", "typecode": "140100"}
            if request.url.params.get("extensions") == "all":
                row["adcode"] = "440106"
            if request.url.params.get("keywords") == "酒店":
                row.update(id="hotel", name="酒店", typecode="100100")
            body = {"pois": [row]}
        return httpx.Response(200, json={"status": "1", **body})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AmapProvider("test-key", client=client)
        facts = await provider.city("广州", constraints)
        assert facts.places and facts.hotels
        assert await provider.search("广州", "博物馆", constraints)
        assert await provider.by_id("广州", "museum", constraints, "attraction")
        assert poi_requests and all(r.url.params["extensions"] == "all" for r in poi_requests)


@pytest.mark.parametrize("distance,destination_city,accepted", [
    (517, "020", True), (3500, "020", False), (517, "0755", False),
])
async def test_no_transit_uses_only_verified_short_local_walk(distance, destination_city, accepted):
    endpoints = []

    def respond(request):
        endpoints.append(request.url.path)
        body = {"transits": []}
        if "walking" in request.url.path:
            body = {"paths": [{"distance": str(distance), "cost": {"duration": "414"}}]}
        return httpx.Response(200, json={"status": "1", "route": body})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AmapProvider("test-key", client=client)
        if accepted:
            result = await provider.route("113.26,23.14", "113.27,23.13", "020", destination_city, "public")
            assert result["minutes"] == 7
            assert result["cost"].low == result["cost"].high == 0
            assert result["segments"] == ["步行 517 米"]
            assert any("walking" in e.description for e in provider.evidence.values())
        else:
            with pytest.raises(ProviderError) as caught:
                await provider.route("113.26,23.14", "113.27,23.13", "020", destination_city, "public")
            assert caught.value.code == "route_not_found"
        assert ("/v5/direction/walking" in endpoints) == (destination_city == "020")
