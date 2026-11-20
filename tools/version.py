"""Vela version utility.

SemVer x.y.z with the project's release policy:

  z  fix / patch / small feature / small performance work
  y  feature work (feat) / large performance improvement
  x  refactor / breaking change

Bumping y resets z to 0; bumping x resets y and z to 0.  z is unbounded:
0.1.9 -> 0.1.10, and 21 consecutive patches give 0.1.21 (never 0.2.1).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "vela" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def read_version() -> str:
    init_text = INIT.read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', init_text, re.M)
    if not m:
        raise SystemExit("vela/__init__.py: __version__ not found")
    version = m.group(1)
    py_text = PYPROJECT.read_text(encoding="utf-8")
    pm = re.search(r'^version\s*=\s*"([^"]+)"', py_text, re.M)
    if not pm:
        raise SystemExit("pyproject.toml: version not found")
    if pm.group(1) != version:
        raise SystemExit(
            f"version mismatch: __init__.py={version} pyproject.toml={pm.group(1)}")
    if not SEMVER.match(version):
        raise SystemExit(f"not x.y.z SemVer: {version!r}")
    return version


def write_version(version: str) -> None:
    if not SEMVER.match(version):
        raise SystemExit(f"not x.y.z SemVer: {version!r}")
    init_text = INIT.read_text(encoding="utf-8")
    init_text = re.sub(r'^(__version__\s*=\s*")[^"]+(")',
                       rf"\g<1>{version}\g<2>", init_text, count=1, flags=re.M)
    INIT.write_text(init_text, encoding="utf-8")

    py_text = PYPROJECT.read_text(encoding="utf-8")
    py_text = re.sub(r'^(version\s*=\s*")[^"]+(")',
                     rf"\g<1>{version}\g<2>", py_text, count=1, flags=re.M)
    PYPROJECT.write_text(py_text, encoding="utf-8")


def bumped(version: str, part: str) -> str:
    x, y, z = (int(n) for n in SEMVER.match(version).groups())
    if part == "z":
        z += 1
    elif part == "y":
        y += 1
        z = 0
    elif part == "x":
        x += 1
        y = 0
        z = 0
    else:
        raise SystemExit(f"unknown part {part!r} (use z|y|x)")
    return f"{x}.{y}.{z}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Vela version tool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    b = sub.add_parser("bump", help="z=patch, y=feature, x=breaking")
    b.add_argument("part", choices=["z", "y", "x"])
    b.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("set")
    s.add_argument("version")
    args = ap.parse_args()

    current = read_version()
    if args.cmd == "show":
        print(current)
        return 0
    if args.cmd == "set":
        write_version(args.version)
        print(f"{current} -> {args.version}")
        return 0
    nxt = bumped(current, args.part)
    if not args.dry_run:
        write_version(nxt)
    print(f"{current} -> {nxt}" + (" (dry-run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
