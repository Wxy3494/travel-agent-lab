"""Isolated product UI QA with synthetic history and reference content."""
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.schemas import TravelInfo, TripResult
from app.storage import Repository
from app.travel_references import parse_references, targets_for
from scripts.check_form_browser import free_port
from scripts.desktop_launcher import ROOT, Launcher


def check():
    sys.path.insert(0, str(ROOT / ".local/browser-qa"))
    from playwright.sync_api import expect, sync_playwright
    out = ROOT / "artifacts/navigation-20261101"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"browser-{uuid.uuid4().hex}.sqlite3"
    launcher = Launcher(api_port=free_port(), ui_port=free_port(), env={"TRAVEL_MODE": "demo",
        "DEEPSEEK_API_KEY": "", "AMAP_API_KEY": "", "MEITUAN_TRAVEL_ENABLED": "false", "TRAVEL_DB": str(path)})
    report = {"passed": False, "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "scope": "installed Chrome, isolated demo API and synthetic fixtures; external navigation popup intercepted, native app not tested", "checks": []}
    try:
        launcher.ensure_running()
        with httpx.Client(base_url=f"http://127.0.0.1:{launcher.api_port}", trust_env=False, timeout=30) as api:
            start = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
            trip = TripResult.model_validate(api.post("/trips", json={"constraints": {"origin": "广州", "cities": ["广州"],
                "start_date": str(start), "days": 2, "people": 2, "budget": 6000}}).json())
            targets = targets_for(trip)
            attraction = next(t for t in targets if t["kind"] == "attraction")
            hotel = next(t for t in targets if t["kind"] == "hotel")
            content = f"### {attraction['name']}\n开放时间09:00-17:00，演示入场参考。\n\n### {hotel['name']}\n演示标准间¥77/晚，非真实报价。"
            trip.travel_info = TravelInfo(status="success", content=content, references=parse_references(content, targets))
            travel = next(e for day in trip.days for e in day.events if e.kind == "travel")
            from app.schemas import RouteStep
            travel.route_steps = [RouteStep(kind="walk", distance=1549, start_location="113,23", end_location="113.01,23.01"),
                RouteStep(kind="walk", distance=145, start_location="113.01,23.01", end_location="113.02,23.02")]
            repo = Repository(path)
            for n in range(24):
                row = trip.model_copy(deep=True)
                row.id = f"browser-history-{n}"
                row.constraints.start_date += timedelta(days=n + 1)
                repo.save(row)
            trip.version = 2
            repo.save(trip, expected_version=1)
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, executable_path=str(Path("C:/Program Files/Google/Chrome/Application/chrome.exe")))
                report["browser"] = browser.version
                page = browser.new_page(viewport={"width": 1363, "height": 871}, locale="zh-CN")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(launcher.url)
                recent = page.locator('[data-testid="stSidebar"] [class*="st-key-recent_"]')
                expect(recent).to_have_count(5)
                expect(page.get_by_test_id("stApp")).to_have_attribute("data-test-script-state", "notRunning")
                expect(page.get_by_text("规划策略", exact=True)).to_have_count(0)
                expect(page.get_by_label("行程编号", exact=True)).to_have_count(0)
                recent.first.get_by_role("button").click()
                expect(page.get_by_role("button", name="重新生成行程", exact=True)).to_be_visible()
                expect(page.locator('[data-testid="stJson"]')).to_have_count(0)
                page.get_by_test_id("stSidebar").screenshot(path=str(out / "recent-history.png"))
                report["checks"].append("five latest readable history buttons; no ID, strategy or JSON controls")
                first_day = page.get_by_test_id("stExpander").filter(has=page.locator("summary", has_text="第 1 天"))
                first_day.locator("summary").click()
                expect(first_day.get_by_text("演示入场参考", exact=False)).to_be_visible()
                expect(first_day.get_by_text("演示标准间", exact=False)).to_be_visible()
                expect(first_day.get_by_text("步行共 1694 米", exact=False)).to_be_visible()
                link = first_day.get_by_role("link", name="步行导航", exact=True)
                href = link.get_attribute("href")
                assert parse_qs(urlsplit(href).query)["mode"] == ["walk"]
                link.evaluate("e=>e.scrollIntoView({block:'center',behavior:'instant'})")
                page.screenshot(path=str(out / "walking-navigation.png"))
                page.context.route("https://uri.amap.com/navigation**", lambda route: route.fulfill(
                    status=200, content_type="text/html", body="<html><body>navigation test</body></html>"))
                with page.expect_popup() as opened:
                    link.click()
                popup = opened.value
                popup.wait_for_load_state()
                assert popup.url == href
                popup.close()
                expect(page.get_by_text("来源说明", exact=True)).to_have_count(0)
                expect(page.get_by_text("项目预算假设，非实时价格；请用自己的报价调整", exact=True)).to_have_count(0)
                report["checks"].append("adjacent walks merge; walking link popup preserves endpoints and walk mode; no raw source dump")
                expect(page.get_by_text("美团景点与门票参考", exact=True)).to_have_count(0)
                first_day.get_by_text("演示标准间", exact=False).scroll_into_view_if_needed()
                expect(page.get_by_test_id("stApp")).to_have_attribute("data-test-script-state", "notRunning")
                page.screenshot(path=str(out / "integrated-references.png"))
                report["checks"].append("attraction and hotel reference content appears inside corresponding day")
                page.get_by_role("button", name="查看全部历史（25）", exact=True).click()
                dialog = page.get_by_role("dialog")
                expect(dialog.get_by_text("共25条旅行记录", exact=False)).to_be_visible()
                dialog.get_by_role("button", name="下一页", exact=True).click()
                expect(dialog.get_by_text("第2页", exact=False)).to_be_visible()
                dialog.screenshot(path=str(out / "all-history-page2.png"))
                dialog.get_by_label("按城市或日期查找", exact=True).fill(str(start))
                dialog.get_by_label("按城市或日期查找", exact=True).press("Tab")
                expect(dialog.get_by_text("共1条旅行记录", exact=False)).to_be_visible()
                dialog.get_by_role("combobox", name="选择旅行", exact=True).click()
                page.get_by_role("option").first.click()
                dialog.get_by_role("button", name="打开选中旅行", exact=True).click()
                expect(page.get_by_role("dialog")).to_have_count(0)
                expect(page.locator('.st-key-field_form_date input[type="date"]')).to_have_value(str(start))
                report["checks"].append("all 25 histories paginate, search and restore without remembering IDs")
                assert not errors, errors
                report["checks"].append("no browser JavaScript errors")
                report["passed"] = True
                browser.close()
    finally:
        for _, process in reversed(launcher.processes):
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        report["screenshots"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob("*.png")}
        (out / "browser-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    check()
