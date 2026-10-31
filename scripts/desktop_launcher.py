"""Double-click startup for Windows. Reuses healthy services; never kills port owners."""
import argparse
import json
import os
import socket
import subprocess
import time
import webbrowser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


class LaunchError(Exception):
    pass


class Launcher:
    def __init__(self, root=ROOT, api_port=8000, ui_port=8501, timeout=40, env=None):
        self.root = Path(root)
        self.python = self.root / ".venv/Scripts/python.exe"
        self.api_port, self.ui_port, self.timeout = api_port, ui_port, timeout
        self.env = {**os.environ, **(env or {}), "TRAVEL_API_URL": f"http://127.0.0.1:{api_port}"}
        self.processes = []

    @property
    def url(self):
        return f"http://127.0.0.1:{self.ui_port}"

    def healthy(self, service):
        port = self.api_port if service == "api" else self.ui_port
        path = "/health" if service == "api" else "/_stcore/health"
        try:
            response = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=1, trust_env=False)
            if response.status_code != 200:
                return False
            if service == "ui":
                return response.text.strip() == "ok"
            body = response.json()
            return body.get("status") == "ok" and body.get("mode") in {"demo", "live"}
        except (httpx.HTTPError, ValueError, AttributeError):
            return False

    def start(self, service):
        if self.healthy(service):
            return False
        port = self.api_port if service == "api" else self.ui_port
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise LaunchError(f"端口 {port} 被占用，但服务未正常响应。请关闭冲突程序后再试。") from None
        if service == "api":
            command = [str(self.python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                       "--port", str(port), "--no-access-log"]
        else:
            command = [str(self.python), "-m", "streamlit", "run", "app/ui.py", "--server.address",
                       "127.0.0.1", "--server.port", str(port), "--server.headless", "true"]
        log_dir = self.root / ".local"
        log_dir.mkdir(exist_ok=True)
        with (log_dir / f"{service}-desktop.log").open("ab") as log:
            process = subprocess.Popen(command, cwd=self.root, env=self.env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.processes.append((service, process))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise LaunchError(f"{service} 启动失败。请查看项目 .local 文件夹中的 {service}-desktop.log。")
            if self.healthy(service):
                return True
            time.sleep(0.25)
        raise LaunchError(f"{service} 启动超过 {self.timeout} 秒。请稍后重试或查看启动日志。")

    def stop_new_processes(self):
        # Only handles created by this invocation; no taskkill, process-name search or port-owner kill.
        for _, process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def ensure_running(self, progress=lambda message: None):
        if not self.python.exists():
            raise LaunchError("项目运行环境缺失，请按 README 恢复 .venv 后再启动。")
        try:
            progress("正在检查并启动规划服务…")
            api_started = self.start("api")
            progress("正在检查并启动聊天页面…")
            ui_started = self.start("ui")
            return {"url": self.url, "api_started": api_started, "ui_started": ui_started,
                    "started_pids": {service: process.pid for service, process in self.processes}}
        except Exception:
            self.stop_new_processes()
            raise


def desktop():
    # No Tk/Qt dependency: Windows opens the browser via its registered URL handler.
    import ctypes

    def show_error(message):
        ctypes.windll.user32.MessageBoxW(None, message, "行旅 · 启动提示", 0x10)

    launcher = Launcher()
    try:
        result = launcher.ensure_running()
        result["browser_opened"] = bool(webbrowser.open(launcher.url))
        (ROOT / ".local").mkdir(exist_ok=True)
        (ROOT / ".local/desktop-last-launch.json").write_text(json.dumps(result), encoding="utf-8")
        if not result["browser_opened"]:
            show_error(f"服务已就绪，但默认浏览器未能打开。请访问 {launcher.url}")
    except LaunchError as exc:
        show_error(str(exc))
    except Exception:  # noqa: BLE001 - Do not expose secret-bearing startup exceptions.
        show_error("启动未完成，请查看项目 .local 文件夹中的启动日志，或重新双击启动文件。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true", help="Readiness check for automated local tests")
    args = parser.parse_args()
    if args.no_browser:
        try:
            print(json.dumps(Launcher().ensure_running(), ensure_ascii=False))
        except LaunchError as exc:
            raise SystemExit(str(exc)) from None
    else:
        desktop()
