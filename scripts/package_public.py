"""Create a source archive with required UI assets. Never publish private runtime data."""
import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["README.md", "pyproject.toml", "requirements.lock", "Dockerfile", "compose.yaml",
         ".gitignore", ".gitattributes", ".dockerignore", ".env.example", ".streamlit/config.toml", "启动行旅.cmd"]
UI_ASSETS = ("app/ui_theme.css", "app/assets/coastal-evening.jpg")


def source_files(root):
    paths = [root / f for f in [*FILES, *UI_ASSETS]]
    for directory in ["app", "scripts", "tests", "evals", "docs", ".github"]:
        paths += [p for p in (root / directory).rglob("*") if p.is_file()
                  and p.suffix in {".py", ".ps1", ".md", ".json", ".yml"} and "__pycache__" not in p.parts]
    for path in paths:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Missing or unsafe release file: {path.relative_to(root)}")
    return sorted(set(paths))


def git_revision(root):
    def git(*args):
        result = subprocess.run(["git", "-c", f"safe.directory={root.resolve().as_posix()}", *args], cwd=root,
                                capture_output=True, text=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    try:
        commit = git("rev-parse", "HEAD")
        dirty = git("status", "--porcelain", "--untracked-files=normal")
        return {"commit": commit, "dirty": bool(dirty) if dirty is not None else None}
    except OSError:
        return {"commit": None, "dirty": None}


def build_package(root=ROOT, output=None):
    root = Path(root)
    paths = source_files(root)
    out = Path(output) if output else root / "artifacts/release"
    out.mkdir(parents=True, exist_ok=True)
    entries = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    if any(name.startswith(("data/", "artifacts/", ".venv/", ".local/", ".git/"))
           or (name.startswith(".env") and name != ".env.example") for name in entries):
        raise ValueError("Private file selected for release")
    archive = out / "travel-agent-lab-source.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path in paths:
            z.write(path, path.relative_to(root).as_posix())
    manifest = {"files": entries, "required_ui_assets": list(UI_ASSETS), "git": git_revision(root),
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "published": False}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"archive": str(archive), "source_files": len(entries), "published": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(json.dumps(build_package(output=args.output)))
