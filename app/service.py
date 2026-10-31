import asyncio
import re
import time
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.adjustments import suggestions
from app.agents import AutoGenAgents, DemoAgents
from app.audit import audit_result
from app.input_parser import INTEGER, number
from app.names import normalize_cities, resolve_names
from app.planning import allocate_days, materialize
from app.providers import AmapProvider, FixtureProvider, ProviderError
from app.render import render_markdown
from app.schemas import Constraints, DayChoice, Issue, Metrics, Review, Selection, TripRequest, TripResult
from app.storage import VersionConflict
from app.weather import WeatherService

UNSUPPORTED = re.compile(r"(?:自动|帮我|代我|替我).{0,8}(?:付款|支付|下单|预订|取消订单)|(?:保存|记录|输入).{0,8}(?:银行卡|护照|身份证|密码)|(?:银行卡号|身份证号|护照号|账号密码)")
SENSITIVE_NUMBER = re.compile(r"(?<!\d)(?:\d{17}[\dXx]|\d{16,19})(?!\d)")


def blocked_input(text):
    return bool(UNSUPPORTED.search(text) or SENSITIVE_NUMBER.search(text))


class TravelService:
    def __init__(self, settings, repository, provider_factory=None, agents_factory=None):
        self.settings, self.repository = settings, repository
        self.weather_service = WeatherService(settings)
        self.provider_factory = provider_factory or (
            (lambda: FixtureProvider(settings.max_tool_calls)) if settings.mode == "demo" else
            (lambda: AmapProvider(settings.amap_key, settings.max_tool_calls)))
        self.agents_factory = agents_factory or (DemoAgents if settings.mode == "demo" else AutoGenAgents)

    async def create(self, request):
        return await self.execute(request, str(uuid4()), 1, 0)

    async def revise(self, trip_id, revision):
        previous = self.repository.get(trip_id)
        if previous is None:
            return None
        if previous.version != revision.base_version:
            raise VersionConflict("请使用最新行程版本修改")
        if revision.adjustment_id:
            adjustment = next((a for a in previous.adjustments if a.id == revision.adjustment_id), None)
            if not adjustment or revision.changes or revision.instruction:
                raise ValueError("调整方案已失效或与其他修改冲突，请读取最新版本")
            revision.changes = adjustment.changes
        updated = Constraints.model_validate({**previous.constraints.model_dump(mode="json"), **revision.changes})
        request = TripRequest(prompt=revision.instruction, constraints=updated, strategy=previous.strategy)
        return await self.execute(request, trip_id, previous.version + 1, previous.version,
                                  is_revision=True, explicit_changes=revision.changes, previous=previous, revision=revision)

    async def execute(self, request, trip_id, version, expected, is_revision=False, explicit_changes=None,
                      previous=None, revision=None):
        start = time.perf_counter()
        result = TripResult(id=trip_id, version=version, mode=self.settings.mode,
                            strategy=request.strategy, status="failed", constraints=request.constraints)
        provider, agents = None, None
        try:
            if blocked_input(request.prompt + " " + request.constraints.model_dump_json()):
                result.status = "blocked"
                result.issues = [Issue(code="unsupported_action", message="系统只提供规划，不能代为交易或保存身份证、银行卡和密码；请删除敏感信息")]
            else:
                async with asyncio.timeout(self.settings.timeout):
                    agents = self.agents_factory(self.settings, result.trace)
                    try:
                        c = await agents.extract(request.prompt, request.constraints, revision=is_revision)
                        if is_revision and explicit_changes:
                            c = Constraints.model_validate({**c.model_dump(mode="json"), **explicit_changes})
                        submitted = c
                        c = normalize_cities(c)
                    except ValidationError:
                        raise ProviderError("extracted_constraints_invalid", "提取的需求字段存在冲突或无效，请一次检查日期、人数、预算、城市及必去/排除地点") from None
                    result.constraints = c
                    if blocked_input(c.model_dump_json()):
                        raise ProviderError("unsupported_action", "提取内容含越权操作或敏感信息；请求未执行")
                    missing = c.missing()
                    if missing:
                        result.status = "needs_input"
                        result.questions = ["请一次补充：" + "、".join(missing)]
                    elif c.start_date < datetime.now(ZoneInfo("Asia/Shanghai")).date():
                        result.status = "needs_input"
                        result.questions = ["出发日期已过去，请提供今天或未来的日期"]
                    else:
                        provider = self.provider_factory()
                        facts = await agents.research(c, provider, request.strategy)
                        if self.settings.mode == "live" and self.settings.weather_provider == "custom":
                            for city in facts:
                                try:
                                    weather = await self.weather_service.lookup(city)
                                    facts[city].weather = [{"date": str(f.date), "dayweather": f.day_condition,
                                        "daytemp": f.high_c, "nighttemp": f.low_c, "source": weather.source,
                                        "updated_at": weather.updated_at, "is_demo": weather.is_demo}
                                                          for f in weather.forecasts]
                                    facts[city].evidence_ids.append(provider.record("live", weather.source,
                                        f"{city}天气预报；服务更新时间：{weather.updated_at or '未提供'}；仅适用于返回日期"))
                                except ProviderError:
                                    facts[city].weather = []
                        for city, address in c.lodging_addresses.items():
                            if city not in facts:
                                raise ProviderError("lodging_invalid", f"住宿城市{city}不在这次旅行中，请检查城市名称。")
                            point = await provider.lodging(city, address, c)
                            facts[city].lodging = point
                            facts[city].evidence_ids.append(point.evidence_id)
                        raw_cities = [("origin", submitted.origin, c.origin), *[("cities", raw, canonical)
                            for raw, canonical in zip(submitted.cities, c.cities, strict=True)]]
                        for field, raw, canonical in raw_cities:
                            if raw != canonical:
                                ev = provider.record("estimate", "明确城市全称映射及行政区查询", f"{raw} → {canonical}；行政区 {facts[canonical].adcode}")
                                result.name_normalizations.append({"field": field, "submitted": raw, "canonical": canonical,
                                                                   "adcode": facts[canonical].adcode, "evidence_id": ev})
                        schedule = allocate_days(c)
                        for day, ids in c.day_place_ids.items():
                            city = schedule[day - 1]
                            for pid in ids:
                                if not any(p.id == pid for p in facts[city].places):
                                    p = await provider.by_id(city, pid, c, "attraction")
                                    if p:
                                        facts[city].places.append(p)
                                        facts[city].evidence_ids.append(p.evidence_id)
                        c, result.clarifications = await resolve_names(c, facts, provider)
                        result.constraints = c
                        if result.clarifications:
                            result.questions = [q.query + "：" + q.reason for q in result.clarifications]
                            raise ProviderError("place_confirmation", "请先确认地点候选或修改地点名称")
                        for city in c.cities:
                            count = len({b.place_id for b in c.bindings if b.field == "must_visit" and b.city == city})
                            capacity = 3 * schedule.count(city)
                            if count > capacity:
                                raise ProviderError("too_many_activities", f"{city}有{count}处必去地点，当前日数最多安排{capacity}处；请增加天数或明确调整需求")
                        for pid, source in c.indoor_sources.items():
                            if not source.strip():
                                raise ProviderError("indoor_source_missing", "确认室内属性需填写依据说明")
                            for city in c.cities:
                                p = next((p for p in facts[city].places if p.id == pid), None)
                                if p is None:
                                    p = await provider.by_id(city, pid, c, "attraction")
                                    if p:
                                        facts[city].places.append(p)
                                if p:
                                    ev = provider.record("user", "用户确认室内属性", f"{p.name} / {pid}：{source}；未代为核验")
                                    p.indoor, p.indoor_evidence_id = True, ev
                                    facts[city].evidence_ids.extend([p.evidence_id, ev])
                                    break
                        reuse_days, afternoon_day = revision_scope(result, previous, revision, request.prompt)
                        if reuse_days or afternoon_day:
                            provider.evidence.update({ev.id: ev for ev in previous.evidence})
                        feedback = []
                        last_errors = None
                        for attempt in range(self.settings.max_repairs + 1):
                            selection = await agents.select(c, facts, feedback)
                            selection = apply_day_choices(selection, c, previous, reuse_days, afternoon_day)
                            result.trace.append({"step": "selection", "days": {str(d.day): [pid for pid in d.place_ids
                                if d.city in facts and any(p.id == pid for p in facts[d.city].places)] for d in selection.days}})
                            days, budget, checks = await materialize(c, selection, facts, provider, previous,
                                                                   reuse_days, afternoon_day)
                            review = (await agents.review(c, days, checks) if request.strategy == "multi" else
                                      Review(approved=True))
                            result.days, result.budget, result.issues = days, budget, checks
                            result.evidence = list(provider.evidence.values())
                            existing_codes = {i.code for i in checks}
                            result.issues += [i for i in audit_result(result)
                                              if i.code.removeprefix("audit_") not in existing_codes]
                            if not review.approved or review.issues:
                                messages = {i.message for i in result.issues}
                                result.issues += [Issue(code="reviewer_issue", message=m)
                                                 for m in review.issues if m not in messages]
                                if not review.approved and not review.issues:
                                    result.issues.append(Issue(code="reviewer_rejected", message="审核 Agent 未通过方案"))
                            errors = [i for i in result.issues if i.severity == "error"]
                            result.trace.append({"step": "validation", "attempt": attempt + 1,
                                                 "error_codes": [i.code for i in errors], "passed": not errors})
                            if not errors:
                                result.status = "ready"
                                break
                            result.status = "needs_review"
                            feedback = [i.message for i in errors]
                            fingerprint = tuple((i.code, i.message) for i in errors)
                            terminal = {"over_budget", "uncertain", "accessibility_unverified", "day_too_long", "time_window"}
                            codes = {i.code.removeprefix("audit_") for i in errors}
                            time_failure = bool(codes & {"time_window", "day_too_long"})
                            if codes <= terminal or time_failure and codes <= terminal | {"missing_must_visit"} or fingerprint == last_errors:
                                break
                            last_errors = fingerprint
                        result.adjustments = suggestions(result)
        except TimeoutError:
            result.status = "failed"
            result.issues = [Issue(code="request_timeout", message="规划超时，已停止；请减少城市或地点后重试")]
        except ProviderError as exc:
            result.status = ("blocked" if exc.code == "unsupported_action" else
                             "needs_review" if exc.code == "too_many_activities" else
                             "needs_input" if exc.code in {"demo_text_unsupported", "extracted_constraints_invalid", "place_confirmation", "anchor_invalid", "ambiguous_city", "lodging_invalid"}
                             else "failed")
            result.issues = [Issue(code=exc.code, message=str(exc))]
            if result.status == "needs_input":
                result.questions = result.questions or [str(exc)]
        except ValidationError:
            result.status = "failed"
            result.issues = [Issue(code="tool_data_invalid", message="资料或日程数据结构无效；未生成成功方案")]
        except Exception as exc:  # noqa: BLE001 - Convert unexpected errors at the API trust boundary.
            # Do not leak SDK exceptions containing credentials, URLs or user payloads.
            result.status = "failed"
            result.issues = [Issue(code="internal_error", message="内部规划错误，请使用测试定位；未生成成功方案")]
            result.trace.append({"step": "internal_error", "error_type": type(exc).__name__})
        finally:
            usage = agents.metrics() if agents else {}
            if provider:
                result.evidence = list(provider.evidence.values())
                result.trace.extend(provider.events)
            for resource in (provider, agents):
                if resource:
                    try:
                        await asyncio.wait_for(resource.close(), timeout=3)
                    except Exception as exc:  # noqa: BLE001
                        result.trace.append({"step": "cleanup", "error_type": type(exc).__name__})
            result.metrics = Metrics(elapsed_ms=round((time.perf_counter() - start) * 1000, 2),
                                     tool_calls=provider.calls if provider else 0, **usage)
            if self.settings.input_price is not None and self.settings.output_price is not None:
                result.metrics.estimated_model_cost_cny = round((result.metrics.input_tokens * self.settings.input_price +
                                                                 result.metrics.output_tokens * self.settings.output_price) / 1_000_000, 6)
            # Never persist submitted credentials through blocked structured constraints.
            if result.status == "blocked":
                result.constraints = Constraints()
            if result.status == "needs_review" and not result.adjustments:
                result.adjustments = suggestions(result)
            result.markdown = render_markdown(result)
        self.repository.save(result, expected)
        return result

