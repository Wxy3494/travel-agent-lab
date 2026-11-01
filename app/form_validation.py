"""Validate the visible trip form and return errors attached to its widget keys."""
from dataclasses import dataclass, field
from datetime import date, time

from pydantic import ValidationError

from app.names import CITY_ALIASES
from app.schemas import Constraints

FORM_DEFAULTS = {
    "form_origin": "", "form_destinations": "", "form_date": None, "form_days": None,
    "form_people": None, "form_budget": None, "form_interests": "", "form_must": "",
    "form_excluded": "", "form_transport": "public", "form_return": False, "form_accessible": False,
    "form_rooms": 0, "form_hotel_budget": None, "form_food_budget": None, "form_ticket_budget": None,
    "form_price_ranges": {}, "form_lodging": "",
    "form_pricing_note": "项目预算假设，非实时价格；请用自己的报价调整",
    "form_daily_start": time(9), "form_daily_end": time(21, 30),
}

FIELD_KEYS = {
    "origin": "form_origin", "cities": "form_destinations", "start_date": "form_date", "days": "form_days",
    "people": "form_people", "budget": "form_budget", "interests": "form_interests", "must_visit": "form_must",
    "excluded": "form_excluded", "rooms": "form_rooms", "lodging_addresses": "form_lodging",
    "hotel_per_room_night": "form_hotel_budget", "food_per_person_day": "form_food_budget",
    "ticket_per_person_visit": "form_ticket_budget", "daily_start": "form_daily_start", "daily_end": "form_daily_end",
    "day_rules": "form_days", "day_place_ids": "form_days", "opening_rules": "form_must",
    "reservations": "form_must", "place_choices": "form_must", "anchors": "form_lodging",
    "indoor_sources": "form_interests", "visit_minutes": "form_daily_end",
}


@dataclass
class FormResult:
    constraints: Constraints | None = None
    errors: dict[str, list[str]] = field(default_factory=dict)
    notes: dict[str, list[str]] = field(default_factory=dict)

    def error(self, key, message):
        if message not in self.errors.setdefault(key, []):
            self.errors[key].append(message)


def split_names(value):
    return [v.strip() for v in value.replace("，", ",").replace("、", ",").split(",") if v.strip()]


def lodging_inputs(text, cities):
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.replace("：", ":", 1).split(":", 1)
        if len(parts) == 2:
            city, address = (p.strip() for p in parts)
        elif len(cities) == 1:
            city, address = cities[0], line
        else:
            raise ValueError("多个城市请每行填写“城市：完整酒店名称或地址”。")
        city = CITY_ALIASES.get(city, city)
        if city in result:
            raise ValueError(f"{city}重复填写了住处，请每个城市保留一个住宿地址。")
        if not address:
            raise ValueError("冒号后还没有住宿地址，请补充酒店完整名称或地址。")
        result[city] = address
    return result


def model_errors(exc):
    """Never echo rejected inputs or a speculative list of unrelated constraints."""
    result = {}
    root_keys = {
        "目的地城市不能重复": ["form_destinations"], "旅行天数不能少于目的地城市数量": ["form_days"],
        "必去地点与排除地点冲突": ["form_must", "form_excluded"], "房间数不能超过人数": ["form_rooms"],
        "不支持跨午夜活动，请填写同一天内的活动时段": ["form_daily_start", "form_daily_end"],
        "指定日序超过旅行天数": ["form_days"], "每日活动时段不能重复": ["form_days"],
        "每日最多三处地点，不能重复": ["form_days"], "住宿城市和地址不能为空，地址最多200字": ["form_lodging"],
        "城市名称不能为空或超过60字": ["form_destinations"],
        "同一景点同一天只能保留一个预约确认": ["form_must"],
    }
    for error in exc.errors(include_input=False, include_url=False):
        loc, kind, ctx = error["loc"], error["type"], error.get("ctx", {})
        message = error["msg"].removeprefix("Value error, ")
        keys = [FIELD_KEYS[loc[0]]] if loc and loc[0] in FIELD_KEYS else root_keys.get(message, [])
        if not keys:
            # A program/state incompatibility must not be presented as the user's typo.
            keys, message = ["form"], "保存的表单设置与当前版本不兼容，尚未提交。请开始新的旅行或重新读取历史版本。"
        elif kind == "string_too_long":
            message = f"此项最多填写{ctx['max_length']}个字，请缩短内容。"
        elif kind == "too_long":
            message = f"此项最多填写{ctx['max_length']}项，请减少数量。"
        elif kind == "less_than_equal":
            message = f"此项不能超过{ctx['le']:g}。"
        elif kind == "greater_than_equal":
            message = f"此项不能少于{ctx['ge']:g}。"
        elif kind == "greater_than":
            message = f"此项需要大于{ctx['gt']:g}。"
        elif kind not in {"value_error", "string_too_long", "too_long"}:
            message = "此项格式不正确，请核对填写内容。"
        for key in keys:
            if message not in result.setdefault(key, []):
                result[key].append(message)
    return result


