"""Form-first travel planning with optional text revisions."""
import base64
import json
import os
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import streamlit as st
from dotenv import load_dotenv
from pydantic import ValidationError

from app.config import client_read_timeout
from app.presentation import day_description, hotel_details, issue_message, plain_text, weather_line
from app.render import escape, render_markdown
from app.schemas import MAX_TRIP_DAYS, Constraints, TripResult
from app.service import blocked_input

load_dotenv()
API = os.getenv("TRAVEL_API_URL", "http://127.0.0.1:8000").rstrip("/")
st.set_page_config(page_title="行旅 · 旅行规划", page_icon="🧭", layout="wide",
                   initial_sidebar_state="collapsed")
TODAY = datetime.now(ZoneInfo("Asia/Shanghai")).date()
ASSETS = Path(__file__).with_name("assets")
HERO_IMAGE = "data:image/jpeg;base64," + base64.b64encode(
    (ASSETS / "coastal-evening.jpg").read_bytes()).decode("ascii")
st.markdown("<style>" + Path(__file__).with_name("ui_theme.css").read_text(encoding="utf-8") + "</style>",
            unsafe_allow_html=True)

FORM_DEFAULTS = {
    "form_origin": "", "form_destinations": "", "form_date": None, "form_days": None,
    "form_people": None, "form_budget": None, "form_interests": "", "form_must": "",
    "form_excluded": "", "form_transport": "public", "form_return": False, "form_accessible": False,
    "form_rooms": 0, "form_hotel_budget": None, "form_food_budget": None, "form_ticket_budget": None,
    "form_price_ranges": {},
    "form_lodging": "",
    "form_pricing_note": "项目预算假设，非实时价格；请用自己的报价调整",
    "form_daily_start": time(9), "form_daily_end": time(21, 30),
}


def split_names(value):
    return [v.strip() for v in value.replace("，", ",").replace("、", ",").split(",") if v.strip()]


def lodging_inputs(text, cities):
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.replace("：", ":", 1).split(":", 1)
        if len(parts) == 2:
            city, address = (p.strip() for p in parts)
        elif len(cities) == 1:
            city, address = cities[0], line
        else:
            raise ValueError("多个城市请每行填写“城市：完整酒店名称或地址”。")
        if city in result:
            raise ValueError(f"{city}重复填写了住处，请每个城市保留一个住宿地址。")
        result[city] = address
    return result


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
    if field == "place_choices":
        return "；".join(query.split(":", 1)[-1] + "：已确认具体地点" for query in value)
    if field == "day_place_ids":
        return "；".join(f"第{day}天：已指定{len(ids)}处活动（详见下方日程差异）" for day, ids in value.items())
    if field == "opening_rules":
        return "；".join(f"{r['on_date']}：{'闭馆' if r['closed'] else (r['start'][:5] + '～' + r['end'][:5])}；{r['source']}" for r in value)
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


