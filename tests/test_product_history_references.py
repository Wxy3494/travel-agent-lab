"""Product regressions: complete history and grounded, advisory consultation data."""
import json
import sqlite3
from datetime import date, time, timedelta

from fastapi.testclient import TestClient

from app.admissions import AdmissionService
from app.config import Settings
from app.main import create_app
from app.meituan import MeituanTravelService, query_for
from app.providers import FixtureProvider
from app.render import render_markdown
from app.schemas import Constraints, DayChoice, PlaceBinding, Selection, TravelInfo, TripRequest, VisitCheck
from app.storage import Repository
from app.travel_references import matching_reference, parse_references, references_for, targets_for


async def test_history_migration_retains_more_than_five_hundred_and_paginates(service, constraints, tmp_path):
    result = await service.create(TripRequest(constraints=constraints))
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE trips(id TEXT,version INTEGER,payload TEXT,PRIMARY KEY(id,version))")
        for n in range(526):
            saved = result.model_copy(deep=True)
            saved.id = f"history-{n}"
            saved.constraints.start_date = date(2030, 1, 1) + timedelta(days=n)
            db.execute("INSERT INTO trips VALUES(?,?,?)", (saved.id, saved.version, saved.model_dump_json()))
        db.execute("PRAGMA user_version=1")
    repo = Repository(path)
    assert repo.count() == 526 and len(repo.list(limit=5)) == 5
    assert repo.list(limit=5)[0]["id"] == "history-525"
    assert repo.list(limit=20, offset=520)[-1]["id"] == "history-0"
    assert repo.list(query="2030-01-01")[0]["id"] == "history-0"
    assert Repository(path).count() == 526
    old = repo.get("history-0")
    old.version = 2
    repo.save(old, expected_version=1)
    assert repo.list(limit=1)[0]["id"] == old.id and repo.count() == 526 and repo.get(old.id, 1).version == 1
    api = TestClient(create_app(service.settings, type(service)(service.settings, repo)))
    page = api.get("/trips/history?limit=20&offset=520").json()
    assert page["total"] == 526 and len(page["items"]) == 6
    assert api.get("/trips/history?offset=-1").status_code == 422


def test_reference_binding_uses_exact_branch_and_refuses_conflicting_time_ranges():
    targets = [{"place_id": "park", "name": "青秀山风景区", "city": "南宁", "kind": "attraction", "dates": [date(2026, 11, 7)]},
               {"place_id": "museum", "name": "广西壮族自治区博物馆", "city": "南宁", "kind": "attraction", "dates": [date(2026, 11, 9)]},
               {"place_id": "hotel", "name": "初见酒店(北站店)", "city": "南宁", "kind": "hotel", "dates": [date(2026, 11, 7)]}]
    text = "### 青秀山风景区\n开放时间06:00-23:00，另一段称06:00-24:00。\n\n### 广西壮族自治区博物馆\n开放时间09:00-17:00，周一闭馆，免费，需预约。\n\n### 初见酒店(市中心店)\n¥80/晚"
    refs = parse_references(text, targets)
    assert len(refs) == 2 and matching_reference(refs, "hotel") is None
    assert not matching_reference(refs, "park").windows
    museum = matching_reference(refs, "museum")
    assert museum.closed_weekdays == [0] and museum.windows[0].end == time(17)
    assert "初见酒店" not in museum.content  # Other unrequested branches cannot contaminate a place section.
    refs = parse_references("### 青秀山风景区\n开放时间：06:00-23:00（景区内观光车、小火车营业时间为08:00-18:00）。", targets)
    assert matching_reference(refs, "park").windows[0].end == time(23)


