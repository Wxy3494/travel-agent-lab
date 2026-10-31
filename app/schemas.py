from datetime import UTC, date, datetime, time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator, model_validator

from app.day_input import MAX_TRIP_DAYS, day_references

DayIndex = Annotated[int, Field(ge=1, le=MAX_TRIP_DAYS)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @field_validator("*", mode="after")
    @classmethod
    def local_minute_time(cls, value):
        if isinstance(value, time) and (value.tzinfo is not None or value.second or value.microsecond):
            raise ValueError("请输入旅行所在地的HH:MM时间，不包含秒或时区")
        return value


class PriceRange(Model):
    low: float = Field(ge=0, le=1_000_000_000)
    high: float = Field(ge=0, le=1_000_000_000)

    @model_validator(mode="after")
    def ordered(self):
        if self.low > self.high:
            raise ValueError("预算下限不能超过上限")
        return self


class DayRule(Model):
    day: int = Field(ge=1, le=MAX_TRIP_DAYS)
    start: time | None = None
    end: time | None = None
    pace: Literal["standard", "relaxed"] = "standard"
    period: Literal["day", "afternoon"] = "day"
    visit_minutes: int | None = Field(default=None, ge=30, le=240)

    @model_validator(mode="after")
    def ordered(self):
        if self.start and self.end and self.start >= self.end:
            raise ValueError("活动时段须在同一天内，开始时间早于结束时间")
        if self.period == "afternoon" and self.end and self.end <= time(13):
            raise ValueError("下午活动须在13:00之后结束")
        return self


class OpeningRule(Model):
    place_id: str = Field(min_length=1, max_length=100)
    on_date: date
    closed: bool = False
    start: time | None = None
    end: time | None = None
    source: str = Field(min_length=1, max_length=300)
    source_url: str = Field(default="", max_length=1000)
    last_entry: time | None = None
    reservation_required: bool | None = None
    ticket_price: float | None = Field(default=None, ge=0, le=1_000_000)
    checked_at: datetime | None = None

    @model_validator(mode="after")
    def complete(self):
        if not self.closed and (not self.start or not self.end or self.start >= self.end):
            raise ValueError("开放时段必须有同一天的开始与结束时间")
        if self.checked_at and self.checked_at.tzinfo is None:
            raise ValueError("核对时间须带时区")
        return self


class EntryWindow(Model):
    start: time
    end: time

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end:
            raise ValueError("入场时段须在同一天内，开始早于结束")
        return self


class ReservationConfirmation(Model):
    place_id: str = Field(min_length=1, max_length=100)
    on_date: date
    entry_window: EntryWindow
    people: int = Field(ge=1, le=10)


class VisitCheck(Model):
    place_id: str
    place_name: str
    city: str
    on_date: date
    opening_status: Literal["open", "closed", "unknown"] = "unknown"
    windows: list[EntryWindow] = Field(default_factory=list, max_length=8)
    last_entry: time | None = None
    reservation_required: bool | None = None
    booking_url: str = ""
    booking_days_ahead: int | None = Field(default=None, ge=0, le=365)
    same_day_booking: bool | None = None
    availability: Literal["available", "sold_out", "unknown"] = "unknown"
    availability_checked_at: datetime | None = None
    ticket_price: float | None = Field(default=None, ge=0, le=1_000_000)
    ticket_scope: str = Field(default="成人基础门票；不含特展、讲解和优惠", max_length=200)
    authority: Literal["official", "provider", "user", "fixture", "unknown"] = "unknown"
    scope: Literal["regular", "date_specific"] = "regular"
    source_urls: list[str] = Field(default_factory=list, max_length=8)
    checked_at: datetime | None = None
    expires_at: datetime | None = None
    evidence_id: str = ""
    note: str = Field(default="", max_length=800)
    enforced: bool = True

    @model_validator(mode="after")
    def complete(self):
        if self.opening_status == "open" and not self.windows:
            raise ValueError("已知开放必须有明确时段")
        for stamp in (self.checked_at, self.expires_at, self.availability_checked_at):
            if stamp and stamp.tzinfo is None:
                raise ValueError("核验时间必须带时区")
        return self


class AnchorChoice(Model):
    hotel_id: str | None = Field(default=None, max_length=100)
    departure_station_id: str | None = Field(default=None, max_length=100)
    arrival_station_id: str | None = Field(default=None, max_length=100)


class PlaceBinding(Model):
    field: Literal["must_visit", "excluded"]
    query: str
    place_id: str
    canonical_name: str
    city: str
    basis: str
    evidence_id: str


class Constraints(Model):
    origin: str | None = Field(default=None, max_length=60)
    cities: list[str] = Field(default_factory=list, max_length=3)
    start_date: date | None = None
    days: int | None = Field(default=None, ge=1, le=MAX_TRIP_DAYS)
    people: int | None = Field(default=None, ge=1, le=10)
    budget: float | None = Field(default=None, gt=0, le=1_000_000)
    interests: list[str] = Field(default_factory=list, max_length=8)
    must_visit: list[str] = Field(default_factory=list, max_length=10)
    excluded: list[str] = Field(default_factory=list, max_length=10)
    transport: Literal["public", "driving"] = "public"
    return_to_origin: bool = False
    home_transport_scope: Literal["separate", "in_day"] = "separate"
    accessibility_required: bool = False
    rooms: int | None = Field(default=None, ge=1, le=10)
    hotel_per_room_night: PriceRange = Field(default_factory=lambda: PriceRange(low=180, high=320))
    food_per_person_day: PriceRange = Field(default_factory=lambda: PriceRange(low=70, high=110))
    ticket_per_person_visit: PriceRange = Field(default_factory=lambda: PriceRange(low=0, high=80))
    pricing_note: str = Field(default="项目预算假设，非实时价格；请用自己的报价调整", max_length=300)
    arrival_time: time | None = None
    departure_time: time | None = None
    daily_start: time = time(9)
    daily_end: time = time(21, 30)
    daily_end_scope: Literal["return", "last_activity"] = "return"
    lodging_addresses: dict[str, str] = Field(default_factory=dict, max_length=4)
    visit_minutes: int = Field(default=90, ge=30, le=240)
    day_rules: list[DayRule] = Field(default_factory=list, max_length=MAX_TRIP_DAYS)
    opening_rules: list[OpeningRule] = Field(default_factory=list, max_length=90)
    reservations: list[ReservationConfirmation] = Field(default_factory=list, max_length=90)
    place_choices: dict[str, str] = Field(default_factory=dict, max_length=20)
    bindings: list[PlaceBinding] = Field(default_factory=list, max_length=20)
    anchors: dict[str, AnchorChoice] = Field(default_factory=dict, max_length=4)
    day_place_ids: dict[DayIndex, list[str]] = Field(default_factory=dict, max_length=MAX_TRIP_DAYS)
    optimize_routes: bool = True
    indoor_sources: dict[str, str] = Field(default_factory=dict, max_length=20)

    @model_validator(mode="after")
    def consistent(self):
        submitted_fields = set(self.model_fields_set)
        if self.origin is not None:
            self.origin = self.origin.strip() or None
        for key in ("interests", "must_visit", "excluded"):
            values = [v.strip() for v in getattr(self, key)]
            if any(not v or len(v) > 80 for v in values):
                raise ValueError("兴趣和地点必须为1～80字的非空名称")
            setattr(self, key, list(dict.fromkeys(values)))
        self.cities = [c.strip() for c in self.cities]
        self.lodging_addresses = {city.strip(): address.strip() for city, address in self.lodging_addresses.items()}
        if any(not city or len(city) > 60 or not address or len(address) > 200
               for city, address in self.lodging_addresses.items()):
            raise ValueError("住宿城市和地址不能为空，地址最多200字")
        if any(not c or len(c) > 60 for c in self.cities):
            raise ValueError("城市名称不能为空或超过60字")
        if len(self.cities) != len(set(self.cities)):
            raise ValueError("目的地城市不能重复")
        if self.days and len(self.cities) > self.days:
            raise ValueError("旅行天数不能少于目的地城市数量")
        if set(self.must_visit) & set(self.excluded):
            raise ValueError("必去地点与排除地点冲突")
        if self.people and self.rooms and self.rooms > self.people:
            raise ValueError("房间数不能超过人数")
        if self.daily_start >= self.daily_end:
            raise ValueError("不支持跨午夜活动，请填写同一天内的活动时段")
        if self.days == 1 and self.arrival_time and self.departure_time and self.arrival_time >= self.departure_time:
            raise ValueError("首日到达时间必须早于末日离开时间")
        if len({r.day for r in self.day_rules}) != len(self.day_rules):
            raise ValueError("每日活动时段不能重复")
        if self.days and any(day > self.days for day in [*[r.day for r in self.day_rules], *self.day_place_ids]):
            raise ValueError("指定日序超过旅行天数")
        if any(len(ids) > 3 or len(set(ids)) != len(ids) for ids in self.day_place_ids.values()):
            raise ValueError("每日最多三处地点，不能重复")
        if len({(r.place_id, r.on_date) for r in self.reservations}) != len(self.reservations):
            raise ValueError("同一景点同一天只能保留一个预约确认")
        object.__setattr__(self, "__pydantic_fields_set__", submitted_fields)
        return self

    def missing(self) -> list[str]:
        labels = {"origin": "出发城市", "cities": "目的地城市（按顺序）", "start_date": "出发日期",
                  "days": "旅行天数", "people": "人数", "budget": "全团总预算（人民币）"}
        return [label for key, label in labels.items() if not getattr(self, key)]


# A patch omits unchanged fields; it never fills them with Constraints defaults.
ConstraintPatch = create_model(
    "ConstraintPatch", __base__=Model,
    **{name: ((Annotated[field.annotation | None, *field.metadata]
               if field.metadata else field.annotation | None), None)
       for name, field in Constraints.model_fields.items()},
)


class TripRequest(Model):
    prompt: str = Field(default="", max_length=3000)
    constraints: Constraints = Field(default_factory=Constraints)
    strategy: Literal["single", "multi"] = "multi"

    @field_validator("prompt")
    @classmethod
    def valid_day_reference(cls, value):
        day_references(value)
        return value


class RevisionRequest(Model):
    base_version: int = Field(ge=1)
    instruction: str = Field(default="", max_length=3000)
    changes: dict = Field(default_factory=dict)
    adjustment_id: str | None = None
    target_day: int | None = Field(default=None, ge=1, le=MAX_TRIP_DAYS)
    target_period: Literal["day", "afternoon"] = "day"

    @field_validator("changes")
    @classmethod
    def valid_patch_fields(cls, value):
        ConstraintPatch.model_validate(value)
        return value

    @field_validator("instruction")
    @classmethod
    def valid_day_reference(cls, value):
        day_references(value)
        return value


class Evidence(Model):
    id: str
    kind: Literal["live", "estimate", "fixture", "user"]
    provider: str
    description: str
    url: str = ""
    fetched_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class Place(Model):
    id: str
    name: str
    city: str
    location: str
    indoor: bool | None = None
    evidence_id: str
    address: str = ""
    category: Literal["attraction", "hotel", "station"] = "attraction"
    indoor_evidence_id: str | None = None

    @model_validator(mode="after")
    def coordinates(self):
        try:
            lon, lat = map(float, self.location.split(","))
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("地点坐标无效") from None
        return self


class CityFacts(Model):
    city: str
    citycode: str
    adcode: str
    location: str
    places: list[Place]
    hotels: list[Place] = Field(default_factory=list)
    stations: list[Place] = Field(default_factory=list)
    weather: list[dict] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    lodging: Place | None = None


class DayChoice(Model):
    day: int = Field(ge=1, le=MAX_TRIP_DAYS)
    city: str
    place_ids: list[str] = Field(max_length=3)
    reason: str = Field(max_length=600)


class Selection(Model):
    days: list[DayChoice] = Field(min_length=1, max_length=MAX_TRIP_DAYS)


class Review(Model):
    approved: bool
    issues: list[str] = Field(default_factory=list, max_length=20)


class Issue(Model):
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"


class Event(Model):
    kind: Literal["travel", "visit", "meal", "rest", "wait"]
    start: str
    end: str
    title: str
    city: str
    evidence_ids: list[str]
    place_id: str | None = None
    map_url: str | None = None
    cost: PriceRange
    note: str = ""
    start_location: str | None = None
    end_location: str | None = None
    route_segments: list[str] = Field(default_factory=list)
    is_return: bool = False


class DayWeather(Model):
    city: str
    date: date
    condition: str = "暂未发布，临近出发时查看"
    high_c: float | None = None
    low_c: float | None = None
    source: str = ""
    updated_at: str = ""
    is_demo: bool = False
    available: bool = False


class HomeTransfer(Model):
    direction: Literal["outbound", "return"]
    from_city: str
    to_city: str
    estimated_minutes: int = Field(gt=0)
    unit_price: PriceRange
    cost: PriceRange
    evidence_ids: list[str] = Field(min_length=1)
    start_location: str
    end_location: str
    route_segments: list[str] = Field(default_factory=list)
    note: str


class DayPlan(Model):
    day: int
    date: date
    city: str
    events: list[Event]
    overnight_city: str | None = None
    hotel_options: list[Place] = Field(default_factory=list)
    weather: str
    weather_details: list[DayWeather] = Field(default_factory=list)
    departure_note: str = ""
    alternative: str
    reason: str
    travel_minutes: int = 0
    route_comparison: dict = Field(default_factory=dict)
    indoor_options: list[Place] = Field(default_factory=list)
    home_transfers: list[HomeTransfer] = Field(default_factory=list, max_length=2)


class BudgetItem(Model):
    category: str
    quantity: int
    unit: str
    unit_price: PriceRange
    total: PriceRange
    evidence_id: str
    day: int | None = None
    event_start: str | None = None


class Budget(Model):
    currency: str = "CNY"
    scope: str = "全团总计"
    items: list[BudgetItem]
    total: PriceRange
    status: Literal["within_estimate", "uncertain", "over_budget"]


class Metrics(Model):
    elapsed_ms: float = 0
    tool_calls: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_model_cost_cny: float | None = None


class Clarification(Model):
    key: str
    field: str
    query: str
    reason: str
    options: list[Place] = Field(default_factory=list)


class Adjustment(Model):
    id: str
    title: str
    impact: str
    changes: dict


class CloneRequest(Model):
    version: int | None = Field(default=None, ge=1)


class TravelInfo(Model):
    status: Literal["success", "unavailable"]
    content: str = Field(default="", max_length=40000)
    message: str = ""
    queried_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_url: str = "https://developer.meituan.com/ai-hub/skill-list/detail/12"
    query_fingerprint: str = ""
    queried_places: int = 0
    total_places: int = 0


class TripResult(Model):
    id: str
    version: int
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    mode: Literal["demo", "live"]
    strategy: Literal["single", "multi"]
    status: Literal["ready", "needs_input", "needs_review", "blocked", "failed"]
    constraints: Constraints
    questions: list[str] = Field(default_factory=list)
    days: list[DayPlan] = Field(default_factory=list)
    budget: Budget | None = None
    issues: list[Issue] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list)
    metrics: Metrics = Field(default_factory=Metrics)
    markdown: str = ""
    clarifications: list[Clarification] = Field(default_factory=list)
    adjustments: list[Adjustment] = Field(default_factory=list)
    revision_scope: dict = Field(default_factory=dict)
    name_normalizations: list[dict] = Field(default_factory=list)
    visit_checks: list[VisitCheck] = Field(default_factory=list)
    admission_verification: bool = False
    travel_info: TravelInfo | None = None

    @model_validator(mode="before")
    @classmethod
    def legacy_home_transport(cls, value):
        # Old saved JSON scheduled home-city journeys inside the daily timeline.
        if isinstance(value, dict) and isinstance(value.get("constraints"), dict) and "home_transport_scope" not in value["constraints"]:
            value = {**value, "constraints": {**value["constraints"], "home_transport_scope": "in_day"}}
        return value
