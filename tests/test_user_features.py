from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.audit import audit_result
from app.config import Settings
from app.input_parser import demo_patch
from app.main import create_app
from app.presentation import day_description, hotel_details, issue_message
from app.providers import FixtureProvider, ProviderError
from app.render import render_markdown
from app.schemas import Constraints, DayChoice, DayRule, Issue, RevisionRequest, Selection, TripRequest
from app.weather import WeatherService


async def test_thirty_days_generates_free_days_and_can_revise_day_thirty(service, constraints):
    c = Constraints.model_validate({**constraints.model_dump(), "days": 30, "budget": 100000})
    trip = await service.create(TripRequest(constraints=c))
    assert trip.status == "ready", trip.issues
    assert len(trip.days) == 30 and trip.days[-1].date == c.start_date + timedelta(days=29)
    visits = [e.place_id for d in trip.days for e in d.events if e.kind == "visit"]
    assert len(visits) == len(set(visits))
    assert any(not any(e.kind == "visit" for e in d.events) for d in trip.days)
    assert not audit_result(trip)
    changed = await service.revise(trip.id, RevisionRequest(base_version=1, instruction="第三十天只安排下午"))
    assert changed.status == "ready", changed.issues
    assert changed.constraints.days == 30 and changed.constraints.day_rules[0].day == 30
    assert changed.days[:29] == trip.days[:29]
    assert not audit_result(changed)


def test_day_limits_and_chinese_thirty_are_consistent():
    assert demo_patch("玩三十天") == {"days": 30}
    assert demo_patch("第三十天只安排下午") == {"day_rules": [{"day": 30, "start": "13:00"}]}
    assert DayRule(day=30).day == RevisionRequest(base_version=1, target_day=30).target_day
    assert len(Selection(days=[DayChoice(day=d, city="广州", place_ids=[], reason="自由活动") for d in range(1, 31)]).days) == 30
    with pytest.raises(ValidationError):
        Constraints(days=31)


async def test_impossible_intercity_transfer_stops_without_repeating_on_each_day(service, constraints):
    calls = []

    class SlowTransfer(FixtureProvider):
        async def route(self, origin, destination, origin_city, destination_city, transport):
            calls.append((origin_city, destination_city))
            route = await super().route(origin, destination, origin_city, destination_city, transport)
            return {**route, "minutes": 2000}

    service.provider_factory = SlowTransfer
    c = Constraints.model_validate({**constraints.model_dump(), "cities": ["深圳"], "days": 4})
    trip = await service.create(TripRequest(constraints=c))
    assert trip.status == "needs_review"
    assert len(calls) == 1 and not trip.days
    assert any("确认实际车次" in issue.message for issue in trip.issues)


async def test_saved_technical_explanations_are_clean_in_export_and_old_download(service, constraints):
    trip = await service.create(TripRequest(constraints=constraints))
    trip.days[0].reason = "按bindings的place_id满足must_visit，不重复其他POI。"
    trip.issues.append(Issue(code="reviewer_issue", message="第1天reason称已安排，但events为空，must_visit未落实。"))
    trip.issues.append(Issue(code="audit_missing_must_visit", message="结果审计未通过：missing_must_visit"))
    document = render_markdown(trip)
    for word in ["bindings", "place_id", "must_visit", "POI", "reason", "events", "audit_"]:
        assert word not in document
    assert "查看地图" in document and "搜索房价" in document
    assert "不是该酒店的报价" in document and "间 / 晚" in document
    trip.markdown = "old raw bindings place_id must_visit"
    old_version = trip.version
    trip.version += 1
    service.repository.save(trip, old_version)
    api = TestClient(create_app(service.settings, service))
    assert "bindings" not in api.get(f"/trips/{trip.id}/export").text
    assert "必去景点" in issue_message(trip.issues[-1])
    assert issue_message(Issue(code="reviewer_issue", message="第2天存在代码校验为error的day_too_long条目，error不可豁免")) == \
        "第2天的交通和游玩安排超过了可用时间，请减少景点或调整出行时间。"
    empty = trip.days[0].model_copy(update={"events": []})
    assert "还没有排出" in day_description(empty, ready=False)


async def test_hotel_links_keep_identity_and_budget_units(service, constraints):
    trip = await service.create(TripRequest(constraints=constraints))
    hotel = trip.days[0].hotel_options[0]
    value = hotel_details(hotel, constraints, trip.days[0].date)
    assert hotel.location in httpx.URL(value["map_url"]).params["position"]
    assert hotel.name in httpx.URL(value["search_url"]).params["q"]
    assert "间 / 晚" in value["budget_label"] and "不是该酒店的报价" in value["price_note"]


def test_weather_endpoint_distinguishes_demo_and_unavailable_future_dates(service, constraints):
    api = TestClient(create_app(service.settings, service))
    response = api.get("/weather", params={"city": "广州", "on_date": str(constraints.start_date + timedelta(days=30))})
    assert response.status_code == 200
    data = response.json()
    assert data["is_demo"] and len(data["forecasts"]) == 4
    assert "尚未提供" in data["message"]
    assert api.get("/weather", params={"city": ""}).status_code == 422


async def test_live_amap_weather_parses_temperatures_and_caches(constraints):
    calls = []

    def respond(request):
        calls.append(request)
        data = {"geocodes": [{"adcode": "440100", "citycode": "020", "location": "113.2,23.1"}]}
        if "weather" in request.url.path:
            data = {"forecasts": [{"reporttime": "2026-10-16 09:00:00", "casts": [{"date": str(constraints.start_date),
                "dayweather": "晴", "nightweather": "多云", "daytemp_float": "27.5", "nighttemp": "19", "daywind": "北", "daypower": "3"}]}]}
        return httpx.Response(200, json={"status": "1", **data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        weather = WeatherService(Settings(mode="live", amap_key="test-key"), client)
        future = await weather.lookup("广州", constraints.start_date + timedelta(days=30))
        report = await weather.lookup("广州", constraints.start_date)
        assert future.message and not report.message
        assert report.forecasts[0].high_c == 27.5 and report.forecasts[0].low_c == 19
        assert not report.is_demo and len(calls) == 2
        assert "test-key" not in report.model_dump_json()


async def test_custom_weather_adapter_is_validated_and_keeps_credentials_private(constraints):
    def respond(request):
        assert request.headers["Authorization"] == "Bearer private-key"
        assert request.url.params["city"] == "广州"
        return httpx.Response(200, json={"city": "广州", "source": "测试天气服务", "forecasts": [
            {"date": str(constraints.start_date), "day_condition": "小雨", "high_c": 23, "low_c": 18}]})

    settings = Settings(mode="live", weather_provider="custom", weather_api_url="https://weather.example/forecast", weather_api_key="private-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await WeatherService(settings, client).lookup("广州")
        assert report.forecasts[0].day_condition == "小雨" and "private-key" not in report.model_dump_json()
        bad = Settings(mode="live", weather_provider="custom", weather_api_url="http://weather.example/forecast?key=private-key")
        with pytest.raises(ProviderError) as caught:
            await WeatherService(bad, client).lookup("广州")
        assert "private-key" not in str(caught.value)
