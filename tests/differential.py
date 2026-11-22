"""Differential regression runner: compile with Vela, compare against CPython.

For every .vl/.py file in the given paths:
  * compile with Vela; by default any compile/unsupported error fails the run
    (strict gate: 0 skips allowed, and at least 1 program must pass);
  * run the .exe and `python <file>` and compare stdout byte-for-byte
    plus exit code.

Usage:
    python tests/differential.py [paths...] [--boxed] [--allow-skip] [--json out.json]
Default path: tests/corpus.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vela.driver import compile_source  # noqa: E402
from vela.errors import VelaError  # noqa: E402


def run_paths(paths: list[str], timeout: float = 60.0,
              boxed: bool = False, dynamic_int: bool = False) -> dict:
    # CPython's stdout encoding follows the locale on Windows (GBK), while Vela
    # always writes UTF-8.  Pin the child to UTF-8 so the comparison is about
    # program semantics, not console code pages.
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    report = {"passed": 0, "skipped": [], "mismatches": [], "crashes": [], "cases": []}
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                source = f.read()
        except OSError as exc:
            report["skipped"].append({"file": path, "reason": f"read error: {exc}"})
            continue
        try:
            result = compile_source(source, path, boxed=boxed, dynamic_int=dynamic_int)
        except VelaError as exc:
            report["skipped"].append({"file": path, "reason": str(exc)})
            continue
        except SyntaxError as exc:
            report["skipped"].append({"file": path, "reason": f"syntax error: {exc.msg}"})
            continue

        fd, exe = tempfile.mkstemp(prefix="vela_diff_", suffix=".exe")
        os.close(fd)
        try:
            with open(exe, "wb") as f:
                f.write(result.exe)
            try:
                native = subprocess.run([exe], capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                report["crashes"].append({"file": path, "reason": "timeout"})
                continue
            py = subprocess.run([sys.executable, path], capture_output=True,
                                timeout=timeout, env=env)
        finally:
            try:
                os.unlink(exe)
            except OSError:
                pass

        entry = {"file": os.path.basename(path), "seconds": result.stats.seconds}
        if native.returncode != py.returncode:
            report["crashes"].append({
                "file": path,
                "reason": f"exit mismatch native={native.returncode} cpython={py.returncode}",
                "stderr": native.stderr.decode("utf-8", "replace")[:400],
            })
            continue
        if native.stdout != py.stdout:
            report["mismatches"].append({
                "file": path,
                "native": native.stdout[:400].decode("utf-8", "replace"),
                "cpython": py.stdout[:400].decode("utf-8", "replace"),
            })
            continue
        report["passed"] += 1
        report["cases"].append(entry)
    return report


def check_gate(report: dict, allow_skip: bool = False) -> bool:
    if report["mismatches"] or report["crashes"]:
        return False
    if report["passed"] <= 0:
        return False
    if not allow_skip and report["skipped"]:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - old/odd consoles
        pass
    args = list(sys.argv[1:] if argv is None else argv)
    json_out = None
    if "--json" in args:
        i = args.index("--json")
        json_out = args[i + 1]
        del args[i:i + 2]
    allow_skip = False
    if "--allow-skip" in args:
        allow_skip = True
        args.remove("--allow-skip")
    boxed = False
    dynamic_int = False
    if "--boxed" in args:
        boxed = True
        args.remove("--boxed")
    if "--boxed-dyn" in args:
        boxed = True
        dynamic_int = True
        args.remove("--boxed-dyn")

    paths: list[str] = []
    for a in args:
        paths.extend(sorted(glob.glob(a)) if any(c in a for c in "*?[") else [a])
    if not paths:
        paths = sorted(glob.glob(os.path.join(ROOT, "tests", "corpus", "*.vl")))
    t0 = time.perf_counter()
    report = run_paths(paths, boxed=boxed, dynamic_int=dynamic_int)
    dt = time.perf_counter() - t0
    for s in report["skipped"]:
        tag = "SKIP " if allow_skip else "FAIL(SKIP)"
        print(f"{tag} {os.path.basename(s['file'])}: {s['reason'][:100]}")
    for m in report["mismatches"]:
        print(f"FAIL  {os.path.basename(m['file'])}")
        print(f"      native : {m['native']!r}")
        print(f"      cpython: {m['cpython']!r}")
    for c in report["crashes"]:
        print(f"CRASH {os.path.basename(c['file'])}: {c['reason']}")
        if c.get("stderr"):
            print(f"      {c['stderr'][:200]}")
    print(f"\n{report['passed']} passed, {len(report['mismatches'])} mismatched, "
          f"{len(report['crashes'])} crashed, {len(report['skipped'])} skipped "
          f"({dt * 1000:.0f} ms)")
    if json_out:
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"json: {json_out}")
    return 0 if check_gate(report, allow_skip=allow_skip) else 1


if __name__ == "__main__":
    raise SystemExit(main())
