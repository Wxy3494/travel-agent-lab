"""Version comparisons report actual persisted changes, not model descriptions."""


def compare(before, after):
    old, new = before.constraints.model_dump(mode="json"), after.constraints.model_dump(mode="json")
    constraints = [{"field": k, "before": old[k], "after": new[k]} for k in old if old[k] != new[k]]
    a = {day.day: day.model_dump(mode="json") for day in before.days}
    b = {day.day: day.model_dump(mode="json") for day in after.days}
    days = [{"day": day, "before": a.get(day), "after": b.get(day)} for day in sorted(set(a) | set(b))
            if a.get(day) != b.get(day)]
    return {"from_version": before.version, "to_version": after.version, "constraints": constraints, "days": days,
            "budget_before": before.budget.total.model_dump() if before.budget else None,
            "budget_after": after.budget.total.model_dump() if after.budget else None}
