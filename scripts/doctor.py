"""Read-only environment report. Never print secret values or account information."""
import importlib.metadata
import json
import shutil
import sys
from urllib.parse import urlparse

from app.config import Settings

s = Settings.from_env()
print(json.dumps({"python": sys.version.split()[0], "mode": s.mode,
    "model": s.model, "model_host": urlparse(s.base_url).hostname,
    "configured": {"DEEPSEEK_API_KEY": bool(s.model_key), "AMAP_API_KEY": bool(s.amap_key)},
    "docker_cli": bool(shutil.which("docker")),
    "versions": {p: importlib.metadata.version(p) for p in
                 ["autogen-agentchat", "autogen-ext", "fastapi", "pydantic", "streamlit", "httpx"]},
    "limits": {"timeout": s.timeout, "model_calls": s.max_model_calls, "tool_calls": s.max_tool_calls,
               "repairs": s.max_repairs}}, ensure_ascii=False, indent=2))
