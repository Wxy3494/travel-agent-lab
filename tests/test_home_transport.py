"""Home-city journeys are outside the daily sightseeing schedule by default."""
from datetime import date, timedelta
from math import ceil
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from app.audit import audit_result
from app.main import create_app
from app.providers import FixtureProvider
from app.render import render_markdown
from app.schemas import Constraints, PriceRange, RevisionRequest, TripRequest, TripResult


class LongDistanceFixture(FixtureProvider):
    def __init__(self, home_minutes=840, local_minutes=20):
        super().__init__(max_calls=300)
        self.home_minutes, self.local_minutes = home_minutes, local_minutes
        self.route_calls = []

    async def city(self, city, constraints):
        if city in self.cache:
            return self.cache[city]
        base = {"武汉": "广州", "北京": "深圳", "上海": "珠海"}[city]
        facts = (await super().city(base, constraints)).model_copy(deep=True)
        facts.city = city
        facts.citycode = {"武汉": "027", "北京": "010", "上海": "021"}[city]
        for index, point in enumerate(facts.places):
            point.city, point.name = city, city + ("博物馆" if index == 0 else f"测试景点{index}")
        for point in facts.hotels + facts.stations:
            point.city, point.name = city, city + "测试住宿或车站"
        self.cache[city] = facts
        return facts

    async def route(self, origin, destination, origin_city, destination_city, transport):
        self.tick()
        cross = origin_city != destination_city
        self.route_calls.append((origin_city, destination_city))
        ev = self.record("fixture", "离线长距离交通", "固定测试数据，非真实车次或报价")
        return {"minutes": self.home_minutes if cross else self.local_minutes,
            "cost": PriceRange(low=100 if cross else 3, high=100 if cross else 3),
            "evidence_id": ev, "note": "人工测试交通报价", "segments": ["测试交通线路"]}


def request(**changes):
    return TripRequest(constraints=Constraints(origin="武汉", cities=["北京"],
        start_date=date.today() + timedelta(days=2), days=changes.pop("days", 2),
        people=changes.pop("people", 3), budget=changes.pop("budget", 20000),
        return_to_origin=changes.pop("return_to_origin", True), **changes))


async def trip_with(service, **changes):
    service.provider_factory = LongDistanceFixture
    return await service.create(request(**changes))


@pytest.mark.parametrize("days,home_minutes", [(1, 840), (3, 840), (2, 1800)])
async def test_default_long_round_trip_does_not_consume_sightseeing_time(service, days, home_minutes):
    provider = LongDistanceFixture(home_minutes=home_minutes)
    service.provider_factory = lambda: provider
    result = await service.create(request(days=days))
    assert result.status == "ready", result.issues
    assert len(result.days) == days and result.constraints.home_transport_scope == "separate"
    assert not audit_result(result)
    transfers = [t for day in result.days for t in day.home_transfers]
    assert [(t.direction, t.from_city, t.to_city) for t in transfers] == [("outbound", "武汉", "北京"), ("return", "北京", "武汉")]
    assert all(t.estimated_minutes == home_minutes + 45 for t in transfers)
    assert all(any(e.kind == "visit" for e in d.events) for d in result.days)
    assert all("00:00" <= e.start < e.end <= "23:59" for d in result.days for e in d.events)
    assert all(e.city == "北京" for d in result.days for e in d.events)
    assert all(d.travel_minutes < home_minutes for d in result.days)
    assert "从北京住宿区域" in result.days[0].departure_note
    assert sum(1 for a, b in provider.route_calls if a != b) == 2
    exported = render_markdown(result)
    assert "往返交通（单独安排）" in exported and "不占每日游玩时间" in exported
    assert not any(i.severity == "error" for i in result.issues)


@pytest.mark.parametrize("transport,people", [("public", 1), ("public", 3), ("public", 10), ("driving", 3), ("driving", 5), ("driving", 10)])
async def test_external_fares_counted_once_per_person_or_car(service, transport, people):
    result = await trip_with(service, transport=transport, people=people)
    quantity = ceil(people / 4) if transport == "driving" else people
    rows = [i for i in result.budget.items if i.category.startswith(("去程交通：", "返程交通："))]
    assert result.status == "ready" and len(rows) == 2
    assert all(r.quantity == quantity and r.total.low == 100 * quantity and r.event_start is None for r in rows)
    assert sum(i.total.high for i in result.budget.items) == result.budget.total.high
    assert not audit_result(result)


async def test_no_return_choice_and_same_city_do_not_add_spurious_transfers(service):
    result = await trip_with(service, return_to_origin=False)
    assert [t.direction for d in result.days for t in d.home_transfers] == ["outbound"]
    assert not any(i.category.startswith("返程交通：") for i in result.budget.items)
    same_city = request().constraints.model_copy(update={"origin": "北京"})
    result = await service.create(TripRequest(constraints=same_city))
    assert not any(d.home_transfers for d in result.days) and not audit_result(result)


