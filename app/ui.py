"""Form-first travel planning with optional text revisions."""
import base64
import os
import uuid
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import streamlit as st
from dotenv import load_dotenv

from app.assistant import planning_reply
from app.config import client_read_timeout
from app.form_validation import FORM_DEFAULTS, validate_form
from app.navigation import route_lines
from app.photos import fetch_photo, gallery_html
from app.presentation import (
    admission_summary,
    day_description,
    hotel_details,
    issue_message,
    plain_text,
    weather_line,
)
from app.render import escape, render_markdown
from app.schemas import MAX_TRIP_DAYS, TripResult
from app.travel_references import matching_reference, reference_body, references_for

load_dotenv()
API = os.getenv("TRAVEL_API_URL", "http://127.0.0.1:8000").rstrip("/")
st.set_page_config(page_title="行旅 · 旅行规划", page_icon="🧭", layout="wide",
                   initial_sidebar_state="auto")
TODAY = datetime.now(ZoneInfo("Asia/Shanghai")).date()
ASSETS = Path(__file__).with_name("assets")
HERO_IMAGE = "data:image/jpeg;base64," + base64.b64encode(
    (ASSETS / "coastal-evening.jpg").read_bytes()).decode("ascii")
st.markdown("<style>" + Path(__file__).with_name("ui_theme.css").read_text(encoding="utf-8") + "</style>",
            unsafe_allow_html=True)

def constraint_text(field, value):
    if value is None or value == [] or value == {}:
        return "未设置"
    if isinstance(value, bool):
        return "是" if value else "否"
    if field == "day_rules":
        return "；".join(f"第{r['day']}天（{'下午' if r.get('period') == 'afternoon' else '整天'}）："
            f"{(r.get('start') or '默认开始')[:5]}～{(r.get('end') or '默认结束')[:5]}，"
            f"{'轻松' if r.get('pace') == 'relaxed' else '适中'}，"
            f"{str(r['visit_minutes']) + '分钟/处' if r.get('visit_minutes') else '沿用默认游览时长'}" for r in value)
    if field == "bindings":
        return "；".join(f"{b['query']} → {b['canonical_name']}（{b['city']}）" for b in value)
    if field == "anchors":
        return "；".join(city + "：" + "、".join({"hotel_id": "酒店", "arrival_station_id": "到达车站",
            "departure_station_id": "出发车站"}[k] for k, pid in positions.items() if pid) for city, positions in value.items())
    if field == "lodging_addresses":
        return "；".join(city + "：" + address for city, address in value.items())
    if field == "daily_end_scope":
        return "最后一个游玩项目结束" if value == "last_activity" else "含返回住处的交通"
    if field == "home_transport_scope":
        return "出发地往返交通单独安排" if value == "separate" else "往返交通计入每日时间"
    if field == "place_choices":
        return "；".join(query.split(":", 1)[-1] + "：已确认具体地点" for query in value)
    if field == "day_place_ids":
        return "；".join(f"第{day}天：已指定{len(ids)}处活动（详见下方日程差异）" for day, ids in value.items())
    if field == "opening_rules":
        return "；".join(f"{r['on_date']}：{'闭馆' if r['closed'] else (r['start'][:5] + '～' + r['end'][:5])}；{r['source']}" for r in value)
    if field == "reservations":
        return "；".join(f"{r['on_date']}：你确认已预约{r['people']}人，入场{r['entry_window']['start'][:5]}～{r['entry_window']['end'][:5]}（未验证凭证）" for r in value)
    if field == "indoor_sources":
        return "；".join(value.values())
    if isinstance(value, dict) and "low" in value:
        return f"¥{value['low']:g}" if value["low"] == value["high"] else f"¥{value['low']:g}～{value['high']:g}（估算）"
    if isinstance(value, list):
        return "、".join(map(str, value))
    return {"public": "公共交通", "driving": "自驾"}.get(str(value), str(value))


