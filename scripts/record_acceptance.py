"""Record dated local evidence without copying private live requests into a release."""
import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.package_public import ROOT, source_files


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record(evidence_dir):
    evidence_dir = Path(evidence_dir)
    tree = ET.parse(evidence_dir / "pytest.xml")
    suites = tree.findall(".//testsuite")
    counts = {key: sum(int(s.attrib.get(key, 0)) for s in suites) for key in ("tests", "failures", "errors", "skipped")}
    if not counts["tests"] or counts["failures"] or counts["errors"]:
        raise ValueError("Record only a successful, complete test run")
    if "All checks passed!" not in (evidence_dir / "ruff.txt").read_text(encoding="utf-8-sig"):
        raise ValueError("Ruff report did not pass")
    if "No broken requirements found" not in (evidence_dir / "pip-check.txt").read_text(encoding="utf-8-sig"):
        raise ValueError("Dependency report did not pass")
    evaluations = {}
    for suite in ("v3", "v2"):
        path = evidence_dir / f"evaluation-{suite}/report.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["passed"] != data["total"] or data["execution"] != "offline_contracts_no_model":
            raise ValueError("Offline evaluation did not pass")
        evaluations[suite] = {"passed": data["passed"], "total": data["total"], "model_calls": 0,
                              "report_sha256": digest(path), "fixture_sha256": data["fixture_sha256"]}
    history = []
    for name, scope in [("artifacts/live-readiness/running-api.json", "2026-10-15 广州两日生成、预算修改、历史及导出的小样本"),
                        ("artifacts/simplified-itinerary-20261016/live-lodging-check.json", "2026-10-16 武汉一家公开酒店的地址定位")]:
        path = ROOT / name
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            history.append({"scope": scope, "record": name, "record_sha256": digest(path),
                            "recorded_passed": bool(data.get("passed")), "retested_now": False})
    snapshot = {"checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "pytest": {**counts, "report_sha256": digest(evidence_dir / "pytest.xml")},
        "ruff": "passed", "pip_check": "passed", "offline": evaluations,
        "historical_live": history, "current_live_end_to_end": "not_rerun_no_paid_model_or_map_calls",
        "current_ui": "AppTest_including_isolated_archive_extraction; no_new_browser_visual_check",
        "delivery": {"type": "source_only_using_existing_dependencies", "fresh_machine_install": "not_run",
                     "license": "not_selected", "published": False},
        "docker_runtime": "not_run", "cloud_ci": "not_run", "independent_learning": "not_evaluated",
        "source_sha256": {p.relative_to(ROOT).as_posix(): digest(p) for p in source_files(ROOT)
                          if p.relative_to(ROOT).parts[0] != "docs" and p.name != "README.md"}}
    (ROOT / "docs/acceptance_snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    overview = f'''# 当前验收状态

记录时间：{snapshot['checked_at']}。本页和 [机器可读摘要](acceptance_snapshot.json) 是当前状态入口；历史记录保留原日期，不当作本次验收。

| 范围 | 当前证据 | 限制 |
|---|---|---|
| 自动化回归 | {counts['tests']} 项通过，失败 {counts['failures']}、错误 {counts['errors']}、跳过 {counts['skipped']} | 含 Replay、MockTransport、AppTest；不是实际旅行质量成绩 |
| 规范与依赖 | Ruff、pip check 通过 | 使用当前 Windows 虚拟环境 |
| 离线场景 | v3 {evaluations['v3']['passed']}/{evaluations['v3']['total']}；v2 {evaluations['v2']['passed']}/{evaluations['v2']['total']} | 模型调用为零；已知回归，非盲测 |
| 发布包界面 | 独立进程解压后运行 AppTest，CSS 加载、JPG 嵌入通过 | 复用本机依赖，未在全新机器安装，未重新做浏览器外观检查 |
| 真实接口历史 | 2026-10-15 广州两日生成、预算修改、历史及导出；2026-10-16 武汉酒店地址定位有通过记录 | 当前源码有后续修改，本轮未重新发送模型或地图请求 |
| 真实全覆盖 | 未通过完整验收 | 其他路线、实时房价/房态/余票、七天天气厂商、30天真实规划仍未验收 |
| Docker、云端 CI | 本轮未运行 | 配置不等于运行证据 |
| Git 与外发 | 本地基线和修复提交；ZIP 清单记录最终提交与各文件 SHA256 | 无外部推送；未选开源许可证；不能伪称已有远端历史或云端 CI 成绩 |

本轮修复：发布包与 wheel 纳入 CSS/JPG；文字和结构化日序在输入阶段拒绝越界；跨城停留时间按必去地点容量调整后再判断不可满足；网页读取后端超时上限并增加30秒返回缓冲；“增加一天”上限统一为30天。

证据目录：`{evidence_dir.relative_to(ROOT).as_posix() if evidence_dir.is_absolute() else evidence_dir.as_posix()}`。源码和配置哈希保存在机器摘要中；文档与摘要自身不纳入该集合以避免自引用，发布包清单另外覆盖它们。

发布包复核：`python -m scripts.package_public`，然后 `python -m scripts.check_release --smoke-ui`。`artifacts/release/manifest.json` 包含包哈希、最终提交及工作区是否干净。源码 ZIP 故意不带 `.git`；单独的 `history.bundle` 保留本地公开源码历史。
'''
    (ROOT / "docs/ACCEPTANCE.md").write_text(overview, encoding="utf-8")
    return {"tests": counts["tests"], "snapshot": "docs/acceptance_snapshot.json"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    print(json.dumps(record(parser.parse_args().evidence_dir)))
