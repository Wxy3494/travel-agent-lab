"""Optional isolated Chrome/Edge QA. Requires Playwright, never uses live providers."""
import hashlib
import json
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from scripts.desktop_launcher import ROOT, Launcher


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def check():
    local = ROOT / ".local/browser-qa"
    if local.is_dir():
        sys.path.insert(0, str(local))
    from playwright.sync_api import expect, sync_playwright

    executable = next((p for p in [Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")] if p.is_file()), None)
    if not executable:
        raise RuntimeError("Install Chrome or Edge before running this optional browser check")
    out = ROOT / "artifacts/field-errors-20261101"
    out.mkdir(parents=True, exist_ok=True)
    launcher = Launcher(api_port=free_port(), ui_port=free_port(), env={"TRAVEL_MODE": "demo",
        "DEEPSEEK_API_KEY": "", "AMAP_API_KEY": "", "MEITUAN_TRAVEL_ENABLED": "false",
        "VISIT_PROVIDER": "official", "TRAVEL_DB": str(out / f"browser-{uuid.uuid4().hex}.sqlite3")})
    report = {"passed": False, "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
              "scope": "isolated demo API, real installed browser, no live model/map/ticket calls", "checks": []}
    try:
        launcher.ensure_running()
        with httpx.Client(base_url=f"http://127.0.0.1:{launcher.api_port}", trust_env=False) as api, sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=str(executable))
            report["browser"] = browser.version
            page = browser.new_page(viewport={"width": 1363, "height": 871}, locale="zh-CN")
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(launcher.url)
            card = page.locator(".st-key-trip_form")
            button = page.get_by_role("button", name="生成旅行计划", exact=True)
            button.click()
            expect(card.get_by_test_id("stAlert")).to_have_count(6)
            for key in ("origin", "destinations", "date", "days", "people", "budget"):
                field = card.locator(".st-key-field_form_" + key)
                expect(field.get_by_test_id("stAlert")).to_have_count(1)
                assert field.get_by_test_id("stAlert").bounding_box()["y"] > field.get_by_test_id("stWidgetLabel").bounding_box()["y"]
            assert "请检查输入：城市不能重复" not in page.inner_text("body")
            card.evaluate("e=>e.scrollIntoView({block:'center'})")
            card.screenshot(path=str(out / "required-errors.png"))
            report["checks"].append("six missing values each shown under its own field")

            def fill(label, value):
                control = page.get_by_label(label, exact=True)
                control.fill(str(value))
                control.press("Tab")

            fill("出发地", "广州")
            fill("目的地（按顺序，用逗号分隔）", "广州")
            target = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
            date_field = card.locator(".st-key-field_form_date")
            for part, value in [("year", target.year), ("month", target.month), ("day", target.day)]:
                segment = date_field.locator(f'[data-type="{part}"]')
                segment.click()
                segment.press_sequentially(str(value), delay=70)
            date_field.locator('[data-type="day"]').press("Tab")
            expect(date_field.locator('input[type="date"]')).to_have_value(target.isoformat())
            fill("旅行天数", 2)
            fill("出行总人数（含本人）", 2)
            fill("全团总预算（人民币 / 元）", 6000)
            expect(card.get_by_test_id("stAlert")).to_have_count(0)
            assert api.get("/trips").json() == []
            report["checks"].append("correcting fields clears errors without creating a trip")

            card.locator("summary").click()
            fill("房间数（0表示按每间2人估算）", 3)
            field = card.locator(".st-key-field_form_rooms")
            alert = field.get_by_test_id("stAlert")
            expect(alert).to_contain_text("2人")
            expect(alert).to_contain_text("3间")
            expect(card.get_by_test_id("stAlert")).to_have_count(1)
            control = field.get_by_test_id("stNumberInputContainer")
            assert 0 <= alert.bounding_box()["y"] - (control.bounding_box()["y"] + control.bounding_box()["height"]) < 45
            field.scroll_into_view_if_needed()
            page.screenshot(path=str(out / "room-error.png"))
            card.screenshot(path=str(out / "room-error-card.png"))
            expect(control).to_have_css("border-top-color", "rgb(241, 125, 136)")
            report["checks"].append("room mismatch shown immediately below room input with red border and actual counts")

            card.locator("summary").click()
            expect(card.locator("details")).not_to_have_attribute("open", "")
            expect(page.get_by_test_id("stApp")).to_have_attribute("data-test-script-state", "notRunning")
            button.click()
            expect(card.locator("details")).to_have_attribute("open", "")
            expect(alert).to_be_visible()
            expect(page.get_by_test_id("stApp")).to_have_attribute("data-test-script-state", "notRunning")
            report["checks"].append("submitting an error inside collapsed options reveals the problem")
            fill("房间数（0表示按每间2人估算）", 2)
            expect(page.get_by_label("房间数（0表示按每间2人估算）", exact=True)).to_have_value("2")
            expect(card.get_by_test_id("stAlert")).to_have_count(0)
            assert api.get("/trips").json() == []
            button.click()
            expect(page.get_by_role("button", name="重新生成行程", exact=True)).to_be_visible(timeout=30000)
            trips = api.get("/trips").json()
            assert len(trips) == 1
            saved = api.get("/trips/" + trips[0]["id"]).json()
            assert saved["status"] == "ready" and saved["constraints"]["people"] == saved["constraints"]["rooms"] == 2
            expect(card.get_by_test_id("stAlert")).to_have_count(0)
            card.evaluate("e=>e.scrollIntoView({block:'center'})")
            card.screenshot(path=str(out / "valid-form.png"))
            report["checks"].append("two people two rooms submits once and produces ready itinerary")
            assert not page_errors, page_errors
            report["checks"].append("no browser JavaScript errors")
            report["passed"] = True
            browser.close()
    finally:
        # Only our temporary server process trees; never target user ports or process names.
        for _, process in reversed(launcher.processes):
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        report["screenshots"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob("*.png")}
        (out / "browser-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    check()
