"""Real Chrome over demo planning, persisted chat and cached genuine POI photo fixtures."""
import argparse
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
    def __init__(self, wrapper, api_wrapper=None, **kwargs):
        super().__init__(**kwargs)
        self.wrapper = wrapper
        self.api_wrapper = api_wrapper

    def start(self, service):
        if service != "ui" and not self.api_wrapper:
            return super().start(service)
        command = ([str(self.python), "-m", "uvicorn", self.api_wrapper.stem + ":app", "--app-dir", str(self.api_wrapper.parent),
                    "--host", "127.0.0.1", "--port", str(self.api_port)] if service == "api" else
                   [str(self.python), "-m", "streamlit", "run", str(self.wrapper), "--server.address", "127.0.0.1",
                    "--server.port", str(self.ui_port), "--server.headless", "true"])
        with (ROOT / ".local/assistant-browser-ui.log").open("ab") as log:
            process = subprocess.Popen(command, cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL, stdout=log,
                stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.processes.append((service, process))
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if self.healthy(service):
                return True
            if process.poll() is not None:
                raise RuntimeError("Fixture UI failed")
            time.sleep(.2)
        raise RuntimeError("Fixture UI did not become ready")


def check(evidence_dir=None):
    sys.path.insert(0, str(ROOT / ".local/browser-qa"))
    from playwright.sync_api import expect, sync_playwright
    out = ROOT / (evidence_dir or "artifacts/assistant-20261101")
    out.mkdir(parents=True, exist_ok=True)
    photo_dir = ROOT / "artifacts/assistant-20261101"
    assets = json.loads((photo_dir / "photo-fixtures.json").read_text(encoding="utf-8"))
    assert {a["category"] for a in assets} == {"attraction", "hotel"}
    wrapper = out / f"ui-fixture-{uuid.uuid4().hex}.py"
    wrapper.write_text(f'''import sys, runpy
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from app import photos
assets = { {a['url']: str(photo_dir / a['file']) for a in assets}!r}
photos.fetch_photo = lambda url: ("image/jpeg", Path(assets[url]).read_bytes()) if url in assets else None
runpy.run_path({str(ROOT / 'app/ui.py')!r}, run_name="__main__")
''', encoding="utf-8")
    api_wrapper = out / f"assistant_api_fixture_{uuid.uuid4().hex}.py"
    api_wrapper.write_text(f'''import asyncio,sys
sys.path.insert(0, {str(ROOT)!r})
from app.main import app
from app.assistant import TourAssistant
service=app.state.service
original=service.revise
async def slow_revise(*args,**kwargs):
    await asyncio.sleep(2)
    return await original(*args,**kwargs)
service.revise=slow_revise
base=TourAssistant(service)
async def decide(message,trip,history):
    result,_=await base.decide(message,trip,history)
    if result.action=='revise':
        result.reply='好的，我会按你的要求重新安排，其他条件保持不变。'
    return result
app.state.tour_assistant=TourAssistant(service,decide)
''', encoding="utf-8")
    path = out / f"browser-{uuid.uuid4().hex}.sqlite3"
    launcher = PhotoFixtureLauncher(wrapper, api_wrapper, api_port=free_port(), ui_port=free_port(), env={"TRAVEL_MODE": "demo",
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
                expect(page.get_by_text("好的，我会按你的要求重新安排，其他条件保持不变。", exact=True)).to_be_visible(timeout=1500)
                assert api.get(f"/trips/{trip.id}").json()["version"] == 2
                page.locator(".st-key-tour_assistant").screenshot(path=str(out / "assistant-progress.png"))
                report["checks"].append("model acknowledgement is visible while delayed replanning is still running")
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
                repository = Repository(path)
                pending = repository.begin_chat(trip.id, uuid.uuid4().hex, "预算改成4000元", 3)
                repository.release_chat(pending["claim"])
                page.reload()
                recent.first.click()
                page.get_by_role("button", name="重新发送刚才的消息", exact=True).click()
                expect(page.get_by_test_id("stChatMessage")).to_have_count(6, timeout=30000)
                expect(page.get_by_test_id("stChatMessage").last).to_contain_text("已根据你的意见重新安排", timeout=30000)
                saved = api.get(f"/trips/{trip.id}").json()
                assert saved["version"] == 4 and saved["constraints"]["budget"] == 4000
                assert repository.pending_chat(trip.id) is None
                page.locator(".st-key-tour_assistant").screenshot(path=str(out / "assistant-chat.png"))
                report["checks"].append("interrupted chat is recovered after page reload using the original request ID, saving one version")
                before = api.get(f"/trips/{trip.id}").json()
                chat.fill("第二天轻松点，少走路")
                chat.press("Enter")
                expect(page.get_by_test_id("stChatMessage")).to_have_count(8, timeout=30000)
                expect(page.get_by_test_id("stChatMessage").last).to_contain_text("第2天", timeout=30000)
                after = api.get(f"/trips/{trip.id}").json()
                assert after["version"] == 5 and after["days"][0] == before["days"][0]
                assert any(rule.get("reduce_walking") and rule["day"] == 2 for rule in after["constraints"]["day_rules"])
                expect(page.get_by_test_id("stChatMessage").last).to_contain_text("第2天")
                page.locator(".st-key-tour_assistant").screenshot(path=str(out / "assistant-chat.png"))
                report["checks"].append("relax second day and reduce walking replans automatically, reports concrete changes and preserves first day")
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
        if (out / "assistant-progress.png").is_file():
            report["screenshots"]["assistant-progress.png"] = hashlib.sha256((out / "assistant-progress.png").read_bytes()).hexdigest()
        (out / "browser-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir")
    check(parser.parse_args().evidence_dir)
