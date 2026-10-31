"""Date-bound admission checks. Web content never supplies executable instructions."""
import hashlib
import re
import time as monotonic_time
from datetime import UTC, datetime, time, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx

from app.schemas import EntryWindow, Issue, Selection, VisitCheck

GD_GUIDE = "https://www.gdmuseum.org.cn/col666/"
GD_HOME = "https://www.gdmuseum.org.cn/"
PALACE_GUIDE = "https://www.dpm.org.cn/Visit.html"


def safe_url(value, local=False):
    parsed = urlparse(value)
    return bool(parsed.hostname and not parsed.username and not parsed.password and not parsed.fragment
        and (parsed.scheme == "https" or local and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
        and not ({k.lower() for k in parse_qs(parsed.query)} & {"key", "api_key", "token", "access_token", "secret"}))


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def confirmation(c, check):
    return next((r for r in c.reservations if r.place_id == check.place_id and r.on_date == check.on_date
                 and r.people == c.people), None)


def admission_issues(check, c):
    """Pure checks also used before any route is requested."""
    prefix = f"{check.on_date} {check.place_name}："
    result = []
    severity = "error" if check.enforced else "warning"
    if check.availability == "sold_out" and not confirmation(c, check):
        result.append(Issue(code="reservation_required", message=prefix + "接口显示该日已售罄或约满，请更换日期或景点。", severity=severity))
    if check.opening_status == "closed":
        result.append(Issue(code="opening_conflict", message=prefix + "该日闭馆或按常规规则不开放，请调整日期或景点。", severity=severity))
    elif check.opening_status == "unknown":
        result.append(Issue(code="admission_unverified", message=prefix + "开放时间未核实，暂不能确认可以入场。", severity=severity))
    if check.reservation_required is None:
        result.append(Issue(code="admission_unverified", message=prefix + "是否需要预约尚未核实。", severity=severity))
    elif check.reservation_required and not confirmation(c, check) and check.availability != "sold_out":
        text = "需要预约；还没有你对本次日期、人数和入场时段的确认。"
        if check.availability == "sold_out":
            text = "接口显示该日已约满，且没有已预约确认，请更换日期或景点。"
        elif check.booking_days_ahead is not None and check.on_date > datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=check.booking_days_ahead):
            text += "该日期尚未进入放票期，请到放票日再确认。"
        elif check.same_day_booking is False and check.on_date <= datetime.now(ZoneInfo("Asia/Shanghai")).date():
            text += "官方不提供当日购票，不能指望到现场补票。"
        result.append(Issue(code="reservation_required", message=prefix + text, severity=severity))
    if check.ticket_price is None:
        result.append(Issue(code="ticket_unverified", message=prefix + "票价未核实，当前仍按门票预算计算。", severity=severity))
    return result


def entry_windows(check, c):
    windows = list(check.windows)
    booked = confirmation(c, check)
    # A booking slot restricts entry, not the entire length of a museum visit.
    return windows, booked.entry_window if booked else None


