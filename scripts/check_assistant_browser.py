"""Real Chrome over demo planning, persisted chat and cached genuine POI photo fixtures."""
import hashlib
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.schemas import TripResult
from app.storage import Repository
from scripts.check_form_browser import free_port
from scripts.desktop_launcher import ROOT, Launcher


class PhotoFixtureLauncher(Launcher):
    def __init__(self, wrapper, **kwargs):
        super().__init__(**kwargs)
        self.wrapper = wrapper

    def start(self, service):
        if service != "ui":
            return super().start(service)
        command = [str(self.python), "-m", "streamlit", "run", str(self.wrapper), "--server.address", "127.0.0.1",
                   "--server.port", str(self.ui_port), "--server.headless", "true"]
        with (ROOT / ".local/assistant-browser-ui.log").open("ab") as log:
            process = subprocess.Popen(command, cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL, stdout=log,
                stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.processes.append((service, process))
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if self.healthy("ui"):
                return True
            if process.poll() is not None:
                raise RuntimeError("Fixture UI failed")
            time.sleep(.2)
        raise RuntimeError("Fixture UI did not become ready")


def check():
    sys.path.insert(0, str(ROOT / ".local/browser-qa"))
    from playwright.sync_api import expect, sync_playwright
    out = ROOT / "artifacts/assistant-20261101"
    out.mkdir(parents=True, exist_ok=True)
    assets = json.loads((out / "photo-fixtures.json").read_text(encoding="utf-8"))
    assert {a["category"] for a in assets} == {"attraction", "hotel"}
    wrapper = out / f"ui-fixture-{uuid.uuid4().hex}.py"
    wrapper.write_text(f'''import sys, runpy
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from app import photos
assets = { {a['url']: str(out / a['file']) for a in assets}!r}
photos.fetch_photo = lambda url: ("image/jpeg", Path(assets[url]).read_bytes()) if url in assets else None
runpy.run_path({str(ROOT / 'app/ui.py')!r}, run_name="__main__")
''', encoding="utf-8")
    path = out / f"browser-{uuid.uuid4().hex}.sqlite3"
    launcher = PhotoFixtureLauncher(wrapper, api_port=free_port(), ui_port=free_port(), env={"TRAVEL_MODE": "demo",
        "DEEPSEEK_API_KEY": "", "AMAP_API_KEY": "", "MEITUAN_TRAVEL_ENABLED": "false", "TRAVEL_DB": str(path)})
    report = {"passed": False, "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "scope": "Chrome with demo planning/chat; cached genuine AMap POI photos injected for visual QA, no live planning",
        "checks": []}
    try:
        launcher.ensure_running()
        with httpx.Client(base_url=f"http://127.0.0.1:{launcher.api_port}", trust_env=False, timeout=30) as api:
            start = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
            trip = TripResult.model_validate(api.post("/trips", json={"constraints": {"origin": "广州", "cities": ["广州"],
                "start_date": str(start), "days": 2, "people": 2, "budget": 6000}, "autonomous": True}).json())
            venue = next(a for a in assets if a["category"] == "attraction")
            hotel = next(a for a in assets if a["category"] == "hotel")
            event = next(e for e in trip.days[0].events if e.kind == "visit" and "博物馆" in e.title)
            event.title, event.photos = venue["name"], [venue["url"]]
            stay = trip.days[0].suggested_stay or trip.days[0].hotel_options[0]
            stay.name, stay.photos = hotel["name"], [hotel["url"]]
            trip.days[0].suggested_stay = stay
            trip.version = 2
            Repository(path).save(trip, expected_version=1)
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe")
                report["browser"] = browser.version
                page = browser.new_page(viewport={"width": 1363, "height": 871}, locale="zh-CN")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(launcher.url)
                recent = page.locator('[data-testid="stSidebar"] [class*="st-key-recent_"] button')
                expect(recent).to_have_count(1)
                recent.first.click()
                expect(page.get_by_role("heading", name="旅游助手", exact=True)).to_be_visible()
                for text in ["景点入场核验与预约确认", "版本、对比与恢复", "酒店、车站与开放信息（可选）", "分日修改与雨天替换", "选择一个调整方案", "进一步修改"]:
                    expect(page.get_by_text(text, exact=True)).to_have_count(0)
                report["checks"].append("manual planning panels removed; one tourism assistant chat entry")
                images = page.locator(".place-gallery img")
                expect(images).to_have_count(2)
                for picture in images.all():
                    picture.scroll_into_view_if_needed()
                    expect(picture).to_have_js_property("complete", True)
                    assert picture.evaluate("e=>e.naturalWidth>0")
                images.first.evaluate("e=>e.scrollIntoView({block:'center',behavior:'instant'})")
                page.screenshot(path=str(out / "attraction-photo-layout.png"))
                images.last.evaluate("e=>e.scrollIntoView({block:'center',behavior:'instant'})")
                page.screenshot(path=str(out / "hotel-photo-layout.png"))
                report["checks"].append("attraction and hotel photo cards load below the corresponding content")
                chat = page.locator(".st-key-assistant_input textarea")
                chat.fill("你好")
                chat.press("Enter")
                expect(page.get_by_test_id("stChatMessage").last).to_contain_text("我在的")
                assert api.get(f"/trips/{trip.id}").json()["version"] == 2
                report["checks"].append("greeting gets a customer-service reply without changing itinerary")
                chat.fill("预算改成4500元")
                chat.press("Enter")
                expect(page.get_by_test_id("stChatMessage").last).to_contain_text("已根据你的意见重新安排", timeout=30000)
                saved = api.get(f"/trips/{trip.id}").json()
                assert saved["version"] == 3 and saved["constraints"]["budget"] == 4500
                assert len(api.get(f"/trips/{trip.id}/messages").json()) == 4
                page.locator(".st-key-tour_assistant").screenshot(path=str(out / "assistant-chat.png"))
                report["checks"].append("chat changes budget and itinerary, saves one new version and replies after completion")
                page.reload()
                recent.first.click()
                expect(page.get_by_test_id("stChatMessage")).to_have_count(4)
                expect(page.get_by_test_id("stChatMessage").last).to_contain_text("已根据你的意见重新安排")
                report["checks"].append("conversation persists after reload and reopening history")
                assert not errors, errors
                report["checks"].append("no browser JavaScript errors")
                report["passed"] = True
                browser.close()
    finally:
        for _, process in reversed(launcher.processes):
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        report["screenshots"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob("*-layout.png")}
        if (out / "assistant-chat.png").is_file():
            report["screenshots"]["assistant-chat.png"] = hashlib.sha256((out / "assistant-chat.png").read_bytes()).hexdigest()
        (out / "browser-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    check()
