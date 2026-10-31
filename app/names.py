"""Resolve names using identity and provenance, never silently pick a fuzzy hit."""
from app.schemas import Clarification, PlaceBinding

CITY_ALIASES = {city + "市": city for city in
                ["广州", "深圳", "珠海", "北京", "上海", "杭州", "成都", "南京", "武汉", "西安", "苏州"]}
# These aliases apply only to synthetic fixture records; they are not a live POI authority.
FIXTURE_ALIASES = {("广州", "广东博物馆"): "广东省博物馆"}


def normalize_cities(c):
    data = c.model_dump(mode="json")
    data["origin"] = CITY_ALIASES.get(c.origin, c.origin)
    data["cities"] = [CITY_ALIASES.get(city, city) for city in c.cities]
    data["anchors"] = {CITY_ALIASES.get(city, city): value for city, value in data["anchors"].items()}
    data["lodging_addresses"] = {CITY_ALIASES.get(city, city): value for city, value in data["lodging_addresses"].items()}
    return type(c).model_validate(data)


async def resolve_names(c, facts, provider):
    bindings, questions = [], []
    for field in ("must_visit", "excluded"):
        for query in getattr(c, field):
            key = field + ":" + query
            options = {}
            for city in c.cities:
                for p in facts[city].places:
                    if p.name == query or query in p.name or query in p.address:
                        options[p.id] = p
                # Search even if an exact in-context match exists: same-name branches matter.
                for p in await provider.search(city, query, c, "attraction"):
                    options[p.id] = p
                    if not any(old.id == p.id for old in facts[city].places):
                        facts[city].places.append(p)
                    if p.evidence_id not in facts[city].evidence_ids:
                        facts[city].evidence_ids.append(p.evidence_id)
            selected, basis = None, ""
            if key in c.place_choices:
                selected = options.get(c.place_choices[key])
                basis = "用户确认候选POI ID"
            else:
                exact = [p for p in options.values() if p.name == query]
                if len(exact) == 1:
                    selected, basis = exact[0], "同一目标城市范围内唯一规范名"
                if provider.__class__.__name__ == "FixtureProvider":
                    aliases = [p for f in facts.values() for p in f.places
                               if FIXTURE_ALIASES.get((p.city, query)) == p.name]
                    if len(aliases) == 1 and not exact:
                        selected, basis = aliases[0], "人工演示数据别名表；不代表真实查询验证"
            if selected:
                ev = provider.record("fixture" if basis.startswith("人工") else "user" if key in c.place_choices else "estimate",
                                     "地点名称映射", f"{field}：{query} → {selected.name} ({selected.id})；{basis}")
                bindings.append(PlaceBinding(field=field, query=query, place_id=selected.id,
                    canonical_name=selected.name, city=selected.city, basis=basis, evidence_id=ev))
            else:
                reason = "没有查到该地点，请修改名称或补充所在城市" if not options else "名称未唯一确认，请选择具体地点"
                questions.append(Clarification(key=key, field=field, query=query, reason=reason,
                                                options=list(options.values())[:20]))
    must_ids = {b.place_id for b in bindings if b.field == "must_visit"}
    excluded_ids = {b.place_id for b in bindings if b.field == "excluded"}
    if must_ids & excluded_ids:
        questions.append(Clarification(key="identity_conflict", field="must_visit", query="必去与排除",
                                        reason="不同名称映射到了同一个地点，请修改其中一项"))
    data = c.model_dump(mode="json")
    data["bindings"] = [b.model_dump(mode="json") for b in bindings]
    return type(c).model_validate(data), questions


def must_ids(c):
    return {b.place_id for b in c.bindings if b.field == "must_visit"}


def excluded_ids(c):
    return {b.place_id for b in c.bindings if b.field == "excluded"}