class AdmissionService:
    def __init__(self, settings, client=None):
        self.settings, self.client = settings, client
        self.pages = {}

    async def page(self, url, provider, budget):
        cached = self.pages.get(url)
        if cached and monotonic_time.monotonic() - cached[0] < 300:
            return cached[1:]
        if budget[0] >= self.settings.max_visit_calls:
            raise ValueError("本次入场资料查询达到上限")
        budget[0] += 1
        provider.tick()
        client = self.client or httpx.AsyncClient(timeout=8, follow_redirects=False)
        try:
            response = await client.get(url)
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise ValueError("官方页面过大")
            parser = PageText()
            parser.feed(response.text)
            text = re.sub(r"\s+", "", "".join(parser.parts)).translate(str.maketrans("：—–～", ":---"))
            stamp = datetime.now(UTC)
            digest = hashlib.sha256(response.content).hexdigest()
            self.pages[url] = (monotonic_time.monotonic(), text, stamp, digest)
            return text, stamp, digest
        finally:
            if self.client is None:
                await client.aclose()

    async def official(self, point, on_date, provider, budget):
        check = VisitCheck(place_id=point.id, place_name=point.name, city=point.city, on_date=on_date)
        normalized = point.name.replace("（", "(").replace("）", ")").replace(" ", "")
        if (point.city == "广州" and "珠江东路" in point.address
                and normalized in {"广东省博物馆", "广东省博物馆(新馆)", "广东省博物馆新馆"}):
            guide, stamp, guide_hash = await self.page(GD_GUIDE, provider, budget)
            home, home_stamp, home_hash = await self.page(GD_HOME, provider, budget)
            if not ("9:00-17:00" in guide and "16:00停止入场" in guide and "周一闭馆" in guide
                    and "实名制预约" in guide and "免费向公众开放" in home):
                raise ValueError("官网规则已变化或不完整，需要重新核对")
            check.windows = [EntryWindow(start=time(9), end=time(17))]
            check.last_entry, check.reservation_required, check.ticket_price = time(16), True, 0
            check.booking_url = GD_GUIDE
            check.ticket_scope = "常设基本陈列免费；收费特展、讲解及其他项目未包含"
            check.source_urls = [GD_GUIDE, GD_HOME]
            check.checked_at = min(stamp, home_stamp)
            hashes = [guide_hash, home_hash]
        elif point.city == "北京" and "景山前街" in point.address and normalized == "故宫博物院":
            guide, stamp, digest = await self.page(PALACE_GUIDE, provider, budget)
            markers = ("8:30", "17:00", "16:30", "15:30", "周一", "闭馆", "60元/人", "40元/人", "预约")
            if not all(word in guide for word in markers):
                raise ValueError("官网规则已变化或不完整，需要重新核对")
            high_season = 4 <= on_date.month <= 10
            check.windows = [EntryWindow(start=time(8, 30), end=time(17) if high_season else time(16, 30))]
            check.last_entry = time(16) if high_season else time(15, 30)
            check.reservation_required, check.same_day_booking, check.booking_days_ahead = True, False, 7
            check.ticket_price = 60 if high_season else 40
            check.source_urls, check.booking_url = [PALACE_GUIDE], PALACE_GUIDE
            check.checked_at, hashes = stamp, [digest]
        else:
            check.note = "尚无该具体场馆的官方规则适配；可自行核对后填写，或接入票务API。不会套用其他分馆信息。"
            return check
        check.authority = "official"
        check.opening_status = "closed" if on_date.weekday() == 0 else "open"
        check.expires_at = check.checked_at + timedelta(hours=24)
        check.note = "已核对官网常规规则；周一及节假日例外、临时闭馆和临时特展仍需临近出发刷新公告。预约余量未知，不代表已约到。"
        check.evidence_id = provider.record("live", "景点官网入场规则", f"{point.city} {point.name}常规入场规则；页面SHA256：{' / '.join(hashes)}", check.source_urls[0])
        return check

    async def custom(self, point, on_date, provider, budget):
        url = self.settings.visit_api_url
        if not safe_url(url, local=True) or urlparse(url).query:
            raise ValueError("请配置有效入场核验API地址，密钥应单独填写")
        if budget[0] >= self.settings.max_visit_calls:
            raise ValueError("本次入场资料查询达到上限")
        budget[0] += 1
        provider.tick()
        client = self.client or httpx.AsyncClient(timeout=8, follow_redirects=False)
        try:
            headers = {"Authorization": "Bearer " + self.settings.visit_api_key} if self.settings.visit_api_key else {}
            response = await client.get(url, params={"place_id": point.id, "name": point.name,
                "city": point.city, "date": str(on_date)}, headers=headers)
            response.raise_for_status()
            check = VisitCheck.model_validate(response.json())
            if (check.place_id, check.place_name, check.city, check.on_date) != (point.id, point.name, point.city, on_date):
                raise ValueError("接口返回的场馆或日期不一致")
            check.authority, check.enforced, check.evidence_id = "provider", True, ""
            if not check.source_urls or not all(safe_url(link) for link in check.source_urls):
                raise ValueError("入场资料缺少有效来源链接")
            if check.booking_url and not safe_url(check.booking_url):
                raise ValueError("预约入口无效")
            check.evidence_id = provider.record("live", "票务入场资料接口", f"{point.name} / {on_date}；按具体场馆和日期查询，接口声明不代表已完成预约", check.source_urls[0])
            return check
        finally:
            if self.client is None:
                await client.aclose()

    async def lookup(self, point, on_date, c, provider, budget):
        from app.providers import FixtureProvider
        check = VisitCheck(place_id=point.id, place_name=point.name, city=point.city, on_date=on_date)
        if isinstance(provider, FixtureProvider) and self.settings.visit_provider == "official":
            check.authority, check.enforced = "fixture", False
            check.note = "离线地点仅演示，开放、预约和票价未作真实核验。"
        else:
            try:
                check = await (self.custom(point, on_date, provider, budget) if self.settings.visit_provider == "custom"
                               else self.official(point, on_date, provider, budget))
                now = datetime.now(UTC)
                if check.authority != "unknown" and (not check.checked_at or not check.expires_at
                        or check.checked_at > now + timedelta(minutes=5) or check.checked_at < now - timedelta(hours=24)
                        or check.expires_at < now or check.expires_at > check.checked_at + timedelta(hours=24)):
                    check = VisitCheck(place_id=point.id, place_name=point.name, city=point.city, on_date=on_date,
                                       note="核验资料已过期或缺少有效时间，不能继续当作已确认信息。")
                if (check.availability != "unknown" and (not check.availability_checked_at
                    or check.availability_checked_at < now - timedelta(minutes=10)
                    or check.availability_checked_at > now + timedelta(minutes=5))):
                    check.availability = "unknown"
            except (httpx.HTTPError, ValueError, TypeError, KeyError):
                check.note = "入场资料查询失败或返回信息不完整，请在官方渠道核对后补充。"
        rule = next((r for r in c.opening_rules if r.place_id == point.id and r.on_date == on_date), None)
        if rule:
            # A manual declaration can tighten official rules, never reopen a known closure.
            if rule.closed:
                check.opening_status, check.windows = "closed", []
            elif check.opening_status == "unknown":
                check.opening_status = "open"
                check.windows = [EntryWindow(start=rule.start, end=rule.end)]
            elif check.opening_status == "open":
                check.windows = [EntryWindow(start=max(w.start, rule.start), end=min(w.end, rule.end))
                                 for w in check.windows if max(w.start, rule.start) < min(w.end, rule.end)]
                if not check.windows:
                    check.opening_status = "closed"
            if rule.last_entry:
                check.last_entry = min(check.last_entry, rule.last_entry) if check.last_entry else rule.last_entry
            fresh = rule.checked_at and datetime.now(UTC) - timedelta(hours=24) <= rule.checked_at <= datetime.now(UTC) + timedelta(minutes=5)
            if fresh and (rule.reservation_required is True or check.reservation_required is None):
                check.reservation_required = rule.reservation_required
            if fresh and rule.ticket_price is not None and check.ticket_price is None:
                check.ticket_price = rule.ticket_price
            ev = provider.record("user", "用户自行核对入场信息", rule.source, rule.source_url if safe_url(rule.source_url) else "")
            # Manual overrides are explicitly user evidence; original links stay visible in the note.
            check.evidence_id, check.authority = ev, "user"
            if fresh:
                check.checked_at = min(check.checked_at, rule.checked_at) if check.checked_at else rule.checked_at
                check.expires_at = check.checked_at + timedelta(hours=24)
            check.source_urls = list(dict.fromkeys(check.source_urls + ([rule.source_url] if safe_url(rule.source_url) else [])))
            check.note += " 有你自行核对的资料参与，未代为验证预约凭证。"
        return check

    async def arrange(self, c, selection, facts, provider, cache=None, budget=None):
        checks, notes = {} if cache is None else cache, []
        budget = [0] if budget is None else budget
        days = [d.model_copy(deep=True) for d in selection.days]
        pools = {city: {p.id: p for p in f.places} for city, f in facts.items()}

        async def lookup(day, pid):
            key = (day.day, pid)
            if key not in checks:
                point = pools.get(day.city, {}).get(pid)
                if point:
                    checks[key] = await self.lookup(point, c.start_date + timedelta(days=day.day - 1), c, provider, budget)
            return checks.get(key)

        must = {b.place_id for b in c.bindings if b.field == "must_visit"}
        used = {pid for d in days for pid in d.place_ids}
        for day in days:
            for pid in list(day.place_ids):
                check = await lookup(day, pid)
                if not check or check.opening_status != "closed" and not (check.availability == "sold_out" and not confirmation(c, check)):
                    continue
                pinned = pid in c.day_place_ids.get(day.day, [])
                if pid in must and not pinned:
                    for target in days:
                        if target.day == day.day or target.city != day.city or len(target.place_ids) >= 3:
                            continue
                        candidate = await lookup(target, pid)
                        if candidate and candidate.opening_status == "open" and candidate.availability != "sold_out":
                            day.place_ids.remove(pid)
                            target.place_ids.append(pid)
                            notes.append(Issue(code="admission_rescheduled", severity="warning",
                                message=f"{check.place_name}原定第{day.day}天不宜到访，已移到第{target.day}天，并重新算路。"))
                            break
                elif pid not in must and not pinned:
                    day.place_ids.remove(pid)
                    replacement = None
                    for point in list(pools[day.city].values())[:20]:
                        if point.id in used or point.name in c.excluded or any(b.field == "excluded" and b.place_id == point.id for b in c.bindings):
                            continue
                        candidate = await lookup(day, point.id)
                        if candidate and candidate.opening_status == "open" and not admission_issues(candidate, c):
                            replacement = point
                            day.place_ids.append(point.id)
                            used.add(point.id)
                            break
                    notes.append(Issue(code="admission_replaced", severity="warning", message=f"第{day.day}天的{check.place_name}不宜到访，"
                        + (f"已替换为{replacement.name}并重新算路。" if replacement else "已移除这处可选活动，留出自由时间；没有擅自替换必去景点。")))
        for day in days:
            for pid in day.place_ids:
                await lookup(day, pid)
        return Selection(days=days), list(checks.values()), notes
