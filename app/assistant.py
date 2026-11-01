"""A conversational front door to the existing planner, with persistent idempotent turns."""
import asyncio
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, ValidationError, create_model, field_validator

from app.agents import AutoGenAgents
from app.day_input import day_references
from app.input_parser import demo_patch, merge_day_rules
from app.presentation import issue_message, plain_text
from app.providers import ProviderError
from app.schemas import ConstraintPatch, Model, ReservationConfirmation, RevisionRequest
from app.service import blocked_input
from app.storage import VersionConflict


class AssistantRequest(Model):
    base_version: int = Field(ge=1)
    request_id: str = Field(min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    message: str = Field(min_length=1, max_length=3000)

    @field_validator("message")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("请先输入你的想法")
        return value.strip()


class AssistantDecision(Model):
    action: Literal["answer", "revise", "refresh", "choose", "booking", "restore"] = "answer"
    reply: str = Field(default="", max_length=1500)
    changes: dict = Field(default_factory=dict)
    choices: dict[str, str] = Field(default_factory=dict)
    target_day: int | None = Field(default=None, ge=1, le=30)
    target_period: Literal["day", "afternoon"] = "day"
    version: int | None = Field(default=None, ge=1)
    booking: dict = Field(default_factory=dict)


PREFERENCE_FIELDS = {"origin", "cities", "start_date", "days", "people", "budget", "interests", "must_visit", "excluded",
    "transport", "return_to_origin", "accessibility_required", "rooms", "hotel_per_room_night", "food_per_person_day",
    "ticket_per_person_visit", "daily_start", "daily_end", "visit_minutes", "day_rules", "lodging_addresses"}
PROTECTED_CUES = {"origin": r"出发|从", "cities": r"目的地|城市|去|换成|改成|改为", "start_date": r"日期|出发|开始|明天|后天|今天|\d{4}[-年/.]",
    "days": r"(?:玩|共|改成|改为|增加|减少|延长|缩短|安排|行程)[^。]{0,10}(?:天|日)",
    "people": r"人数|个人|位|人同行|人出行|人去|人玩|带.*(?:孩子|父母|老人)|\d+人",
    "budget": r"预算|费用|花费|元|块|钱", "must_visit": r"必去|一定去|想去|不去|别去|取消|排除",
    "excluded": r"不去|别去|排除|避开|不要去|取消"}
CONDITION_TOPICS = {"origin": "出发地", "cities": "目的地|城市", "start_date": "日期|出发日期|开始日期",
    "days": "天数|旅行天数|行程天数", "people": "人数|出行人数|同行人数", "budget": "预算|总费用|总花费",
    "must_visit": "必去(?:景点|地点)?", "excluded": "排除(?:景点|地点)?", "transport": "交通方式|出行方式",
    "return_to_origin": "返程|往返", "accessibility_required": "无障碍", "rooms": "房间数",
    "hotel_per_room_night": "住宿预算|酒店预算|房费", "food_per_person_day": "餐饮预算|餐费",
    "ticket_per_person_visit": "门票预算|门票费用", "daily_start": "出发时间|活动开始时间",
    "daily_end": "结束时间|活动结束时间", "visit_minutes": "游览时长", "day_rules": "分日安排",
    "lodging_addresses": "住宿地址|酒店地址|住处"}
KEEP_AFTER = r"(?:不变|照旧|保持原样|沿用原(?:有|来|先)(?:安排|设置)?|维持原(?:有|来|先)|(?:不要|别|不用|不能|不再|不)(?:更改|修改|调整|改变|变动|改|换|动))"
KEEP_BEFORE = r"(?:(?:不要|别|不用|不能|不再|不)(?:更改|修改|调整|改变|变动|改|换|动)|保持|保留|沿用|维持)"
AssistantPreferences = create_model("AssistantPreferences", __base__=Model,
    **{name: (field.annotation, deepcopy(field)) for name, field in ConstraintPatch.model_fields.items() if name in PREFERENCE_FIELDS},
    pace=(Literal["standard", "relaxed"] | None, None), walking=(Literal["less", "normal"] | None, None))


class AssistantWireDecision(AssistantDecision):
    changes: AssistantPreferences = Field(default_factory=AssistantPreferences)


def normalize_preferences(message, trip, decision):
    changes = dict(decision.changes)
    pace, walking = changes.pop("pace", None), changes.pop("walking", None)
    less_walking = bool(re.search(r"少走路|少步行|减少步行|不想走太多|别走太多", message))
    reset = bool(re.search(r"恢复正常|正常安排|恢复标准|不用轻松|不要太轻松", message))
    relaxed = (less_walking or bool(re.search(r"轻松|悠闲|休闲|慢一点", message))) and not reset
    if pace is not None and not ((pace == "relaxed" and relaxed) or (pace == "standard" and reset)):
        raise ValueError("请告诉我希望哪一天轻松些，我来调整。")
    if walking is not None and not ((walking == "less" and less_walking) or (walking == "normal" and reset)):
        raise ValueError("请告诉我希望哪一天减少步行，我来调整。")
    if relaxed or pace is not None or walking is not None:
        if trip.constraints.days is None:
            raise ValueError("请先告诉我旅行天数，我再安排每天的游玩节奏。")
        references = day_references(message, trip.constraints.days)
        targets = references or ([decision.target_day] if decision.target_day else list(range(1, trip.constraints.days + 1)))
        rules = {r["day"]: r for r in changes.get("day_rules", [])}
        for day in targets:
            rules[day] = {**rules.get(day, {}), "day": day, "pace": "relaxed" if relaxed else pace or "standard"}
            if less_walking or walking is not None:
                rules[day]["reduce_walking"] = walking != "normal"
        changes["day_rules"] = list(rules.values())
    return changes


def revision_reply(before, after):
    if after.status == "failed" or after.questions or not after.days:
        return planning_reply(after, changed=True)
    summaries = []
    for old, new in zip(before.days, after.days, strict=False):
        if old == new:
            continue
        old_visits = [e.title for e in old.events if e.kind == "visit"]
        visits = [e.title for e in new.events if e.kind == "visit"]
        old_walk = sum(s.distance or 0 for e in old.events for s in e.route_steps if s.kind == "walk")
        new_walk = sum(s.distance or 0 for e in new.events for s in e.route_steps if s.kind == "walk")
        details = f"第{new.day}天安排" + ("、".join(visits) if visits else "自由活动和休息")
        if len(old_visits) != len(visits):
            details += f"，景点从{len(old_visits)}处调整为{len(visits)}处"
        if old_walk and new_walk:
            details += f"，接驳步行约从{round(old_walk)}米调整为{round(new_walk)}米"
        elif old.travel_minutes != new.travel_minutes:
            details += f"，交通从{old.travel_minutes}分钟调整为{new.travel_minutes}分钟"
        summaries.append(details)
    reply = "已根据你的意见重新安排。" + "；".join(summaries[:3]) + "。" if summaries else planning_reply(after, changed=True)
    preserved = after.revision_scope.get("preserved_days", [])
    if preserved:
        reply += "其他日期保留原安排。"
    if after.constraints.budget == before.constraints.budget:
        reply += f"全团预算仍为{after.constraints.budget:g}元。"
    if any(i.severity == "error" for i in after.issues):
        reply += "部分时间或入场条件仍需确认，具体提示已放在上方行程中。"
    return reply


def protected_changes(message, changes):
    """Explicit preservation outranks a model patch, including restore actions."""
    clauses = [re.sub(r"\s+", "", s) for s in re.split(r"[，,。；;！!？?\n]|但是|但|不过", message) if s.strip()]
    locked = set()
    for field, topic in CONDITION_TOPICS.items():
        if any(re.search(rf"(?:{topic})(?:仍然|仍|还是|都|全部|继续|先|也|要|请|保持|维持|的数值|金额|额度|条件|设置|为原来){{0,3}}(?:(?:在|为|是)?\d+(?:\.\d+)?(?:元|块|人|天|间)?)?{KEEP_AFTER}", s)
               or re.search(rf"{KEEP_BEFORE}(?:原有|原来|原定|现在|当前|之前|现有|我的|原先|的)?(?:{topic})", s) for s in clauses):
            locked.add(field)
    if any(re.search(r"(?:其他|其余)(?:的)?(?:条件|信息|设置|要求|安排)?(?:都|全部|一律)?(?:不变|照旧|保持原样)", s) for s in clauses):
        mentioned = {field for field, topic in CONDITION_TOPICS.items()
                     if any("其他" not in s and "其余" not in s and re.search(topic, s) for s in clauses)}
        if any(re.search(r"第[一二三四五六七八九十两\d]+天|轻松|下午|上午|节奏", s) for s in clauses):
            mentioned.add("day_rules")
        locked |= PREFERENCE_FIELDS - mentioned
    return {k: v for k, v in changes.items() if k not in locked}


def planning_reply(trip, changed=False):
    if trip.status == "failed":
        cause = issue_message(trip.issues[0]) if trip.issues else "查询暂时没有完成"
        return "这次没有完成新的安排：" + cause + "。之前的行程记录仍然保留。"
    if trip.questions:
        if trip.clarifications:
            choices = [q.query + "：" + "；".join(p.name + ("（" + p.address + "）" if p.address else "") for p in q.options[:3])
                       for q in trip.clarifications if q.options]
            if choices:
                return "我需要确认你指的是哪一处：" + "。".join(choices) + "。告诉我名称或附近位置就好。"
        return "我还需要确认一下：" + "；".join(plain_text(q) for q in trip.questions[:3]) + "。直接告诉我即可，我来继续安排。"
    start = "已根据你的意见重新安排。" if changed else "我已经整理好这次旅行的路线和住宿建议。"
    errors = [i for i in trip.issues if i.severity == "error"]
    if errors:
        message = issue_message(errors[0])
        return start + "目前还有一项需要确认：" + message + "。你可以告诉我偏好或新的条件，我会继续协调。"
    return start + "每日安排已更新在上方。想少走路、换住处、避开某个景点，直接告诉我就好。"


def safe_patch(message, trip, decision):
    changes = normalize_preferences(message, trip, decision)
    if set(changes) - PREFERENCE_FIELDS:
        raise ValueError("我需要先核对相关地点或预约信息，再进行调整。请直接说明想改变的条件。")
    changes = protected_changes(message, changes)
    raw = trip.constraints.model_dump(mode="json")
    for field, cue in PROTECTED_CUES.items():
        if field in changes and changes[field] != raw[field] and not re.search(cue, message):
            raise ValueError("为了保持你的原定条件，我还需要确认一下具体想改哪一项。")
    ConstraintPatch.model_validate(changes)
    if "day_rules" in changes:
        changes["day_rules"] = merge_day_rules(raw, changes)["day_rules"]
    if ("days" in changes and changes["days"] != raw["days"]) or ("cities" in changes and changes["cities"] != raw["cities"]):
        changes["day_place_ids"] = {}
        changes["day_rules"] = [r for r in changes.get("day_rules", raw["day_rules"]) if r["day"] <= changes.get("days", raw["days"])]
        if "cities" in changes:
            changes["place_choices"] = {}
            changes["anchors"] = {city: anchor for city, anchor in raw["anchors"].items() if city in changes["cities"]}
    return changes


def booking_changes(message, trip, decision):
    value = decision.booking
    if not re.search(r"已经?预约|已完成预约|预约成功|已约好", message):
        raise ValueError("请在实际预约完成后告诉我景点、日期、入场时段和人数，我再记入行程。")
    name = value.get("place_name", "")
    dates = [date(int(y), int(m), int(d)) for y, m, d in re.findall(r"(\d{4})[-年/.](\d{1,2})[-月/.](\d{1,2})", message)]
    dates += [trip.constraints.start_date + timedelta(days=n - 1) for n in day_references(message, trip.constraints.days)]
    times = [f"{int(h):02}:{m or '00'}" for h, m in re.findall(r"(?<!\d)(\d{1,2})(?:[:：](\d{2})|点)", message)]
    people = re.findall(r"(\d+)\s*人", message)
    if not name or name not in message or str(value.get("on_date")) not in {str(d) for d in dates} or len(times) < 2 or not people:
        raise ValueError("请补充已预约的景点、完整日期（或第几天）、入场时段和人数，我来更新安排。")
    check = next((v for v in trip.visit_checks if v.place_name == name and str(v.on_date) == value["on_date"]), None)
    if not check or value.get("start") != times[0] or value.get("end") != times[1] or value.get("people") != int(people[0]):
        raise ValueError("预约信息与当前行程尚未对应，请再确认景点、日期、时段和人数。")
    reservation = ReservationConfirmation(place_id=check.place_id, on_date=check.on_date, people=value["people"],
        entry_window={"start": value["start"], "end": value["end"]})
    if reservation.people != trip.constraints.people:
        raise ValueError("预约人数与出行人数不同，请确认这次预约覆盖了哪些同行人。")
    rows = [r.model_dump(mode="json") for r in trip.constraints.reservations
            if (r.place_id, r.on_date) != (reservation.place_id, reservation.on_date)]
    return {"reservations": rows + [reservation.model_dump(mode="json")]}


class TourAssistant:
    def __init__(self, service, decider=None):
        self.service, self.decider = service, decider

    async def decide(self, message, trip, history):
        if self.decider:
            return await self.decider(message, trip, history), 0
        if self.service.settings.mode == "demo":
            if trip.clarifications:
                selections = {}
                for q in trip.clarifications:
                    options = [p for p in q.options if p.name in message]
                    ordinal = re.search(r"第([一二三123])个", message)
                    if ordinal and len(trip.clarifications) == 1:
                        index = {"一": 0, "二": 1, "三": 2, "1": 0, "2": 1, "3": 2}[ordinal[1]]
                        options = q.options[index:index + 1]
                    if len(options) == 1:
                        selections[q.key] = options[0].id
                if selections:
                    return AssistantDecision(action="choose", choices=selections), 0
            if re.search(r"^(你好|谢谢|您好|嗨)[！!。，,\s]*$|为什么|多少钱|需要预约吗|怎么走|天气.*[？?]", message):
                reply = "我在的。你可以告诉我想去哪、想怎么游玩，我会结合当前行程来安排。"
                if "预算" in message or "钱" in message:
                    reply = f"你这次的全团预算是{trip.constraints.budget:g}元。当前费用是估算，酒店和门票的实际价格还需要对应日期确认。"
                elif "预约" in message:
                    reply = "需要预约的景点会在每日安排里标明。实际预约完成后，告诉我景点、日期、时段和人数，我来核对安排。"
                return AssistantDecision(reply=reply), 0
            if re.search(r"重新核验|再查.*开放|刷新.*入场", message):
                return AssistantDecision(action="refresh"), 0
            try:
                patch = demo_patch(message)
                days = day_references(message, trip.constraints.days)
                return AssistantDecision(action="revise", changes=patch, target_day=days[0] if len(set(days)) == 1 else None,
                    target_period="afternoon" if "只修改" in message and "下午" in message else "day"), 0
            except ProviderError:
                return AssistantDecision(reply="我还没理解具体想怎样调整。你可以说“第二天轻松一点”或“预算改成3000元”，我来重新安排。"), 0
        agents = AutoGenAgents(self.service.settings, [])
        try:
            payload = {"request": message, "history": history[-8:], "conditions": trip.constraints.model_dump(mode="json"),
                "trip_status": trip.status, "version": trip.version,
                "days": [{"day": d.day, "city": d.city, "visits": [{"name": e.title, "time": e.start} for e in d.events if e.kind == "visit"],
                    "hotels": [{"name": p.name, "address": p.address} for p in d.hotel_options]} for d in trip.days],
                "questions": trip.questions, "clarifications": [c.model_dump(mode="json") for c in trip.clarifications],
                "admission": [{"place_name": v.place_name, "on_date": str(v.on_date), "opening": v.opening_status,
                    "reservation_required": v.reservation_required, "ticket_price": v.ticket_price} for v in trip.visit_checks],
                "reference_date": str(datetime.now(ZoneInfo("Asia/Shanghai")).date())}
            result = await agents.json_result("tour_assistant", "你是亲切简洁的旅游客服。根据上下文回答或替客人调整行程，禁止让客人操作参数面板。"
                "budget是客人设定的全团总预算，回答时直接确认额度，不把它说成模型假设；不要求客人改报价，不罗列默认单价上下限。"
                "一般回复两三句话，语气自然，直接称呼你。"
                "咨询、问原因、致谢用answer，不修改行程。明确要求改安排用revise，changes仅含用户偏好；未提到的预算、人数、日期、城市、必去保留。"
                "按天修改给target_day。changes字段不得包含内部编号、核验事实或预约确认。缺信息时answer只问最关键的问题。"
                "例如第二天轻松点少走路：action=revise,target_day=2,changes={day_rules:[{day:2,pace:relaxed,reduce_walking:true}]}。"
                "日序字段是day，不是day_index；不要自创未提供的参数。客人说就这样或好的时通常answer确认当前安排，不要再次修改。"
                "已有候选且客人明确选了某地点用choose，choices必须来自给定候选。要求重新查入场规则用refresh。"
                "只有客人明确已经完成预约并给出景点、日期、时段、人数才用booking，booking含place_name,on_date,start,end,people。"
                "要求恢复历史才用restore，version为目标版本。任何时候都不代客预订付款，不编造房价、图片、闭馆及已预约事实。"
                "revise的reply用一句自然的话及时回应客人说明将如何安排，不要声称已经更新成功；更新结果由系统核验后回复。", payload, AssistantWireDecision)
            return AssistantDecision(**{**result.model_dump(exclude={"changes"}),
                "changes": result.changes.model_dump(mode="json", exclude_unset=True, exclude_none=True)}), agents.metrics()["model_calls"]
        finally:
            await agents.close()

    async def handle(self, trip_id, request, progress=None):
        repo = self.service.repository
        trip = repo.get(trip_id)
        if trip is None:
            return None
        if blocked_input(request.message):
            return {"reply": "请只告诉我旅行偏好和条件，证件、银行卡或支付信息不需要提供。", "trip": trip.model_dump(mode="json"), "changed": False}
        turn = repo.begin_chat(trip_id, request.request_id, request.message.strip(), request.base_version,
                               lease_seconds=self.service.settings.timeout + 30)
        if "response" in turn:
            return turn["response"]
        claim = turn["claim"]
        try:
            return await self.handle_claimed(trip, request, turn, progress)
        finally:
            # Graceful cancellation releases immediately; a dead process is recovered by lease expiry.
            repo.release_chat(claim)

    async def handle_claimed(self, trip, request, turn, progress=None):
        repo, trip_id, claim = self.service.repository, trip.id, turn["claim"]
        def update(stage, message):
            if progress:
                progress({"stage": stage, "message": message})
        if turn["result_version"] is not None:
            committed = repo.get(trip_id, turn["result_version"])
            original = repo.get(trip_id, request.base_version)
            response = {"reply": revision_reply(original, committed) if original else planning_reply(committed, changed=True), "trip": trip.model_dump(mode="json"),
                        "changed": True, "model_calls": 0}
            if trip.version != committed.version:
                response["reply"] = "刚才的修改已经保存，无需再次执行。我已为你加载当前最新安排。"
            repo.finish_chat(claim, response)
            return response
        current, reply, calls = trip, "", 0
        try:
            if trip.version != request.base_version:
                raise VersionConflict("行程已有新安排，我已为你加载最新版，请再告诉我想调整的内容。")
            day_references(request.message, trip.constraints.days or 30)
            async with asyncio.timeout(self.service.settings.timeout):
                update("understanding", "收到，我正在结合当前行程理解你的想法。")
                history = repo.messages(trip_id)
                effective_message = request.message
                confirmation = bool(re.fullmatch(r"(?:就这样|就按这个|就按你说的|好的|好|可以|没问题)[！!。\s]*", effective_message))
                if confirmation:
                    prior = history[:-1]
                    if prior and prior[-1]["content"].startswith("我需要先核对相关地点或预约信息"):
                        effective_message = next((m["content"] for m in reversed(prior) if m["role"] == "user" and
                            re.search(r"轻松|少走路|少步行", m["content"])), effective_message)
                decision, calls = await self.decide(effective_message, trip, history)
                if confirmation and effective_message == request.message and decision.action != "answer":
                    decision = AssistantDecision(reply="好的，就按当前这版安排。之后有新的想法，随时告诉我。")
                if decision.action == "answer":
                    reply = plain_text(decision.reply) or "我在的，直接告诉我你的旅行想法就好。"
                    if re.search(r"已(?:经)?(?:修改|更新|调整|预订|付款|订好|预约成功)", reply):
                        reply = "这次我还没有更改行程。请告诉我具体想调整的条件，我来安排。"
                elif decision.action == "refresh":
                    current = await self.service.refresh_visits(trip_id, trip.version, chat_claim=claim)
                else:
                    changes = {}
                    if decision.action == "choose":
                        allowed = {q.key: {p.id for p in q.options} for q in trip.clarifications}
                        if not decision.choices or any(pid not in allowed.get(key, set()) for key, pid in decision.choices.items()):
                            raise ValueError("还没能对应到唯一地点，请告诉我具体分馆或附近地址，我来核对。")
                        changes = {"place_choices": {**trip.constraints.place_choices, **decision.choices}}
                    elif decision.action == "booking":
                        changes = booking_changes(request.message, trip, decision)
                    elif decision.action == "restore":
                        if not re.search(r"恢复|还原|回到|上一版|之前", request.message):
                            raise ValueError("请说明想恢复哪次安排，我来处理。")
                        version = decision.version or trip.version - 1
                        saved = repo.get(trip_id, version)
                        if not saved or version >= trip.version:
                            raise ValueError("没有找到对应的旧安排，请说明想恢复的版本或需求。")
                        changes = saved.constraints.model_dump(mode="json")
                    else:
                        changes = safe_patch(effective_message, trip, decision)
                        if not changes:
                            raise ValueError("请再说具体一点，比如哪天轻松些、想换什么景点，或者新的预算。")
                    changes = protected_changes(effective_message, changes)
                    if not changes:
                        raise ValueError("已保留你要求不变的条件。这次没有更改行程，你可以告诉我具体想调整哪一天或哪些活动。")
                    remaining_calls = self.service.settings.max_model_calls - calls
                    if remaining_calls < 1:
                        raise ValueError("本次查询额度已用完，已有安排已保留。稍后再告诉我这条需求，我继续处理。")
                    acknowledgement = plain_text(decision.reply)
                    if not acknowledgement or re.search(r"已(?:经)?(?:修改|更新|调整|预订|付款|订好|预约成功)", acknowledgement):
                        acknowledgement = "好的，我会按你的要求重新安排，保留没有要求改变的条件。"
                    update("planning", acknowledgement)
                    update("checking", "正在重新安排活动、查询路线并核对费用，完成后会自动更新上方行程。")
                    current = await self.service.revise(trip_id, RevisionRequest(base_version=trip.version, changes=changes,
                        target_day=decision.target_day, target_period=decision.target_period, autonomous=True), model_budget=remaining_calls, chat_claim=claim)
                if not reply:
                    reply = revision_reply(trip, current) if current.version != trip.version else planning_reply(current)
        except asyncio.CancelledError:
            repo.release_chat(claim)
            raise
        except (ValueError, ValidationError, VersionConflict) as exc:
            current = repo.get(trip_id)
            reply = str(exc) if not isinstance(exc, ValidationError) else "这些条件还有冲突，请确认日期、人数和预算，我来继续协调。"
        except ProviderError as exc:
            current = repo.get(trip_id)
            reply = str(exc) if exc.code.startswith("model_") or exc.code == "missing_model_key" else "查询暂时未完成，已有安排还在。请稍后重试刚才的消息。"
        except TimeoutError:
            current = repo.get(trip_id)
            reply = "我暂时没能完成这次查询或调整，已有安排还在。你可以稍后再告诉我一次。"
        except Exception:  # noqa: BLE001 - Never expose model/provider exceptions or credentials.
            current = repo.get(trip_id)
            reply = "这次处理没有完成，已有安排已保留，请稍后重试。"
        response = {"reply": reply, "trip": current.model_dump(mode="json"), "changed": current.version != trip.version,
                    "model_calls": calls + (current.metrics.model_calls if current.version != trip.version else 0)}
        repo.finish_chat(claim, response)
        return response
