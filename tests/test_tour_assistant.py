"""Customer conversations must be grounded, versioned, isolated and safe to retry."""
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.assistant import AssistantDecision, TourAssistant
from app.main import create_app
from app.schemas import TripRequest
from app.storage import Repository


async def setup_chat(service, constraints, decision=None):
    trip = await service.create(TripRequest(constraints=constraints))
    api = create_app(service.settings, service)
    if decision:
        async def decide(message, trip, history):
            return decision
        api.state.tour_assistant = TourAssistant(service, decide)
    return TestClient(api), trip


def send(api, trip, message, request_id=None):
    return api.post(f"/trips/{trip.id}/chat", json={"base_version": trip.version,
        "request_id": request_id or uuid4().hex, "message": message})


async def test_question_and_greeting_get_reply_without_creating_versions(service, constraints):
    api, trip = await setup_chat(service, constraints)
    for text in ("你好", "这次预算多少钱？", "需要预约吗？"):
        result = send(api, trip, text).json()
        assert result["reply"] and not result["changed"] and result["trip"]["version"] == 1
    assert len(service.repository.versions(trip.id)) == 1
    messages = api.get(f"/trips/{trip.id}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"] * 3
    assert "6000" in messages[3]["content"]
    assert Repository(service.settings.db_path).messages(trip.id) == messages


async def test_natural_revision_is_persisted_once_and_retry_is_idempotent(service, constraints):
    api, trip = await setup_chat(service, constraints)
    key = uuid4().hex
    changed = send(api, trip, "预算改成3000元", key)
    assert changed.status_code == 200
    data = changed.json()
    assert data["changed"] and data["trip"]["version"] == 2 and data["trip"]["constraints"]["budget"] == 3000
    assert send(api, trip, "预算改成3000元", key).json() == data
    assert len(service.repository.versions(trip.id)) == 2 and len(service.repository.messages(trip.id)) == 2
    assert send(api, trip, "预算改成5000元", key).status_code == 409
    assert service.repository.get(trip.id, 1).constraints.budget == 6000


async def test_one_day_request_preserves_other_days_and_context_reaches_decider(service, constraints):
    api, trip = await setup_chat(service, constraints)
    data = send(api, trip, "第二天轻松一点，只安排下午").json()
    updated = service.repository.get(trip.id)
    assert data["changed"] and updated.days[0] == trip.days[0]
    assert all(e.start >= "13:00" for e in updated.days[1].events)
    captured = []
    async def reply(message, current, history):
        captured.append((current.constraints.day_rules, history))
        return AssistantDecision(reply="我会沿用刚才第二天下午的安排。")
    api.app.state.tour_assistant = TourAssistant(service, reply)
    result = send(api, updated, "那就按刚才说的吧").json()
    assert not result["changed"] and captured[0][0] and any("轻松" in m["content"] for m in captured[0][1])


async def test_forbidden_or_unrequested_model_patch_does_not_mutate_trip(service, constraints):
    for changes in ({"reservations": []}, {"budget": 9000}, {"people": 5}, {"opening_rules": []}):
        api, trip = await setup_chat(service, constraints, AssistantDecision(action="revise", changes=changes))
        result = send(api, trip, "每天安排轻松一点").json()
        assert not result["changed"] and result["trip"]["version"] == 1
        assert result["trip"]["constraints"]["budget"] == 6000 and result["trip"]["constraints"]["people"] == 2


async def test_sensitive_input_is_never_sent_to_model_or_saved(service, constraints):
    async def forbidden(*args):
        raise AssertionError("must not call model")
    api, trip = await setup_chat(service, constraints)
    api.app.state.tour_assistant = TourAssistant(service, forbidden)
    text = "我的身份证号是110105199001011234"
    response = send(api, trip, text)
    assert response.status_code == 200 and "110105" not in response.text
    assert service.repository.messages(trip.id) == []


async def test_stale_version_and_invalid_day_do_not_create_another_version(service, constraints):
    api, trip = await setup_chat(service, constraints)
    send(api, trip, "预算改成3000元")
    stale = send(api, trip, "预算改成4000元").json()
    assert stale["trip"]["version"] == 2 and not stale["changed"]
    current = service.repository.get(trip.id)
    bad = send(api, current, "第0天轻松一点").json()
    assert not bad["changed"] and "第1天" in bad["reply"]
    assert send(api, current, "   ").status_code == 422


async def test_model_cannot_claim_an_update_on_answer_only(service, constraints):
    api, trip = await setup_chat(service, constraints, AssistantDecision(reply="已经修改好了，也已经预订了酒店。"))
    result = send(api, trip, "能介绍一下吗").json()
    assert not result["changed"] and "还没有更改" in result["reply"]


async def test_booking_needs_user_assertion_date_slot_people_and_matching_place(service, constraints):
    api, trip = await setup_chat(service, constraints)
    check = trip.visit_checks[0]
    booking = {"place_name": check.place_name, "on_date": str(check.on_date), "people": 2, "start": "09:00", "end": "12:00"}
    async def decide(*args):
        return AssistantDecision(action="booking", booking=booking)
    api.app.state.tour_assistant = TourAssistant(service, decide)
    not_booked = send(api, trip, f"我想预约{check.place_name}").json()
    assert not not_booked["changed"] and not not_booked["trip"]["constraints"]["reservations"]
    confirmed = send(api, trip, f"我已经预约{check.place_name}，{check.on_date} 09:00到12:00，2人").json()
    assert confirmed["changed"] and confirmed["trip"]["constraints"]["reservations"][0]["people"] == 2


async def test_assistant_restore_creates_new_version_and_keeps_history(service, constraints):
    api, trip = await setup_chat(service, constraints)
    send(api, trip, "预算改成3000元")
    current = service.repository.get(trip.id)
    async def decide(*args):
        return AssistantDecision(action="restore", version=1)
    api.app.state.tour_assistant = TourAssistant(service, decide)
    restored = send(api, current, "恢复之前第一版的安排").json()
    assert restored["trip"]["version"] == 3 and restored["trip"]["constraints"]["budget"] == 6000
    assert service.repository.get(trip.id, 2).constraints.budget == 3000


def test_no_customer_planning_panels_left_in_ui_source():
    text = (Path(__file__).resolve().parents[1] / "app/ui.py").read_text(encoding="utf-8")
    assert 'st.subheader("旅游助手")' in text and "st.chat_input" in text
    for removed in ("景点入场核验与预约确认", "版本、对比与恢复", "酒店、车站与开放信息（可选）", "分日修改与雨天替换", "选择一个调整方案"):
        assert removed not in text
