"""Exercise form + text revision against the actual demo API in-process."""
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ui(monkeypatch, service):
    import httpx
    api = TestClient(create_app(service.settings, service))

    def local_request(method, url, json=None, **kwargs):
        parsed = urlsplit(url)
        assert parsed.hostname == "127.0.0.1"
        return api.request(method, parsed.path + ("?" + parsed.query if parsed.query else ""), json=json)

    monkeypatch.setenv("TRAVEL_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(httpx, "request", local_request)
    app = AppTest.from_file(str(ROOT / "app/ui.py"), default_timeout=10).run()
    assert not app.exception
    return app


def submit_form(ui):
    next(b for b in ui.button if b.label in {"生成旅行计划", "重新生成行程"}).click().run()
    assert not ui.exception


def fill_example(ui):
    """Enter a small complete trip through the same fields used by visitors."""
    ui.text_input(key="form_origin").set_value("广州")
    ui.text_input(key="form_destinations").set_value("广州")
    ui.date_input(key="form_date").set_value(datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1))
    ui.number_input(key="form_days").set_value(2)
    ui.number_input(key="form_people").set_value(2)
    ui.number_input(key="form_budget").set_value(6000.0)
    ui.text_input(key="form_interests").set_value("博物馆，公园")


def test_admission_questions_use_assistant_without_manual_panel(ui):
    fill_example(ui)
    submit_form(ui)
    trip = ui.session_state.trip
    ui.chat_input(key="assistant_input").set_value("需要预约吗？").run()
    assert not ui.exception and ui.chat_message
    assert ui.session_state.trip["version"] == 1
    assert not ui.session_state.trip["constraints"]["reservations"]
    ui.chat_input(key="assistant_input").set_value("重新核验景点入场信息").run()
    assert not ui.exception and ui.session_state.trip["version"] == 2
    assert ui.session_state.trip["id"] == trip["id"]
    assert ui.session_state.trip["metrics"]["model_calls"] == 0


def test_reopened_page_recovers_interrupted_chat_with_original_request_id(ui, service):
    from uuid import uuid4
    fill_example(ui)
    submit_form(ui)
    trip = ui.session_state.trip
    turn = service.repository.begin_chat(trip["id"], uuid4().hex, "预算改成3000元", trip["version"])
    service.repository.release_chat(turn["claim"])
    ui.run()
    assert not ui.exception
    assert ui.session_state.pending_chat["request_id"] == turn["claim"][1]
    ui.button(key="retry_chat").click().run()
    assert not ui.exception and ui.session_state.trip["version"] == 2
    assert ui.session_state.trip["constraints"]["budget"] == 3000
    assert len(service.repository.messages(trip["id"])) == 2
    assert service.repository.pending_chat(trip["id"]) is None


def test_blank_form_marks_each_missing_field_without_a_generic_warning(ui):
    submit_form(ui)
    assert ui.session_state.trip is None
    message = " ".join(item.value for item in ui.error)
    for field in ["出发地", "目的地", "游玩开始日期", "旅行天数", "人数", "全团总预算"]:
        assert field in message
    assert len(ui.error) == 6 and not ui.warning
    assert set(ui.session_state.form_errors) == {"form_origin", "form_destinations", "form_date", "form_days", "form_people", "form_budget"}


def test_room_error_is_specific_and_clears_on_edit_without_submitting_or_resetting_input(ui, service):
    fill_example(ui)
    ui.number_input(key="form_rooms").set_value(3)
    submit_form(ui)
    assert set(ui.session_state.form_errors) == {"form_rooms"}
    assert "2人" in ui.error[0].value and "3间" in ui.error[0].value
    assert ui.session_state.form_expand_details is True
    assert service.repository.list() == [] and ui.session_state.trip is None
    ui.number_input(key="form_people").set_value(3).run()
    assert not ui.exception and not ui.error and ui.session_state.form_errors == {}
    assert ui.number_input(key="form_rooms").value == 3 and ui.number_input(key="form_budget").value == 6000
    assert service.repository.list() == []
    submit_form(ui)
    assert ui.session_state.trip["status"] == "ready" and len(service.repository.list()) == 1


