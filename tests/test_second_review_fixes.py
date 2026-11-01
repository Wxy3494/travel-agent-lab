"""Fault injection for preserved conditions, interrupted turns and independent audits."""
import asyncio
import sqlite3
from uuid import uuid4

import pytest

from app.assistant import AssistantDecision, AssistantRequest, TourAssistant
from app.audit import audit_result
from app.schemas import PriceRange, RevisionRequest, TripRequest
from app.storage import Repository, VersionConflict


def message(trip, text="预算改成3000元"):
    return AssistantRequest(base_version=trip.version, request_id=uuid4().hex, message=text)


@pytest.mark.parametrize("text", ["预算不变，第二天轻松一点", "不要改预算，第二天轻松一点", "保持原有预算，第二天轻松一点",
    "预算保持不变，第二天轻松一点", "全团预算照旧，第二天轻松一点", "预算不要修改，第二天轻松一点", "第二天轻松一点，其他条件不变",
    "预算保持在6000元不变，第二天轻松一点", "预算 不变，第二天轻松一点"])
async def test_explicitly_preserved_budget_defeats_injected_patch(service, constraints, text):
    trip = await service.create(TripRequest(constraints=constraints))
    async def bad_model(*args):
        return AssistantDecision(action="revise", changes={"budget": 9000,
            "day_rules": [{"day": 2, "start": "13:00", "end": "18:00", "visit_minutes": 60}]}, target_day=2)
    response = await TourAssistant(service, bad_model).handle(trip.id, message(trip, text))
    saved = service.repository.get(trip.id)
    assert response["changed"] and saved.version == 2 and saved.constraints.budget == 6000
    assert saved.days[0] == trip.days[0] and saved.constraints.day_rules[0].day == 2
    assert all(e.start >= "13:00" for e in saved.days[1].events)


@pytest.mark.parametrize(("text", "field", "bad"), [("人数不变，只改预算为9000元", "people", 5),
    ("不要调整出发地，只改预算为9000元", "origin", "深圳"), ("目的地照旧，只改预算为9000元", "cities", ["深圳"]),
    ("天数不变，只改预算为9000元", "days", 3)])
async def test_preservation_is_field_specific_and_explicit_budget_change_still_works(service, constraints, text, field, bad):
    trip = await service.create(TripRequest(constraints=constraints))
    async def bad_model(*args):
        return AssistantDecision(action="revise", changes={"budget": 9000, field: bad})
    response = await TourAssistant(service, bad_model).handle(trip.id, message(trip, text))
    saved = service.repository.get(trip.id)
    assert response["changed"] and saved.constraints.budget == 9000
    assert getattr(saved.constraints, field) == getattr(trip.constraints, field)


async def test_cancel_during_processing_reopens_and_retries_without_duplicate_messages(service, constraints):
    trip = await service.create(TripRequest(constraints=constraints))
    request = message(trip)
    started = asyncio.Event()
    async def interrupted(*args):
        started.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(TourAssistant(service, interrupted).handle(trip.id, request))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    service.repository = Repository(service.settings.db_path)
    response = await TourAssistant(service).handle(trip.id, request)
    assert response["changed"] and service.repository.get(trip.id).version == 2
    assert len(service.repository.messages(trip.id)) == 2
    assert await TourAssistant(service).handle(trip.id, request) == response


async def test_crashed_worker_lease_expires_and_old_owner_is_fenced(service, constraints):
    trip = await service.create(TripRequest(constraints=constraints))
    clock = [1000.0]
    repo = Repository(service.settings.db_path, clock=lambda: clock[0])
    request = message(trip)
    first = repo.begin_chat(trip.id, request.request_id, request.message, trip.version, lease_seconds=10)
    with pytest.raises(VersionConflict, match="处理中"):
        Repository(repo.path, clock=lambda: clock[0]).begin_chat(trip.id, request.request_id, request.message, trip.version)
    clock[0] += 11
    newer = Repository(repo.path, clock=lambda: clock[0])
    second = newer.begin_chat(trip.id, request.request_id, request.message, trip.version)
    assert first["claim"] != second["claim"]
    clone = trip.model_copy(deep=True)
    clone.version = 2
    with pytest.raises(VersionConflict, match="中断"):
        repo.save(clone, expected_version=1, chat_claim=first["claim"])
    with pytest.raises(VersionConflict, match="中断"):
        repo.finish_chat(first["claim"], {})
    repo.release_chat(first["claim"])
    with pytest.raises(VersionConflict, match="处理中"):
        newer.begin_chat(trip.id, request.request_id, request.message, trip.version)
    newer.release_chat(second["claim"])
    service.repository = newer
    response = await TourAssistant(service).handle(trip.id, request)
    assert response["changed"] and len(newer.messages(trip.id)) == 2
    assert len(newer.versions(trip.id)) == 2


@pytest.mark.parametrize("action", ["revise", "refresh"])
async def test_commit_before_reply_interruption_recovers_receipt_without_replanning(service, constraints, monkeypatch, action):
    trip = await service.create(TripRequest(constraints=constraints))
    request = message(trip)
    repo = service.repository
    async def decide(*args):
        return AssistantDecision(action=action, changes={"budget": 3000})
    def interrupt_reply(*args):
        raise asyncio.CancelledError
    monkeypatch.setattr(repo, "finish_chat", interrupt_reply)
    with pytest.raises(asyncio.CancelledError):
        await TourAssistant(service, decide).handle(trip.id, request)
    assert repo.get(trip.id).version == 2
    service.repository = Repository(repo.path)
    async def forbidden(*args):
        raise AssertionError("Already committed: must recover, not invoke model again")
    response = await TourAssistant(service, forbidden).handle(trip.id, request)
    assert response["changed"] and response["model_calls"] == 0
    assert len(service.repository.versions(trip.id)) == 2 and len(service.repository.messages(trip.id)) == 2
    assert await TourAssistant(service, forbidden).handle(trip.id, request) == response