def show_trip(trip, latest):
    labels = {"ready": "方案已生成", "needs_review": "行程还需要调整", "needs_input": "还需要补充一些信息",
              "blocked": "这项请求无法执行", "failed": "这次规划未完成"}
    st.markdown(f"**{labels[trip['status']]}** · 第{trip['version']}版")
    for q in trip["questions"]:
        st.write(plain_text(q))
    if trip["status"] == "needs_input":
        st.caption("请在上方补充缺少的信息，再生成行程；也可以用下方文字输入框补充。")
    for issue in trip["issues"]:
        if issue["severity"] == "error":
            st.warning(issue_message(issue))
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
        total = trip["budget"]["total"] if trip["budget"] else None
        if total:
            st.markdown(f"全团估算 **¥{total['low']:,.0f} – {total['high']:,.0f}** · 预算 ¥{c['budget']:,.0f}")
        nights = Counter(d["overnight_city"] for d in trip["days"] if d["overnight_city"])
        if nights:
            st.caption("住宿：" + " · ".join(f"{city}{count}晚" for city, count in nights.items()))
        for day in trip["days"]:
            with st.expander(f"第 {day['day']} 天 · {day['date']} · {day['city']}", expanded=False):
                for weather in day.get("weather_details", []):
                    st.markdown("**天气 · " + escape(weather_line(weather)) + "**")
                    if weather.get("source"):
                        st.caption(weather["source"] + " · 更新时间：" + (weather.get("updated_at") or "服务未提供"))
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
                    st.caption(f"¥{e['cost']['low']:.0f}–{e['cost']['high']:.0f} / 全团 · {plain_text(e['note'])}")
                    for segment in e.get("route_segments", []):
                        st.caption(plain_text(segment))
                if not day.get("weather_details"):
                    st.write(f"天气：{plain_text(day['weather'])}")
                st.write(f"备选：{plain_text(day['alternative'])}")
                if day["overnight_city"]:
                    st.write(f"当晚住在{day['overnight_city']}；填写的住处用于计算路程，未填写时按住宿区域估算。以下酒店仅供参考，房价和房源需另行确认。")
                    for h in day["hotel_options"]:
                        hotel = hotel_details(h, c, day["date"])
                        st.markdown(f"**{escape(hotel['name'])}** · [查看地图]({hotel['map_url']}) · [搜索房价]({hotel['search_url']})")
                        st.caption(hotel["budget_label"])
                        st.caption(hotel["price_note"])
    if latest and trip["days"]:
        with st.expander("预算明细、风险与来源"):
            if trip["budget"]:
                st.dataframe([{"项目": i["category"], "数量": f"{i['quantity']} {i['unit']}",
                    "下限 / 元": i["total"]["low"], "上限 / 元": i["total"]["high"]}
                    for i in trip["budget"]["items"]], hide_index=True, width="stretch")
                st.caption(c["pricing_note"])
            for issue in trip["issues"]:
                st.write("• " + issue_message(issue))
            st.divider()
            for ev in trip["evidence"]:
                st.caption(f"{plain_text(ev['kind'])} · {plain_text(ev['provider'])} · {ev['fetched_at']}")
                st.write(plain_text(ev["description"]))
                if ev["url"]:
                    st.link_button("来源说明", ev["url"])
        st.download_button("下载行程文档", render_markdown(TripResult.model_validate(trip)),
            file_name=f"trip-{trip['id']}-v{trip['version']}.md", mime="text/markdown",
            key=f"download-{trip['id']}-{trip['version']}")
        st.caption("修改上方表单或在下方输入修改要求，都将生成新版本并保留历史结果。")


st.session_state.setdefault("trip", None)
st.session_state.setdefault("revision_notes", [])
form_initialized = "form_origin" in st.session_state
for key, value in FORM_DEFAULTS.items():
    st.session_state.setdefault(key, value)
if not form_initialized and st.session_state.trip:
    sync_form(st.session_state.trip["constraints"])
if "sync_form" in st.session_state:
    sync_form(st.session_state.pop("sync_form"))
    st.session_state.revision_instruction = ""

with st.sidebar:
    st.markdown("### 🧭 行旅")
    if st.button("＋ 开始新的旅行", width="stretch"):
        st.session_state.update(FORM_DEFAULTS)
        st.session_state.revision_notes = []
        st.session_state.trip = None
        st.session_state.pop("weather_report", None)
        st.rerun()
    health = request("GET", "/health")
    if health:
        st.session_state.api_timeout = health.get("request_timeout_seconds")
        st.caption("真实接口 · 价格为估算" if health["mode"] == "live" else "离线演示 · 人工示例数据")
        if health["mode"] == "demo":
            st.caption("支持广州、深圳、珠海的基础需求；没有调用模型或真实地图。")
    with st.expander("历史旅行列表"):
        history_query = st.text_input("按城市或日期查找", key="history_query")
        entries = request("GET", "/trips?query=" + quote(history_query, safe="")) or []
        picked = st.selectbox("选择旅行", entries, index=None,
                              format_func=lambda r: f"{r['title']} · 第{r['version']}版 · {r['id'][:8]}")
        if st.button("打开选中旅行", disabled=picked is None):
            loaded = request("GET", f"/trips/{picked['id']}")
            if loaded:
                st.session_state.trip = loaded
                st.session_state.sync_form = loaded["constraints"]
                st.rerun()
    with st.expander("找回行程与历史版本"):
        trip_id = st.text_input("行程编号")
        version = st.number_input("版本（0为最新）", min_value=0, step=1)
        if st.button("读取行程") and trip_id:
            suffix = f"?version={version}" if version else ""
            trip = request("GET", f"/trips/{quote(trip_id, safe='')}{suffix}")
            if trip:
                st.session_state.trip = trip
                st.session_state.sync_form = trip["constraints"]
                st.session_state.revision_notes = []
                st.rerun()
    with st.expander("执行信息与开发设置"):
        strategy = st.selectbox("规划策略", ["multi", "single"],
            format_func=lambda s: "三智能体" if s == "multi" else "单智能体基线")
        trip = st.session_state.trip
        if trip:
            st.caption(f"行程编号：{trip['id']}")
            st.json({"metrics": trip["metrics"], "trace": trip["trace"]})
            st.download_button("下载结果数据", json.dumps(trip, ensure_ascii=False, indent=2),
                file_name=f"trip-{trip['id']}-v{trip['version']}.json", mime="application/json")

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