def test_valid_two_rooms_two_people_is_not_rejected(ui):
    fill_example(ui)
    ui.number_input(key="form_rooms").set_value(2)
    submit_form(ui)
    assert not ui.error and ui.session_state.trip["status"] == "ready"
    assert ui.session_state.trip["constraints"]["rooms"] == 2


def test_form_reports_all_conflicts_and_clears_each_repaired_field(ui):
    from datetime import time
    fill_example(ui)
    ui.text_input(key="form_destinations").set_value("广州，深圳")
    ui.number_input(key="form_days").set_value(1)
    ui.text_input(key="form_must").set_value("广州塔")
    ui.text_input(key="form_excluded").set_value("广州塔")
    ui.time_input(key="form_daily_start").set_value(time(22))
    submit_form(ui)
    assert set(ui.session_state.form_errors) == {"form_days", "form_must", "form_excluded", "form_daily_start", "form_daily_end"}
    assert not ui.warning
    ui.number_input(key="form_days").set_value(2).run()
    assert "form_days" not in ui.session_state.form_errors and "form_must" in ui.session_state.form_errors
    ui.text_input(key="form_excluded").set_value("").run()
    assert "form_must" not in ui.session_state.form_errors and "form_excluded" not in ui.session_state.form_errors
    ui.time_input(key="form_daily_start").set_value(time(9)).run()
    assert not ui.error and not ui.session_state.trip
    submit_form(ui)
    assert ui.session_state.trip["status"] == "ready"


async def test_shorter_form_does_not_inherit_hidden_old_day_four_setting(ui, service):
    from app.schemas import Constraints, TripRequest
    c = Constraints(origin="广州", cities=["广州"], start_date=datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1),
        days=4, people=2, budget=6000, day_place_ids={4: ["440100_13"]})
    previous = await service.create(TripRequest(constraints=c))
    ui.session_state.trip = previous.model_dump(mode="json")
    ui.session_state.sync_form = previous.constraints.model_dump(mode="json")
    ui.run()
    ui.number_input(key="form_days").set_value(2).run()
    assert any("重新安排" in c.value for c in ui.caption)
    submit_form(ui)
    assert not ui.error and ui.session_state.trip["version"] == 2 and ui.session_state.trip["status"] == "ready"
    assert ui.session_state.trip["constraints"]["day_place_ids"] == {}


def test_thirty_day_form_hotels_and_automatic_weather(ui):
    fill_example(ui)
    ui.number_input(key="form_days").set_value(30)
    ui.number_input(key="form_budget").set_value(100000.0)
    submit_form(ui)
    assert ui.session_state.trip["status"] == "ready" and len(ui.session_state.trip["days"]) == 30
    text = " ".join(item.value for item in ui.caption)
    assert "不是该酒店的报价" in text
    assert any("查看地图" in item.value for item in ui.markdown)
    assert not any(b.label == "查询天气" for b in ui.button)
    assert not any(t.key == "weather_city" for t in ui.text_input)
    assert any("天气 · 广州" in item.value for item in ui.markdown)
    trip = ui.session_state.trip
    ui.session_state[f"day_{trip['id']}_{trip['version']}_30"] = True
    ui.run()
    assert any("临近出发" in item.value for item in ui.markdown)