async def test_destination_to_destination_time_is_still_enforced(service):
    c = request(days=3).constraints.model_copy(update={"cities": ["北京", "上海"]})
    service.provider_factory = LongDistanceFixture
    result = await service.create(TripRequest(constraints=c))
    assert result.status == "needs_review"
    assert any(i.code in {"day_too_long", "time_window"} and "北京到上海" in i.message for i in result.issues)


async def test_explicit_old_mode_and_local_time_are_still_enforced(service):
    old = await trip_with(service, home_transport_scope="in_day")
    assert old.status == "needs_review" and any(i.code in {"time_window", "day_too_long"} for i in old.issues)
    service.provider_factory = lambda: LongDistanceFixture(local_minutes=900)
    result = await service.create(request())
    assert result.status == "needs_review" and any(i.code == "day_too_long" for i in result.issues)


async def test_external_cost_still_blocks_over_budget(service):
    result = await trip_with(service, budget=100)
    assert result.status == "needs_review" and any("budget" in i.code for i in result.issues)
    assert len([t for d in result.days for t in d.home_transfers]) == 2


async def test_time_change_refresh_and_partial_revision_do_not_duplicate_fares(service):
    result = await trip_with(service, days=3)
    changed = await service.revise(result.id, RevisionRequest(base_version=1, target_day=2,
        changes={"day_rules": [{"day": 2, "start": "10:00", "end": "18:00"}]}))
    assert changed.status == "ready", changed.issues
    assert len([t for d in changed.days for t in d.home_transfers]) == 2
    assert len([i for i in changed.budget.items if i.category.startswith(("去程交通：", "返程交通："))]) == 2
    assert not audit_result(changed)
    refreshed = await service.refresh_visits(result.id, 2)
    assert refreshed.status == "ready" and not audit_result(refreshed)


@pytest.mark.parametrize("tamper", ["missing_leg", "wrong_city", "wrong_fare", "missing_cost", "wrong_day", "local_time"])
async def test_independent_audit_checks_external_fares_and_still_checks_local_time(service, tamper):
    result = await trip_with(service)
    if tamper == "missing_leg":
        result.days[0].home_transfers = []
    elif tamper == "wrong_city":
        result.days[0].home_transfers[0].from_city = "上海"
    elif tamper == "wrong_fare":
        result.days[0].home_transfers[0].cost.high += 100
    elif tamper == "missing_cost":
        result.budget.items = [i for i in result.budget.items if not i.category.startswith("去程交通：")]
    elif tamper == "wrong_day":
        result.days[1].home_transfers += result.days[0].home_transfers
        result.days[0].home_transfers = []
    else:
        next(e for e in result.days[0].events if e.kind == "visit").end = "23:59"
    assert any(i.code == ("audit_activity_time" if tamper == "local_time" else "audit_home_transport") for i in audit_result(result))


async def test_legacy_saved_json_retains_old_mode_and_new_request_defaults_separate(service, constraints):
    result = await service.create(TripRequest(constraints=constraints))
    raw = result.model_dump(mode="json")
    raw["constraints"].pop("home_transport_scope")
    for day in raw["days"]:
        day.pop("home_transfers")
    loaded = TripResult.model_validate(raw)
    assert loaded.constraints.home_transport_scope == "in_day" and "home_transport_scope" not in raw["constraints"]
    assert not audit_result(loaded) and Constraints().home_transport_scope == "separate"


async def test_form_regeneration_upgrades_old_blocked_trip_to_default_mode(service, monkeypatch):
    old = await trip_with(service, home_transport_scope="in_day")
    assert old.status == "needs_review"
    api = TestClient(create_app(service.settings, service))
    def local(method, url, json=None, **kwargs):
        parsed = urlsplit(url)
        assert parsed.hostname == "127.0.0.1"
        return api.request(method, parsed.path + ("?" + parsed.query if parsed.query else ""), json=json)
    monkeypatch.setattr(httpx, "request", local)
    ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app/ui.py"), default_timeout=10)
    ui.session_state.trip = old.model_dump(mode="json")
    ui.run()
    next(b for b in ui.button if b.label == "重新生成行程").click().run()
    assert not ui.exception
    result = ui.session_state.trip
    assert result["version"] == 2 and result["status"] == "ready"
    assert result["constraints"]["home_transport_scope"] == "separate"
    assert any(e.label == "出发地往返交通（单独安排）" for e in ui.expander)
    assert any("不占每日游玩时间" in i.value for i in ui.info)
    assert not any("超过当天可用" in w.value for w in ui.warning)