with st.container(key="planning_section"), st.form("trip_form", border=True):
    st.markdown('<div class="travel-form-kicker">规划你的旅行</div>', unsafe_allow_html=True)
    st.subheader("开启一段旅行")
    st.caption("下面六项为必填信息。多城旅行按目的地顺序安排。")
    a, b, c = st.columns(3)
    origin = a.text_input("出发地", key="form_origin", placeholder="例如：广州", max_chars=60)
    destinations = b.text_input("目的地（按顺序，用逗号分隔）", key="form_destinations",
                                placeholder="例如：广州，深圳", max_chars=185)
    start = c.date_input("出发日期", key="form_date", value=None,
                         min_value=min(TODAY, st.session_state.form_date or TODAY), format="YYYY/MM/DD")
    a, b, c = st.columns(3)
    days = a.number_input("旅行天数", key="form_days", min_value=1, max_value=MAX_TRIP_DAYS,
                          value=None, step=1, placeholder="1～30天")
    people = b.number_input("随行人数", key="form_people", min_value=1, max_value=10,
                            value=None, step=1, placeholder="1～10人")
    budget = c.number_input("全团总预算（人民币 / 元）", key="form_budget", min_value=1.0,
                            max_value=1_000_000.0, value=None, step=100.0, placeholder="例如：6000")
    previous = st.session_state.trip
    hint_col, submit_col = st.columns([3, 1], vertical_alignment="bottom")
    hint_col.caption("还有偏好或时间要求？可展开填写下方选项。")
    submit = submit_col.form_submit_button(
        "重新生成行程" if previous and previous["status"] != "blocked" else "生成旅行计划",
        type="primary", width="stretch")
    details = st.expander("＋ 添加旅行偏好与详细要求（可选）")
    with details:
        st.markdown("**兴趣与出行偏好**")
        interests = st.text_input("兴趣（逗号分隔）", key="form_interests", placeholder="博物馆，公园，美食")
        a, b = st.columns(2)
        must = a.text_input("必去地点", key="form_must", placeholder="例如：广州塔")
        excluded = b.text_input("排除地点", key="form_excluded", placeholder="例如：白云山")
        transport = st.radio("交通方式", ["public", "driving"], key="form_transport", horizontal=True,
                             format_func=lambda v: "公共交通" if v == "public" else "自驾")
        a, b = st.columns(2)
        return_home = a.checkbox("计入最后一天返回出发地的交通", key="form_return")
        accessible = b.checkbox("有无障碍需求（需核实设施）", key="form_accessible")
    with details:
        st.markdown("**住宿与费用预算**")
        lodging = st.text_area("酒店或住宿地址（可选）", key="form_lodging", height=90, max_chars=1000,
            placeholder="单城可直接填完整酒店名称或地址。多城每行一项，例如：\n武汉：酒店完整名称（含分店）\n长沙：街道门牌地址")
        st.caption("填写后会先核对位置，再计算每天从住处出发和返回的路程；未填的城市按住宿区域（城市中心）估算。")
        st.caption("每项填一个预算金额即可。不填则自动估算；修改已有行程时，未填写的项目沿用当前估算。费用不代表实时价格。")
        rooms = st.number_input("房间数（0表示按每间2人估算）", key="form_rooms", min_value=0, max_value=10, step=1)
        prices = {}
        for label, field in [("hotel", "每间每晚住宿"), ("food", "每人每天餐饮"), ("ticket", "每人每处门票")]:
            amount = st.number_input(field + "预算 / 元", key=f"form_{label}_budget", min_value=0.0,
                                     max_value=1_000_000.0, value=None, placeholder="不填则自动估算")
            if amount is not None:
                prices[label] = {"low": amount, "high": amount}
            elif label in st.session_state.form_price_ranges:
                prices[label] = st.session_state.form_price_ranges[label]
        pricing_note = st.session_state.form_pricing_note
        if any(st.session_state[f"form_{label}_budget"] is not None for label in ("hotel", "food", "ticket")):
            pricing_note = "住宿、餐饮与门票按填写的预算或现有估算计算，非实时价格；实际费用可能变化。"
    with details:
        st.markdown("**每天的出游时间**")
        st.caption("按旅行所在地时间计算。开始指从住处出发，结束指最后一个游玩项目结束；返回住处的交通另计，不支持跨午夜。")
        a, b = st.columns(2)
        daily_start = a.time_input("每天从住处出发", key="form_daily_start")
        daily_end = b.time_input("最后一个游玩项目结束", key="form_daily_end")
