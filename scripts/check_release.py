"""Check archive assets, hashes and current acceptance evidence; never publish."""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from scripts.package_public import ROOT, UI_ASSETS


def verify_archive(root=ROOT, output=None):
    root = Path(root)
    folder = Path(output) if output else root / "artifacts/release"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    archive = folder / "travel-agent-lab-source.zip"
    if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest["archive_sha256"]:
        raise ValueError("Archive digest mismatch")
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        if set(names) != set(manifest["files"]) or len(names) != len(set(names)):
            raise ValueError("Archive entries do not match manifest")
        if not set(UI_ASSETS) <= set(names):
            raise ValueError("Required UI assets are missing")
        for name, expected in manifest["files"].items():
            path = PurePosixPath(name)
            if (path.is_absolute() or ".." in path.parts or ":" in name or "\\" in name
                    or name.startswith(("data/", "artifacts/", ".local/", ".venv/", ".git/"))
                    or name.startswith(".env") and name != ".env.example"):
                raise ValueError("Unsafe or private archive entry")
            if hashlib.sha256(package.read(name)).hexdigest() != expected:
                raise ValueError(f"Archive file digest mismatch: {name}")
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
                raise ValueError(f"Working source differs from archive: {name}")
    return {"archive_files": len(names), "source_hashes": "matched", "required_ui_assets": "present",
            "published": False, "git": manifest["git"]}


def smoke_extracted_ui(archive, parent=None):
    """Run the extracted UI in an isolated process, without this checkout or live keys."""
    with tempfile.TemporaryDirectory(prefix="release-smoke-", dir=parent) as work:
        extracted = Path(work) / "source"
        with zipfile.ZipFile(archive) as package:
            package.extractall(extracted)
        code = '''
import base64, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
import httpx
import app
assert Path(app.__file__).resolve().parent == Path.cwd() / "app"
def local(method, url, **kwargs):
    body = {"status":"ok", "mode":"demo", "request_timeout_seconds":300} if url.endswith("/health") else []
    return httpx.Response(200,json=body)
httpx.request=local
from streamlit.testing.v1 import AppTest
screen=AppTest.from_file(str(Path.cwd()/"app/ui.py"),default_timeout=20).run()
assert not screen.exception, list(screen.exception)
assert screen.text_input(key="form_origin") is not None
markup="\\n".join(item.value for item in screen.markdown)
assert (Path.cwd()/"app/ui_theme.css").read_text(encoding="utf-8") in markup
assert "data:image/jpeg;base64,"+base64.b64encode((Path.cwd()/"app/assets/coastal-evening.jpg").read_bytes()).decode("ascii") in markup
print(json.dumps({"extracted_ui":"passed_AppTest", "css":"loaded", "jpg":"embedded", "original_checkout_imported":False}))
'''
        import os
        env = {**os.environ, "TRAVEL_MODE": "demo", "DEEPSEEK_API_KEY": "", "AMAP_API_KEY": "",
               "TRAVEL_API_URL": "http://127.0.0.1:8000"}
        result = subprocess.run([sys.executable, "-I", "-c", code], check=False, cwd=extracted, env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=50,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("Extracted UI smoke failed: " + result.stderr[-2500:])
        return json.loads(result.stdout.strip().splitlines()[-1])


def verify_snapshot(root=ROOT):
    data = json.loads((root / "docs/acceptance_snapshot.json").read_text(encoding="utf-8"))
    for name, expected in data["source_sha256"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Acceptance evidence is stale: {name}")
    if data["pytest"]["tests"] <= 0 or data["pytest"]["failures"] or data["pytest"]["errors"]:
        raise ValueError("Acceptance tests did not pass")
    for suite in data["offline"].values():
        if suite["passed"] != suite["total"] or suite["model_calls"] != 0:
            raise ValueError("Offline acceptance did not pass")
    return {"evidence_consistent": True, "tests": data["pytest"]["tests"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--smoke-ui", action="store_true")
    args = parser.parse_args()
    folder = args.output or ROOT / "artifacts/release"
    result = {**verify_archive(output=folder), **verify_snapshot()}
    if args.smoke_ui:
        result.update(smoke_extracted_ui(folder / "travel-agent-lab-source.zip", parent=folder))
    print(json.dumps(result))