def test_form_and_text_revisions_keep_state_and_versions(ui, service):
    fill_example(ui)
    submit_form(ui)
    original = ui.session_state.trip
    assert original["status"] == "ready" and original["version"] == 1
    ui.chat_input(key="assistant_input").set_value("预算改成1元").run()
    assert not ui.exception
    low = ui.session_state.trip
    assert low["version"] == 2 and low["status"] == "needs_review"
    assert ui.number_input(key="form_budget").value == 1
    assert ui.text_input(key="form_destinations").value == "广州"
    # Editing the visible form creates the next version, rather than a new trip.
    ui.number_input(key="form_budget").set_value(6000.0)
    ui.text_input(key="form_excluded").set_value("广东省博物馆")
    submit_form(ui)
    third = ui.session_state.trip
    assert third["id"] == original["id"] and third["version"] == 3 and third["status"] == "ready"
    names = [e["title"] for d in third["days"] for e in d["events"] if e["kind"] == "visit"]
    assert "广东省博物馆" not in names
    assert service.repository.get(original["id"], 1).constraints.excluded == []


def test_history_reads_restore_form_and_price_assumptions(ui, service):
    fill_example(ui)
    ui.number_input(key="form_hotel_budget").set_value(305.5)
    ui.number_input(key="form_food_budget").set_value(90.75)
    ui.number_input(key="form_ticket_budget").set_value(0.0)
    submit_form(ui)
    trip = ui.session_state.trip
    assert trip["constraints"]["hotel_per_room_night"] == {"low": 305.5, "high": 305.5}
    assert trip["constraints"]["food_per_person_day"] == {"low": 90.75, "high": 90.75}
    assert trip["constraints"]["ticket_per_person_visit"] == {"low": 0.0, "high": 0.0}
    lodging = next(i for i in trip["budget"]["items"] if i["category"] == "住宿")
    assert lodging["total"] == {"low": 305.5, "high": 305.5}
    next(b for b in ui.button if b.label == "＋ 开始新的旅行").click().run()
    assert ui.session_state.trip is None
    ui.button(key="recent_" + trip["id"]).click().run()
    assert not ui.exception
    assert ui.number_input(key="form_hotel_budget").value == 305.5
    assert ui.number_input(key="form_food_budget").value == 90.75
    assert ui.number_input(key="form_ticket_budget").value == 0.0
    assert ui.text_input(key="form_origin").value == "广州"


def test_automatic_cost_estimates_survive_other_form_changes(ui):
    fill_example(ui)
    submit_form(ui)
    original = ui.session_state.trip
    assert ui.number_input(key="form_hotel_budget").value is None
    ui.number_input(key="form_budget").set_value(5000.0)
    submit_form(ui)
    revised = ui.session_state.trip
    for field in ["hotel_per_room_night", "food_per_person_day", "ticket_per_person_visit"]:
        assert revised["constraints"][field] == original["constraints"][field]
    assert not any("下限" in n.label or "上限" in n.label for n in ui.number_input)


def test_candidate_confirmation_in_page_produces_verified_version(ui):
    fill_example(ui)
    ui.text_input(key="form_must").set_value("博物")
    submit_form(ui)
    pending = ui.session_state.trip
    assert pending["status"] == "needs_input" and pending["clarifications"][0]["options"]
    point = pending["clarifications"][0]["options"][0]
    ui.chat_input(key="assistant_input").set_value("选第一个").run()
    assert not ui.exception
    assert ui.session_state.trip["status"] == "ready" and ui.session_state.trip["version"] == 2
    assert ui.session_state.trip["constraints"]["bindings"][0]["place_id"] == point["id"]


def test_history_picker_restores_without_uuid_and_original_versions_remain(ui, service):
    fill_example(ui)
    submit_form(ui)
    old = ui.session_state.trip
    next(b for b in ui.button if b.label == "＋ 开始新的旅行").click().run()
    assert ui.session_state.trip is None
    entries = service.repository.list()
    ui.button(key="all_history").click().run()
    next(s for s in ui.selectbox if s.label == "选择旅行").set_value(entries[0]).run()
    next(b for b in ui.button if b.label == "打开选中旅行").click().run()
    assert not ui.exception and ui.session_state.trip["id"] == old["id"]
    assert service.repository.get(old["id"]).version == 1
    assert not any(e.label == "版本、对比与恢复" for e in ui.expander)