if submit:
    try:
        previous_constraints = previous["constraints"] if previous else {}
        advanced = {key: st.session_state.trip["constraints"].get(key, {}) for key in
                    ["anchors", "day_place_ids", "place_choices", "indoor_sources"]} if st.session_state.trip else {}
        advanced["opening_rules"] = st.session_state.trip["constraints"].get("opening_rules", []) if st.session_state.trip else []
        rules = [r for r in previous_constraints.get("day_rules", []) if days and r["day"] <= days]
        constraints = Constraints(origin=origin.strip() or None, cities=split_names(destinations), start_date=start,
            days=days, people=people, budget=budget, interests=split_names(interests), must_visit=split_names(must),
            excluded=split_names(excluded), transport=transport, return_to_origin=return_home,
            accessibility_required=accessible, rooms=rooms or None, pricing_note=pricing_note,
            arrival_time=None, departure_time=None, daily_start=daily_start, daily_end=daily_end,
            daily_end_scope="last_activity", lodging_addresses=lodging_inputs(lodging, split_names(destinations)),
            visit_minutes=previous_constraints.get("visit_minutes", 90), day_rules=rules, **advanced,
            **{field: prices[label] for label, field in [("hotel", "hotel_per_room_night"),
                ("food", "food_per_person_day"), ("ticket", "ticket_per_person_visit")] if label in prices})
        missing = constraints.missing()
        if missing:
            st.warning("请补充：" + "、".join(missing))
        else:
            with st.spinner("正在查询地点、安排路线并检查预算…"):
                if previous and previous["status"] != "blocked":
                    trip = request("POST", f"/trips/{previous['id']}/revise",
                        {"base_version": previous["version"], "changes": constraints.model_dump(mode="json")})
                else:
                    trip = request("POST", "/trips", {"constraints": constraints.model_dump(mode="json"), "strategy": strategy})
            if trip:
                st.session_state.trip = trip
                st.session_state.sync_form = trip["constraints"]
                st.rerun()
    except ValidationError:
        st.warning("请检查输入：城市不能重复，天数不能少于城市数量，必去与排除不能冲突，房间数不能超过人数；活动时间须在同一天内，指定日序不能超过旅行天数。")
    except ValueError as exc:
        st.warning(str(exc))

