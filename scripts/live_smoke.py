"""Four live AMap + AutoGen end-to-end checks. Explicit --run-live is mandatory."""
import argparse
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.audit import audit_result
from app.config import Settings
from app.schemas import Constraints, RevisionRequest, TripRequest
from app.service import TravelService
from app.storage import Repository


async def run():
    settings = replace(Settings.from_env(), mode="live")
    if not settings.model_key or not settings.amap_key:
        raise SystemExit("Both DEEPSEEK_API_KEY and AMAP_API_KEY are required. No request sent.")
    output = Path("artifacts/live-smoke")
    output.mkdir(parents=True, exist_ok=True)
    service = TravelService(settings, Repository(output / "trips.sqlite3"))
    records = []
    for cities, days in [(["广州"], 2), (["深圳"], 2), (["广州", "深圳"], 5), (["广州", "珠海"], 4)]:
        result = await service.create(TripRequest(constraints=Constraints(
            origin=cities[0], cities=cities, days=days, people=2, budget=10000,
            start_date=datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1))))
        record = result.model_dump(mode="json")
        record["audit_codes"] = [i.code for i in audit_result(result)]
        records.append(record)
        print(f"{' -> '.join(cities)}: {result.status}")
        if len(records) == 1:
            revised = await service.revise(result.id, RevisionRequest(base_version=result.version, changes={"budget": 15000}))
            change = revised.model_dump(mode="json")
            change["audit_codes"] = [i.code for i in audit_result(revised)]
            change["acceptance_step"] = "revision_of_first_single_city"
            records.append(change)
            print(f"single-city revision: {revised.status}")
    (output / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return all(r["status"] == "ready" and not r["audit_codes"] and
               any(e["kind"] == "live" for e in r["evidence"]) and
               r["metrics"]["model_calls"] > 0 for r in records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-live", action="store_true", help="Explicit opt-in to external API/model usage")
    if not parser.parse_args().run_live:
        parser.error("Pass --run-live only after configuring API keys and checking your budget.")
    raise SystemExit(0 if asyncio.run(run()) else 1)
