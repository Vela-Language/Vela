"""Survey CPython's Lib/test against the Vela native profile.

Read-only analysis: parse each test_*.py, run Vela's analyzer, record the first
unsupported construct. Output: JSON summary + Markdown table for docs.
"""
from __future__ import annotations

import ast
import collections
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from vela.driver import analyze_source  # noqa: E402
from vela.errors import UnsupportedError, VelaError  # noqa: E402

TEST_DIR = os.environ.get("VELA_TEST_DIR") or next(
    (p for p in (r"H:\Python3.15.2\Lib\test",
                 os.path.join(sys.base_prefix, "Lib", "test"))
     if os.path.isdir(p)), "")
OUT = os.path.join(ROOT, "temp", "cpython_suite_survey.json")


def reason(exc: Exception) -> str:
    msg = str(exc)
    if "unsupported by the Vela native profile (v0.1): " in msg:
        msg = msg.split("unsupported by the Vela native profile (v0.1): ", 1)[1]
    return msg.split(" (")[0].split("\n")[0][:80]


def main() -> int:
    if not TEST_DIR or not os.path.isdir(TEST_DIR):
        print("CPython test directory not found; set VELA_TEST_DIR", file=sys.stderr)
        return 2
    files = sorted(glob.glob(os.path.join(TEST_DIR, "test_*.py")))
    stats = collections.Counter()
    blockers = collections.Counter()
    examples: dict[str, list[str]] = collections.defaultdict(list)
    in_profile = []
    for path in files:
        name = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                src = f.read()
        except OSError:
            stats["read_error"] += 1
            continue
        try:
            ast.parse(src, path)
        except SyntaxError:
            stats["syntax_error"] += 1
            continue
        try:
            analyze_source(src, path)
        except UnsupportedError as exc:
            stats["unsupported"] += 1
            blockers[reason(exc)] += 1
            examples[reason(exc)].append(name)
        except VelaError as exc:
            stats["type_or_compile_error"] += 1
            blockers["type/compile: " + reason(exc)] += 1
        except Exception as exc:  # noqa: BLE001
            stats["internal_error"] += 1
            blockers["internal: " + type(exc).__name__] += 1
        else:
            stats["in_profile"] += 1
            in_profile.append(name)

    result = {
        "test_dir": TEST_DIR,
        "files": len(files),
        "stats": dict(stats),
        "in_profile": in_profile,
        "top_blockers": blockers.most_common(15),
        "examples": {k: v[:5] for k, v in examples.items()},
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"files: {len(files)}")
    print("stats:", dict(stats))
    print("top blockers:")
    for k, v in blockers.most_common(12):
        print(f"  {v:5d}  {k}")
    print("json:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