trip = st.session_state.trip
if trip:
    st.divider()
    st.subheader("你的旅行方案")
    st.caption("天气自动匹配每天预计所在城市，仅展示今天起七天内已发布的预报；跨城当天分别显示出发和抵达城市。")
    with st.container(border=True):
        show_trip(trip, latest=True)
    if trip.get("revision_scope"):
        st.caption(plain_text(trip["revision_scope"]["explanation"]))
    if trip.get("clarifications"):
        with st.form("confirm_places"):
            st.subheader("确认具体地点")
            confirmed = {}
            for q in trip["clarifications"]:
                st.write(q["query"] + "：" + plain_text(q["reason"]))
                if q["options"]:
                    choice = st.selectbox(q["query"] + "的候选地点", q["options"], index=None, key="confirm_" + q["key"],
                        format_func=lambda p: f"{p['city']} · {p['name']} · {p.get('address', '')}")
                    if choice:
                        confirmed[q["key"]] = choice["id"]
            if st.form_submit_button("确认地点并继续规划", type="primary"):
                if len(confirmed) == len(trip["clarifications"]):
                    revise_choice(trip, {"place_choices": {**trip["constraints"].get("place_choices", {}), **confirmed}})
                else:
                    st.warning("请选择每个候选；查无结果或名称冲突时，请先修改上方必去/排除地点。")
    if trip.get("adjustments"):
        with st.expander("选择一个调整方案", expanded=True):
            st.caption("点击后才应用该方案并保存新版本；不会自动更改人数、必去地点或单价。")
            for adjustment in trip["adjustments"]:
                st.write(adjustment["title"])
                st.caption(adjustment["impact"])
                if st.button("选择此方案：" + adjustment["title"], key="adjust_" + adjustment["id"]):
                    revise_choice(trip, adjustment_id=adjustment["id"])
    with st.expander("版本、对比与恢复"):
        versions = request("GET", f"/trips/{trip['id']}/versions") or []
        picked_version = st.selectbox("查看版本", [v["version"] for v in versions],
                                      format_func=lambda v: f"第{v}版", key="history_version")
        a, b = st.columns(2)
        if a.button("查看所选版本") and picked_version:
            accept_trip(request("GET", f"/trips/{trip['id']}?version={picked_version}"))
        if b.button("读取最新版"):
            accept_trip(request("GET", f"/trips/{trip['id']}"))
        if st.button("从当前版本复制为新旅行"):
            accept_trip(request("POST", f"/trips/{trip['id']}/clone", {"version": trip["version"]}))
        if st.button("与当前版本对比") and picked_version:
            diff = request("GET", f"/trips/{trip['id']}/compare?from_version={picked_version}&to_version={trip['version']}")
            if diff:
                st.write(f"第{diff['from_version']}版 → 第{diff['to_version']}版")
                st.write("全团费用估算：" + constraint_text("budget", diff["budget_before"]) + " → " +
                         constraint_text("budget", diff["budget_after"]))
                labels = {"origin": "出发地", "cities": "目的地", "start_date": "出发日期", "days": "旅行天数",
                    "people": "人数", "budget": "全团总预算", "interests": "兴趣", "must_visit": "必去地点", "excluded": "排除地点",
                    "transport": "交通方式", "return_to_origin": "计入返程", "accessibility_required": "无障碍需求", "rooms": "房间数",
                    "hotel_per_room_night": "每间每晚住宿预算", "food_per_person_day": "每人每天餐饮预算",
                    "ticket_per_person_visit": "每人每处门票预算", "pricing_note": "费用说明", "arrival_time": "首日到达",
                    "departure_time": "末日离开", "daily_start": "每日开始", "daily_end": "每日结束", "visit_minutes": "每处游览分钟数",
                    "day_rules": "分日时段与节奏", "opening_rules": "开放与闭馆说明", "place_choices": "候选确认",
                    "bindings": "地点名称映射", "anchors": "酒店与车站位置", "day_place_ids": "指定活动", "optimize_routes": "路线顺序比较",
                    "indoor_sources": "室内属性依据"}
                labels.update(lodging_addresses="酒店或住宿地址", daily_end_scope="结束时间含义")
                rows = [{"需求项目": labels.get(r["field"], "其他需求"), "修改前": constraint_text(r["field"], r["before"]),
                         "修改后": constraint_text(r["field"], r["after"])} for r in diff["constraints"]]
                if rows:
                    st.dataframe(rows, hide_index=True, width="stretch")
                else:
                    st.caption("两版本的旅行需求没有变化。")
                for changed_day in diff["days"]:
                    st.write(f"第{changed_day['day']}天的变化")
                    rows = [{"版本": label, "时间": f"{e['start']}–{e['end']}", "活动": e["title"],
                             "费用": f"{e['cost']['low']}–{e['cost']['high']}"}
                            for label, data in [("之前", changed_day["before"]), ("之后", changed_day["after"])]
                            if data for e in data["events"]]
                    st.dataframe(rows, hide_index=True, width="stretch")
    with st.expander("酒店、车站与开放信息（可选）"):
        st.caption("选择已查询的地点后参与实际接驳算路。演示地点不是酒店房源或实际车次。开放/室内说明由你提供，系统不会冒充已核实。")
        saved_anchors = trip["constraints"].get("anchors", {})
        if saved_anchors:
            st.write("当前接驳位置", saved_anchors)
            if st.button("清除已选酒店与车站，按城市中心重新估算"):
                revise_choice(trip, {"anchors": {}})
        search_city = st.text_input("查询城市", value=trip["constraints"]["cities"][0] if trip["constraints"]["cities"] else "广州")
        category = st.selectbox("地点类型", ["hotel", "station", "attraction"],
                                format_func=lambda k: {"hotel": "酒店", "station": "车站", "attraction": "景点"}[k])
        search_query = st.text_input("地点名称", placeholder="输入酒店、车站或景点名称")
        if st.button("查询地点候选") and search_city.strip() and search_query.strip():
            st.session_state.point_search = request("GET", f"/places/search?city={quote(search_city)}&query={quote(search_query)}&category={category}")
        found = st.session_state.get("point_search") or {}
        options = found.get("options", [])
        point = st.selectbox("选择已查询地点", options, index=None,
                             format_func=lambda p: f"{p['city']} · {p['name']} · {p.get('address', '')}")
        if found and not options:
            st.info("没有查到符合城市与类型的地点，请补充名称后重新查询。")
        if point and point["category"] in {"hotel", "station"}:
            purpose = st.selectbox("位置用途", ["hotel_id"] if point["category"] == "hotel" else
                ["arrival_station_id", "departure_station_id"],
                format_func=lambda k: {"hotel_id": "当日出发与住宿位置", "arrival_station_id": "到达车站", "departure_station_id": "出发车站"}[k])
            if st.button("使用此位置重新算路"):
                anchors = trip["constraints"].get("anchors", {})
                changes = {"anchors": {**anchors, point["city"]: {**anchors.get(point["city"], {}), purpose: point["id"]}}}
                if purpose == "hotel_id":
                    changes["lodging_addresses"] = {city: address for city, address in trip["constraints"].get("lodging_addresses", {}).items()
                                                    if city != point["city"]}
                revise_choice(trip, changes)
        if point and point["category"] == "attraction":
            with st.form("opening_information"):
                st.write(point["name"])
                source = st.text_input("开放或室内属性的依据说明", max_chars=300)
                indoor = st.checkbox("我根据上述依据确认该地点有室内活动空间")
                apply_opening = st.checkbox("同时设置该地点某日的开放时间")
                on_date = st.date_input("适用日期", value=date.fromisoformat(trip["constraints"]["start_date"]) if trip["constraints"]["start_date"] else TODAY)
                closed = st.checkbox("该日闭馆")
                a, b = st.columns(2)
                opened = a.time_input("开放开始", value=time(9))
                closing = b.time_input("开放结束", value=time(17))
                if st.form_submit_button("保存提供的信息并重新核验"):
                    if not source.strip() or not (indoor or apply_opening):
                        st.warning("请填写依据并选择需要设置的信息。")
                    else:
                        changes = {}
                        if indoor:
                            changes["indoor_sources"] = {**trip["constraints"].get("indoor_sources", {}), point["id"]: source.strip()}
                        if apply_opening:
                            rules = [r for r in trip["constraints"].get("opening_rules", [])
                                     if not (r["place_id"] == point["id"] and r["on_date"] == str(on_date))]
                            rules.append({"place_id": point["id"], "on_date": str(on_date), "closed": closed,
                                          "start": opened.isoformat(), "end": closing.isoformat(), "source": source.strip()})
                            changes["opening_rules"] = rules
                        revise_choice(trip, changes)
    if trip["days"]:
        with st.expander("分日修改与雨天替换"):
            st.caption("选择修改范围并重新核验；未影响日期会保留。全局约束变化时会说明扩大重算范围。")
            selected_day = st.selectbox("选择修改哪一天", trip["days"], format_func=lambda d: f"第{d['day']}天 · {d['date']} · {d['city']}")
            all_visits = [e for d in trip["days"] for e in d["events"] if e["kind"] == "visit"]
            protected = {b["place_id"] for b in trip["constraints"].get("bindings", []) if b["field"] == "must_visit"}
            visits = [e for e in selected_day["events"] if e["kind"] == "visit" and e["place_id"] not in protected]
            used_ids = {e["place_id"] for e in all_visits}
            replacement_options = [p for p in selected_day.get("indoor_options", []) if p["id"] not in used_ids]
            old = st.selectbox("要替换的可选活动", visits, index=None, format_func=lambda e: e["title"])
            new = st.selectbox("有依据的室内候选", replacement_options, index=None,
                               format_func=lambda p: p["name"] + " · " + ("用户提供属性" if p.get("indoor_evidence_id") else "演示数据分类"))
            if st.button("应用室内替换并重新算路", disabled=not (old and new)):
                day_ids = [new["id"] if e["place_id"] == old["place_id"] else e["place_id"]
                           for e in selected_day["events"] if e["kind"] == "visit"]
                revise_choice(trip, {"day_place_ids": {**trip["constraints"].get("day_place_ids", {}), selected_day["day"]: day_ids}},
                              target_day=selected_day["day"])
            with st.form("scoped_time"):
                period = st.selectbox("修改活动范围", ["day", "afternoon"], format_func=lambda p: "整天" if p == "day" else "只修改下午")
                a, b = st.columns(2)
                scope_start = a.time_input("修改后的活动开始", value=time(13))
                scope_end = b.time_input("修改后的活动结束", value=time(18))
                if st.form_submit_button("应用分日时间修改"):
                    old_rules = trip["constraints"].get("day_rules", [])
                    current_rule = next((r for r in old_rules if r["day"] == selected_day["day"]), None)
                    rules = [r for r in old_rules if r["day"] != selected_day["day"]]
                    rule = {"day": selected_day["day"], "start": scope_start.isoformat(),
                            "end": scope_end.isoformat(), "period": period}
                    if current_rule and current_rule.get("visit_minutes") is not None:
                        rule["visit_minutes"] = current_rule["visit_minutes"]
                    rules.append(rule)
                    revise_choice(trip, {"day_rules": rules}, target_day=selected_day["day"], target_period=period)
    with st.container(border=True):
        st.subheader("进一步修改")
        st.caption("基于已生成的方案，只改你明确要求的内容。上方表单的改动请点击“重新生成行程”。")
        with st.form("text_revision", border=False):
            instruction = st.text_area("修改要求", placeholder="例如：预算改成3000元；排除广州塔。",
                                       max_chars=3000, height=80, key="revision_instruction")
            revise = st.form_submit_button("提交修改", type="primary")
    if st.session_state.revision_notes:
        with st.expander("文字修改记录"):
            for item in st.session_state.revision_notes:
                st.write(item)
