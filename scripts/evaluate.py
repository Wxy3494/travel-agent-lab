"""Reproducible contract report; live model comparison requires an explicit flag."""
import argparse
import asyncio
import hashlib
import json
import statistics
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.audit import audit_result
from app.config import Settings
from app.providers import FixtureProvider
from app.schemas import RevisionRequest, TripRequest
from app.service import TravelService
from app.storage import Repository

ROOT = Path(__file__).resolve().parents[1]


async def evaluate(live_compare=False, output="artifacts/evaluation", split="all", max_cases=None, suite="current"):
    files = {"legacy": ("evals/cases.json", "evals/manifest.json", 1),
             "extended": ("evals/extensions-v2.json", "evals/extensions-v2-manifest.json", 2),
             "current": ("evals/cases-v3.json", "evals/manifest-v3.json", 3)}
    case_file, manifest_file, protocol = files[suite]
    data = (ROOT / case_file).read_bytes()
    manifest = json.loads((ROOT / manifest_file).read_text(encoding="utf-8"))
    if hashlib.sha256(data).hexdigest() != manifest["case_file_sha256"]:
        raise SystemExit("Frozen case file changed; review and version the evaluation protocol first.")
    cases = [c for c in json.loads(data) if split == "all" or c["split"] == split]
    if max_cases is not None:
        cases = cases[:max_cases]
    settings = Settings.from_env()
    if live_compare and not settings.model_key:
        raise SystemExit("Missing DEEPSEEK_API_KEY; no paid request was sent.")
    settings = replace(settings, mode="live" if live_compare else "demo")
    anchor = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=2)
    rows = []
    with tempfile.TemporaryDirectory(prefix="travel-eval-") as folder:
        for strategy in (["single", "multi"] if live_compare else ["multi"]):
            for case in cases:
                service = TravelService(settings, Repository(Path(folder) / f"{strategy}-{case['id']}.db"),
                                        provider_factory=lambda case=case: FixtureProvider(scenario=case.get("scenario", "normal")))
                payload = {"strategy": strategy, "prompt": case.get("prompt", ""), "constraints": {
                    "origin": "广州", "cities": ["广州"], "start_date": str(anchor), "days": 2,
                    "people": 2, "budget": 6000, **case.get("changes", {})}}
                if case.get("closed_fixture"):
                    payload["constraints"]["opening_rules"] = [{"place_id": case["closed_fixture"], "on_date": str(anchor),
                                                              "closed": True, "source": "评测提供闭馆信息，非真实状态"}]
                row = {"id": case["id"], "name": case["name"], "split": case["split"],
                       "strategy": strategy, "input": payload}
                try:
                    request = TripRequest.model_validate(payload)
                    result = await service.create(request)
                    initial = result
                    phase_passed = result.status == case.get("initial_expected", result.status)
                    workflow = [result.metrics.model_dump()]
                    workflow_results = [result.model_dump(mode="json")]
                    if "revision" in case:
                        result = await service.revise(result.id, RevisionRequest(base_version=1, changes=case["revision"]))
                        workflow.append(result.metrics.model_dump())
                        workflow_results.append(result.model_dump(mode="json"))
                    if "followup" in case:
                        result = await service.revise(result.id, RevisionRequest(base_version=result.version, **case["followup"]))
                        workflow.append(result.metrics.model_dump())
                        workflow_results.append(result.model_dump(mode="json"))
                    passed = phase_passed and result.status == case.get("expected")
                    if "preserved_day" in case:
                        index = case["preserved_day"] - 1
                        passed = passed and len(result.days) > index and len(initial.days) > index and result.days[index] == initial.days[index]
                    for flag, day, boundary, compare in [("first_start_after", 0, "start", "after"),
                        ("last_end_before", -1, "end", "before"), ("day2_start_after", 1, "start", "after")]:
                        if flag in case:
                            events = result.days[day].events if len(result.days) > max(0, day) else []
                            passed = passed and bool(events) and all((getattr(e, boundary) >= case[flag] if compare == "after"
                                                                     else getattr(e, boundary) <= case[flag]) for e in events)
                    if "issue" in case:
                        passed = passed and any(i.code == case["issue"] for i in result.issues)
                    names = [e.title for d in result.days for e in d.events if e.kind == "visit"]
                    if "includes" in case:
                        passed = passed and case["includes"] in names
                    if "excludes" in case:
                        passed = passed and case["excludes"] not in names
                    if "hotel_quantity" in case:
                        passed = passed and result.budget is not None and any(
                            item.category == "住宿" and item.quantity == case["hotel_quantity"] for item in result.budget.items)
                    if result.status == "ready":
                        known = {ev.id for ev in result.evidence}
                        passed = passed and not any(i.severity == "error" for i in result.issues)
                        passed = passed and all(set(event.evidence_ids) <= known for d in result.days for event in d.events)
                        passed = passed and not audit_result(result)
                    row.update(passed=bool(passed), audit_codes=[i.code for i in audit_result(result)],
                               error_detected=bool([i for i in result.issues if i.severity == "error"]),
                               repair_attempts=max(0, len([t for t in result.trace if t["step"] == "validation"]) - 1),
                               result=result.model_dump(mode="json"), initial_status=initial.status,
                               workflow_metrics={k: None if k == "estimated_model_cost_cny" and any(m[k] is None for m in workflow)
                                                 else sum(m[k] for m in workflow) for k in workflow[0]},
                               workflow_results=workflow_results,
                               tool_snapshot="FixtureProvider synthetic data", workflow_requests=len(workflow))
                except ValidationError:
                    row.update(passed=case.get("expected_http") == 422, validation_error=True)
                rows.append(row)
    output_dir = ROOT / output
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {"generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
              "execution": "live_model_fixture_tools" if live_compare else "offline_contracts_no_model",
              "fixture_sha256": hashlib.sha256(data).hexdigest(), "anchor_date": str(anchor),
              "model": settings.model if live_compare else None, "total": len(rows),
              "suite": suite, "protocol_version": protocol,
              "split": split, "temperature": 0, "max_cases": max_cases,
              "implementation_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                  for p in ["app/agents.py", "app/model_client.py", "app/service.py", "app/input_parser.py",
                            "app/schemas.py", "app/planning.py", "app/audit.py", "app/providers.py", "app/names.py",
                            "app/adjustments.py", "requirements.lock"]},
              "limits": {"model_calls": settings.max_model_calls, "tool_calls": settings.max_tool_calls,
                         "repairs": settings.max_repairs, "timeout": settings.timeout},
              "passed": sum(r["passed"] for r in rows), "results": rows}
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 旅行规划评测报告", "", f"执行类型：`{report['execution']}`", "",
             "本报告的通过率表示预定义场景契约满足情况，不等于真实旅游质量、用户满意度或全国覆盖率。",
             "离线模式没有模型调用，不能用来证明多 Agent 优于单 Agent。真实模型对照仍使用固定测试工具数据。", "",
             f"用例文件 SHA256：`{report['fixture_sha256']}`", f"日期锚点：{anchor}", "",
             f"场景通过：{report['passed']}/{report['total']}", "",
             "| 策略 | 场景 | 结果 |", "|---|---|---|"]
    lines += [f"| {r['strategy']} | {r['id']} {r['name']} | {'通过' if r['passed'] else '未通过'} |" for r in rows]
    summaries = {}
    for strategy in sorted({r["strategy"] for r in rows}):
        results = [r["result"] for r in rows if r["strategy"] == strategy and "result" in r]
        selected_rows = [r for r in rows if r["strategy"] == strategy]
        metrics = [r["workflow_metrics"] for r in selected_rows if "workflow_metrics" in r]
        elapsed = [m["elapsed_ms"] for m in metrics]
        summaries[strategy] = {"contract_passed": sum(r["passed"] for r in selected_rows),
            "cases": len(selected_rows), "ready": sum(r["status"] == "ready" for r in results),
            "median_elapsed_ms": statistics.median(elapsed) if elapsed else None,
            "model_calls": sum(m["model_calls"] for m in metrics),
            "tool_calls": sum(m["tool_calls"] for m in metrics),
            "input_tokens": sum(m["input_tokens"] for m in metrics),
            "output_tokens": sum(m["output_tokens"] for m in metrics),
            "estimated_model_cost_cny": sum(m["estimated_model_cost_cny"] for m in metrics) if settings.input_price is not None and settings.output_price is not None else None}
        lines += ["", (f"{strategy}：可执行场景耗时中位数 {statistics.median(elapsed) if elapsed else 0:.2f} ms；"
                  f"模型调用合计 {sum(r['metrics']['model_calls'] for r in results)}。")]
    report["summaries"] = summaries
    if live_compare:
        lines += ["", "## 单Agent与三Agent对照", "",
                  "相同模型、结构化输入、确定性资料、硬约束和计费上限；单Agent由控制器查询资料并调用规划角色，三Agent增加资料工具调用与独立审核。",
                  "两组保留相同代码校验返工，因此测量的是角色分工的增量，不是完全无工具聊天的对比。",
                  "temperature=0也不能消除服务端随机性；每场景一次，只作为探索性结果。", "",
                  "| 策略 | 契约通过 | ready | 模型调用 | 工具调用 | 输入token | 输出token |", "|---|---:|---:|---:|---:|---:|---:|"]
        lines += [f"| {s} | {m['contract_passed']}/{m['cases']} | {m['ready']} | {m['model_calls']} | {m['tool_calls']} | {m['input_tokens']} | {m['output_tokens']} |"
                  for s, m in summaries.items()]
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"execution": report["execution"], "passed": report["passed"], "total": report["total"],
                      "report": str(output_dir / "report.md")}, ensure_ascii=False))
    return report["passed"] == report["total"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-compare", action="store_true", help="Makes paid model calls; tools remain fixed fixtures")
    parser.add_argument("--split", choices=["all", "dev", "holdout"], default="all")
    parser.add_argument("--output", default="artifacts/evaluation")
    parser.add_argument("--max-cases", type=int, help="Run a small pilot before the full paid comparison")
    parser.add_argument("--suite", choices=["current", "legacy", "extended"], default="current")
    args = parser.parse_args()
    if args.max_cases is not None and args.max_cases < 1:
        parser.error("--max-cases must be positive")
    raise SystemExit(0 if asyncio.run(evaluate(args.live_compare, args.output, args.split, args.max_cases, args.suite)) else 1)
