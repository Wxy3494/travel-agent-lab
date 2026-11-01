"""Meituan integration regressions; never use real credentials or the network."""
import asyncio
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from app.agents import DemoAgents
from app.config import Settings
from app.main import create_app
from app.meituan import MeituanTravelService, clean_content, query_for
from app.render import render_markdown
from app.schemas import Constraints, RevisionRequest, TravelInfo, TripRequest
from app.service import TravelService
from app.storage import Repository


def service_with_meituan(tmp_path, runner):
    settings = Settings(mode="live", db_path=str(tmp_path / "meituan-test.db"),
        meituan_enabled=True, meituan_token="fake-meituan-private-token")
    from app.providers import FixtureProvider
    service = TravelService(settings, Repository(settings.db_path), provider_factory=FixtureProvider, agents_factory=DemoAgents)
    service.meituan_service = MeituanTravelService(settings, runner)
    return service


async def sample(service, constraints):
    return await service.create(TripRequest(constraints=Constraints.model_validate({**constraints.model_dump(),
        "days": 1, "must_visit": ["广东省博物馆"], "day_place_ids": {1: ["440100_0"]}})))


async def test_automatic_query_saved_exported_cached_and_date_change_refetches(tmp_path, constraints):
    calls = []
    async def runner(query, city, timeout):
        calls.append((query, city, timeout))
        assert "不创建、修改、支付或取消订单" in query and "fake-meituan-private-token" not in query
        return 0, json.dumps({"status": "success", "data": "**门票参考** ¥XX起\\n[购票说明](https://www.meituan.com/) </answer>"}), "private error not displayed"
    service = service_with_meituan(tmp_path, runner)
    result = await sample(service, constraints)
    assert result.status == "ready" and result.travel_info.status == "success" and len(calls) == 1
    assert "¥XX起" in result.travel_info.content and "</answer>" not in result.travel_info.content
    assert "补充旅行资料" in render_markdown(result)
    assert service.repository.get(result.id).travel_info.content == result.travel_info.content
    # A text consultation is not fabricated into the strict, date-bound entry checks.
    assert all(v.ticket_price is None and v.authority == "fixture" for v in result.visit_checks)
    fresh = await service.refresh_visits(result.id, 1)
    assert len(calls) == 1 and fresh.travel_info.queried_at == result.travel_info.queried_at
    changed = await service.revise(result.id, RevisionRequest(base_version=2,
        changes={"start_date": str(constraints.start_date + timedelta(days=1))}))
    assert changed.travel_info.status == "success" and len(calls) == 2
    assert "fake-meituan-private-token" not in changed.model_dump_json()


@pytest.mark.parametrize("mode,enabled", [("demo", True), ("live", False)])
async def test_demo_or_disabled_does_not_contact_meituan(service, constraints, mode, enabled):
    result = await sample(service, constraints)
    async def forbidden(*args):
        raise AssertionError("must remain offline")
    external = MeituanTravelService(Settings(mode=mode, meituan_enabled=enabled, meituan_token="not-for-demo"), forbidden)
    await external.enrich(result)
    assert result.travel_info is None


@pytest.mark.parametrize("failure", ["auth", "channel", "network", "timeout", "missing_cli", "empty", "malformed", "oversize", "json_list"])
async def test_failure_preserves_itinerary_and_never_echoes_secret(tmp_path, constraints, failure):
    async def runner(*args):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("fake-meituan-private-token", 1)
        if failure == "missing_cli":
            raise FileNotFoundError("fake-meituan-private-token")
        code = {"auth": 3, "channel": 4, "network": 1}.get(failure, 0)
        text = ("{broken" if failure == "malformed" else "[]" if failure == "json_list" else "x" * 120001 if failure == "oversize" else
            json.dumps({"status": "success", "data": ""}))
        return code, text, "fake-meituan-private-token raw traceback"
    service = service_with_meituan(tmp_path, runner)
    result = await sample(service, constraints)
    assert result.status == "ready" and result.days and result.budget
    assert result.travel_info.status == "unavailable" and not result.travel_info.content
    assert "fake-meituan-private-token" not in result.model_dump_json()