def sync_form(c):
    """Run before widgets are created, including after text revisions and history reads."""
    values = {
        "form_origin": c.get("origin") or "", "form_destinations": "，".join(c.get("cities", [])),
        "form_date": date.fromisoformat(c["start_date"]) if c.get("start_date") else None,
        "form_days": c.get("days"), "form_people": c.get("people"), "form_budget": c.get("budget"),
        "form_interests": "，".join(c.get("interests", [])),
        "form_must": "，".join(c.get("must_visit", [])), "form_excluded": "，".join(c.get("excluded", [])),
        "form_transport": c.get("transport", "public"), "form_return": c.get("return_to_origin", False),
        "form_accessible": c.get("accessibility_required", False), "form_rooms": c.get("rooms") or 0,
        "form_pricing_note": c.get("pricing_note", FORM_DEFAULTS["form_pricing_note"]),
        "form_lodging": "\n".join(city + "：" + address for city, address in c.get("lodging_addresses", {}).items()),
    }
    values["form_price_ranges"] = {}
    for key, field in [("daily_start", "daily_start"), ("daily_end", "daily_end")]:
        values[f"form_{key}"] = time.fromisoformat(c[field]) if c.get(field) else FORM_DEFAULTS[f"form_{key}"]
    for label, field in [("hotel", "hotel_per_room_night"), ("food", "food_per_person_day"),
                         ("ticket", "ticket_per_person_visit")]:
        price = c.get(field)
        values[f"form_{label}_budget"] = float(price["low"]) if price and price["low"] == price["high"] else None
        if price:
            values["form_price_ranges"][label] = price
    st.session_state.update(values)
    st.session_state.form_validation_started = False
    st.session_state.form_errors = {}
    st.session_state.form_expand_details = False


def request(method, path, payload=None):
    try:
        timeout = httpx.Timeout(client_read_timeout(st.session_state.get("api_timeout")), connect=10, write=30, pool=10)
        response = httpx.request(method, API + path, json=payload, timeout=timeout)
        if response.is_error:
            st.error(f"请求未完成（{response.status_code}）：{response.json().get('detail', '请检查输入')}")
            return None
        return response.json()
    except httpx.TimeoutException:
        st.warning("等待服务响应超时。后台可能仍在处理，请先在历史旅行列表检查结果，避免立即重复提交。")
        return None
    except (httpx.HTTPError, ValueError):
        st.error("暂时无法连接规划服务，请启动服务后重试。")
        return None


def accept_trip(updated):
    if updated:
        st.session_state.trip = updated
        st.session_state.sync_form = updated["constraints"]
        st.rerun()


def revise_choice(trip, changes=None, **options):
    accept_trip(request("POST", f"/trips/{trip['id']}/revise",
                        {"base_version": trip["version"], "changes": changes or {}, **options}))


def show_route(value):
    for line in route_lines(value):
        links = [f"[{'步行导航' if len(line['links']) == 1 else f'第{n + 1}段导航'}]({url})"
                 for n, url in enumerate(line["links"])]
        st.markdown(escape(plain_text(line["text"])) + (" · " + " · ".join(links) if links else ""))


@st.cache_data(ttl=21600, max_entries=128, show_spinner=False)
def cached_photo(url):
    return fetch_photo(url)


@st.cache_data(ttl=21600, max_entries=256, show_spinner=False)
def lookup_photos(api_url, city, pid, category):
    try:
        result = httpx.request("GET", api_url + f"/places/photos?city={quote(city, safe='')}&place_id={quote(pid, safe='')}&category={category}", timeout=8)
        result.raise_for_status()
        return result.json().get("photos", [])
    except (httpx.HTTPError, ValueError, AttributeError):
        return []


def show_photos(name, city, pid, photos, category, mode):
    urls = photos or (lookup_photos(API, city, pid, category) if mode == "live" and pid else [])
    pictures = []
    for url in urls[:4]:
        picture = cached_photo(url)
        if picture:
            pictures.append(picture)
        if len(pictures) == 2:
            break
    markup = gallery_html(name, pictures)
    if markup:
        st.markdown(markup, unsafe_allow_html=True)


