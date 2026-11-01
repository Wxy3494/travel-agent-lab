"""Visible field validation must not accuse valid inputs or inherit hidden stale day settings."""
from copy import deepcopy
from datetime import date, time, timedelta

import pytest
from pydantic import ValidationError

from app.form_validation import FORM_DEFAULTS, model_errors, validate_form
from app.schemas import Constraints


def values(**changes):
    return {**deepcopy(FORM_DEFAULTS), "form_origin": "武汉", "form_destinations": "北京",
            "form_date": date.today() + timedelta(days=2), "form_days": 4, "form_people": 2,
            "form_rooms": 2, "form_budget": 6000, **changes}


def test_two_people_two_rooms_default_times_and_blank_optional_costs_are_valid():
    result = validate_form(values())
    assert not result.errors and result.constraints.rooms == 2 and result.constraints.people == 2
    assert result.constraints.daily_start == time(9) and result.constraints.daily_end == time(21, 30)
    assert result.constraints.hotel_per_room_night.high == 320
    assert result.constraints.home_transport_scope == "separate"


@pytest.mark.parametrize("changes,keys", [
    ({"form_origin": "  "}, {"form_origin"}),
    ({"form_destinations": ""}, {"form_destinations"}),
    ({"form_date": None}, {"form_date"}),
    ({"form_date": date.today() - timedelta(days=1)}, {"form_date"}),
    ({"form_days": None}, {"form_days"}),
    ({"form_people": None}, {"form_people"}),
    ({"form_budget": None}, {"form_budget"}),
    ({"form_people": 1}, {"form_rooms"}),
    ({"form_destinations": "北京，北京市", "form_days": 1}, {"form_destinations"}),
    ({"form_destinations": "北京，上海", "form_days": 1}, {"form_days"}),
    ({"form_must": "黄鹤楼", "form_excluded": "黄鹤楼"}, {"form_must", "form_excluded"}),
    ({"form_daily_start": time(22), "form_daily_end": time(9)}, {"form_daily_start", "form_daily_end"}),
    ({"form_daily_start": None}, {"form_daily_start"}),
    ({"form_lodging": "上海：某酒店"}, {"form_lodging"}),
    ({"form_lodging": "北京："}, {"form_lodging"}),
    ({"form_destinations": "北京，上海", "form_lodging": "某酒店"}, {"form_lodging"}),
    ({"form_interests": "，".join(str(i) for i in range(9))}, {"form_interests"}),
    ({"form_must": "景" * 81}, {"form_must"}),
    ({"form_food_budget": -1}, {"form_food_budget"}),
])
def test_only_real_problem_fields_are_marked(changes, keys):
    result = validate_form(values(**changes))
    assert set(result.errors) == keys
    assert result.constraints is None
    assert all("请检查输入：" not in m for messages in result.errors.values() for m in messages)


def test_multiple_errors_all_reported_and_message_shows_actual_counts():
    result = validate_form(values(form_people=1, form_days=1, form_destinations="北京，上海"))
    assert set(result.errors) == {"form_rooms", "form_days"}
    assert "1人" in result.errors["form_rooms"][0] and "2间" in result.errors["form_rooms"][0]
    assert "2个目的地" in result.errors["form_days"][0]


def test_shortening_days_clears_invisible_out_of_range_activity_settings_without_changing_must_visits():
    previous = {"constraints": {"cities": ["北京"], "days": 4, "day_place_ids": {"4": ["saved-point"]},
        "day_rules": [{"day": 4, "pace": "relaxed"}], "must_visit": ["故宫"]}}
    result = validate_form(values(form_days=2, form_must="故宫"), previous)
    assert not result.errors and result.constraints.day_place_ids == {} and result.constraints.day_rules == []
    assert result.constraints.must_visit == ["故宫"] and "form_days" in result.notes
    assert previous["constraints"]["day_place_ids"] == {"4": ["saved-point"]}


def test_unchanged_trip_or_alias_keeps_explicit_day_choices_and_price_assumptions():
    previous = {"constraints": {"cities": ["北京"], "days": 4, "day_place_ids": {"4": ["saved-point"]}}}
    submitted = values(form_destinations="北京市", form_price_ranges={"hotel": {"low": 200, "high": 350}})
    result = validate_form(submitted, previous)
    assert not result.errors and result.constraints.day_place_ids == {4: ["saved-point"]}
    assert result.constraints.hotel_per_room_night.high == 350


def test_model_error_mapping_does_not_echo_rejected_input_or_guess_other_fields():
    with pytest.raises(ValidationError) as exc:
        Constraints(origin="private" * 20)
    errors = model_errors(exc.value)
    assert set(errors) == {"form_origin"} and "private" not in str(errors)