async def test_stale_cache_and_insufficient_time_budget(service, constraints):
    result = await sample(service, constraints)
    query, fingerprint, _, _ = query_for(result)
    calls = []
    async def runner(*args):
        calls.append(args)
        return 0, json.dumps({"status": "success", "data": "新的参考资料"}), ""
    external = MeituanTravelService(Settings(mode="live", meituan_enabled=True, meituan_token="fake"), runner)
    previous = result.model_copy(deep=True)
    previous.travel_info = TravelInfo(status="success", content="旧资料", query_fingerprint=fingerprint,
        queried_at=datetime.now(UTC) - timedelta(minutes=11))
    await external.enrich(result, previous, timeout=10)
    assert len(calls) == 1 and result.travel_info.content == "新的参考资料"
    await external.enrich(result, previous, timeout=1)
    assert len(calls) == 1 and "等待上限" in result.travel_info.message
    assert query


async def test_missing_credential_does_not_start_process(service, constraints):
    result = await sample(service, constraints)
    async def forbidden(*args):
        raise AssertionError
    external = MeituanTravelService(Settings(), forbidden)
    report = await external.lookup(result)
    assert report.status == "unavailable" and "尚未配置" in report.message


def test_result_cleanup_preserves_prices_removes_unsafe_markup_and_secrets():
    text = clean_content('¥X元起\\n[正常链接](https://www.meituan.com/) [坏链接](javascript:alert) '
        '[含凭证](https://example.org/?token=unknown) ![图](https://example.org/a.jpg) <think>hidden reasoning</think> '
        'fake-meituan-private-token <answer>仅供参考</answer>', 'fake-meituan-private-token')
    assert "¥X元起\n" in text and "https://www.meituan.com/" in text
    assert all(s not in text for s in ["javascript:", "token=", "a.jpg", "hidden reasoning", "fake-meituan-private-token", "<answer>"])


async def test_cli_uses_literal_arguments_minimal_environment_and_cancels_child(tmp_path, monkeypatch):
    script = tmp_path / "index.js"; script.write_text("// stub", encoding="utf-8")
    node = tmp_path / "node.exe"; node.write_text("stub", encoding="utf-8")
    captured = {}
    class Process:
        returncode = 0
        def __init__(self, args, **kwargs):
            captured.update(args=args, **kwargs)
        def communicate(self, timeout):
            return b'{"status":"success","data":"test"}', b''
        def kill(self):
            captured["killed"] = True
        def wait(self, timeout):
            return 0
    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "another-secret-never-forward")
    settings = Settings(meituan_token="fake-secret", meituan_node_path=str(node), meituan_cli_path=str(script))
    external = MeituanTravelService(settings)
    unsafe = "景点'; $(malicious) `echo`"
    await external.run_cli(unsafe, "广州", 10)
    assert captured["args"][captured["args"].index("--query") + 1] == unsafe
    assert "fake-secret" not in str(captured["args"]) and not captured.get("shell")
    assert captured["env"]["MEITUAN_HT_TOKEN"] == "fake-secret" and "DEEPSEEK_API_KEY" not in captured["env"]
    def cancel(*args, **kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr(Process, "communicate", cancel)
    with pytest.raises(asyncio.CancelledError):
        await external.run_cli("query", "广州", 10)
    assert captured["killed"]


def test_health_reveals_only_configuration_boolean(service):
    settings = replace(service.settings, meituan_enabled=True, meituan_token="test-hidden-token")
    client = TestClient(create_app(settings, service))
    result = client.get("/health")
    assert result.json()["meituan_configured"] is True and "test-hidden-token" not in result.text


async def test_ui_and_export_show_reference_without_claiming_booked(service, constraints, monkeypatch):
    result = await sample(service, constraints)
    result.travel_info = TravelInfo(status="success", content="广东省博物馆 · 免费，需预约；广州塔 ¥XX起",
        message="美团返回的参考资料，不代表已预约。", queried_places=2, total_places=2)
    api = TestClient(create_app(service.settings, service))
    from urllib.parse import urlsplit

    import httpx
    def local_request(method, url, json=None, **kwargs):
        parsed = urlsplit(url)
        return api.request(method, parsed.path, json=json)
    monkeypatch.setattr(httpx, "request", local_request)
    ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app/ui.py"), default_timeout=10)
    ui.session_state.trip = result.model_dump(mode="json")
    ui.run()
    assert not ui.exception
    assert not any(e.label == "美团景点与门票参考" for e in ui.expander)
    assert any("¥XX起" in m.value for m in ui.markdown)
    assert any("不代表已预约" in c.value for c in ui.caption)