def show_assistant(trip):
    if st.session_state.get("pending_chat", {}).get("trip_id", trip["id"]) != trip["id"]:
        st.session_state.pop("pending_chat", None)
    with st.container(key="tour_assistant", border=True):
        st.subheader("旅游助手")
        st.caption("说说你的想法，我来协调路线、景点和住宿。")
        messages = request("GET", f"/trips/{trip['id']}/messages") or []
        if not messages:
            with st.chat_message("assistant"):
                st.write(planning_reply(TripResult.model_validate(trip)))
        for message in messages:
            with st.chat_message(message["role"]):
                st.write(message["content"])
        message = st.chat_input("例如：第二天轻松一点，想多留些时间吃当地美食", key="assistant_input", max_chars=3000)
        if message:
            st.session_state.pending_chat = {"trip_id": trip["id"], "base_version": trip["version"],
                "message": message, "request_id": uuid.uuid4().hex}
        pending = st.session_state.get("pending_chat")
        retry = bool(pending) and st.button("重新发送刚才的消息", key="retry_chat") if not message else False
        if (message or retry) and pending:
            with st.chat_message("user"):
                st.write(pending["message"])
            with st.chat_message("assistant"), st.spinner("我正在查看并安排…"):
                response = request("POST", f"/trips/{pending['trip_id']}/chat",
                    {k: v for k, v in pending.items() if k != "trip_id"})
            if response:
                st.session_state.trip = response["trip"]
                if response.get("changed"):
                    st.session_state.sync_form = response["trip"]["constraints"]
                st.session_state.pop("pending_chat", None)
                st.rerun()