async def test_recent_five_all_history_search_and_older_page_without_debug_controls(ui, service):
    from app.schemas import Constraints, TripRequest
    original = await service.create(TripRequest(constraints=Constraints(origin="广州", cities=["广州"],
        start_date=datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1), days=2, people=2, budget=6000)))
    for n in range(24):
        row = original.model_copy(deep=True)
        row.id = f"older-test-{n}"
        row.constraints.start_date += timedelta(days=n + 1)
        service.repository.save(row)
    ui.run()
    assert len([b for b in ui.button if b.key and b.key.startswith("recent_")]) == 5
    assert not ui.json and not any(s.label == "规划策略" for s in ui.selectbox)
    assert not any(t.label == "行程编号" for t in ui.text_input)
    ui.button(key="all_history").click().run()
    assert len(ui.selectbox(key="history_choice").options) == 20
    next(b for b in ui.button if b.label == "下一页").click().run()
    assert len(ui.selectbox(key="history_choice").options) == 5
    assert ui.session_state.history_page == 1
    ui.text_input(key="history_query").set_value(str(original.constraints.start_date)).run()
    assert ui.session_state.history_page == 0 and len(ui.selectbox(key="history_choice").options) == 1
    ui.selectbox(key="history_choice").set_value(service.repository.list(query=str(original.constraints.start_date))[0]).run()
    next(b for b in ui.button if b.label == "打开选中旅行").click().run()
    assert not ui.exception and ui.session_state.trip["id"] == original.id and not ui.session_state.history_open


def test_concise_results_hide_source_dump_but_keep_actual_problem_messages(ui):
    fill_example(ui)
    submit_form(ui)
    trip = ui.session_state.trip
    trip["evidence"][0]["description"] = "RAW_SOURCE_SENTINEL"
    trip["constraints"]["pricing_note"] = "项目预算假设，非实时价格；请用自己的报价调整"
    event = next(e for d in trip["days"] for e in d["events"] if e["kind"] == "travel")
    event["route_steps"] = [{"kind": "walk", "distance": 1549, "start_location": "113,23", "end_location": "113.01,23.01"},
        {"kind": "walk", "distance": 145, "start_location": "113.01,23.01", "end_location": "113.02,23.02"}]
    ui.session_state.trip = trip
    ui.run()
    text = " ".join(m.value for m in [*ui.markdown, *ui.caption])
    assert "RAW_SOURCE_SENTINEL" not in text and "请用自己的报价调整" not in text
    assert "步行共 1694 米" in text and "mode=walk" in text
    assert not any(b.label == "来源说明" for b in ui.button)
    assert any(e.label == "费用明细" for e in ui.expander)


def test_advanced_time_form_and_adjustment_apply_work(ui):
    from datetime import time
    fill_example(ui)
    ui.time_input(key="form_daily_start").set_value(time(11))
    ui.time_input(key="form_daily_end").set_value(time(19))
    ui.text_area(key="form_lodging").set_value("广州演示住宿区域（非真实酒店）")
    ui.number_input(key="form_budget").set_value(1.0)
    submit_form(ui)
    old = ui.session_state.trip
    assert old["status"] == "needs_review"
    assert all(e["start"] >= "11:00" for e in old["days"][0]["events"])
    assert all(e["end"] <= "19:00" for d in old["days"] for e in d["events"] if e["kind"] == "visit")
    ui.chat_input(key="assistant_input").set_value("预算改成6000元").run()
    assert not ui.exception
    assert ui.session_state.trip["status"] == "ready" and ui.session_state.trip["version"] == 2
    assert ui.time_input(key="form_daily_start").value == time(11)
    assert ui.text_area(key="form_lodging").value == "广州：广州演示住宿区域（非真实酒店）"
    assert ui.session_state.trip["constraints"]["daily_end_scope"] == "last_activity"
    assert not any(n.key == "form_rule_day" for n in ui.number_input)
    assert not any(t.key in {"form_arrival", "form_departure"} for t in ui.time_input)
