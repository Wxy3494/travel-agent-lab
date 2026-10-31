"""Bounded, public official-page smoke check. No model, map key, bookings or ticket orders."""
import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.admissions import AdmissionService
from app.config import Settings
from app.providers import Provider
from app.schemas import Constraints, Place


async def check(output):
    admission, provider = AdmissionService(Settings()), Provider()
    on_date = datetime.now(UTC).date() + timedelta(days=3)
    rows = []
    for name, city, address in [("广东省博物馆", "广州", "珠江东路2号"), ("故宫博物院", "北京", "景山前街4号")]:
        point = Place(id="public-official-smoke-" + city, name=name, city=city, address=address,
                      location="113,23", evidence_id="not_a_map_lookup")
        result = await admission.lookup(point, on_date, Constraints(), provider, [0])
        rows.append(result.model_dump(mode="json"))
    report = {"checked_at": datetime.now(UTC).isoformat(), "scope": "public_official_pages_only_no_map_model_booking",
              "http_calls": provider.calls, "verified": sum(r["authority"] == "official" for r in rows),
              "total": len(rows), "checks": rows, "evidence": [e.model_dump(mode="json") for e in provider.evidence.values()]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verified": report["verified"], "total": len(rows), "http_calls": provider.calls,
                      "report": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("artifacts/admissions-20261031/official-pages.json"))
    asyncio.run(check(parser.parse_args().output))