def show_trip(trip, latest):
    labels = {"ready": "方案已生成", "needs_review": "行程还需要调整", "needs_input": "还需要补充一些信息",
              "blocked": "这项请求无法执行", "failed": "这次规划未完成"}
    refs = references_for(trip)
    pending_codes = {"admission_unverified", "ticket_unverified", "audit_admission_unverified", "audit_ticket_unverified"}
    only_pending = trip["status"] == "needs_review" and all(i["code"] in pending_codes for i in trip["issues"] if i["severity"] == "error")
    label = "行程已安排，部分入场信息待确认" if only_pending else labels[trip["status"]]
    st.markdown(f"**{label}** · 第{trip['version']}版")
    for q in trip["questions"]:
        st.write(plain_text(q))
    if trip["status"] == "needs_input":
        st.caption("请在上方补充缺少的信息，再生成行程；也可以用下方文字输入框补充。")
    errors = [issue for issue in trip["issues"] if issue["severity"] == "error" and issue["code"] not in pending_codes]
    if any(i["severity"] == "error" and i["code"] in pending_codes for i in trip["issues"]):
        st.info("部分景点的开放、预约或票价还缺少可直接核对的资料。已把查询参考和待确认事项放在对应景点下方；请在出发前确认。")
    for issue in errors[:8]:
        st.warning(issue_message(issue))
    if len(errors) > 8:
        with st.expander(f"其他{len(errors) - 8}条待处理事项"):
            for issue in errors[8:]:
                st.write(issue_message(issue))
    c = trip["constraints"]
    if trip.get("name_normalizations") or c.get("bindings"):
        with st.expander("地点名称确认记录"):
            for entry in trip.get("name_normalizations", []):
                st.write(entry["submitted"] + " → " + entry["canonical"])
            for binding in c.get("bindings", []):
                st.write(f"{binding['query']} → {binding['canonical_name']} · {binding['city']}")
                st.caption(plain_text(binding["basis"]))
    if trip["days"]:
        st.write(" → ".join(c["cities"]) + f" · {c['days']}天 · {c['people']}人")
        if c.get("home_transport_scope") == "separate":
            st.info("以下按已到达目的地安排当地游玩。出发地往返交通单独列出，不占每日游玩时间；实际出行日期与车次请另行确认。")
        transfers = [t for day in trip["days"] for t in day.get("home_transfers", [])]
        if transfers:
            with st.expander("出发地往返交通（单独安排）"):
                for t in transfers:
                    label = "去程" if t["direction"] == "outbound" else "返程"
                    duration = t["estimated_minutes"]
                    st.markdown(f"**{label}：{t['from_city']} → {t['to_city']}**")
                    st.write(f"路线参考约{duration // 60}小时{duration % 60}分钟 · 全团费用估算 ¥{t['cost']['low']:,.0f}–{t['cost']['high']:,.0f}")
                    st.caption("往返交通单独安排，实际日期与车次请确认。")
                    show_route(t)
        total = trip["budget"]["total"] if trip["budget"] else None
        if total:
            st.markdown(f"全团估算 **¥{total['low']:,.0f} – {total['high']:,.0f}** · 预算 ¥{c['budget']:,.0f}")
        nights = Counter(d["overnight_city"] for d in trip["days"] if d["overnight_city"])
        if nights:
            st.caption("住宿：" + " · ".join(f"{city}{count}晚" for city, count in nights.items()))
        for day in trip["days"]:
            panel = st.expander(f"第 {day['day']} 天 · {day['date']} · {day['city']}", expanded=day["day"] == 1,
                key=f"day_{trip['id']}_{trip['version']}_{day['day']}", on_change="rerun")
            if not panel.open:
                continue
            with panel:
                for weather in day.get("weather_details", []):
                    st.markdown("**天气 · " + escape(weather_line(weather)) + "**")
                if day.get("departure_note"):
                    st.write(day["departure_note"])
                if c.get("daily_end_scope") == "last_activity":
                    st.caption(f"每天 {c['daily_start'][:5]} 从住处出发，最后一个游玩项目在 {c['daily_end'][:5]} 前结束；随后返程单独计算。")
                st.write(day_description(day, trip["status"] == "ready"))
                st.caption(f"当日交通合计 {day.get('travel_minutes', 0)} 分钟（含缓冲）。")
                comparison = day.get("route_comparison", {})
                if "before_minutes" in comparison:
                    st.caption(f"同一地图工具比较：原顺序 {comparison['before_minutes']} 分钟 → 采用顺序 {comparison['after_minutes']} 分钟；非全局最优。")
                for e in day["events"]:
                    title = escape(e["title"])
                    if e["map_url"]:
                        title = f"[{title}]({e['map_url']})"
                    st.markdown(f"**{e['start']}–{e['end']}**　{title}")
                    if e["kind"] in {"travel", "visit"}:
                        st.caption(f"全团费用约 ¥{e['cost']['low']:.0f}–{e['cost']['high']:.0f}")
                    if e["kind"] == "visit":
                        show_photos(e["title"], e["city"], e.get("place_id"), e.get("photos", []), "attraction", trip["mode"])
                    check = next((v for v in trip.get("visit_checks", []) if v["place_id"] == e.get("place_id") and v["on_date"] == day["date"]), None)
                    if check:
                        st.caption("入场核验：" + admission_summary(check, c))
                    ref = matching_reference(refs, e.get("place_id")) if e["kind"] == "visit" else None
                    if ref:
                        st.caption("入场参考（待确认）")
                        if day["date"] not in {str(d) for d in ref.dates}:
                            st.caption("这份资料原按" + "、".join(str(d) for d in ref.dates) + "查询；到访日期调整后，当日开放和预约仍需重新确认。")
                        st.markdown(plain_text(reference_body(ref)))
                    show_route(e)
                if not day.get("weather_details"):
                    st.write(f"天气：{plain_text(day['weather'])}")
                st.write(f"备选：{plain_text(day['alternative'])}")
                if day["overnight_city"]:
                    st.write(f"当晚住在{day['overnight_city']}；填写的住处用于计算路程，未填写时按住宿区域估算。以下酒店仅供参考，房价和房源需另行确认。")
                    for h in [day["suggested_stay"]] if day.get("suggested_stay") else day["hotel_options"][:1]:
                        ref = matching_reference(refs, h["id"], day["date"])
                        hotel = hotel_details(h, c, day["date"], ref, bool(trip.get("travel_info", {}) and trip["travel_info"].get("queried_hotels")))
                        st.markdown(f"**{escape(hotel['name'])}** · [查看地图]({hotel['map_url']}) · [搜索房价]({hotel['search_url']})")
                        if h.get("address"):
                            st.caption("地址：" + h["address"])
                        show_photos(h["name"], h["city"], h["id"], h.get("photos", []), "hotel", trip["mode"])
                        st.caption(hotel["budget_label"])
                        st.caption(hotel["price_note"])
                        if ref:
                            if ref.identity_confirmed:
                                st.caption("已按高德地址确认分店")
                            st.caption("房价参考（最终价格以购买页面为准）")
                            st.markdown(plain_text(reference_body(ref)))
    if latest and trip["days"]:
        with st.expander("费用明细"):
            info = trip.get("travel_info")
            if info and not refs and info.get("content"):
                st.caption("补充旅行资料：本次回复未能按具体地点匹配，尚未用于排程。")
                st.markdown(info["content"])
            elif info and info["status"] != "success":
                st.caption(info["message"])
            if trip["budget"]:
                st.dataframe([{"项目": i["category"], "数量": f"{i['quantity']} {i['unit']}",
                    "下限 / 元": i["total"]["low"], "上限 / 元": i["total"]["high"]}
                    for i in trip["budget"]["items"]], hide_index=True, width="stretch")
                st.caption("费用为估算，实际以购买页面为准。")
        st.download_button("下载行程文档", render_markdown(TripResult.model_validate(trip)),
            file_name=f"trip-{trip['id']}-v{trip['version']}.md", mime="text/markdown",
            key=f"download-{trip['id']}-{trip['version']}")
        st.caption("想调整安排，直接告诉下方的旅游助手。")


