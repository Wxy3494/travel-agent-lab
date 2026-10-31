"""Install the pinned, dependency-free official CLI locally without npm install hooks."""
import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import httpx
from dotenv import set_key

from app.meituan import CLI_ARCHIVE_SHA256, CLI_ARCHIVE_URL, CLI_VERSION, ROOT


def install():
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node.js >=18 is required; install it before setting up Meituan.")
    response = httpx.get(CLI_ARCHIVE_URL, timeout=30, follow_redirects=False)
    response.raise_for_status()
    blob = response.content
    if hashlib.sha256(blob).hexdigest() != CLI_ARCHIVE_SHA256:
        raise ValueError("Official CLI archive checksum mismatch")
    folder = ROOT / ".local/meituan-cli"
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        for name in ("package/package.json", "package/README.md", "package/dist/index.js"):
            member = archive.getmember(name)
            if not member.isfile():
                raise ValueError("Unexpected archive member")
            dest = folder / Path(name).relative_to("package")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.extractfile(member).read())
    set_key(ROOT / ".env", "MEITUAN_NODE_PATH", str(Path(node).resolve()))
    set_key(ROOT / ".env", "MEITUAN_CLI_PATH", str(folder / "dist/index.js"))
    print(json.dumps({"installed": True, "version": CLI_VERSION, "credential_printed": False}))


if __name__ == "__main__":
    install()