async def test_v3_pending_turn_migrates_without_rewriting_old_payload(service, constraints):
    trip = await service.create(TripRequest(constraints=constraints))
    request = message(trip)
    with sqlite3.connect(service.settings.db_path) as db:
        original = db.execute("SELECT payload FROM trips").fetchone()[0]
        db.execute("DROP TABLE assistant_turns")
        db.execute("CREATE TABLE assistant_turns(trip_id TEXT, request_id TEXT, message TEXT, base_version INTEGER,response TEXT,PRIMARY KEY(trip_id,request_id))")
        db.execute("INSERT INTO assistant_turns VALUES(?,?,?,?,NULL)", (trip.id, request.request_id, request.message, trip.version))
        db.execute("INSERT INTO assistant_messages(trip_id,role,content,created_at,version) VALUES(?,'user',?,'legacy',1)", (trip.id, request.message))
        db.execute("PRAGMA user_version=3")
    service.repository = Repository(service.settings.db_path)
    result = await TourAssistant(service).handle(trip.id, request)
    assert result["changed"] and len(service.repository.messages(trip.id)) == 2
    with sqlite3.connect(service.settings.db_path) as db:
        assert db.execute("SELECT payload FROM trips WHERE version=1").fetchone()[0] == original
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4


async def test_expired_lease_with_atomic_saved_result_recovers_without_model_call(service, constraints):
    trip = await service.create(TripRequest(constraints=constraints))
    clock = [1000.0]
    service.repository = Repository(service.settings.db_path, clock=lambda: clock[0])
    request = message(trip)
    turn = service.repository.begin_chat(trip.id, request.request_id, request.message, trip.version, lease_seconds=10)
    await service.revise(trip.id, RevisionRequest(base_version=1, changes={"budget": 3000}), chat_claim=turn["claim"])
    # Simulate abrupt process death: no finally block, release or reply was executed.
    clock[0] += 11
    service.repository = Repository(service.settings.db_path, clock=lambda: clock[0])
    async def forbidden(*args):
        raise AssertionError("Saved result must be used")
    result = await TourAssistant(service, forbidden).handle(trip.id, request)
    assert result["changed"] and result["model_calls"] == 0
    assert result["trip"]["constraints"]["budget"] == 3000
    assert len(service.repository.versions(trip.id)) == 2 and len(service.repository.messages(trip.id)) == 2


def recalculate(trip):
    trip.budget.total = PriceRange(low=round(sum(i.total.low for i in trip.budget.items), 2),
                                   high=round(sum(i.total.high for i in trip.budget.items), 2))
    trip.budget.status = "over_budget" if trip.budget.total.low > trip.constraints.budget else "uncertain" if trip.budget.total.high > trip.constraints.budget else "within_estimate"


@pytest.mark.parametrize("tamper", ["remove_all", "remove_one", "duplicate", "wrong_day", "wrong_start", "wrong_quantity", "wrong_cost"])
async def test_audit_rebuilds_expected_event_ledger_even_after_totals_are_recomputed(service, constraints, tamper):
    trip = await service.create(TripRequest(constraints=constraints))
    assert not audit_result(trip)
    original_events = [d.model_dump() for d in trip.days]
    rows = trip.budget.items
    item = next(i for i in rows if i.event_start is not None)
    if tamper == "remove_all":
        trip.budget.items = [i for i in rows if i.event_start is None]
    elif tamper == "remove_one":
        rows.remove(item)
    elif tamper == "duplicate":
        rows.append(item.model_copy(deep=True))
    elif tamper == "wrong_day":
        item.day += 1
    elif tamper == "wrong_start":
        item.event_start = "00:01"
    elif tamper == "wrong_quantity":
        item.quantity = 1
        item.total = item.unit_price.model_copy()
    else:
        item.unit_price = PriceRange(low=0, high=0)
        item.total = PriceRange(low=0, high=0)
    recalculate(trip)
    assert [d.model_dump() for d in trip.days] == original_events
    codes = {i.code for i in audit_result(trip)}
    assert "audit_event_budget" in codes
    if tamper in {"remove_all", "remove_one", "duplicate", "wrong_day", "wrong_start"}:
        assert "audit_budget_coverage" in codes


@pytest.mark.parametrize("value", [0, 1])
async def test_travel_summary_is_compared_even_when_zero(service, constraints, value):
    trip = await service.create(TripRequest(constraints=constraints))
    assert all(day.travel_minutes > 1 for day in trip.days) and not audit_result(trip)
    for day in trip.days:
        day.travel_minutes = value
    assert "audit_travel_minutes" in {i.code for i in audit_result(trip)}


async def test_genuine_zero_transport_day_still_passes_independent_audit(service, constraints):
    trip = await service.create(TripRequest(constraints=constraints))
    for day in trip.days:
        day.events = [e for e in day.events if e.kind not in {"travel", "visit"}]
        day.travel_minutes = 0
    trip.budget.items = [i for i in trip.budget.items if i.event_start is None]
    recalculate(trip)
    assert not audit_result(trip)