st.session_state.setdefault("trip", None)
st.session_state.setdefault("revision_notes", [])
st.session_state.setdefault("form_validation_started", False)
st.session_state.setdefault("form_errors", {})
st.session_state.setdefault("form_expand_details", False)
st.session_state.setdefault("history_open", False)
st.session_state.setdefault("history_page", 0)
form_initialized = "form_origin" in st.session_state
for key, value in FORM_DEFAULTS.items():
    st.session_state.setdefault(key, value)
if not form_initialized and st.session_state.trip:
    sync_form(st.session_state.trip["constraints"])
if "sync_form" in st.session_state:
    sync_form(st.session_state.pop("sync_form"))
    st.session_state.revision_instruction = ""

def close_history():
    st.session_state.history_open = False


def reset_history_page():
    st.session_state.history_page = 0


def open_history_trip(trip_id):
    loaded = request("GET", f"/trips/{quote(trip_id, safe='')}")
    if loaded:
        st.session_state.trip = loaded
        st.session_state.sync_form = loaded["constraints"]
        st.session_state.revision_notes = []
        close_history()
        st.rerun()


@st.dialog("全部历史旅行", width="large", on_dismiss=close_history)
def history_dialog():
    query = st.text_input("按城市或日期查找", key="history_query", max_chars=100, on_change=reset_history_page)
    offset = st.session_state.history_page * 20
    data = request("GET", f"/trips/history?query={quote(query, safe='')}&limit=20&offset={offset}") or {}
    rows, total = data.get("items", []), data.get("total", 0)
    st.caption(f"共{total}条旅行记录 · 第{st.session_state.history_page + 1}页")
    picked = st.selectbox("选择旅行", rows, index=None, key="history_choice",
                          format_func=lambda r: f"{r['title']} · 第{r['version']}版", placeholder="请选择历史旅行")
    if st.button("打开选中旅行", disabled=picked is None):
        open_history_trip(picked["id"])
    a, b, c = st.columns(3)
    if a.button("上一页", disabled=offset == 0):
        st.session_state.history_page -= 1
        st.rerun()
    if b.button("下一页", disabled=offset + 20 >= total):
        st.session_state.history_page += 1
        st.rerun()
    if c.button("关闭历史列表"):
        close_history()
        st.rerun()


strategy = "multi"
with st.sidebar:
    st.markdown("### 🧭 行旅")
    if st.button("＋ 开始新的旅行", width="stretch"):
        st.session_state.update(FORM_DEFAULTS)
        st.session_state.revision_notes = []
        st.session_state.trip = None
        st.session_state.form_validation_started = False
        st.session_state.form_errors = {}
        st.session_state.form_expand_details = False
        st.session_state.pop("weather_report", None)
        st.rerun()
    health = request("GET", "/health")
    if health:
        st.session_state.api_timeout = health.get("request_timeout_seconds")
        st.caption("真实接口 · 价格为估算" if health["mode"] == "live" else "离线演示 · 人工示例数据")
        if health["mode"] == "demo":
            st.caption("支持广州、深圳、珠海的基础需求；没有调用模型或真实地图。")
    st.markdown("#### 最近的旅行")
    recent = request("GET", "/trips/history?limit=5") or {}
    for entry in recent.get("items", []):
        if st.button(entry["title"], key="recent_" + entry["id"], width="stretch"):
            open_history_trip(entry["id"])
    if not recent.get("items"):
        st.caption("还没有旅行记录，生成后会自动保存在这里。")
    if st.button(f"查看全部历史（{recent.get('total', 0)}）", key="all_history", width="stretch"):
        st.session_state.history_open = True
        st.session_state.history_page = 0

