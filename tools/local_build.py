"""Build the current working tree into dist/ as a runnable local compiler.

Local versioning rule (permanent project rule):

  base = latest published Pre-Release tag (e.g. 0.3.0-dev.1)
  local = <base>-local.<N>

N increments on every local build and resets to 1 when the remote
Pre-Release base changes.  The staged compiler in ``dist/local`` reports this
local version via ``vela version``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
STAGE = DIST / "local"
STATE = DIST / ".vela-local.json"
REPO = "Vela-Language/Vela"


def source_version() -> str:
    text = (ROOT / "vela" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("vela/__init__.py: __version__ not found")
    return m.group(1)


def latest_prerelease(repo: str = REPO, timeout: float = 12.0) -> str | None:
    url = f"https://api.github.com/repos/{repo}/releases?per_page=20"
    headers = {"User-Agent": "vela-local-build",
               "Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            releases = json.load(resp)
    except Exception:
        return None
    pre = [r for r in releases if r.get("prerelease") and not r.get("draft")]
    if not pre:
        return None
    pre.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return pre[0]["tag_name"].lstrip("v")


def next_version(base: str, state: dict | None) -> tuple[str, int]:
    if state and state.get("base") == base:
        n = int(state.get("n", 0)) + 1
    else:
        n = 1
    return f"{base}-local.{n}", n


def read_state() -> dict | None:
    if not STATE.exists():
        return None
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build(version: str, base: str, n: int) -> None:
    DIST.mkdir(parents=True, exist_ok=True)
    if STAGE.exists():
        shutil.rmtree(STAGE)
    target = STAGE / "vela"
    shutil.copytree(ROOT / "vela", target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    init = target / "__init__.py"
    text = init.read_text(encoding="utf-8")
    text = re.sub(r'^__version__\s*=\s*".*"',
                  f'__version__ = "{version}"', text, count=1, flags=re.M)
    init.write_text(text, encoding="utf-8")

    cmd = (
        "@echo off\r\n"
        "setlocal\r\n"
        "set \"PYTHONPATH=%~dp0local;%PYTHONPATH%\"\r\n"
        "python -P -m vela %*\r\n"
        "exit /b %ERRORLEVEL%\r\n"
    )
    (DIST / "vela.cmd").write_text(cmd, encoding="ascii", newline="")
    STATE.write_text(json.dumps({"base": base, "n": n, "version": version},
                                ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
    print(f"local build: {version}")
    print(f"  compiler: {target}")
    print(f"  launcher: {DIST / 'vela.cmd'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-network", action="store_true")
    ap.add_argument("--base", default=None,
                    help="override base version (default: latest Pre-Release)")
    args = ap.parse_args()

    state = read_state()
    base = args.base
    if base is None and not args.no_network:
        base = latest_prerelease()
    if base is None:
        base = state["base"] if state and state.get("base") else source_version()

    version, n = next_version(base, state)
    if args.dry_run:
        print(version)
        return 0
    build(version, base, n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
