"""Confirm consultation branches using complete addresses and a unique AMap POI."""
import re
import unicodedata

from app.schemas import TravelReference
from app.travel_references import normalized


def address_key(value):
    text = unicodedata.normalize("NFKC", value).replace("中国", "").strip()
    # Province/city/district prefixes may be omitted by AMap. Keep street,
    # building, floor and room details so neighbouring businesses cannot merge.
    for _ in range(5):
        shortened = re.sub(r"^[\u4e00-\u9fff]{2,9}(?:自治区|自治州|省|市|区|县|镇)", "", text)
        if shortened == text:
            break
        text = shortened
    key = normalized(text)
    return key if len(key) >= 6 and re.search(r"\d+号", key) else None


def brand(name, city):
    value = unicodedata.normalize("NFKC", name).split("(", 1)[0].strip()
    value = value.removeprefix(city)
    return normalized(value)


def hotel_sections(content):
    headings, offset = [], 0
    for line in content.splitlines(keepends=True):
        match = re.match(r"^\s*#{1,3}\s+(.+?)\s*$", line)
        bold = re.match(r"^\s*\[?\*\*(.+?)\*\*", line)
        name = (match or bold)[1] if match or bold else ""
        hotel = bool(re.search(r"酒店|宾馆|旅馆|旅社|民宿", name)) and not re.match(r"酒店(?:地址|详情|价格|名称|房型|信息|设施)", name)
        if match or hotel:
            headings.append((offset, name if hotel else None))
        offset += len(line)
    sections = [(name, content[start:headings[n + 1][0] if n + 1 < len(headings) else len(content)])
                for n, (start, name) in enumerate(headings) if name]
    if not sections:
        names = {name for name in re.findall(r"\*\*([^*\n]+)\*\*", content)
                 if re.search(r"酒店|宾馆|旅馆|旅社|民宿", name) and not re.match(r"酒店(?:地址|详情|价格|名称|房型|信息|设施)", name)}
        if len(names) == 1:
            sections = [(next(iter(names)), content)]
    return sections


async def confirm_hotels(report, targets, provider, constraints):
    from app.providers import ProviderError
    hotels = [t for t in targets if t["kind"] == "hotel"]
    confirmed = []
    for supplier_name, section in hotel_sections(report.content):
        raw = re.sub(r"[*`#]", "", section)
        addresses = re.findall(r"(?:酒店地址|详细地址|完整地址|地址)\s*[:：]\s*([^\n；;。]+)", raw)
        addresses += re.findall(r"位于([^\n，,；;。]+号[^\n，,；;。]*)", raw)
        keys = {key for a in addresses if (key := address_key(a))}
        eligible = [t for t in hotels if address_key(t.get("address", "")) in keys
                    and brand(t["name"], t["city"]) == brand(supplier_name, t["city"])]
        if len({t["place_id"] for t in eligible}) != 1:
            continue
        target = eligible[0]
        try:
            options = await provider.search(target["city"], supplier_name[:80], constraints, "hotel")
            matches = {p.id for p in options if address_key(p.address) == address_key(target["address"])
                       and brand(p.name, p.city) == brand(target["name"], target["city"])}
            if matches != {target["place_id"]}:
                continue
            ref = TravelReference(**target, content=section[:4000], identity_confirmed=True,
                                  identity_note="已按高德完整地址确认同一家分店")
            confirmed.append(ref)
            provider.record("live", "高德酒店分店确认", f"{target['name']}：完整地址与唯一地点编号匹配；未核实价格及房态")
        except (ProviderError, ValueError, TypeError):
            continue
    # Unknown/mismatched branches never inherit a different hotel's prices.
    report.references = [r for r in report.references if r.kind != "hotel"] + list({r.place_id: r for r in confirmed}.values())
