"""Human-facing descriptions for both new and previously saved trips."""
import re
from urllib.parse import urlencode

AUDIT_MESSAGES = {
    "home_transport": "往返交通的起终点、费用或来源与行程不一致，请重新生成",
    "day_count": "行程天数或日期顺序与填写的信息不一致",
    "city_order": "城市游玩顺序与填写的信息不一致",
    "date_sequence": "行程日期没有按顺序衔接",
    "overnight_city": "住宿城市与当天的游玩城市不一致",
    "activity_time": "有活动超出可用时间，或与其他安排重叠",
    "source_missing": "部分地点、路线或费用缺少可核对的来源",
    "place_city": "有景点与当天所在城市不符，请重新选择",
    "opening_conflict": "有景点与已填写的开放时间冲突",
    "admission_unverified": "部分景点的入场资料尚未核实，请先完成景点入场核验",
    "reservation_required": "有景点需要预约或已约满，请确认预约或调整到访日期",
    "reservation_window": "有景点的预计入场时间不在你确认的预约时段内",
    "ticket_unverified": "部分门票价格尚未核实，目前费用仍是预算参考",
    "ticket_price": "门票费用与已核对的票价或人数不一致",
    "visit_duration": "景点游览时长与填写的要求不一致",
    "travel_minutes": "当日交通总时长与分段交通安排不一致",
    "duplicate_place": "行程重复安排了同一个景点",
    "missing_must_visit": "还有必去景点未能排入行程，请调整时间或交通安排",
    "excluded_place": "行程包含了你要求排除的地点",
    "budget_missing": "行程还没有完整的预算明细",
    "budget_arithmetic": "费用明细与合计金额不一致，请重新计算",
    "party_quantities": "住宿晚数、房间数或餐饮人数计算不一致",
    "budget_status": "预算判断与费用合计不一致",
    "over_budget": "预计费用已经超过总预算",
    "uncertain": "预计费用可能超过总预算，需要调整安排或预算",
}


def plain_text(value):
    text = str(value or "")
    text = re.sub(r"；页面SHA256：[0-9a-f /]+", "", text)
    text = re.sub(r"(?:结果审计未通过[：:]\s*|audit_)([a-z_]+)",
                  lambda m: AUDIT_MESSAGES.get(m[1], "行程检查发现未解决的问题"), text)
    replacements = {
        "bindings": "已确认的地点", "place_id": "地点编号", "must_visit": "必去景点要求",
        "excluded": "排除地点", "day_too_long": "当天时间不足", "time_window": "可用活动时间",
        "missing_must_visit": "必去景点尚未安排", "events": "实际安排", "reason": "安排说明",
        "error": "错误", "warning": "提醒", "Schema": "信息格式", "POI": "地点",
        "fixture": "演示数据", "estimate": "预算估算", "live": "实际查询", "user": "你提供的信息",
    }
    for key, label in replacements.items():
        text = re.sub(rf"(?<![a-zA-Z0-9_]){re.escape(key)}(?![a-zA-Z0-9_])", label, text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:B[0-9A-Z]{7,}|ev_[0-9a-f]+)\b", "该地点", text)
    text = text.replace("代码校验为错误的", "检查发现的").replace("条目", "问题")
    return text


def issue_message(issue):
    code = issue.get("code", "") if isinstance(issue, dict) else issue.code
    message = issue.get("message", "") if isinstance(issue, dict) else issue.message
    if code.startswith("audit_"):
        return AUDIT_MESSAGES.get(code[6:], "行程中还有未解决的问题，请调整后重新生成")
    if code == "reviewer_issue":
        matched_day = re.search(r"第(\d+)天", message)
        prefix = f"第{matched_day[1]}天" if matched_day else "当天"
        if re.search(r"day_too_long|time_window", message):
            return prefix + "的交通和游玩安排超过了可用时间，请减少景点或调整出行时间。"
        if re.search(r"must_visit|events为空", message):
            return prefix + "还没有排入必去景点，请先解决交通或时间冲突，再重新生成。"
        if "重复" in message and "城际交通" in message:
            return "多天的城际交通安排出现重复，请确认出发和到达时间后重新生成。"
    return plain_text(message)


