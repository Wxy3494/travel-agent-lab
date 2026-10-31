"""Small, paid compatibility gate: JSON extraction + AutoGen tool use + plan + review.

Uses fixture tools to isolate model compatibility before spending map quota.
"""
import argparse
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.agents import AutoGenAgents
from app.config import Settings
from app.planning import materialize, validate_selection
from app.providers import FixtureProvider
from app.schemas import Constraints


async def probe():
    settings = replace(Settings.from_env(), mode="live", max_model_calls=10, max_repairs=0)
    if not settings.model_key:
        raise SystemExit("Missing DEEPSEEK_API_KEY. No request sent.")
    trace = []
    agents = AutoGenAgents(settings, trace)
    provider = FixtureProvider()
    anchor = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
    result = {"execution": "live_model_fixture_tools", "model": settings.model, "passed": False}
    try:
        c = await agents.extract(f"从广州出发，{anchor}广州玩2天，2人全团6000元", Constraints())
        if c.missing() or c.cities != ["广州"] or c.days != 2 or c.people != 2 or c.budget != 6000:
            raise ValueError("extraction_contract")
        facts = await agents.research(c, provider, "multi")
        selection = await agents.select(c, facts, [])
        if len(selection.days) != 2:
            raise ValueError("selection_contract")
        if validate_selection(c, selection, facts):
            raise ValueError("selection_identity_contract")
        days, budget, checks = await materialize(c, selection, facts, provider)
        if not budget or any(i.severity == "error" for i in checks):
            raise ValueError("schedule_contract")
        review = await agents.review(c, days, checks)
        result["review"] = review.model_dump(mode="json")
        if not review.approved or review.issues:
            raise ValueError("review_contract")
        result.update(passed=True, json_extraction=True, tool_call=True,
                      selection_schema=True, review_schema=bool(review))
    except Exception as exc:  # noqa: BLE001
        result["error_type"] = type(exc).__name__
        result["error_code"] = getattr(exc, "code", "probe_contract_failed")
    finally:
        result.update(trace=trace, metrics=agents.metrics())
        await agents.close()
        await provider.close()
    out = Path("artifacts/capability")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "report": str(out / "report.json")}, ensure_ascii=False))
    return result["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-live", action="store_true")
    if not parser.parse_args().run_live:
        parser.error("Pass --run-live after configuring your local key and checking account prices.")
    raise SystemExit(0 if asyncio.run(probe()) else 1)
