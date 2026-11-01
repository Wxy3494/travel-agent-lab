"""Bind consultation text to exact places; it never becomes verified inventory."""
import re
import unicodedata
from datetime import timedelta

from app.schemas import EntryWindow, TravelReference


def normalized(value):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value)).casefold()


def targets_for(trip, selection=None, facts=None):
    targets = {}
    c = trip.constraints
    days = selection.days if selection else trip.days
    for day in days:
        when = c.start_date + timedelta(days=day.day - 1)
        if selection:
            places = [p for p in facts[day.city].places if p.id in day.place_ids]
            visits = [(p.id, p.name) for p in places]
            hotels = [facts[day.city].lodging] if facts[day.city].lodging else facts[day.city].hotels[:3]
        else:
            visits = [(e.place_id, e.title) for e in day.events if e.kind == "visit"]
            hotels = [day.suggested_stay] if day.suggested_stay else day.hotel_options
        for pid, name in visits:
            targets.setdefault(("attraction", pid), {"place_id": pid, "name": name, "city": day.city,
                "kind": "attraction", "dates": []})["dates"].append(when)
        if day.day < c.days:
            for hotel in hotels:
                targets.setdefault(("hotel", hotel.id), {"place_id": hotel.id, "name": hotel.name, "city": hotel.city,
                    "kind": "hotel", "dates": [], "address": hotel.address, "location": hotel.location})["dates"].append(when)
    return list(targets.values())


def parse_references(content, targets):
    from app.admissions import safe_url
    headings, offset = [], 0
    for line in content.splitlines(keepends=True):
        label = re.match(r"^\s*#{1,3}\s+(.+?)\s*$", line)
        bold = re.match(r"^\s*(?:[-*\d.、]+\s*)?\[?\*\*(.+?)\*\*", line)
        heading = label or bold
        text = normalized(heading[1] if heading else line.lstrip("#*-0123456789.、 "))
        matches = [t for t in targets if (text == normalized(t["name"]) if heading else
                   unicodedata.normalize("NFKC", line).lstrip("-* ").startswith(unicodedata.normalize("NFKC", t["name"]) + " ·"))]
        if matches:
            headings.append((offset, max(matches, key=lambda t: len(normalized(t["name"])))))
        elif label or bold and re.search(r"酒店|宾馆|旅馆|博物馆|公园|景区|风景区", bold[1]) and not re.match(
                r"酒店(?:地址|详情|价格|名称|房型|信息|设施)", bold[1]):
            headings.append((offset, None))
        offset += len(line)
    if not headings and len(targets) == 1 and normalized(targets[0]["name"]) in normalized(content):
        headings = [(0, targets[0])]
    sections = {}
    for index, (start, target) in enumerate(headings):
        if target is None:
            continue
        end = headings[index + 1][0] if index + 1 < len(headings) else len(content)
        sections.setdefault((target["kind"], target["place_id"]), (target, []))[1].append(content[start:end].strip())
    result = []
    for target, blocks in sections.values():
        text = "\n\n".join(dict.fromkeys(blocks))[:4000]
        raw = unicodedata.normalize("NFKC", re.sub(r"[*`#]", "", text))
        # Conflicting time ranges or unspecified 24h access are not guessed.
        opening_lines = [line for line in raw.splitlines() if re.search(r"(?:开放时间|开放时段|开园时间|营业时间|全天)\s*[:：]", line)]
        opening_text = "\n".join(opening_lines) if opening_lines else raw
        opening_text = re.sub(r"[（(][^()（）]*(?:观光车|小火车|接驳|餐厅|售票窗口)[^()（）]*[)）]", "", opening_text)
        ranges = set(re.findall(r"(?<!\d)([0-2]?\d:[0-5]\d)\s*[-—–~～至]\s*([0-2]?\d:[0-5]\d)", opening_text))
        windows = []
        if len(ranges) == 1 and re.search(r"开放|开园|营业|全天", raw):
            start, end = next(iter(ranges))
            if end == "24:00":
                end = "23:59"
            try:
                windows = [EntryWindow(start=start.zfill(5), end=end.zfill(5))]
            except ValueError:
                pass
        closed = sorted({{"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}[w]
                         for w in re.findall(r"周([一二三四五六日天])(?:闭馆|休馆|关闭)", raw)})
        links = list(dict.fromkeys(url for url in re.findall(r"https?://[^\s<>)]+", text) if safe_url(url)))[:8]
        result.append(TravelReference(**target, content=text, source_urls=links, windows=windows, closed_weekdays=closed))
    return result


def references_for(trip):
    """Old saved text is grouped at display time without rewriting historical JSON."""
    info = trip.get("travel_info") if isinstance(trip, dict) else trip.travel_info
    if not info:
        return []
    from app.schemas import TripResult
    model = TripResult.model_validate(trip) if isinstance(trip, dict) else trip
    info = model.travel_info
    return info.references or parse_references(info.content, targets_for(model))


def matching_reference(references, place_id, on_date=None):
    return next((r for r in references if r.place_id == place_id and (on_date is None or str(on_date) in {str(d) for d in r.dates})), None)


def reference_body(reference):
    lines = reference.content.splitlines()
    if lines and normalized(lines[0]) == normalized(reference.name):
        lines = lines[1:]
    return re.sub(r"(?m)^\s*#{1,6}\s+(.+)$", r"**\1**", "\n".join(lines).strip())