async def test_hotel_queries_include_dates_rooms_and_refs_export_next_to_places(service, constraints):
    result = await service.create(TripRequest(constraints=constraints))
    hotel = result.days[0].hotel_options[0]
    query, _, _, _ = query_for(result)
    assert hotel.name in query and str(constraints.start_date) in query and "checkout" in query and '"rooms":1' in query
    target = next(t for t in targets_for(result) if t["kind"] == "hotel" and t["place_id"] == hotel.id)
    event = next(e for e in result.days[0].events if e.kind == "visit")
    point = next(t for t in targets_for(result) if t["place_id"] == event.place_id)
    content = f"### {event.title}\n开放时间09:00-17:00，门票¥XX起，需预约。\n\n### {hotel.name}\n演示房型 ¥77/间/晚；不是实时房价。"
    result.travel_info = TravelInfo(status="success", content=content,
        references=parse_references(content, [point, target]))
    assert matching_reference(references_for(result), hotel.id, result.days[0].date)
    assert matching_reference(references_for(result), hotel.id, result.days[-1].date) is None
    text = render_markdown(result)
    assert text.index("美团房型") < text.index("## 第 2 天") and "¥77" in text
    assert "入场参考" in text and "美团景点与门票参考" not in text
    assert result.constraints.hotel_per_room_night.high == 320
    result.travel_info.references = []
    assert len(references_for(result.model_dump(mode="json"))) == 2  # Old versions stay readable.


async def test_advisory_closure_moves_must_visit_but_still_requires_verification():
    c = Constraints(origin="广州", cities=["广州"], start_date=date(2030, 1, 7), days=2, people=2, budget=6000,
        must_visit=["广东省博物馆"], bindings=[PlaceBinding(field="must_visit", query="广东省博物馆", canonical_name="广东省博物馆",
            place_id="440100_0", city="广州", basis="fixture", evidence_id="fixture")])
    assert c.start_date.weekday() == 0
    provider = FixtureProvider()
    facts = {"广州": await provider.city("广州", c)}
    service = AdmissionService(Settings())
    async def unknown(point, on_date, *args):
        return VisitCheck(place_id=point.id, place_name=point.name, city=point.city, on_date=on_date)
    service.lookup = unknown
    reference = parse_references("### 广东省博物馆\n开放时间10:00-17:00，周一闭馆；票价未核实。",
        [{"place_id": "440100_0", "name": "广东省博物馆", "city": "广州", "kind": "attraction", "dates": [c.start_date]}])
    chosen = Selection(days=[DayChoice(day=1, city="广州", place_ids=["440100_0"], reason="test"),
                             DayChoice(day=2, city="广州", place_ids=[], reason="test")])
    arranged, checks, notes = await service.arrange(c, chosen, facts, provider, references=reference)
    assert arranged.days[0].place_ids == [] and arranged.days[1].place_ids == ["440100_0"]
    assert "美团参考" in notes[0].message
    assert all(v.authority == "unknown" and v.ticket_price is None for v in checks)
    assert checks[-1].reference_windows[0].start == time(10)
    await provider.close()


async def test_reference_failure_before_materialization_keeps_a_trip(service, constraints):
    result = await service.create(TripRequest(constraints=constraints))
    calls = []
    async def runner(query, city, timeout):
        calls.append(query)
        return 0, json.dumps({"status": "success", "data": "未取得房价，不估算。"}), ""
    api = MeituanTravelService(Settings(meituan_token="fake"), runner)
    response = await api.lookup(result)
    assert calls and response.status == "success" and not response.references


async def test_reference_opening_time_reaches_real_schedule_without_becoming_verified(tmp_path, constraints):
    from app.agents import DemoAgents
    from app.service import TravelService
    settings = Settings(mode="live", meituan_enabled=True, meituan_token="fake")
    service = TravelService(settings, Repository(tmp_path / "schedule.sqlite3"), FixtureProvider, DemoAgents)
    async def runner(*args):
        return 0, json.dumps({"status": "success", "data": "### 广东省博物馆\n开放时间10:00-18:00；门票999元；预约情况未知。"}), ""
    service.meituan_service = MeituanTravelService(settings, runner)
    async def unknown(point, on_date, *args):
        return VisitCheck(place_id=point.id, place_name=point.name, city=point.city, on_date=on_date)
    service.admission_service.lookup = unknown
    c = constraints.model_copy(update={"days": 1, "must_visit": ["广东省博物馆"], "day_place_ids": {1: ["440100_0"]}})
    trip = await service.create(TripRequest(constraints=c))
    visits = [e for day in trip.days for e in day.events if e.kind == "visit"]
    assert visits and visits[0].start >= "10:00" and trip.status == "needs_review"
    assert trip.visit_checks[0].authority == "unknown" and trip.visit_checks[0].ticket_price is None
    assert visits[0].cost.high == c.ticket_per_person_visit.high * c.people