def revision_scope(result, previous, revision, prompt):
    """Preserve unaffected dates only when the dependency boundary is demonstrably local."""
    if previous is None or not previous.days or not previous.budget:
        return set(), None
    target = revision.target_day if revision else None
    period = revision.target_period if revision else "day"
    explicit = re.search(rf"第({INTEGER})天", prompt)
    if explicit:
        target = number(explicit[1])
        if "只修改" in prompt and "下午" in prompt:
            period = "afternoon"
    before, after = previous.constraints.model_dump(mode="json"), result.constraints.model_dump(mode="json")
    changed = {key for key in after if before[key] != after[key]} - {"bindings"}
    local = {"day_rules", "day_place_ids", "opening_rules", "indoor_sources"}
    targets = {target} if target else set()
    for field in changed & {"day_rules", "day_place_ids"}:
        if field == "day_rules":
            old = {r["day"]: r for r in before[field]}
            new = {r["day"]: r for r in after[field]}
        else:
            old, new = before[field], after[field]
        targets.update(int(day) for day in set(old) | set(new) if old.get(day) != new.get(day))
    if "opening_rules" in changed:
        targets.update((r.on_date - result.constraints.start_date).days + 1 for r in result.constraints.opening_rules)
    targets = {day for day in targets if 1 <= day <= result.constraints.days}
    if targets and changed <= local and all(i.day is not None for i in previous.budget.items if i.category not in {"住宿", "餐饮"}):
        reuse = set(range(1, result.constraints.days + 1)) - targets
        result.revision_scope = {"requested_days": sorted(targets), "preserved_days": sorted(reuse),
                                 "period": period, "expanded": False,
                                 "explanation": "未影响日期保留活动与时间；受影响日期重新算路，重新核验全团预算。"}
        return reuse, target if period == "afternoon" else None
    if target or changed:
        result.revision_scope = {"requested_days": sorted(targets), "preserved_days": [], "expanded": True,
            "explanation": "城市、日期、人数、全团预算、价格或接驳设置发生全局变化，或旧版缺少分日费用，需扩大重算范围。"}
    return set(), None


def apply_day_choices(selection, c, previous, reuse, afternoon_day):
    days = []
    for d in selection.days:
        ids = c.day_place_ids.get(d.day, d.place_ids)
        if previous and d.day in reuse:
            old = previous.days[d.day - 1]
            d = DayChoice(day=old.day, city=old.city, place_ids=[e.place_id for e in old.events if e.kind == "visit"], reason=old.reason)
        elif previous and d.day == afternoon_day:
            morning = [e.place_id for e in previous.days[d.day - 1].events if e.kind == "visit" and e.end <= "13:00"]
            ids = list(dict.fromkeys([*morning, *ids]))[:3]
            d = d.model_copy(update={"place_ids": ids})
        else:
            if previous and reuse and d.day not in c.day_place_ids:
                old_ids = [e.place_id for e in previous.days[d.day - 1].events if e.kind == "visit"]
                mandatory = {b.place_id for b in c.bindings if b.field == "must_visit"}
                old_ids.sort(key=lambda pid: pid not in mandatory)
                ids = old_ids[:max(len(d.place_ids), len([pid for pid in old_ids if pid in mandatory]))]
            d = d.model_copy(update={"place_ids": ids})
        days.append(d)
    return Selection(days=days)

