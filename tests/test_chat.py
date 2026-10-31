from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.schemas import RevisionRequest, TripRequest


async def test_chat_clarification_then_plan_then_revise(service):
    first = await service.create(TripRequest(prompt="广州两日游"))
    assert first.status == "needs_input" and len(first.questions) == 1
    assert first.constraints.origin is None and first.constraints.budget is None
    assert first.constraints.cities == ["广州"] and first.constraints.days == 2
    ready = await service.revise(first.id, RevisionRequest(base_version=1,
        instruction="从广州出发，明天开始，2人，全团预算6000元，喜欢博物馆"))
    assert ready.status == "ready", ready.issues
    assert ready.constraints.start_date == datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
    low = await service.revise(first.id, RevisionRequest(base_version=2, instruction="预算改成1元"))
    assert low.status == "needs_review" and low.constraints.budget == 1
    assert low.constraints.cities == ["广州"] and low.constraints.people == 2
    assert service.repository.get(first.id, 2).constraints.budget == 6000


async def test_chat_cross_city_complete_prompt(service):
    trip = await service.create(TripRequest(prompt="从广州出发，明天开始先广州后深圳，5天2人，全团预算6000元，喜欢博物馆"))
    assert trip.status == "ready", trip.issues
    assert trip.constraints.origin == "广州" and trip.constraints.cities == ["广州", "深圳"]
    changed = await service.revise(trip.id, RevisionRequest(base_version=1, instruction="排除广州塔"))
    assert changed.status == "ready"
    assert changed.constraints.excluded == ["广州塔"]
    assert changed.constraints.days == 5


async def test_unrecognized_chat_request_is_pending(service):
    trip = await service.create(TripRequest(prompt="帮我安排特别有意思的体验"))
    assert trip.status == "needs_input" and not trip.days
    assert trip.constraints.budget is None and trip.metrics.model_calls == 0