else:
    st.markdown("""<div class="travel-intro-strip">
      <span class="travel-intro-symbol">✧</span><span class="travel-intro-copy">
      <strong>从灵感到路线，一页看清。</strong>
      <small>生成后，每日安排、预算明细、风险与来源会显示在这里。</small></span>
    </div>""", unsafe_allow_html=True)
    if health and health["mode"] == "demo":
        st.caption("离线演示仅支持广州、深圳、珠海；地点、路线和天气为示例，费用为估算。")
    revise = False

if revise:
    prompt = instruction.strip()
    previous = st.session_state.trip
    if prompt:
        with st.spinner("正在根据修改要求重新规划…"):
            if previous["status"] != "blocked":
                updated = request("POST", f"/trips/{previous['id']}/revise",
                    {"base_version": previous["version"], "instruction": prompt})
            else:
                updated = request("POST", "/trips", {"prompt": prompt, "strategy": strategy})
        if updated:
            st.session_state.trip = updated
            st.session_state.sync_form = updated["constraints"]
            note = "输入含敏感信息或越权操作，原文未保留" if blocked_input(prompt) else prompt
            st.session_state.revision_notes.append(f"第{updated['version']}版：{note}")
            st.rerun()
    else:
        st.warning("请先输入要修改的内容。")