if st.session_state.history_open:
    history_dialog()

mode_label = "离线演示 · 示例数据" if health and health["mode"] == "demo" else (
    "真实接口 · 费用估算" if health else "规划服务未连接")
st.markdown(f"""<div class="travel-topbar">
  <div class="travel-brand"><span class="travel-brand-mark">✦</span><span>行旅</span></div>
  <span class="travel-mode">{mode_label}</span>
</div>""", unsafe_allow_html=True)

st.markdown(f"""<section class="travel-hero" aria-label="旅行规划首页">
  <img src="{HERO_IMAGE}" alt="海岸、群山与城市灯光组成的旅行氛围示意图">
  <div class="travel-hero-copy">
    <div class="travel-hero-kicker">你的下一段旅程，从这里开始</div>
    <h1>去看看，<br><em>世界的另一面。</em></h1>
    <p>告诉我们想去哪里，把风景、路线和预算连成一段旅程。</p>
  </div>
</section>""", unsafe_allow_html=True)

field_slots = {}


def form_field(parent, widget_type, label, key, **kwargs):
    with parent.container(key="field_" + key):
        value = getattr(st, widget_type)(label, key=key, **kwargs)
        field_slots[key] = st.empty()
        return value


def begin_form_validation():
    # Button callbacks run before widgets are rebuilt, so a collapsed panel can
    # be reopened without an extra rerun interrupting the user's next edit.
    st.session_state.form_validation_started = True
    checked = validate_form({key: st.session_state[key] for key in FORM_DEFAULTS}, st.session_state.trip, TODAY)
    main_fields = {"form", "form_origin", "form_destinations", "form_date", "form_days", "form_people", "form_budget"}
    if set(checked.errors) - main_fields:
        st.session_state.form_expand_details = True


with st.container(key="planning_section"), st.container(key="trip_form", border=False):
    st.markdown('<div class="travel-form-kicker">规划你的旅行</div>', unsafe_allow_html=True)
    st.subheader("开启一段旅行")
    st.caption("下面六项为必填信息。多城旅行按目的地顺序安排。")
    st.caption("开始日期与天数对应目的地当地游玩；出发地往返交通的实际日期另行安排。")
    a, b, c = st.columns(3)
    field_slots["form"] = st.empty()
    origin = form_field(a, "text_input", "出发地", key="form_origin", placeholder="例如：广州", max_chars=60)
    destinations = form_field(b, "text_input", "目的地（按顺序，用逗号分隔）", key="form_destinations",
                                placeholder="例如：广州，深圳", max_chars=185)
    start = form_field(c, "date_input", "游玩开始日期", key="form_date", value=None,
                         min_value=min(TODAY, st.session_state.form_date or TODAY), format="YYYY/MM/DD")
    a, b, c = st.columns(3)
    days = form_field(a, "number_input", "旅行天数", key="form_days", min_value=1, max_value=MAX_TRIP_DAYS,
                          value=None, step=1, placeholder="1～30天")
    people = form_field(b, "number_input", "出行总人数（含本人）", key="form_people", min_value=1, max_value=10,
                            value=None, step=1, placeholder="1～10人")
    budget = form_field(c, "number_input", "全团总预算（人民币 / 元）", key="form_budget", min_value=1.0,
                            max_value=1_000_000.0, value=None, step=100.0, placeholder="例如：6000")
    previous = st.session_state.trip
    hint_col, submit_col = st.columns([3, 1], vertical_alignment="bottom")
    hint_col.caption("还有偏好或时间要求？可展开填写下方选项。")
    form_feedback = hint_col.empty()
    submit = submit_col.button(
        "重新生成行程" if previous and previous["status"] != "blocked" else "生成旅行计划",
        type="primary", width="stretch", on_click=begin_form_validation)
    details = st.expander("＋ 添加旅行偏好与详细要求（可选）", key="form_expand_details", on_change="rerun")
    with details:
        st.markdown("**兴趣与出行偏好**")
        interests = form_field(st, "text_input", "兴趣（逗号分隔）", key="form_interests", placeholder="博物馆，公园，美食")
        a, b = st.columns(2)
        must = form_field(a, "text_input", "必去地点", key="form_must", placeholder="例如：广州塔")
        excluded = form_field(b, "text_input", "排除地点", key="form_excluded", placeholder="例如：白云山")
        transport = st.radio("交通方式", ["public", "driving"], key="form_transport", horizontal=True,
                             format_func=lambda v: "公共交通" if v == "public" else "自驾")
        a, b = st.columns(2)
        return_home = a.checkbox("计入返回出发地的交通费用", key="form_return")
        accessible = b.checkbox("有无障碍需求（需核实设施）", key="form_accessible")
    with details:
        st.markdown("**住宿与费用预算**")
        lodging = form_field(st, "text_area", "酒店或住宿地址（可选）", key="form_lodging", height=90, max_chars=1000,
            placeholder="单城可直接填完整酒店名称或地址。多城每行一项，例如：\n武汉：酒店完整名称（含分店）\n长沙：街道门牌地址")
        st.caption("填写后会先核对位置，再计算每天从住处出发和返回的路程；未填的城市按住宿区域（城市中心）估算。")
        st.caption("每项填一个预算金额即可。不填则自动估算；修改已有行程时，未填写的项目沿用当前估算。费用不代表实时价格。")
        rooms = form_field(st, "number_input", "房间数（0表示按每间2人估算）", key="form_rooms", min_value=0, max_value=10, step=1)
        for label, field in [("hotel", "每间每晚住宿"), ("food", "每人每天餐饮"), ("ticket", "每人每处门票")]:
            amount = form_field(st, "number_input", field + "预算 / 元", key=f"form_{label}_budget", min_value=0.0,
                                     max_value=1_000_000.0, value=None, placeholder="不填则自动估算")
    with details:
        st.markdown("**每天的出游时间**")
        st.caption("按旅行所在地时间计算。开始指从住处出发，结束指最后一个游玩项目结束；返回住处的交通另计，不支持跨午夜。")
        st.caption("默认按已到达目的地安排当地游玩。出发地往返交通单独估算，不占每日游玩时间；目的地之间的换城仍计入当天安排。")
        a, b = st.columns(2)
        daily_start = form_field(a, "time_input", "每天从住处出发", key="form_daily_start")
        daily_end = form_field(b, "time_input", "最后一个游玩项目结束", key="form_daily_end")

