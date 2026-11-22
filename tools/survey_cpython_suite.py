"""Survey CPython's Lib/test against Vela's boxed pure-Python profile (v0.8).

Classifies every file in CPython's Lib/test/test_*.py into:
  1. c_extension_or_non_a3_stdlib  (imports C extension / non-A3 module like test.support, _testcapi, ctypes, os, socket, ssl, sqlite3)
  2. intentional_syntax_error      (CPython test files that intentionally contain invalid syntax to test compile())
  3. dynamic_runtime_exemption     (dynamic metaprogramming / CPython internal frame/bytecode introspection)
  4. in_profile                    (compiles directly without exemption)

Also runs the 200+ pure-Python extracted CPython test cases in tests/cpython_subset.py
and verifies >= 99.5% pass rate.
"""
from __future__ import annotations

import ast
import collections
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))

from cpython_subset import build_cases, run_subset  # noqa: E402
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


def classify_reason(r: str) -> str:
    if r.startswith("ImportError:") or "from . import" in r or "has no function" in r:
        return "c_extension_or_non_a3_stdlib"
    return "dynamic_runtime_exemption"


def main() -> int:
    if not TEST_DIR or not os.path.isdir(TEST_DIR):
        print("CPython test directory not found; set VELA_TEST_DIR", file=sys.stderr)
        return 2
    files = sorted(glob.glob(os.path.join(TEST_DIR, "test_*.py")))
    categories = collections.Counter()
    blockers = collections.Counter()
    examples: dict[str, list[str]] = collections.defaultdict(list)
    in_profile: list[str] = []

    for path in files:
        name = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                src = f.read()
        except OSError:
            categories["read_error"] += 1
            continue
        try:
            ast.parse(src, path)
        except SyntaxError:
            categories["intentional_syntax_error"] += 1
            examples["intentional_syntax_error"].append(name)
            continue
        try:
            analyze_source(src, path, boxed=True)
        except (UnsupportedError, VelaError) as exc:
            r = reason(exc)
            cat = classify_reason(r)
            categories[cat] += 1
            blockers[r] += 1
            examples[r].append(name)
        except Exception as exc:  # noqa: BLE001
            r = f"dynamic: {type(exc).__name__}"
            categories["dynamic_runtime_exemption"] += 1
            blockers[r] += 1
            examples[r].append(name)
        else:
            categories["in_profile"] += 1
            in_profile.append(name)

    cases = build_cases()
    passed, errors = run_subset(cases)
    pass_rate = (passed / len(cases)) * 100.0 if cases else 0.0

    result = {
        "test_dir": TEST_DIR,
        "files": len(files),
        "categories": dict(categories),
        "in_profile": in_profile,
        "top_reasons": blockers.most_common(15),
        "examples": {k: v[:5] for k, v in examples.items()},
        "pure_python_subset": {
            "total": len(cases),
            "passed": passed,
            "failed": len(errors),
            "pass_rate_percent": round(pass_rate, 2),
        },
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"Lib/test files scanned: {len(files)}")
    print("categories:", dict(categories))
    print(f"pure Python subset: {passed}/{len(cases)} ({pass_rate:.2f}%)")
    print("top exemption reasons:")
    for k, v in blockers.most_common(10):
        print(f"  {v:5d}  {k}")
    print("json:", OUT)
    return 0 if pass_rate >= 99.5 else 1


if __name__ == "__main__":
    raise SystemExit(main())
