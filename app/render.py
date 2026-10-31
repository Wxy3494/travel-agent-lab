import re

from app.presentation import day_description, hotel_details, issue_message, plain_text, weather_line


def escape(value):
    return re.sub(r"([\\`*_{}\[\]<>|])", r"\\\1", str(value)).replace("\n", " ")


def render_markdown(trip):
    labels = {"ready": "规划完成（估算方案）", "needs_input": "需要补充信息", "needs_review": "存在待处理约束",
              "blocked": "请求未执行", "failed": "本次规划失败"}
    lines = [f"# 旅行计划 · 第{trip.version}版", "", f"状态：{labels[trip.status]}", "",
             "数据模式：" + ("离线测试样例；未调用模型或真实地图" if trip.mode == "demo" else "真实接口；价格仍为估算"), ""]
    c = trip.constraints
    lines += ["## 需求摘要", "", f"路线：{escape(c.origin or '待填写')} → {' → '.join(map(escape, c.cities))}",
              f"日期：{c.start_date or '待填写'} · {c.days or '?'}天 · {c.people or '?'}人 · 全团预算 ¥{c.budget or '?'}", ""]
    if trip.questions:
        lines += ["## 待补充", ""] + [f"- {escape(q)}" for q in trip.questions] + [""]
    if c.daily_end_scope == "last_activity":
        lines += [f"出游时间：{c.daily_start:%H:%M}从住处出发，最后一个游玩项目在{c.daily_end:%H:%M}前结束；随后返程单独计算。", ""]
    for mapping in trip.name_normalizations:
        lines += [f"城市名称：{escape(mapping['submitted'])} → {escape(mapping['canonical'])}", ""]
    if c.bindings:
        lines += ["## 地点名称确认", ""] + [f"- {escape(b.query)} → {escape(b.canonical_name)}（{escape(b.city)}）；{escape(plain_text(b.basis))}"
                                             for b in c.bindings] + [""]
    if trip.revision_scope:
        lines += ["修改范围：" + escape(plain_text(trip.revision_scope["explanation"])), ""]
    for day in trip.days:
        lines += [f"## 第 {day.day} 天 · {day.date} · {escape(day.city)}", ""]
        if day.departure_note:
            lines += [escape(day.departure_note), ""]
        lines += [escape(day_description(day, trip.status == "ready")), "",
                  "| 时间 | 活动/交通 | 全团费用区间 | 来源 |", "|---|---|---:|---|"]
        for event in day.events:
            title = escape(event.title)
            if event.map_url:
                title = f"[{title}]({event.map_url})"
            sources = list(dict.fromkeys(plain_text(ev.provider) for ev in trip.evidence if ev.id in event.evidence_ids))
            lines.append(f"| {event.start}–{event.end} | {title} | ¥{event.cost.low:.2f}–{event.cost.high:.2f} | {'、'.join(sources)} |")
        if day.weather_details:
            lines += ["", *["天气：" + escape(weather_line(w)) +
                (f"；{escape(w.source)}；更新时间：{escape(w.updated_at or '服务未提供')}" if w.source else "") for w in day.weather_details]]
        else:
            lines += ["", f"天气：{escape(plain_text(day.weather))}"]
        lines += [f"备选：{escape(plain_text(day.alternative))}"]
        lines += [f"当日交通：{day.travel_minutes}分钟（含衔接缓冲）。"]
        if day.route_comparison.get("before_minutes") is not None:
            lines += [f"路线耗时比较：{day.route_comparison['before_minutes']} → {day.route_comparison['after_minutes']}分钟；简单邻近候选比较，非全局最优。"]
        for event in day.events:
            lines += [f"- {escape(event.title)}：{escape(plain_text(segment))}" for segment in event.route_segments]
        if day.overnight_city:
            lines += [f"住宿城市：{escape(day.overnight_city)}；位置候选仅供查询，房价与可售状态未验证。"]
            for p in day.hotel_options:
                hotel = hotel_details(p, c, day.date)
                lines += [f"- **{escape(hotel['name'])}**：[查看地图]({hotel['map_url']}) · [搜索房价]({hotel['search_url']})",
                          f"  {hotel['budget_label']}；{hotel['price_note']}"]
        lines.append("")
    if trip.budget:
        b = trip.budget
        lines += ["## 预算拆分（人民币，全团）", "", "| 项目 | 数量 | 单价区间 | 合计区间 |",
                  "|---|---:|---:|---:|"]
        for item in b.items:
            lines.append(f"| {escape(item.category)} | {item.quantity} {item.unit} | ¥{item.unit_price.low:.2f}–{item.unit_price.high:.2f} | ¥{item.total.low:.2f}–{item.total.high:.2f} |")
        lines += ["", f"**总计：¥{b.total.low:.2f}–{b.total.high:.2f}**", "", escape(c.pricing_note), ""]
    if trip.issues:
        lines += ["## 风险与未满足约束", ""] + [f"- {escape(issue_message(i))}" for i in trip.issues] + [""]
    if trip.adjustments:
        lines += ["## 可选择的调整（尚未应用）", ""] + [f"- {escape(a.title)}：{escape(a.impact)}" for a in trip.adjustments] + [""]
    lines += ["## 来源与数据类型", ""]
    for ev in trip.evidence:
        label = f"{plain_text(ev.kind)} · {escape(plain_text(ev.provider))}"
        lines += [f"- {label}：{escape(plain_text(ev.description))}；查询/记录时间 {ev.fetched_at}" +
                  (f"；[来源说明]({ev.url})" if ev.url else "")]
    lines += ["", "本系统只提供规划；票价、住宿、开放时间及实际班次请到官方平台确认。"]
    return "\n".join(lines)
