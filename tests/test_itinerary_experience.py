"""Regression coverage for visitor-facing time, lodging and per-day weather."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.agents import DemoAgents
from app.audit import audit_result
from app.config import Settings
from app.names import normalize_cities
from app.presentation import weather_line
from app.providers import AmapProvider, FixtureProvider, ProviderError
from app.render import render_markdown
from app.schemas import Constraints, RevisionRequest, TripRequest
from app.service import TravelService
from app.weather import WeatherReport, itinerary_weather


async def test_return_can_follow_last_activity_deadline_but_visit_cannot(service, constraints):
    class TenMinuteRoutes(FixtureProvider):
        async def route(self, *args):
            return {**await super().route(*args), "minutes": 10}

    service.provider_factory = TenMinuteRoutes
    c = Constraints.model_validate({**constraints.model_dump(), "days": 1, "daily_end": time(10, 50),
        "daily_end_scope": "last_activity", "day_place_ids": {1: ["440100_0"]}})
    trip = await service.create(TripRequest(constraints=c))
    assert trip.status == "ready", trip.issues
    visits = [e for e in trip.days[0].events if e.kind == "visit"]
    assert visits[-1].end == "10:50"
    last = trip.days[0].events[-1]
    assert last.is_return and last.end == "11:10" and not audit_result(trip)
    last.is_return = False
    assert any(i.code == "audit_activity_time" for i in audit_result(trip))
    last.is_return = True
    visits[0].is_return = True
    assert any(i.code == "audit_activity_time" for i in audit_result(trip))
    legacy = await service.create(TripRequest(constraints=c.model_copy(update={"daily_end_scope": "return"})))
    assert legacy.status == "needs_review"


async def test_lodging_coordinates_used_each_day_and_clear_reverts_to_estimate(service, constraints):
    name = "广州东侧演示酒店（非真实酒店）"
    c = Constraints.model_validate({**constraints.model_dump(), "lodging_addresses": {"广州": name}})
    trip = await service.create(TripRequest(constraints=c))
    assert trip.status == "ready", trip.issues
    hotel = trip.days[0].hotel_options[1]
    for day in trip.days:
        assert name in day.departure_note
        assert day.events[0].start_location == hotel.location
        assert day.events[-1].end_location == hotel.location
    revised = await service.revise(trip.id, RevisionRequest(base_version=1, changes={"lodging_addresses": {}}))
    assert revised.status == "ready", revised.issues
    assert all("城市中心估算" in d.departure_note for d in revised.days)
    assert not revised.revision_scope["preserved_days"]
    assert normalize_cities(c.model_copy(update={"lodging_addresses": {"广州市": name}})).lodging_addresses == {"广州": name}


@pytest.mark.parametrize("city,address", [("广州", "不存在的住处"), ("武汉", "一个酒店")])
async def test_unclear_or_wrong_city_lodging_requests_input(service, constraints, city, address):
    c = Constraints.model_validate({**constraints.model_dump(), "lodging_addresses": {city: address}})
    trip = await service.create(TripRequest(constraints=c))
    assert trip.status == "needs_input" and not trip.days
    assert trip.issues[0].code == "lodging_invalid"


@pytest.mark.parametrize("adcode,level,accepted", [
    ("440106", "门牌号", True), ("440305", "门牌号", False), ("440100", "市", False),
])
async def test_live_address_requires_precise_location_in_requested_city(constraints, adcode, level, accepted):
    def respond(request):
        if request.url.path.endswith("/text"):
            data = {"pois": []}
        elif request.url.params.get("address") == "广州":
            data = {"geocodes": [{"location": "113.2,23.1", "adcode": "440100", "citycode": "020"}]}
        else:
            data = {"geocodes": [{"location": "113.3,23.2", "adcode": adcode, "level": level,
                                   "formatted_address": "测试街道1号"}]}
        return httpx.Response(200, json={"status": "1", **data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AmapProvider("private-test-key", client=client)
        if accepted:
            point = await provider.lodging("广州", "测试街道1号", constraints)
            assert point.location == "113.3,23.2" and point.evidence_id in provider.evidence
            assert "private-test-key" not in point.model_dump_json()
        else:
            with pytest.raises(ProviderError, match="不够明确"):
                await provider.lodging("广州", "测试街道1号", constraints)


async def test_same_name_hotel_branches_are_not_silently_selected(constraints):
    def respond(request):
        if request.url.path.endswith("/text"):
            data = {"pois": [{"id": str(i), "name": "同名酒店", "adcode": "440106", "location": f"113.{i},23.1"}
                             for i in (1, 2)]}
        else:
            data = {"geocodes": [{"location": "113.2,23.1", "adcode": "440100", "citycode": "020"}]}
        return httpx.Response(200, json={"status": "1", **data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ProviderError, match="多个同名住宿"):
            await AmapProvider("test-key", client=client).lodging("广州", "同名酒店", constraints)


def test_weather_uses_exact_day_and_never_reuses_out_of_window_forecast():
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    rows = [{"date": str(today + timedelta(days=i)), "dayweather": "小雨", "daytemp": "25.5",
             "nighttemp": "19", "source": "测试天气", "updated_at": "已发布"} for i in range(10)]
    value = itinerary_weather("武汉", today + timedelta(days=6), rows, today=today)
    assert value.available and weather_line(value) == "武汉 · 小雨 · 19～25.5℃"
    assert not itinerary_weather("武汉", today + timedelta(days=7), rows, today=today).available
    assert not itinerary_weather("武汉", today + timedelta(days=1), rows[:1], today=today).available
    assert not itinerary_weather("武汉", today - timedelta(days=1), rows, today=today).available


async def test_seven_day_adapter_attaches_weather_to_actual_cross_city_dates(service, constraints):
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    settings = Settings(mode="live", weather_provider="custom", max_repairs=0)
    planner = TravelService(settings, service.repository, provider_factory=FixtureProvider, agents_factory=DemoAgents)

    async def forecast(city, on_date=None):
        return WeatherReport(city=city, source="七天天气测试服务", updated_at="测试发布时间", forecasts=[
            {"date": today + timedelta(days=i), "day_condition": "小雨" if city == "深圳" else "晴",
             "high_c": 25, "low_c": 18} for i in range(7)])

    planner.weather_service.lookup = forecast
    c = Constraints.model_validate({**constraints.model_dump(), "cities": ["深圳"], "days": 8,
                                   "budget": 100000, "start_date": today})
    trip = await planner.create(TripRequest(constraints=c))
    assert trip.status == "ready", trip.issues
    assert [w.city for w in trip.days[0].weather_details] == ["广州", "深圳"]
    assert all(w.date == today and w.available for w in trip.days[0].weather_details)
    assert trip.days[0].weather_details[1].condition == "小雨"
    assert trip.days[6].weather_details[0].available and not trip.days[7].weather_details[0].available
    assert "七天天气测试服务" in render_markdown(trip) and "18～25℃" in render_markdown(trip)
