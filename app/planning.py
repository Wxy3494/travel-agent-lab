"""Deterministic arithmetic and itinerary constraints; model approval cannot override these."""
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise
from math import ceil
from urllib.parse import urlencode

from app.admissions import admission_issues, confirmation
from app.names import excluded_ids, must_ids
from app.schemas import (
    Budget,
    BudgetItem,
    DayChoice,
    DayPlan,
    Event,
    HomeTransfer,
    Issue,
    PriceRange,
    Selection,
)
from app.weather import itinerary_weather


def money(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def scaled(price, quantity):
    return PriceRange(low=money(Decimal(str(price.low)) * quantity),
                      high=money(Decimal(str(price.high)) * quantity))


def sum_prices(prices):
    rows = list(prices)
    return PriceRange(low=money(sum((Decimal(str(p.low)) for p in rows), Decimal(0))),
                      high=money(sum((Decimal(str(p.high)) for p in rows), Decimal(0))))


def clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def minutes(value):
    return value.hour * 60 + value.minute


def day_window(c, day):
    rule = next((r for r in c.day_rules if r.day == day), None)
    start, end = minutes(c.daily_start), minutes(c.daily_end)
    if rule:
        start = max(start, minutes(rule.start)) if rule.start and rule.period == "day" else start
        end = min(end, minutes(rule.end)) if rule.end else end
    if day == 1 and c.arrival_time:
        start = max(start, minutes(c.arrival_time))
    if day == c.days and c.departure_time:
        end = min(end, minutes(c.departure_time))
    return start, end, rule


def allocate_days(c):
    base, remainder = divmod(c.days, len(c.cities))
    counts = [base + (index < remainder) for index in range(len(c.cities))]
    minimum = [max(1, ceil(len({b.place_id for b in c.bindings if b.field == "must_visit" and b.city == city}) / 3))
               for city in c.cities]
    if sum(minimum) > c.days:
        from app.providers import ProviderError
        raise ProviderError("too_many_activities", f"按每天最多三处景点，各城必去地点至少需要共{sum(minimum)}天；请增加天数或调整必去地点。")
    for index, required in enumerate(minimum):
        while counts[index] < required:
            donor = max(range(len(counts)), key=lambda i: counts[i] - minimum[i])
            counts[donor] -= 1
            counts[index] += 1
    return [city for city, count in zip(c.cities, counts, strict=True) for _ in range(count)]


def demo_selection(c, facts):
    """Transparent fixture planner, not an LLM/multi-agent performance result."""
    used, days = set(), []
    schedule = allocate_days(c)
    for index, city in enumerate(schedule):
        candidates = [p for p in facts[city].places if p.id not in used and p.name not in c.excluded
                      and p.id not in excluded_ids(c)]
        candidates.sort(key=lambda p: (
            p.id not in must_ids(c) and p.name not in c.must_visit,
            not any(term in p.name for term in c.interests),
            p.indoor is not True,
        ))
        remaining_must = [p for p in candidates if p.name in c.must_visit or p.id in must_ids(c)]
        start, end, rule = day_window(c, index + 1)
        relaxed = rule and rule.pace == "relaxed" and rule.period == "day"
        slots = min(3, max(1 if relaxed or end - start < 300 else 2, len(remaining_must)))
        chosen = candidates[:slots]
        if index + 1 in c.day_place_ids:
            available = {p.id: p for p in facts[city].places}
            chosen = [available[pid] for pid in c.day_place_ids[index + 1] if pid in available]
        if not chosen and not used and index + 1 not in c.day_place_ids:
            return None
        used.update(p.id for p in chosen)
        days.append(DayChoice(day=index + 1, city=city, place_ids=[p.id for p in chosen],
                              reason="优先安排必去和感兴趣的地点，并留出交通与休息时间。" if chosen else
                              "已查询的景点已经安排完，这一天留作自由活动，不重复安排同一景点。"))
    return Selection(days=days)


def validate_selection(c, selection, facts):
    errors = []
    if len(selection.days) != c.days or [d.day for d in selection.days] != list(range(1, c.days + 1)):
        errors.append(Issue(code="day_count", message="行程天数或日序与需求不一致"))
    cities = [d.city for d in selection.days]
    compressed = [city for i, city in enumerate(cities) if i == 0 or city != cities[i - 1]]
    if compressed != c.cities:
        errors.append(Issue(code="city_order", message="目的地或城市先后顺序与用户需求不一致"))
    seen, names = set(), set()
    for day in selection.days:
        available = {p.id: p for p in facts[day.city].places} if day.city in facts else {}
        for pid in day.place_ids:
            if pid not in available:
                errors.append(Issue(code="unknown_place", message="规划引用了未查询到或不属于该城市的地点"))
                continue
            p = available[pid]
            if p.evidence_id not in provider_evidence(facts):
                errors.append(Issue(code="place_source_missing", message="地点缺少资料来源"))
            names.add(p.name)
            if pid in seen:
                errors.append(Issue(code="duplicate_place", message=f"地点重复：{p.name}"))
            if p.name in c.excluded or pid in excluded_ids(c):
                errors.append(Issue(code="excluded_place", message=f"包含用户排除地点：{p.name}"))
            seen.add(pid)
    for name in c.must_visit:
        binding = next((b for b in c.bindings if b.field == "must_visit" and b.query == name), None)
        if (binding and binding.place_id not in seen) or (not binding and name not in names):
            errors.append(Issue(code="missing_must_visit", message=f"未安排必去地点：{name}；可能未查到匹配记录"))
    return errors


def provider_evidence(facts):
    return {eid for city in facts.values() for eid in city.evidence_ids}


async def materialize(c, selection, facts, provider, previous=None, reuse_days=None, afternoon_day=None, visit_checks=None):
    """Schedule verified POIs, query every leg, then calculate whole-party cost."""
    errors = validate_selection(c, selection, facts)
    known = set(provider.evidence)
    if any(p.evidence_id not in known for f in facts.values() for p in f.places + f.hotels):
        errors.append(Issue(code="source_missing", message="地点或住宿候选引用了未登记来源"))
    if errors:
        return [], None, errors
    issues = [Issue(code="opening_hours_unknown", severity="warning",
                    message="请查看各景点的入场核验结果；未核实的项目、临时闭馆和无障碍设施仍需出发前确认，游览时长为规划假设"),
              Issue(code="transport_estimate", severity="warning",
                    message="交通耗时为查询时估计，未核验出行日期班次；未选择酒店/车站时使用城市中心衔接假设")]
    if c.accessibility_required:
        issues.append(Issue(code="accessibility_unverified", message="工具未提供可靠无障碍证明，需要人工核实后才能确认方案"))
    days, items = [], []
    price_ev = provider.record("estimate", "用户可调预算假设", c.pricing_note)
    previous_city = c.origin
    origin_facts = facts[c.origin]
    position = origin_facts.location
    rooms = c.rooms or ceil(c.people / 2)
    cars = ceil(c.people / 4)
    reuse_days = set(reuse_days or [])
    anchors = {}
    for city, choice in c.anchors.items():
        if city not in facts:
            from app.providers import ProviderError
            raise ProviderError("anchor_invalid", "酒店/车站所在城市不在当前旅行中，请清除该位置或重新选择")
        anchors[city] = {}
        for key, category in [("hotel_id", "hotel"), ("departure_station_id", "station"), ("arrival_station_id", "station")]:
            if key == "hotel_id" and facts[city].lodging:
                continue
            pid = getattr(choice, key)
            if pid:
                point = await provider.by_id(city, pid, c, category)
                if point is None:
                    from app.providers import ProviderError
                    raise ProviderError("anchor_invalid", "所选酒店/车站未核实坐标、城市或类型，请重新查询选择")
                anchors[city][key] = point
    for city, city_facts in facts.items():
        if city_facts.lodging:
            anchors.setdefault(city, {})["hotel_id"] = city_facts.lodging

    def location(city, key, fallback):
        point = anchors.get(city, {}).get(key)
        return point.location if point else fallback

    position = location(c.origin, "hotel_id", position)
    if selection.days[0].city == c.origin:
        position = location(c.origin, "arrival_station_id", position)
    planned_ids = {pid for day in selection.days for pid in day.place_ids}
    admission_map = {(check.place_id, check.on_date): check for check in visit_checks or []}

    async def home_transfer(from_city, to_city, direction, day):
        # No departure/arrival clock is invented for journeys outside local sightseeing.
        points = [(location(from_city, "hotel_id", facts[from_city].location), from_city),
                  (location(from_city, "departure_station_id", location(from_city, "hotel_id", facts[from_city].location)), from_city),
                  (location(to_city, "arrival_station_id", facts[to_city].location), to_city),
                  (location(to_city, "hotel_id", facts[to_city].location), to_city)]
        prices, sources, segments, durations = [], [], [], []
        for (start_point, start_city), (end_point, end_city) in pairwise(points):
            if start_point == end_point and start_city == end_city:
                continue
            route = await provider.route(start_point, end_point, facts[start_city].citycode, facts[end_city].citycode, c.transport)
            if route["minutes"] <= 0:
                raise ValueError("Route must have positive duration")
            prices.append(route["cost"])
            sources.extend([route["evidence_id"], route.get("cost_evidence_id", route["evidence_id"])])
            segments.extend(route.get("segments", []))
            durations.append(route["minutes"] + (45 if start_city != end_city else 10))
        unit_price = sum_prices(prices)
        quantity = cars if c.transport == "driving" else c.people
        cost = scaled(unit_price, quantity)
        title = ("去程交通：" if direction == "outbound" else "返程交通：") + f"{from_city} → {to_city}"
        items.append(BudgetItem(category=title, quantity=quantity, unit="车" if c.transport == "driving" else "人",
                                unit_price=unit_price, total=cost, evidence_id=sources[-1], day=day))
        return HomeTransfer(direction=direction, from_city=from_city, to_city=to_city,
            estimated_minutes=sum(durations), unit_price=unit_price, cost=cost,
            evidence_ids=list(dict.fromkeys(sources)), start_location=points[0][0], end_location=points[-1][0],
            route_segments=segments,
            note="往返交通单独安排，不占每日游玩时间。耗时与费用按查询路线估算，含接驳和换乘预留；出行日期、实际车次及到达时间尚未确定。")

    for chosen in selection.days:
        local = facts[chosen.city]
        today = c.start_date + timedelta(days=chosen.day - 1)
        weather_cities = list(dict.fromkeys([previous_city, chosen.city] +
            ([c.origin] if chosen.day == c.days and c.return_to_origin else [])))
        departure_hotel = anchors.get(previous_city, {}).get("hotel_id")
        departure_note = (f"从 {departure_hotel.name} 出发（已核对位置）。" if departure_hotel else
                          f"从{previous_city}的住宿区域出发；未填写具体住处，路程暂按城市中心估算。")
        if previous and chosen.day in reuse_days:
            old = previous.days[chosen.day - 1]
            days.append(old.model_copy(deep=True))
            items.extend(i.model_copy(deep=True) for i in previous.budget.items if i.day == chosen.day)
            previous_city = chosen.city
            position = location(chosen.city, "hotel_id", local.location)
            continue
        home_transfers = []
        if c.home_transport_scope == "separate" and chosen.day == 1 and previous_city != chosen.city:
            home_transfers.append(await home_transfer(c.origin, chosen.city, "outbound", chosen.day))
            previous_city = chosen.city
            position = location(chosen.city, "hotel_id", local.location)
            hotel = anchors.get(chosen.city, {}).get("hotel_id")
            departure_note = (f"抵达目的地后，从 {hotel.name} 开始当天游玩。" if hotel else
                              f"抵达目的地后，从{chosen.city}住宿区域开始游玩；未填写住处，路程按城市中心估算。")
        cursor, end_limit, rule = day_window(c, chosen.day)
        events = []
        if cursor >= end_limit:
            issues.append(Issue(code="time_window", message=f"第{chosen.day}天没有可用活动时间，请调整到达/离开或活动时段"))
        if previous and afternoon_day == chosen.day:
            events = [e.model_copy(deep=True) for e in previous.days[chosen.day - 1].events if e.end <= "13:00"]
            starts = {e.start for e in events}
            items.extend(i.model_copy(deep=True) for i in previous.budget.items
                         if i.day == chosen.day and i.event_start in starts)
            if events:
                last = events[-1]
                cursor = max(cursor, int(last.end[:2]) * 60 + int(last.end[3:]), 13 * 60)
                position = last.end_location or position
                previous_city = chosen.city
            if rule and rule.start:
                cursor = max(cursor, minutes(rule.start))
        duration_minutes = sum(int(e.end[:2]) * 60 + int(e.end[3:]) - int(e.start[:2]) * 60 - int(e.start[3:])
                               for e in events if e.kind == "travel")

        async def travel(destination, target_city, title, events=events, end_limit=end_limit, chosen=chosen, is_return=False):
            nonlocal cursor, position, previous_city, duration_minutes
            if position == destination and previous_city == target_city:
                return True
            route = await provider.route(position, destination, facts[previous_city].citycode,
                                         facts[target_city].citycode, c.transport)
            if route["minutes"] <= 0:
                raise ValueError("Route must have positive duration")
            # Reserve explicit transfer buffer; does not claim a scheduled service.
            duration = route["minutes"] + (45 if previous_city != target_city else 10)
            route_limit = end_limit
            if is_return and c.daily_end_scope == "last_activity":
                route_limit = min(23 * 60 + 59, minutes(c.departure_time)) if chosen.day == c.days and c.departure_time else 23 * 60 + 59
            if cursor + duration > route_limit:
                message = f"第{chosen.day}天的{title}无法放入活动时段，需减少活动或延长时间"
                if previous_city != target_city:
                    message = (f"{previous_city}到{target_city}按当前地图路线约需{duration // 60}小时{duration % 60}分钟"
                               f"（含换乘预留），超过当天可用的{max(0, end_limit - cursor)}分钟。"
                               "请先确认实际车次和到达时间，或从目的地开始规划当地游玩；城际交通费用需要另行核算。")
                issues.append(Issue(code="day_too_long" if cursor + duration > 21 * 60 + 30 else "time_window",
                                    message=message))
                return False
            multiplier = cars if c.transport == "driving" else c.people
            cost = scaled(route["cost"], multiplier)
            cost_ev = route.get("cost_evidence_id", route["evidence_id"])
            events.append(Event(kind="travel", start=clock(cursor), end=clock(cursor + duration),
                                title=title, city=target_city, cost=cost,
                                evidence_ids=list(dict.fromkeys([route["evidence_id"], cost_ev])),
                                note=route["note"] + "；表中金额为全团费用；已加换乘缓冲",
                                start_location=position, end_location=destination, route_segments=route.get("segments", []),
                                is_return=is_return))
            items.append(BudgetItem(category=title, quantity=multiplier,
                                    unit="车" if c.transport == "driving" else "人",
                                    unit_price=route["cost"], total=cost, evidence_id=cost_ev,
                                    day=chosen.day, event_start=clock(cursor)))
            cursor += duration
            duration_minutes += duration
            position, previous_city = destination, target_city
            return True

        def pause(kind, length, title, pause_position, end_limit=end_limit, events=events, chosen=chosen):
            nonlocal cursor
            if cursor + length <= end_limit:
                events.append(Event(kind=kind, start=clock(cursor), end=clock(cursor + length), title=title,
                                    city=chosen.city, evidence_ids=[price_ev], cost=PriceRange(low=0, high=0),
                                    note="时间安排假设；餐饮费用已计入每日餐饮预算", start_location=pause_position, end_location=pause_position))
                cursor += length

        if previous_city != chosen.city:
            if not await travel(location(previous_city, "departure_station_id", position), previous_city, "前往出发车站"):
                break
            if not await travel(location(chosen.city, "arrival_station_id", local.location), chosen.city,
                                f"{previous_city} → {chosen.city} 城际交通"):
                # Do not claim arrival or retry the same impossible transfer on every subsequent day.
                break
        available = {p.id: p for p in local.places}
        preserved = {e.place_id for e in events if e.kind == "visit"}
        ids = [pid for pid in chosen.place_ids if pid not in preserved]
        comparison = {}
        in_day_return = chosen.day == c.days and c.return_to_origin and c.home_transport_scope == "in_day"
        destination = (location(c.origin, "arrival_station_id", origin_facts.location) if in_day_return
                       else location(chosen.city, "hotel_id", local.location))
        if chosen.day == c.days and not c.return_to_origin:
            destination = location(chosen.city, "departure_station_id", destination)
        if (c.optimize_routes and len(ids) > 1 and not c.opening_rules and previous_city == chosen.city
                and not (in_day_return and chosen.city != c.origin)):
            ids, comparison = await optimize_order(ids, available, position, destination, local, c, provider,
                                                   remaining_days=c.days - chosen.day)
        for pid in ids:
            place = available[pid]
            entry = admission_map.get((pid, today))
            if entry:
                issues.extend(admission_issues(entry, c))
                if entry.opening_status == "closed" or entry.availability == "sold_out" and not confirmation(c, entry):
                    continue
            if not await travel(place.location, chosen.city, f"前往 {place.name}"):
                continue
            visit_duration = (rule.visit_minutes if rule and rule.visit_minutes and
                              (rule.period == "day" or cursor >= 13 * 60) else c.visit_minutes)
            if cursor < 13 * 60 and cursor + visit_duration > 12 * 60 and not any(e.kind == "meal" for e in events):
                pause("meal", 60, "午餐与休息", position)
            if rule and rule.period == "afternoon" and (cursor >= 13 * 60 or cursor + visit_duration > 13 * 60):
                boundary = max(13 * 60, minutes(rule.start)) if rule.start else 13 * 60
                if cursor < boundary:
                    pause("wait", boundary - cursor, "自由时间至下午活动开始", position)
                visit_duration = rule.visit_minutes or c.visit_minutes
            opening = next((r for r in c.opening_rules if r.place_id == pid and r.on_date == today), None)
            opening_ev = None
            if entry and entry.opening_status == "open":
                booking = confirmation(c, entry)
                valid_starts = []
                for window in entry.windows:
                    candidate = max(cursor, minutes(window.start), minutes(booking.entry_window.start) if booking else 0)
                    if (candidate + visit_duration <= min(end_limit, minutes(window.end))
                            and (entry.last_entry is None or candidate < minutes(entry.last_entry))
                            and (booking is None or candidate < minutes(booking.entry_window.end))):
                        valid_starts.append(candidate)
                if not valid_starts:
                    issues.append(Issue(code="reservation_window" if booking else "opening_conflict",
                        message=f"第{chosen.day}天的{place.name}无法同时满足开放、最晚入场和已确认的预约时段，请调整活动顺序或日期。"))
                    continue
                if min(valid_starts) > cursor:
                    pause("wait", min(valid_starts) - cursor, "等待景点开放或预约入场", position)
            elif opening:
                opening_ev = provider.record("user", "用户提供开放信息（待自行核实来源）", opening.source)
                if opening.closed:
                    issues.append(Issue(code="opening_conflict", message=f"{today} {place.name}按提供的信息闭馆，请选择其他日期或地点"))
                    continue
                if cursor < minutes(opening.start):
                    pause("wait", minutes(opening.start) - cursor, "等待提供的开放时段", position)
                if cursor < minutes(opening.start) or cursor + visit_duration > minutes(opening.end):
                    issues.append(Issue(code="opening_conflict", message=f"第{chosen.day}天的{place.name}不符合提供的开放时段"))
                    continue
            if cursor + visit_duration > end_limit:
                issues.append(Issue(code="time_window", message=f"第{chosen.day}天的{place.name}游览时长无法放入活动时段"))
                continue
            unit_price = c.ticket_per_person_visit
            ticket_ev = price_ev
            ticket_note = "门票预算假设/全团；实际票价、开放与预约需核实"
            if entry and entry.ticket_price is not None:
                unit_price = PriceRange(low=entry.ticket_price, high=entry.ticket_price)
                ticket_ev = entry.evidence_id or price_ev
                label = {"official": "官网规则", "provider": "票务接口", "user": "你自行核对的资料"}.get(entry.authority, "示例资料")
                ticket_note = f"{label}的{entry.ticket_scope}，按{c.people}人计算；优惠资格和额外项目需另核对。"
            cost = scaled(unit_price, c.people)
            events.append(Event(kind="visit", start=clock(cursor), end=clock(cursor + visit_duration), title=place.name,
                                city=chosen.city, place_id=pid, evidence_ids=list(dict.fromkeys([place.evidence_id, price_ev] +
                                    ([place.indoor_evidence_id] if place.indoor_evidence_id else []) + ([opening_ev] if opening_ev else [])
                                    + ([entry.evidence_id] if entry and entry.evidence_id else []) + [ticket_ev])),
                                map_url="https://uri.amap.com/marker?" + urlencode({"position": place.location,
                                                                                  "name": place.name, "src": "travel-agent-lab"}),
                                cost=cost, note=ticket_note,
                                start_location=place.location, end_location=place.location))
            items.append(BudgetItem(category=f"{place.name} 门票", quantity=c.people, unit="人次",
                                    unit_price=unit_price, total=cost, evidence_id=ticket_ev,
                                    day=chosen.day, event_start=clock(cursor)))
            cursor += visit_duration
            relaxed_now = rule and rule.pace == "relaxed" and (rule.period == "day" or cursor >= 13 * 60)
            if c.daily_end_scope == "return" or pid != ids[-1]:
                pause("rest", 30 if relaxed_now else 20, "自由休息", position)
        if in_day_return:
            if previous_city != c.origin:
                await travel(location(previous_city, "departure_station_id", position), previous_city, "前往返程出发车站", is_return=True)
            await travel(location(c.origin, "arrival_station_id", origin_facts.location), c.origin, f"返回 {c.origin}", is_return=True)
        else:
            hotel = anchors.get(chosen.city, {}).get("hotel_id")
            departure_point = anchors.get(chosen.city, {}).get("departure_station_id") if chosen.day == c.days and not c.return_to_origin else None
            await travel(destination, chosen.city, f"到达离开车站 {departure_point.name}" if departure_point else
                         f"返回 {hotel.name}" if hotel else f"返回 {chosen.city} 住宿区域（按城市中心估算）", is_return=True)
            if chosen.day == c.days and c.return_to_origin and c.home_transport_scope == "separate" and chosen.city != c.origin:
                home_transfers.append(await home_transfer(chosen.city, c.origin, "return", chosen.day))
        weather_details = [itinerary_weather(city, today, facts[city].weather,
            demo=any(provider.evidence[eid].kind == "fixture" for eid in facts[city].evidence_ids if eid in provider.evidence))
            for city in weather_cities]
        local_weather = next(w for w in weather_details if w.city == chosen.city)
        weather = local_weather.condition
        if not local_weather.available:
            issues.append(Issue(code="weather_unavailable", severity="warning", message=f"{today} {chosen.city}无可用天气预报"))
        indoor = next((p for p in local.places if p.indoor is True and p.id not in planned_ids
                       and p.name not in c.excluded and p.id not in excluded_ids(c)), None)
        alternative = (f"下雨时可考虑 {indoor.name}，替换后重新算路并核实开放" if indoor else
                       "下雨时减少户外活动；先查询可开放的室内地点，再修改行程重新算路")
        if "雨" in weather:
            issues.append(Issue(code="rain_alternative", severity="warning", message=f"第{chosen.day}天有降雨：{alternative}"))
        days.append(DayPlan(day=chosen.day, date=today, city=chosen.city, events=events,
                            overnight_city=chosen.city if chosen.day < c.days else None,
                            hotel_options=local.hotels if chosen.day < c.days else [],
                            weather=weather, alternative=alternative, reason=chosen.reason,
                            weather_details=weather_details, departure_note=departure_note,
                            travel_minutes=duration_minutes, route_comparison=comparison,
                            home_transfers=home_transfers,
                            indoor_options=[p for p in local.places if p.indoor is True and p.id not in planned_ids
                                            and p.id not in excluded_ids(c) and p.name not in c.excluded][:5]))
    for category, quantity, unit, price in [
        ("住宿", (c.days - 1) * rooms, "间夜", c.hotel_per_room_night),
        ("餐饮", c.days * c.people, "人天", c.food_per_person_day),
    ]:
        items.append(BudgetItem(category=category, quantity=quantity, unit=unit, unit_price=price,
                                total=scaled(price, quantity), evidence_id=price_ev))
    total = sum_prices(item.total for item in items)
    status = "over_budget" if total.low > c.budget else "uncertain" if total.high > c.budget else "within_estimate"
    if status != "within_estimate":
        issues.append(Issue(code=status, message="估算下限已超过预算" if status == "over_budget" else "预算落在估算区间内，费用存在超支风险"))
    if not c.return_to_origin:
        issues.append(Issue(code="return_not_included", severity="warning", message="按需求未计入旅行结束后返回出发城市的交通"))
    known = set(provider.evidence)
    if any(not set(event.evidence_ids) <= known for day in days for event in day.events):
        issues.append(Issue(code="source_missing", message="行程引用了未登记来源"))
    if any(item.evidence_id not in known for item in items):
        issues.append(Issue(code="source_missing", message="预算引用了未登记来源"))
    return days, Budget(items=items, total=total, status=status), issues


async def optimize_order(ids, available, start, end, local, c, provider, remaining_days=0):
    """Compare two bounded orders using provider durations; coordinates only propose an order."""
    def distance(a, b):
        ax, ay = map(float, a.split(","))
        bx, by = map(float, b.split(","))
        return (ax - bx) ** 2 + (ay - by) ** 2
    proposed, pool, position = [], list(ids), start
    while pool:
        pid = min(pool, key=lambda pid: distance(position, available[pid].location))
        proposed.append(pid)
        pool.remove(pid)
        position = available[pid].location
    if proposed == ids:
        return ids, {"method": "邻近候选与当前顺序一致"}
    if provider.max_calls - provider.calls < 2 * (len(ids) + 1) + 5 * remaining_days:
        return ids, {"method": "保留原顺序：预留后续算路调用预算"}

    async def duration(order):
        locations = [start, *[available[pid].location for pid in order], end]
        total = 0
        for a, b in pairwise(locations):
            if a != b:
                route = await provider.route(a, b, local.citycode, local.citycode, c.transport)
                total += route["minutes"] + 10
        return total
    before, after = await duration(ids), await duration(proposed)
    return (proposed if after < before else ids), {
        "method": "坐标邻近提出顺序，再用同一地图工具逐段耗时比较；非全局最优",
        "before_minutes": before, "after_minutes": min(before, after),
        "before_ids": ids, "after_ids": proposed if after < before else ids}
