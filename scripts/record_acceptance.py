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
    official_path = evidence_dir / "official-pages.json"
    official = json.loads(official_path.read_text(encoding="utf-8")) if official_path.is_file() else None
    official_notes = "；".join(row["place_name"] + ("取得规则" if row["authority"] == "official" else "未取得规则，保留待核实")
                              for row in official["checks"]) if official else "本轮未查询公开官网"
    meituan_path = evidence_dir / "api-check.json"
    meituan = json.loads(meituan_path.read_text(encoding="utf-8")) if meituan_path.is_file() else None
    earlier_meituan_path = ROOT / "artifacts/meituan-20261031/api-check.json"
    earlier_meituan = json.loads(earlier_meituan_path.read_text(encoding="utf-8")) if not meituan and earlier_meituan_path.is_file() else None
    browser_path = evidence_dir / "browser-check.json"
    browser = json.loads(browser_path.read_text(encoding="utf-8")) if browser_path.is_file() else None
    if browser:
        if not browser.get("passed") or not browser.get("checks"):
            raise ValueError("Browser check did not pass")
        for name, expected in browser.get("screenshots", {}).items():
            if Path(name).name != name or digest(evidence_dir / name) != expected:
                raise ValueError("Browser screenshot digest mismatch")
    supplier_path = evidence_dir / "supplier-replay-check.json"
    supplier = json.loads(supplier_path.read_text(encoding="utf-8")) if supplier_path.is_file() else None
    branch_path = evidence_dir / "identity-live-check.json"
    branch = json.loads(branch_path.read_text(encoding="utf-8")) if branch_path.is_file() else None
    assistant_path = evidence_dir / "assistant-live-check.json"
    assistant = json.loads(assistant_path.read_text(encoding="utf-8")) if assistant_path.is_file() else None
    photos_path = evidence_dir / "photos-live-check.json"
    photos = json.loads(photos_path.read_text(encoding="utf-8")) if photos_path.is_file() else None
    for probe in (assistant, photos):
        if probe and not probe.get("passed"):
            raise ValueError("Current assistant/photo live probe did not pass")
    regression_path = evidence_dir / "fault-regressions.json"
    regressions = json.loads(regression_path.read_text(encoding="utf-8")) if regression_path.is_file() else None
    if regressions and not regressions.get("passed"):
        raise ValueError("Fault injection regressions did not pass")
    round_notes = ("本轮修复：显式保留预算等条件优先于模型错误补丁；聊天增加任务状态、超时租约、旧执行者隔离和与行程同事务的结果回执，取消及崩溃后可恢复且不重复保存；费用审计从全部交通/游览活动反查账目，检查漏项、重复、归属、人数、金额与来源；交通时长包括零值均强制比较。详见 [第二轮评审修复](SECOND_REVIEW_FIXES.md)。"
                   if regressions else "本轮调整：删除面向客户的手动方案、版本、酒店/车站、分日和预约操作面板，统一由“旅游助手”接收意见、回答咨询并执行核验后的修改；对话持久保存并防止重复发送重复修改。自主模式自动选择已查询住宿位置参与算路，景点和住宿下方展示对应地点的实景照片。详见 [旅游助手与照片](TOUR_ASSISTANT.md)。")
    diagnostic_path = evidence_dir / "model-error-check.json"
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8")) if diagnostic_path.is_file() else None
    if diagnostic:
        round_notes = "本轮定位模型服务故障：一次真实最小请求返回HTTP 402及余额不足错误；最近两次规划在首个模型调用被拒绝，高德调用为零。页面后续请求会明确提示模型服务余额不足，错误记录仅保留状态码和异常类别。模型服务恢复仍待账户补充API余额，未冒称真实规划通过。"
    chat_path = evidence_dir / "chat-response-check.json"
    chat = json.loads(chat_path.read_text(encoding="utf-8")) if chat_path.is_file() else None
    if chat:
        if not chat.get("passed"):
            raise ValueError("Automatic chat replanning did not pass")
        round_notes = "本轮修复旅游助手反复拒绝修改：模型使用可执行偏好Schema，兼容轻松节奏和少步行表达；通过流式接口先回应意图，再显示重排进度，保存后返回具体变化。少步行比较地图路线的可选活动子集，保留必去点与已预约地点；指定日期修改保留其他日期，实际入场规则变化才扩大重算。详见 [旅游助手与照片](TOUR_ASSISTANT.md)。"
    snapshot = {"checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "pytest": {**counts, "report_sha256": digest(evidence_dir / "pytest.xml")},
        "ruff": "passed", "pip_check": "passed", "offline": evaluations,
        "historical_live": history, "current_live_end_to_end": ("limited_model_and_amap_chat_replanning; Meituan_disabled; admissions_pending"
                                                              if chat else "not_rerun_full_route_model_end_to_end"),
        "meituan_live": {"passed": meituan["passed"], "scope": meituan["scope"],
            "checked_at": meituan["checked_at"], "report_sha256": digest(meituan_path)} if meituan else "not_run",
        "historical_meituan_live": {"passed": earlier_meituan["passed"], "scope": earlier_meituan["scope"],
            "checked_at": earlier_meituan["checked_at"], "retested_now": False,
            "report_sha256": digest(earlier_meituan_path)} if earlier_meituan else None,
        "official_admission_pages": {"verified": official["verified"], "total": official["total"],
            "http_calls": official["http_calls"], "report_sha256": digest(official_path),
            "scope": official["scope"]} if official else "not_run",
        "current_ui": "AppTest_and_isolated_installed_browser" if browser else "AppTest; no_new_browser_visual_check",
        "browser_ui": {"passed": True, "browser": browser["browser"], "checks": browser["checks"],
            "scope": browser["scope"], "checked_at": browser["checked_at"],
            "report_sha256": digest(browser_path)} if browser else None,
        "current_supplier_probe": {**supplier, "report_sha256": digest(supplier_path)} if supplier else None,
        "hotel_identity_probe": {"scope": branch["scope"], "identity_confirmed": branch["identity_confirmed"],
            "amap_calls": branch.get("amap_calls", 0), "meituan_calls": branch.get("meituan_calls", 0),
            "report_sha256": digest(branch_path)} if branch else None,
        "assistant_live_probe": {"passed": True, "scope": assistant["scope"],
            "report_sha256": digest(assistant_path)} if assistant else None,
        "photos_live_probe": {"passed": True, "scope": photos["scope"],
            "report_sha256": digest(photos_path)} if photos else None,
        "fault_regressions": {**regressions, "report_sha256": digest(regression_path)} if regressions else None,
        "model_service_diagnostic": {**diagnostic, "report_sha256": digest(diagnostic_path)} if diagnostic else None,
        "chat_response_probe": {**chat, "report_sha256": digest(chat_path)} if chat else None,
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
| 发布包界面 | 独立进程解压后运行 AppTest，CSS 加载、JPG 嵌入通过 | 复用本机依赖，未在全新机器安装 |
| 浏览器交互 | {'本机 Chrome 实测' + str(len(browser['checks'])) + '项通过，保存界面截图' if browser else '本轮未执行'} | {browser['scope'] if browser else 'AppTest不替代浏览器验收'} |
| 旅游助手真实意图 | {'咨询与修改两次真实模型意图通过' if assistant else '真实模型理解、虚构广州行程和获准深圳副本重排通过' if chat else '本轮未执行'} | {'真实模型及高德，停用美团；入场未知仍为待核实，原始行程保留' if chat else '修改执行使用演示规划角色和地图；不代表真实路线全链路验收'} |
| 对应地点实景 | {'广东省博物馆、白天鹅宾馆的高德实景照片取得并加载通过' if photos else '本轮未执行'} | 两处小样本；不保证所有地点有照片，未替代照片使用授权审核 |
| 真实接口历史 | 2026-10-15 广州两日生成、预算修改、历史及导出；2026-10-16 武汉酒店地址定位有通过记录 | 当前源码有后续修改，本轮未重跑这些历史端到端样本 |
| 美团真实查询 | {'FastAPI 查询、保存、读取和导出通过' if meituan and meituan['passed'] else '本轮未重测；此前有通过记录' if earlier_meituan and earlier_meituan['passed'] else '本轮未通过'} | 地图/规划角色为隔离夹具；仅美团 CLI 真实请求，不代表真实路线或票务库存验收 |
| 景点参考试查 | {str(supplier['queries']) + '次只读查询取得回复，景点参考匹配通过' if supplier else '本轮无新增景点试查'} | {'酒店分店名称和金额有差异，可靠房价未通过；路线上下文为夹具，没有真实模型/地图全链路验收' if supplier else '酒店分店试查另列下行'} |
| 分店地址试查 | {'高德地点与美团只读查询已执行；' + ('分店唯一匹配' if branch['identity_confirmed'] else '未能确认分店') if branch else '本轮未执行'} | {'当前回复缺少完整门牌地址，不将其他分店价格套用；不是实时房价验收' if branch and not branch['identity_confirmed'] else '分店确认不代表房价与库存核实'} |
| 官网入场资料 | {str(official['verified']) + '/' + str(official['total']) + ' 个适配场馆实际取得规则' if official else '本轮未查询'} | {official_notes}。常规规则不代表临时公告、实时余量或预约成功 |
| 真实全覆盖 | 未通过完整验收 | 其他路线、实时房价/房态/余票、七天天气厂商、30天真实规划仍未验收 |
| Docker、云端 CI | 本轮未运行 | 配置不等于运行证据 |
| Git 与外发 | 本地基线和修复提交；ZIP 清单记录最终提交与各文件 SHA256 | 无外部推送；未选开源许可证；不能伪称已有远端历史或云端 CI 成绩 |

{round_notes} 此前表单、历史、分店、步行导航、往返交通及景点核验继续纳入完整回归。

{'浏览器检查使用独立 Chrome、演示 API 和数据库；照片使用另行真实取得的对应地点缓存作视觉验收，不代表真实路线验收。' if browser else '本轮没有浏览器截图验收。'}

证据目录：`{evidence_dir.relative_to(ROOT).as_posix() if evidence_dir.is_absolute() else evidence_dir.as_posix()}`。源码和配置哈希保存在机器摘要中；文档与摘要自身不纳入该集合以避免自引用，发布包清单另外覆盖它们。

发布包复核：`python -m scripts.package_public`，然后 `python -m scripts.check_release --smoke-ui`。`artifacts/release/manifest.json` 包含包哈希、最终提交及工作区是否干净。源码 ZIP 故意不带 `.git`；单独的 `history.bundle` 保留本地公开源码历史。
'''
    (ROOT / "docs/ACCEPTANCE.md").write_text(overview, encoding="utf-8")
    return {"tests": counts["tests"], "snapshot": "docs/acceptance_snapshot.json"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    print(json.dumps(record(parser.parse_args().evidence_dir)))
