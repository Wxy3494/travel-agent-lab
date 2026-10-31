"""Admission checks use fixed official-page samples and isolated, read-only API responses."""
from datetime import UTC, date, datetime, time, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.admissions import GD_GUIDE, GD_HOME, PALACE_GUIDE, AdmissionService, admission_issues
from app.agents import DemoAgents
from app.audit import audit_result
from app.config import Settings
from app.main import create_app
from app.planning import materialize
from app.presentation import admission_summary
from app.providers import FixtureProvider, Provider
from app.render import render_markdown
from app.schemas import Constraints, DayChoice, Place, RevisionRequest, Selection, TripRequest, VisitCheck
from app.service import TravelService
from app.storage import Repository


def payload(pid, name, city, on_date, **changes):
    now = datetime.now(UTC)
    return {"place_id": pid, "place_name": name, "city": city, "on_date": str(on_date),
        "opening_status": "open", "windows": [{"start": "09:00", "end": "18:00"}],
        "reservation_required": False, "ticket_price": 20, "scope": "date_specific",
        "source_urls": ["https://venue.example/guide"], "checked_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(), **changes}


def make_service(tmp_path, respond, agents=DemoAgents):
    settings = Settings(db_path=str(tmp_path / "admissions.db"), visit_provider="custom",
                        visit_api_url="https://visits.example/check", visit_api_key="test-private-key")
    service = TravelService(settings, Repository(settings.db_path), agents_factory=agents)
    service.admission_service = AdmissionService(settings, httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    return service


def response_for(request, **changes):
    assert request.method == "GET" and request.headers["Authorization"] == "Bearer test-private-key"
    p = request.url.params
    return httpx.Response(200, json=payload(p["place_id"], p["name"], p["city"], p["date"], **changes))


def one_place(constraints, **changes):
    return Constraints.model_validate({**constraints.model_dump(), "days": 1,
        "must_visit": ["广东省博物馆"], "day_place_ids": {1: ["440100_0"]}, **changes})


def booked(c, start="09:00", end="12:00", **changes):
    return {"place_id": "440100_0", "on_date": str(c.start_date), "people": c.people,
            "entry_window": {"start": start, "end": end}, **changes}


async def test_demo_does_not_fetch_or_claim_real_verification(service, constraints, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("demo must not query official websites")
    monkeypatch.setattr(service.admission_service, "page", forbidden)
    result = await service.create(TripRequest(constraints=constraints))
    assert result.status == "ready" and result.visit_checks
    assert all(not c.enforced and c.authority == "fixture" for c in result.visit_checks)
    assert "未作真实入场核验" in render_markdown(result)


@pytest.mark.parametrize("venue,city,address,url,day,price,last", [
    ("广东省博物馆", "广州", "珠江东路2号", GD_GUIDE, date(2026, 11, 3), 0, time(16)),
    ("故宫博物院", "北京", "景山前街4号", PALACE_GUIDE, date(2026, 10, 30), 60, time(16)),
    ("故宫博物院", "北京", "景山前街4号", PALACE_GUIDE, date(2026, 11, 3), 40, time(15, 30)),
])
async def test_official_rules_cache_and_seasonal_ticket(constraints, venue, city, address, url, day, price, last):
    calls = []
    def respond(request):
        calls.append(str(request.url))
        body = "免费向公众开放" if str(request.url) == GD_HOME else (
            "9:00-17:00 16:00停止入场 周一闭馆 实名制预约" if str(request.url) == GD_GUIDE else
            "8:30 17:00 16:30 15:30 周一闭馆 60元/人 40元/人 预约")
        return httpx.Response(200, text=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        admission = AdmissionService(Settings(), client)
        provider = Provider()
        point = Place(id="venue", name=venue, city=city, address=address, location="113,23", evidence_id="map")
        first = await admission.lookup(point, day, constraints, provider, [0])
        again = await admission.lookup(point, day + timedelta(days=7), constraints, provider, [0])
        assert first.authority == "official" and first.opening_status == "open"
        assert first.ticket_price == price and first.last_entry == last and first.reservation_required
        assert first.checked_at == again.checked_at and len(calls) == (2 if city == "广州" else 1)
        assert url in first.source_urls and first.evidence_id in provider.evidence
        assert "SHA256" in provider.evidence[first.evidence_id].description
        monday = await admission.lookup(point, date(2026, 11, 2), constraints, provider, [0])
        assert monday.opening_status == "closed" and "例外" in monday.note


@pytest.mark.parametrize("failure", ["drift", "timeout", "wrong_branch"])
async def test_official_drift_or_wrong_branch_stays_unknown(constraints, failure):
    calls = []
    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private raw error", request=request)
        return httpx.Response(200, text="new layout with no trusted rule fingerprints")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        admission = AdmissionService(Settings(), client)
        point = Place(id="venue", name="广东省博物馆", city="广州", address="其他分馆" if failure == "wrong_branch" else "珠江东路2号",
                      location="113,23", evidence_id="map")
        check = await admission.lookup(point, constraints.start_date, constraints, Provider(), [0])
        assert check.opening_status == "unknown" and check.ticket_price is None
        assert check.authority == "unknown" and "private" not in check.model_dump_json()
        if failure == "wrong_branch":
            assert not calls


@pytest.mark.parametrize("mutation", [
    {"place_id": "another"}, {"city": "深圳"}, {"on_date": "2020-01-01"}, {"place_name": "another branch"},
    {"checked_at": None}, {"expires_at": None}, {"checked_at": "2020-01-01T00:00:00+00:00"},
    {"expires_at": "2099-01-01T00:00:00+00:00"}, {"checked_at": "2026-01-01T00:00:00"},
    {"source_urls": ["https://site.example/?token=do-not-log"]}, {"booking_url": "javascript:alert(1)"},
])
async def test_custom_rejects_mismatch_stale_unsafe_data(tmp_path, constraints, mutation):
    service = make_service(tmp_path, lambda r: response_for(r, **mutation))
    result = await service.create(TripRequest(constraints=one_place(constraints)))
    assert result.status == "needs_review", result.issues
    assert all(v.authority == "unknown" for v in result.visit_checks)
    assert "test-private-key" not in result.model_dump_json() and "do-not-log" not in result.model_dump_json()
    await service.admission_service.client.aclose()


async def test_free_still_requires_real_booking_and_confirmation_reuses_selection(tmp_path, constraints):
    service = make_service(tmp_path, lambda r: response_for(r, ticket_price=0, reservation_required=True, enforced=False))
    c = one_place(constraints)
    result = await service.create(TripRequest(constraints=c))
    assert result.status == "needs_review" and result.visit_checks[0].enforced
    assert any(i.code == "reservation_required" for i in result.issues)
    revision = RevisionRequest(base_version=1, changes={"reservations": [booked(c)]})
    confirmed = await service.revise(result.id, revision)
    assert confirmed.status == "ready", confirmed.issues
    assert confirmed.metrics.model_calls == 0 and not audit_result(confirmed)
    assert any(t["step"] == "admission_refresh" for t in confirmed.trace)
    assert "未验证凭证" in admission_summary(confirmed.visit_checks[0], confirmed.constraints)
    assert all(e.cost.high == 0 for d in confirmed.days for e in d.events if e.kind == "visit")
    assert "venue.example" in render_markdown(confirmed)
    # Confirmation is bound to date and party size, not just the place name.
    for changes in [{"people": 3}, {"start_date": c.start_date + timedelta(days=1)}]:
        altered = Constraints.model_validate({**confirmed.constraints.model_dump(), **changes})
        check = confirmed.visit_checks[0].model_copy(update={"on_date": altered.start_date})
        assert any(i.code == "reservation_required" for i in admission_issues(check, altered))
    await service.admission_service.client.aclose()


@pytest.mark.parametrize("start,end,last,slot,expected", [
    ("09:00", "18:00", None, ("10:00", "10:30"), True),
    ("16:00", "18:00", "16:00", None, False),
    ("09:00", "10:00", None, None, False),
    ("14:00", "18:00", "17:00", ("09:00", "12:00"), False),
])
async def test_entry_cutoff_window_and_booked_entry_not_entire_visit(tmp_path, constraints, start, end, last, slot, expected):
    service = make_service(tmp_path, lambda r: response_for(r, windows=[{"start": start, "end": end}],
        last_entry=last, reservation_required=bool(slot)))
    c = one_place(constraints, reservations=[booked(one_place(constraints), *slot)] if slot else [])
    result = await service.create(TripRequest(constraints=c))
    assert (result.status == "ready") is expected, result.issues
    visits = [e for d in result.days for e in d.events if e.kind == "visit"]
    if expected:
        assert visits[0].start == "10:00" and visits[0].end == "11:30"
    else:
        assert not visits
    await service.admission_service.client.aclose()


async def test_split_opening_windows_wait_until_afternoon(tmp_path, constraints):
    service = make_service(tmp_path, lambda r: response_for(r,
        windows=[{"start": "09:00", "end": "10:00"}, {"start": "14:00", "end": "18:00"}]))
    result = await service.create(TripRequest(constraints=one_place(constraints)))
    assert result.status == "ready", result.issues
    assert next(e for d in result.days for e in d.events if e.kind == "visit").start == "14:00"
    assert not audit_result(result)
    await service.admission_service.client.aclose()


async def test_mandatory_closed_moves_date_optional_closed_replaced(tmp_path, constraints):
    c = Constraints.model_validate({**constraints.model_dump(), "must_visit": ["广东省博物馆"]})
    def respond(r):
        closed = r.url.params["place_id"] == "440100_0" and r.url.params["date"] == str(c.start_date)
        return response_for(r, opening_status="closed" if closed else "open")
    service = make_service(tmp_path, respond)
    result = await service.create(TripRequest(constraints=c))
    assert result.status == "ready", result.issues
    assert not any(e.place_id == "440100_0" for e in result.days[0].events)
    assert any(e.place_id == "440100_0" for e in result.days[1].events)
    assert any(i.code == "admission_rescheduled" for i in result.issues)
    optional = await service.create(TripRequest(constraints=Constraints.model_validate({**c.model_dump(), "must_visit": []})))
    assert any(i.code == "admission_replaced" for i in optional.issues)
    assert not any(e.place_id == "440100_0" for d in optional.days for e in d.events)
    await service.admission_service.client.aclose()


@pytest.mark.parametrize("closed,sold,confirmed", [(True, False, False), (False, True, False), (False, True, True)])
async def test_pinned_must_not_silently_removed_or_moved(tmp_path, constraints, closed, sold, confirmed):
    service = make_service(tmp_path, lambda r: response_for(r, opening_status="closed" if closed else "open",
        availability="sold_out" if sold else "unknown", availability_checked_at=datetime.now(UTC).isoformat()))
    c = one_place(constraints)
    if confirmed:
        c.reservations = Constraints.model_validate({**c.model_dump(), "reservations": [booked(c)]}).reservations
    result = await service.create(TripRequest(constraints=c))
    assert result.status == ("ready" if confirmed else "needs_review"), result.issues
    if not confirmed:
        assert not any(e.kind == "visit" for d in result.days for e in d.events)
        assert any(i.code == "audit_missing_must_visit" for i in result.issues)
    await service.admission_service.client.aclose()


async def test_actual_ticket_cost_budget_and_independent_tamper_checks(tmp_path, constraints):
    service = make_service(tmp_path, lambda r: response_for(r, ticket_price=300))
    result = await service.create(TripRequest(constraints=one_place(constraints)))
    assert result.status == "ready" and not audit_result(result)
    row = next(i for i in result.budget.items if i.category.endswith(" 门票"))
    assert row.unit_price.low == 300 and row.total.high == 600 and row.evidence_id in {e.id for e in result.evidence}
    for target in ("price", "source", "window", "ticket_row", "missing_check"):
        changed = result.model_copy(deep=True)
        visit = next(e for d in changed.days for e in d.events if e.kind == "visit")
        if target == "price":
            visit.cost.high += 1
        elif target == "source":
            visit.evidence_ids.remove(changed.visit_checks[0].evidence_id)
        elif target == "window":
            changed.visit_checks[0].last_entry = time(9)
        elif target == "ticket_row":
            next(i for i in changed.budget.items if i.category.endswith(" 门票")).unit_price.low = 0
        else:
            changed.visit_checks = []
        assert audit_result(changed), target
    over = await service.create(TripRequest(constraints=one_place(constraints, budget=100)))
    assert over.status == "needs_review" and any("budget" in i.code for i in over.issues)
    await service.admission_service.client.aclose()


async def test_manual_information_provenance_expiry_and_no_override_of_official_closure(tmp_path, constraints):
    service = make_service(tmp_path, lambda r: httpx.Response(503))
    c = one_place(constraints)
    result = await service.create(TripRequest(constraints=c))
    rule = {"place_id": "440100_0", "on_date": str(c.start_date), "start": "09:00", "end": "18:00",
        "reservation_required": False, "ticket_price": 0, "source": "用户电话核对", "checked_at": datetime.now(UTC).isoformat()}
    updated = await service.revise(result.id, RevisionRequest(base_version=1, changes={"opening_rules": [rule]}))
    assert updated.status == "ready", updated.issues
    assert updated.visit_checks[0].authority == "user" and updated.metrics.model_calls == 0
    rule["checked_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    expired = await service.revise(result.id, RevisionRequest(base_version=2, changes={"opening_rules": [rule]}))
    assert expired.status == "needs_review"
    check = updated.visit_checks[0].model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    assert "已过期" in admission_summary(check, c)
    await service.admission_service.client.aclose()
    service.admission_service.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response_for(r, opening_status="closed")))
    rule["checked_at"] = datetime.now(UTC).isoformat()
    updated = await service.revise(result.id, RevisionRequest(base_version=3, changes={"opening_rules": [rule]}))
    assert updated.visit_checks[0].opening_status == "closed" and updated.status == "needs_review"
    await service.admission_service.client.aclose()


async def test_model_cannot_invent_booking_or_checked_info(tmp_path, constraints):
    class FabricatingAgents(DemoAgents):
        async def extract(self, prompt, constraints, revision=False):
            return Constraints.model_validate({**constraints.model_dump(), "reservations": [booked(constraints)]})
    service = make_service(tmp_path, lambda r: response_for(r, reservation_required=True), FabricatingAgents)
    result = await service.create(TripRequest(constraints=one_place(constraints)))
    assert result.status == "needs_review" and not result.constraints.reservations
    await service.admission_service.client.aclose()


def test_refresh_api_version_conflict_legacy_and_incomplete(service, constraints):
    api = TestClient(create_app(service.settings, service))
    result = api.post("/trips", json={"constraints": constraints.model_dump(mode="json")}).json()
    fresh = api.post(f"/trips/{result['id']}/verify-visits", json={"base_version": 1})
    assert fresh.status_code == 200 and fresh.json()["version"] == 2 and fresh.json()["metrics"]["model_calls"] == 0
    assert api.post(f"/trips/{result['id']}/verify-visits", json={"base_version": 1}).status_code == 409
    assert api.post("/trips/missing/verify-visits", json={"base_version": 1}).status_code == 404
    incomplete = api.post("/trips", json={}).json()
    assert api.post(f"/trips/{incomplete['id']}/verify-visits", json={"base_version": 1}).status_code == 422
    from app.schemas import TripResult
    for field in ("visit_checks", "admission_verification"):
        result.pop(field)
    assert TripResult.model_validate(result).admission_verification is False


@pytest.mark.parametrize("field,value", [("people", 0), ("entry_window", {"start": "12:00", "end": "09:00"})])
def test_invalid_confirmation_rejected(constraints, field, value):
    with pytest.raises(ValidationError):
        Constraints.model_validate({**constraints.model_dump(), "reservations": [booked(constraints, **{field: value})]})


async def test_unknown_price_is_not_free_and_stale_inventory_not_current(tmp_path, constraints):
    service = make_service(tmp_path, lambda r: response_for(r, ticket_price=None, availability="sold_out",
        availability_checked_at=(datetime.now(UTC) - timedelta(minutes=11)).isoformat()))
    result = await service.create(TripRequest(constraints=one_place(constraints)))
    assert result.status == "needs_review" and result.visit_checks[0].availability == "unknown"
    assert any(i.code == "ticket_unverified" for i in result.issues)
    assert next(e for d in result.days for e in d.events if e.kind == "visit").cost.high == constraints.ticket_per_person_visit.high * constraints.people
    await service.admission_service.client.aclose()


async def test_query_limit_and_direct_materialize_admission_are_independent(constraints):
    admission = AdmissionService(Settings(max_visit_calls=1), httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="irrelevant"))))
    provider = Provider()
    await admission.page(GD_GUIDE, provider, [0])
    with pytest.raises(ValueError):
        await admission.page(GD_HOME, provider, [1])
    await admission.client.aclose()
    provider = FixtureProvider()
    c = one_place(constraints)
    facts = {"广州": await provider.city("广州", c)}
    check = VisitCheck.model_validate(payload("440100_0", "广东省博物馆", "广州", c.start_date, opening_status="closed"))
    days, _, issues = await materialize(c, Selection(days=[DayChoice(day=1, city="广州", place_ids=["440100_0"], reason="test")]), facts, provider, visit_checks=[check])
    assert not any(e.kind == "visit" for d in days for e in d.events)
    assert any(i.code == "opening_conflict" for i in issues)
