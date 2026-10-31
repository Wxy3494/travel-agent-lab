"""Limited Chinese demo parser. Only explicit recognized fields become a patch.

This is a deterministic convenience for local chat demos, not an LLM extraction claim.
Unrecognized requests stay pending rather than silently succeeding.
"""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.day_input import DAY_REFERENCE, day_references
from app.providers import ProviderError

NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10}
CITIES = r"广州|深圳|珠海"
INTEGER = r"\d+|[一二两三四五六七八九十]+"


def number(value):
    if value.isdigit():
        return int(value)
    if value in NUMBERS:
        return NUMBERS[value]
    if value.count("十") == 1:
        tens, units = value.split("十")
        return (NUMBERS[tens] if tens else 1) * 10 + (NUMBERS[units] if units else 0)
    raise ProviderError("demo_text_unsupported", "请用数字填写天数或人数，例如30天、2人。")


def demo_patch(text):
    patch = {}
    try:
        days = day_references(text)
    except ValueError as exc:
        raise ProviderError("invalid_day", str(exc)) from None
    scoped = DAY_REFERENCE.search(text)
    if scoped and re.search(r"轻松|只安排下午|只安排上午|游览\d+分钟|\d{1,2}[:：]\d{2}", text):
        rule = {"day": days[0]}
        if "只修改" in text and "下午" in text:
            rule["period"] = "afternoon"
        if "轻松" in text:
            rule["pace"] = "relaxed"
        if "只安排下午" in text:
            rule["start"] = "13:00"
        elif "只安排上午" in text:
            rule["end"] = "12:00"
        period = re.search(r"(\d{1,2})[:：](\d{2})\s*(?:到|至|[-–])\s*(\d{1,2})[:：](\d{2})", text)
        if period:
            rule.update(start=f"{int(period[1]):02}:{period[2]}", end=f"{int(period[3]):02}:{period[4]}")
        duration = re.search(r"游览(\d+)分钟", text)
        if duration:
            rule["visit_minutes"] = int(duration[1])
        patch["day_rules"] = [rule]
    elif re.search(r"第.{0,3}天|只.{0,2}(?:上午|下午)|节奏|轻松|预约|开放时间", text):
        raise ProviderError("demo_text_unsupported", "这段离线文字暂未识别；可用高级时间设置、分日修改或地点候选选择完成需求")
    origin = re.search(rf"(?:从|出发地[：:]?\s*)({CITIES})(?:市)?(?:出发)?", text)
    if origin:
        patch["origin"] = origin[1]
    city_text = text[origin.end():] if origin else text
    city_text = re.split(r"(?:必去|排除|不去|不要去|喜欢|兴趣|预算)", city_text)[0]
    cities = list(dict.fromkeys(re.findall(CITIES, city_text)))
    if cities:
        patch["cities"] = cities
    for field, pattern in [("days", rf"(?<![第\d一二两三四五六七八九十])({INTEGER})\s*(?:天|日游)"),
                           ("people", rf"({INTEGER})\s*(?:人|位)"),
                           ("rooms", rf"({INTEGER})\s*间(?:房)?")]:
        match = re.search(pattern, text)
        if match:
            patch[field] = number(match[1])
    dated = re.search(r"(\d{4})[-年/.](\d{1,2})[-月/.](\d{1,2})(?:日)?", text)
    if dated:
        patch["start_date"] = f"{dated[1]}-{int(dated[2]):02}-{int(dated[3]):02}"
    else:
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        for word, offset in [("后天", 2), ("明天", 1), ("今天", 0)]:
            if word in text:
                patch["start_date"] = str(today + timedelta(days=offset))
                break
    budget = re.search(r"(?:全团(?:总)?预算|总预算|预算|全团)[为是：:约\s]*"
                       r"(?:改成|改为|调整为|调整到|控制在|不超过)?\s*(\d+(?:\.\d+)?)\s*(万)?", text)
    if budget:
        patch["budget"] = float(budget[1]) * (10000 if budget[2] else 1)
    for field, label in [("interests", "喜欢|兴趣"), ("must_visit", "必去"), ("excluded", "排除|不去|不要去")]:
        value = re.search(rf"(?:{label})[：:是为\s]*([^，。；;\n]+)", text)
        if value:
            patch[field] = [s.strip() for s in re.split(r"[,、]|和", value[1]) if s.strip()]
    if "自驾" in text:
        patch["transport"] = "driving"
    elif "公共交通" in text:
        patch["transport"] = "public"
    if "不返回" in text:
        patch["return_to_origin"] = False
    elif "返回出发地" in text or "回到出发地" in text:
        patch["return_to_origin"] = True
    if "无障碍" in text:
        patch["accessibility_required"] = "不要求无障碍" not in text
    if not patch:
        raise ProviderError("demo_text_unsupported", "离线演示没有识别出明确旅行字段，请说明城市、完整日期/明天、天数、人数和全团预算")
    return patch


def merge_day_rules(existing, patch):
    """A text patch for one day must not erase another day's explicit rules."""
    if patch.get("day_rules"):
        rules = {r["day"]: r for r in existing.get("day_rules", [])}
        for rule in patch["day_rules"]:
            rules[rule["day"]] = {**rules.get(rule["day"], {}), **rule}
        patch = {**patch, "day_rules": list(rules.values())}
    return {**existing, **patch}