def day_description(day, ready=True):
    """Describe scheduled activities, so an old model explanation cannot claim absent visits."""
    value = day if isinstance(day, dict) else day.model_dump(mode="json")
    visits = [e["title"] for e in value["events"] if e["kind"] == "visit"]
    if visits:
        return f"今天在{value['city']}游玩，依次前往" + "、".join(visits) + "。交通和休息时间见下方安排。"
    if not ready:
        return "这一天还没有排出可执行的游玩安排，请先处理时间或交通提示，再重新生成。"
    return f"今天在{value['city']}自由活动，暂未安排新的景点。你可以休息、逛街，或补充想去的地方。"


def hotel_details(hotel, constraints, on_date, reference=None, queried=False):
    value = hotel if isinstance(hotel, dict) else hotel.model_dump(mode="json")
    c = constraints if isinstance(constraints, dict) else constraints.model_dump(mode="json")
    price = c["hotel_per_room_night"]
    amount = (price["low"] + price["high"]) / 2
    query = f"{value['city']} {value['name']} {on_date} 入住价格"
    return {
        "name": value["name"],
        "map_url": "https://uri.amap.com/marker?" + urlencode({"position": value["location"],
                    "name": value["name"], "src": "travel-agent-lab"}),
        "search_url": "https://www.bing.com/search?" + urlencode({"q": query}),
        "budget_label": f"住宿预算参考：约 ¥{amount:,.0f} / 间 / 晚（按你的住宿预算估算）",
        "price_note": ("已取得美团房型与房价参考；最终房价、税费及可订情况以酒店详情页为准。" if reference else
                       "已查询房价，但尚未取得可准确对应这家分店及入住日期的报价；不套用其他分店价格。" if queried else
                       "本次未取得这家酒店的指定日期报价；上方是预算参考，不是该酒店的报价。"),
    }
def weather_line(value):
    data = value if isinstance(value, dict) else value.model_dump()
    text = data["city"] + " · " + data["condition"]
    low, high = data.get("low_c"), data.get("high_c")
    if low is not None and high is not None:
        text += f" · {low:g}～{high:g}℃"
    elif high is not None:
        text += f" · 白天{high:g}℃"
    elif low is not None:
        text += f" · 夜间{low:g}℃"
    if data.get("is_demo"):
        text += "（演示）"
    return text


def admission_summary(value, constraints):
    from datetime import UTC, datetime, timedelta

    item = value if isinstance(value, dict) else value.model_dump(mode="json")
    c = constraints if isinstance(constraints, dict) else constraints.model_dump(mode="json")
    if not item.get("enforced", True):
        return "离线演示，未作真实入场核验"
    now = datetime.now(UTC)
    expires = datetime.fromisoformat(item["expires_at"]) if item.get("expires_at") else None
    if expires and expires < now:
        return "入场资料已过期，请重新核验；历史价格和余量不能作为出发依据"
    opening = {"unknown": "开放时间待核实", "closed": "该日闭馆或常规不开放", "open": "常规开放时段已核对"}[item["opening_status"]]
    if item["opening_status"] == "open":
        opening += "：" + "、".join(w["start"][:5] + "–" + w["end"][:5] for w in item["windows"])
    if item["opening_status"] == "unknown" and item.get("reference_text"):
        opening += "（已取得美团参考，尚缺可直接核对的官方规则）"
    if item.get("last_entry"):
        opening += "；" + item["last_entry"][:5] + "停止入场"
    confirmed = any(r["place_id"] == item["place_id"] and r["on_date"] == item["on_date"]
        and r["people"] == c.get("people") for r in c.get("reservations", []))
    booking = ("你已确认预约（未验证凭证）" if confirmed else "需预约，尚未确认" if item["reservation_required"] else
               "无需提前预约" if item["reservation_required"] is False else "是否预约待核实")
    stamp = datetime.fromisoformat(item["availability_checked_at"]) if item.get("availability_checked_at") else None
    availability = item["availability"] if stamp and now - timedelta(minutes=10) <= stamp <= now + timedelta(minutes=5) else "unknown"
    if availability == "sold_out" and not confirmed:
        booking += "；接口显示已约满"
    elif item["reservation_required"] and not confirmed:
        booking += "；实时余量未知" if availability == "unknown" else "；查询时可约，不代表已约到"
    price = "票价待核实，仍按预算计算" if item["ticket_price"] is None else f"基础票参考 ¥{item['ticket_price']:g}/人"
    return opening + " · " + booking + " · " + price


