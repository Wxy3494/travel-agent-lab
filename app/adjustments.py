"""Explicit user choices, never automatic relaxation of hard constraints or prices."""
from math import ceil

from app.names import must_ids
from app.schemas import MAX_TRIP_DAYS, Adjustment


def suggestions(result):
    c, choices = result.constraints, []
    codes = {i.code.removeprefix("audit_") for i in result.issues if i.severity == "error"}
    if result.budget and codes & {"over_budget", "uncertain"}:
        amount = ceil(result.budget.total.high)
        if amount > c.budget:
            choices.append(Adjustment(id="increase_budget", title=f"将全团预算调整为 {amount} 元",
                impact=f"增加预算 {amount - c.budget:.0f} 元，按当前费用估算留足空间；人数、地点和单价不变。仍非实时报价。",
                changes={"budget": amount}))
    if codes & {"too_many_activities", "missing_must_visit"} and c.days < MAX_TRIP_DAYS:
        choices.append(Adjustment(id="extra_day", title="增加一天以安排必去地点",
            impact="明确延长旅行一天；保留全部必去地点与人数，新增住宿和餐饮预算后重新核验，不保证一次调整即可解决全部问题。",
            changes={"days": c.days + 1, "day_place_ids": {}}))
    if codes & {"time_window", "day_too_long", "over_budget", "uncertain", "opening_conflict"} and result.days:
        day_ids = {}
        removed = []
        for day in result.days:
            ids = [e.place_id for e in day.events if e.kind == "visit"]
            optional = [pid for pid in ids if pid not in must_ids(c)]
            # Missing activities are also represented in the last planned selection trace.
            planned = next((t.get("days", {}) for t in reversed(result.trace) if t["step"] == "selection"), {})
            ids = planned.get(str(day.day), ids)
            optional = [pid for pid in ids if pid not in must_ids(c)]
            if optional and len(ids) > 1:
                removed.append(optional[-1])
                day_ids[day.day] = [pid for pid in ids if pid != optional[-1]]
            else:
                day_ids[day.day] = ids
        if removed:
            saving = len(removed) * c.people * c.ticket_per_person_visit.high
            choices.append(Adjustment(id="reduce_optional", title="每天减少一处可选活动",
                impact=f"保留必去地点与人数，减少 {len(removed)} 处可选活动；门票预算最多减少 {saving:.0f} 元，交通重新计算后再核验。",
                changes={"day_place_ids": day_ids}))
        if codes & {"time_window", "day_too_long"} and c.visit_minutes > 30:
            choices.append(Adjustment(id="shorter_visits", title="将默认游览时长调整为30分钟",
                impact="缩短默认游览时长；已经明确指定的分日时长不变，必去地点、人数和价格假设不变。重新核验交通与时间窗。",
                changes={"visit_minutes": 30}))
        if c.days < MAX_TRIP_DAYS and not any(a.id == "extra_day" for a in choices):
            choices.append(Adjustment(id="extra_day", title="增加一天旅行时间",
                impact="延长旅行一天并重新分配城市和活动，增加一晚住宿及一天餐饮；原人数、必去地点与单价不变。",
                changes={"days": c.days + 1, "day_place_ids": {}}))
    return choices
