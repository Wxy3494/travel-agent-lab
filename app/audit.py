"""Independent output audit. No dependency on the itinerary builder or its arithmetic.

Used at the service boundary AND by evaluations; failure never becomes ready.
"""
from datetime import timedelta
from decimal import Decimal
from math import ceil

from app.schemas import Issue


def audit_result(result):
    c = result.constraints
    problems = set()
    days = result.days
    if not days:
        return []
    if len(days) != c.days or [d.day for d in days] != list(range(1, c.days + 1)):
        problems.add("day_count")
    cities = [d.city for d in days]
    if [city for i, city in enumerate(cities) if i == 0 or city != cities[i - 1]] != c.cities:
        problems.add("city_order")
    known = {e.id for e in result.evidence}
    names, pids = set(), []
    for day in days:
        if day.date != c.start_date + timedelta(days=day.day - 1):
            problems.add("date_sequence")
        if day.overnight_city != (day.city if day.day < c.days else None):
            problems.add("overnight_city")
        previous = 0
        rule = next((r for r in c.day_rules if r.day == day.day), None)
        lower, upper = c.daily_start, c.daily_end
        if rule:
            lower = max(lower, rule.start) if rule.start and rule.period == "day" else lower
            upper = min(upper, rule.end) if rule.end else upper
        if day.day == 1 and c.arrival_time:
            lower = max(lower, c.arrival_time)
        if day.day == c.days and c.departure_time:
            upper = min(upper, c.departure_time)
        lower, upper = lower.hour * 60 + lower.minute, upper.hour * 60 + upper.minute
        travel_total = 0
        for index, event in enumerate(day.events):
            start, end = None, None
            try:
                def minutes(value):
                    h, m = map(int, value.split(":"))
                    if not (0 <= h < 24 and 0 <= m < 60):
                        raise ValueError
                    return h * 60 + m
                start, end = minutes(event.start), minutes(event.end)
                limit = upper
                if event.is_return:
                    if event.kind != "travel" or any(not e.is_return for e in day.events[index + 1:]):
                        problems.add("activity_time")
                    if c.daily_end_scope == "last_activity":
                        limit = 23 * 60 + 59
                        if day.day == c.days and c.departure_time:
                            limit = min(limit, c.departure_time.hour * 60 + c.departure_time.minute)
                if start < previous or end <= start or start < lower or end > limit:
                    problems.add("activity_time")
                previous = end
                if event.kind == "travel":
                    travel_total += end - start
            except (ValueError, TypeError):
                problems.add("activity_time")
            if not event.evidence_ids or not set(event.evidence_ids) <= known:
                problems.add("source_missing")
            if event.kind == "visit":
                names.add(event.title)
                pids.append(event.place_id)
                if event.city != day.city or not event.place_id:
                    problems.add("place_city")
                opening = next((r for r in c.opening_rules if r.place_id == event.place_id and r.on_date == day.date), None)
                if opening and start is not None and end is not None and (opening.closed or start < opening.start.hour * 60 + opening.start.minute or
                                end > opening.end.hour * 60 + opening.end.minute):
                    problems.add("opening_conflict")
                duration = rule.visit_minutes if rule and rule.visit_minutes and (rule.period == "day" or event.start >= "13:00") else c.visit_minutes
                if start is not None and end is not None and end - start != duration:
                    problems.add("visit_duration")
        if day.travel_minutes != travel_total and day.travel_minutes:
            problems.add("travel_minutes")
        if any(h.evidence_id not in known for h in day.hotel_options):
            problems.add("source_missing")
    if len(pids) != len(set(pids)):
        problems.add("duplicate_place")
    bound_must = [b for b in c.bindings if b.field == "must_visit"]
    bound_excluded = [b for b in c.bindings if b.field == "excluded"]
    unbound_must = set(c.must_visit) - {b.query for b in bound_must}
    if not unbound_must <= names or not {b.place_id for b in bound_must} <= set(pids):
        problems.add("missing_must_visit")
    if set(c.excluded) & names or {b.place_id for b in bound_excluded} & set(pids):
        problems.add("excluded_place")
    if any(b.evidence_id not in known for b in c.bindings):
        problems.add("source_missing")
    b = result.budget
    if b is None:
        problems.add("budget_missing")
    else:
        for item in b.items:
            if item.evidence_id not in known:
                problems.add("source_missing")
            for bound in ("low", "high"):
                calculated = (Decimal(str(getattr(item.unit_price, bound))) * item.quantity).quantize(Decimal("0.01"))
                if abs(calculated - Decimal(str(getattr(item.total, bound)))) > Decimal("0.005"):
                    problems.add("budget_arithmetic")
        for bound in ("low", "high"):
            total = sum((Decimal(str(getattr(i.total, bound))) for i in b.items), Decimal(0))
            if abs(total - Decimal(str(getattr(b.total, bound)))) > Decimal("0.005"):
                problems.add("budget_arithmetic")
        expected = {"住宿": (c.days - 1) * (c.rooms or ceil(c.people / 2)), "餐饮": c.days * c.people}
        for category, quantity in expected.items():
            items = [i for i in b.items if i.category == category]
            if len(items) != 1 or items[0].quantity != quantity:
                problems.add("party_quantities")
        status = "over_budget" if b.total.low > c.budget else "uncertain" if b.total.high > c.budget else "within_estimate"
        if b.status != status:
            problems.add("budget_status")
        if status != "within_estimate":
            problems.add(status)
    return [Issue(code="audit_" + p, message="结果审计未通过：" + p) for p in sorted(problems)]
