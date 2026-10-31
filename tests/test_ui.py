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


def test_admission_details_confirmation_and_refresh(ui):
    fill_example(ui)
    submit_form(ui)
    trip = ui.session_state.trip
    ui.selectbox(key="visit_check_choice").set_value(trip["visit_checks"][0]).run()
    assert not ui.exception
    next(b for b in ui.button if b.label == "保存预约确认并重新检查").click().run()
    assert ui.session_state.trip["version"] == 1
    assert any("先完成实际预约" in w.value for w in ui.warning)
    next(c for c in ui.checkbox if c.label.startswith("我已完成")).check()
    next(b for b in ui.button if b.label == "保存预约确认并重新检查").click().run()
    assert not ui.exception and ui.session_state.trip["version"] == 2
    assert ui.session_state.trip["constraints"]["reservations"][0]["people"] == 2
    next(b for b in ui.button if b.label == "重新核验景点入场信息").click().run()
    assert not ui.exception and ui.session_state.trip["version"] == 3
    assert ui.session_state.trip["metrics"]["model_calls"] == 0


def test_blank_form_lists_all_missing_fields_without_guessing(ui):
    submit_form(ui)
    assert ui.session_state.trip is None
    message = " ".join(item.value for item in ui.warning)
    for field in ["出发城市", "目的地城市", "游玩开始日期", "旅行天数", "人数", "全团总预算"]:
        assert field in message


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
    assert any("临近出发" in item.value for item in ui.markdown)


def test_form_and_text_revisions_keep_state_and_versions(ui, service):
    fill_example(ui)
    submit_form(ui)
    original = ui.session_state.trip
    assert original["status"] == "ready" and original["version"] == 1
    ui.text_area(key="revision_instruction").set_value("预算改成1元")
    next(b for b in ui.button if b.label == "提交修改").click().run()
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
    next(t for t in ui.text_input if t.label == "行程编号").set_value(trip["id"])
    next(b for b in ui.button if b.label == "读取行程").click().run()
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
    ui.selectbox(key="confirm_must_visit:博物").set_value(point)
    next(b for b in ui.button if b.label == "确认地点并继续规划").click().run()
    assert not ui.exception
    assert ui.session_state.trip["status"] == "ready" and ui.session_state.trip["version"] == 2
    assert ui.session_state.trip["constraints"]["bindings"][0]["place_id"] == point["id"]


def test_history_picker_restores_without_uuid_and_clone_preserves_original(ui, service):
    fill_example(ui)
    submit_form(ui)
    old = ui.session_state.trip
    next(b for b in ui.button if b.label == "＋ 开始新的旅行").click().run()
    assert ui.session_state.trip is None
    entries = service.repository.list()
    next(s for s in ui.selectbox if s.label == "选择旅行").set_value(entries[0]).run()
    next(b for b in ui.button if b.label == "打开选中旅行").click().run()
    assert not ui.exception and ui.session_state.trip["id"] == old["id"]
    next(b for b in ui.button if b.label == "从当前版本复制为新旅行").click().run()
    assert not ui.exception
    assert ui.session_state.trip["id"] != old["id"] and service.repository.get(old["id"]).version == 1


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
    ui.button(key="adjust_increase_budget").click().run()
    assert not ui.exception
    assert ui.session_state.trip["status"] == "ready" and ui.session_state.trip["version"] == 2
    assert ui.time_input(key="form_daily_start").value == time(11)
    assert ui.text_area(key="form_lodging").value == "广州：广州演示住宿区域（非真实酒店）"
    assert ui.session_state.trip["constraints"]["daily_end_scope"] == "last_activity"
    assert not any(n.key == "form_rule_day" for n in ui.number_input)
    assert not any(t.key in {"form_arrival", "form_departure"} for t in ui.time_input)