def validate_form(values, previous=None, today=None):
    today = today or date.today()
    result = FormResult()
    old = previous.get("constraints", {}) if previous else {}
    cities = split_names(values["form_destinations"])
    canonical = [CITY_ALIASES.get(c, c) for c in cities]
    origin = values["form_origin"].strip() or None
    days, people, rooms = values["form_days"], values["form_people"], values["form_rooms"]
    start, end = values["form_daily_start"], values["form_daily_end"]
    must, excluded = split_names(values["form_must"]), split_names(values["form_excluded"])
    required = {"form_origin": (origin, "出发地"), "form_destinations": (cities, "目的地"),
        "form_date": (values["form_date"], "游玩开始日期"), "form_days": (days, "旅行天数"),
        "form_people": (people, "出行总人数"), "form_budget": (values["form_budget"], "全团总预算"),
        "form_daily_start": (start, "每天从住处出发的时间"), "form_daily_end": (end, "最后一个游玩项目结束的时间")}
    for key, (value, label) in required.items():
        if value is None or value == "" or value == []:
            result.error(key, "请填写" + label + "。")
    if values["form_date"] and values["form_date"] < today:
        result.error("form_date", "所选开始日期已过去，请选择今天或之后的日期。")
    if len(canonical) != len(set(canonical)):
        result.error("form_destinations", "目的地有重复城市（城市全称与简称也视为同一城市），请保留一次。")
    count = len(set(canonical))
    if days and count > days:
        result.error("form_days", f"你填写了{count}个目的地，至少需要{count}天；当前为{days}天。")
    overlap = sorted(set(must) & set(excluded))
    if overlap:
        message = "“" + "、".join(overlap) + "”同时出现在必去和排除中，请在其中一栏移除。"
        result.error("form_must", message)
        result.error("form_excluded", message)
    if people and rooms and rooms > people:
        result.error("form_rooms", f"当前出行总人数为{people}人，房间数为{rooms}间。房间数不能超过人数，请核对人数（含本人）或房间数。")
    if start is not None and end is not None and start >= end:
        result.error("form_daily_start", f"开始时间{start:%H:%M}应早于结束时间{end:%H:%M}，暂不支持跨午夜。")
        result.error("form_daily_end", f"结束时间{end:%H:%M}应晚于开始时间{start:%H:%M}，暂不支持跨午夜。")
    for key in ("form_interests", "form_must", "form_excluded"):
        if any(len(name) > 80 for name in split_names(values[key])):
            result.error(key, "每个兴趣或地点名称最多80字，请用逗号分隔不同项目。")
    lodging = {}
    try:
        lodging = lodging_inputs(values["form_lodging"], cities)
        allowed = set(canonical + [CITY_ALIASES.get(origin, origin)])
        if any(city not in allowed for city in lodging):
            result.error("form_lodging", "住宿城市需要属于本次目的地或出发地，请核对冒号前的城市。")
    except ValueError as exc:
        result.error("form_lodging", str(exc))
    prices = {}
    for label, name in (("hotel", "hotel_per_room_night"), ("food", "food_per_person_day"), ("ticket", "ticket_per_person_visit")):
        amount = values[f"form_{label}_budget"]
        if amount is not None:
            prices[name] = {"low": amount, "high": amount}
        elif label in values["form_price_ranges"]:
            prices[name] = values["form_price_ranges"][label]
    advanced = {key: old.get(key, {}) for key in ("anchors", "day_place_ids", "place_choices", "indoor_sources")}
    old_cities = [CITY_ALIASES.get(c, c) for c in old.get("cities", [])]
    dimensions_changed = bool(old) and (old.get("days") != days or old_cities != canonical)
    if dimensions_changed and advanced["day_place_ids"]:
        advanced["day_place_ids"] = {}
        result.notes["form_days"] = ["天数或目的地已改变，重新生成时会重新安排原来按天指定的景点；必去地点仍保留。"]
    if old and old_cities != canonical:
        advanced["anchors"] = {city: positions for city, positions in advanced["anchors"].items()
                               if CITY_ALIASES.get(city, city) in set(canonical + ([CITY_ALIASES.get(origin, origin)] if origin else []))}
        advanced["place_choices"], advanced["indoor_sources"] = {}, {}
    rules = [r for r in old.get("day_rules", []) if days and r["day"] <= days]
    note = values["form_pricing_note"]
    if any(values[f"form_{label}_budget"] is not None for label in ("hotel", "food", "ticket")):
        note = "住宿、餐饮与门票按填写的预算或现有估算计算，非实时价格；实际费用可能变化。"
    if not result.errors:
        try:
            result.constraints = Constraints(origin=origin, cities=cities, start_date=values["form_date"], days=days,
                people=people, budget=values["form_budget"], interests=split_names(values["form_interests"]),
                must_visit=must, excluded=excluded, transport=values["form_transport"], return_to_origin=values["form_return"],
                accessibility_required=values["form_accessible"], rooms=rooms or None, pricing_note=note,
                daily_start=start, daily_end=end, daily_end_scope="last_activity", home_transport_scope="separate",
                lodging_addresses=lodging, visit_minutes=old.get("visit_minutes", 90), day_rules=rules,
                opening_rules=old.get("opening_rules", []), reservations=old.get("reservations", []), **advanced, **prices)
        except ValidationError as exc:
            result.errors = model_errors(exc)
    return result
