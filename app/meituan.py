"""Read-only Meituan travel consultation through its pinned official CLI.

Natural-language results are reference material, never proof of inventory or admission.
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.admissions import safe_url
from app.schemas import TravelInfo
from app.travel_references import parse_references, targets_for

ROOT = Path(__file__).resolve().parents[1]
CLI_VERSION = "0.0.6"
CLI_ARCHIVE_URL = "https://registry.npmjs.org/@meituan-travel/ht-ai/-/ht-ai-0.0.6.tgz"
CLI_ARCHIVE_SHA256 = "880d743ce904545878f6dfe2032b81613b194ed0b3706b19b320511778f493e4"
MAX_QUERY_PLACES = 30


def query_for(trip, selection=None, facts=None):
    targets = targets_for(trip, selection, facts)
    rows = [{"city": t["city"], "place": t["name"], "dates": [str(d) for d in t["dates"]]}
            for t in targets if t["kind"] == "attraction"]
    scheduled = {r["place"] for r in rows}
    rows += [{"place": name, "date": "尚未安排具体日期"} for name in trip.constraints.must_visit if name not in scheduled]
    rows = list({json.dumps(r, sort_keys=True, ensure_ascii=False): r for r in rows}.values())
    selected = rows[:MAX_QUERY_PLACES]
    c = trip.constraints
    context = {"cities": c.cities, "start_date": str(c.start_date), "days": c.days, "people": c.people,
               "places": selected, "hotels": [{"name": t["name"], "city": t["city"], "checkin": str(min(t["dates"])),
                   "checkout": str(max(t["dates"]) + timedelta(days=1)), "rooms": c.rooms or (c.people + 1) // 2}
                   for t in targets if t["kind"] == "hotel"][:9]}
    query = ("只读查询景点入场资料，不创建、修改、支付或取消订单，不请求身份证等个人资料。"
        "请按以下城市、具体场馆及到访日期，查询开放时间、闭馆安排、最晚入场、预约要求、成人基础票价、"
        "指定日期是否可售，以及来源或预约链接。只返回查询到的信息；未明确查询到的内容写待核实。"
        "基础票、起价、套票、讲解和优惠价请区分，价格中的X或XX原样保留。"
        "同时按入住退房日期查询所列酒店的房型、每间每晚房价、税费、取消政策和酒店详情链接；"
        "查不到的酒店写未取得报价，禁止使用其他分店数据或预算估算冒充报价。"
        "每个景点或酒店单独使用三级标题，标题必须为提供的完整名称，不要改名、合并分馆或缩写。"
        "不要把有商品或可查询理解为已预约；不要推断临时闭馆或免费博物馆余量。行程信息："
        + json.dumps(context, ensure_ascii=False, separators=(",", ":")))
    fingerprint = hashlib.sha256(query.encode()).hexdigest()
    return query, fingerprint, len(selected), len(rows)


def clean_content(content, token):
    if not isinstance(content, str) or not content.strip() or len(content) > 40000:
        raise ValueError("Empty or oversized travel response")
    if token:
        content = content.replace(token, "[凭证已隐藏]")
    content = content.replace("\\n", "\n")
    content = re.sub(r"<think\b[^>]*>.*?</think>", "", content, flags=re.IGNORECASE | re.DOTALL)
    content = re.sub(r"<[^>]*>", "", content)
    # Do not load remote tracking images or expose credential-bearing result links.
    content = re.sub(r"!\[[^\]]*\]\([^\n]*?\)", "", content)
    content = re.sub(r"\[([^\]]+)\]\(([^\s)]+)\)",
        lambda m: m[0] if safe_url(m[2]) else m[1] + "（链接不可用）", content)
    content = re.sub(r"https?://[^\s<>)]+", lambda m: m[0] if safe_url(m[0]) else "（链接不可用）", content)
    return content.strip()


class MeituanTravelService:
    def __init__(self, settings, runner=None):
        self.settings = settings
        self.runner = runner or self.run_cli

    async def run_cli(self, query, city, timeout):
        node = self.settings.meituan_node_path or shutil.which("node")
        script = Path(self.settings.meituan_cli_path or ROOT / ".local/meituan-cli/dist/index.js")
        if not node or not Path(node).is_file() or not script.is_file():
            raise FileNotFoundError
        # No shell interpolation. Only this service's credential reaches the child.
        inherited = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "USERPROFILE", "HOME", "TEMP", "TMP",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE"}
        env = {k: v for k, v in os.environ.items() if k.upper() in inherited}
        env.update({"MEITUAN_HT_TOKEN": self.settings.meituan_token, "MEITUAN_RAW_JSON": "1"})
        process = subprocess.Popen(  # noqa: ASYNC220 - Windows selector loops lack asyncio subprocess support.
            [str(node), str(script), "-o", "json", "query", "--city", city,
            "--query", query, "--origin-query", query, "--channel", "clawh"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            stdout, stderr = await asyncio.to_thread(process.communicate, timeout=timeout)
            return process.returncode, stdout, stderr
        except (subprocess.TimeoutExpired, asyncio.CancelledError):
            process.kill()
            await asyncio.to_thread(process.wait, timeout=5)
            raise

    async def lookup(self, trip, timeout=None, selection=None, facts=None):
        query, fingerprint, count, total = query_for(trip, selection, facts)
        report = TravelInfo(status="unavailable", query_fingerprint=fingerprint, queried_places=count, total_places=total)
        report.queried_hotels = min(9, sum(t["kind"] == "hotel" for t in targets_for(trip, selection, facts)))
        if not self.settings.meituan_token:
            report.message = "美团旅行凭证尚未配置。"
            return report
        if not count and not report.queried_hotels:
            report.message = "还没有可查询的景点，请先生成行程或填写必去景点。"
            return report
        try:
            code, stdout, _stderr = await self.runner(query, trip.constraints.cities[0], timeout or self.settings.meituan_timeout)
            if code != 0:
                report.message = {3: "美团认证失败，请检查本机凭证或账号权限。",
                    4: "美团渠道权限校验未通过，请核对账号的旅行能力。"}.get(code, "美团资料查询暂不可用，请稍后重新生成行程。")
                return report
            if len(stdout) > 120000:
                raise ValueError("Oversized output")
            data = json.loads(stdout)
            if not isinstance(data, dict) or data.get("status") != "success":
                raise ValueError("Invalid response")
            report.content = clean_content(data.get("data"), self.settings.meituan_token)
            if not report.content:
                raise ValueError("Empty content")
            report.status = "success"
            report.references = parse_references(report.content, targets_for(trip, selection, facts))
            report.message = "美团返回的参考资料；具体票种、当日余量和临时闭馆请以原始页面为准，不代表已预约。"
        except FileNotFoundError:
            report.message = "美团查询组件未就绪，请运行项目的美团组件安装脚本。"
        except subprocess.TimeoutExpired:
            report.message = "美团资料查询超时，原有行程已保留，请稍后重试。"
        except (OSError, ValueError, TypeError, KeyError):
            report.message = "美团资料查询失败或返回内容不完整，原有行程已保留。"
        return report

    async def enrich(self, trip, previous=None, timeout=None, selection=None, facts=None):
        if not self.settings.meituan_enabled or self.settings.mode != "live":
            return
        _, fingerprint, _, _ = query_for(trip, selection, facts)
        saved = previous.travel_info if previous else None
        if (saved and saved.status == "success" and saved.query_fingerprint == fingerprint
                and datetime.now(UTC) - timedelta(minutes=10) <= saved.queried_at <= datetime.now(UTC)):
            trip.travel_info = saved.model_copy(deep=True)
            trip.trace.append({"step": "meituan_reference", "cached": True, "tool_calls": 0})
            return
        if timeout is not None and timeout < 3:
            trip.travel_info = TravelInfo(status="unavailable", message="本次规划已接近等待上限，未追加美团查询；原有行程已保留。")
            return
        trip.travel_info = await self.lookup(trip, timeout, selection, facts)
        trip.trace.append({"step": "meituan_reference", "cached": False, "status": trip.travel_info.status,
                           "places": trip.travel_info.queried_places,
                           "tool_calls": int(bool(self.settings.meituan_token and (trip.travel_info.queried_places or trip.travel_info.queried_hotels)))})
