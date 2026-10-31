"""Create a source-only local archive using an explicit allowlist. Does not publish."""
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["README.md", "pyproject.toml", "requirements.lock", "Dockerfile", "compose.yaml",
         ".gitignore", ".dockerignore", ".env.example", ".streamlit/config.toml", "启动行旅.cmd"]
paths = [ROOT / f for f in FILES]
for directory in ["app", "scripts", "tests", "evals", "docs", ".github"]:
    paths += [p for p in (ROOT / directory).rglob("*") if p.is_file()
              and p.suffix in {".py", ".ps1", ".md", ".json", ".yml"} and "__pycache__" not in p.parts]
paths = sorted(set(paths))
out = ROOT / "artifacts/release"
out.mkdir(parents=True, exist_ok=True)
entries = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
assert not any(name.startswith(("data/", "artifacts/", ".venv/", ".local/", ".git/"))
               or (name.startswith(".env") and name != ".env.example") for name in entries)
archive = out / "travel-agent-lab-source.zip"
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
    for p in paths:
        z.write(p, p.relative_to(ROOT).as_posix())
(out / "manifest.json").write_text(json.dumps({"files": entries,
    "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "published": False}, indent=2), encoding="utf-8")
print(json.dumps({"archive": str(archive), "source_files": len(entries), "published": False}))