if submit:
    st.session_state.form_validation_started = True
checked = validate_form({key: st.session_state[key] for key in FORM_DEFAULTS}, previous, TODAY)
visible_errors = checked.errors if st.session_state.form_validation_started else {}
st.session_state.form_errors = visible_errors
for key, slot in field_slots.items():
    if key in visible_errors:
        slot.error("\n\n".join(visible_errors[key]))
    elif key in checked.notes:
        slot.caption("\n\n".join(checked.notes[key]))
if visible_errors:
    form_feedback.caption(f"有{len(visible_errors)}处信息需要调整，提示已标在对应输入框下方。")
if submit and not checked.errors:
    constraints = checked.constraints
    with st.spinner("正在查询地点、安排路线并检查预算…"):
        if previous and previous["status"] != "blocked":
            trip = request("POST", f"/trips/{previous['id']}/revise",
                {"base_version": previous["version"], "changes": constraints.model_dump(mode="json"), "autonomous": True})
        else:
            trip = request("POST", "/trips", {"constraints": constraints.model_dump(mode="json"), "strategy": strategy, "autonomous": True})
    if trip:
        st.session_state.trip = trip
        st.session_state.sync_form = trip["constraints"]
        st.rerun()

trip = st.session_state.trip
if trip:
    st.divider()
    st.subheader("你的旅行方案")
    st.caption("天气自动匹配行程涉及城市，仅展示今天起七天内已发布的预报；单独安排的往返交通，实际出行日期需另行确认。")
    with st.container(border=True):
        show_trip(trip, latest=True)
    show_assistant(trip)

else:
    st.markdown("""<div class="travel-intro-strip">
      <span class="travel-intro-symbol">✧</span><span class="travel-intro-copy">
      <strong>从灵感到路线，一页看清。</strong>
      <small>生成后，每日安排、预算明细、风险与来源会显示在这里。</small></span>
    </div>""", unsafe_allow_html=True)
    if health and health["mode"] == "demo":
        st.caption("离线演示仅支持广州、深圳、珠海；地点、路线和天气为示例，费用为估算。")
