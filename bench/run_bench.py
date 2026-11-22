"""Benchmark Vela against CPython 3.13 on the same .py sources.

For every bench/*.py:
  1. compile with Vela, run the .exe, verify stdout matches CPython exactly
  2. measure wall time (best of N) for CPython and for the native .exe
  3. report compile time and image size

Usage: python bench/run_bench.py [--repeat N] [--json out.json]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vela.driver import compile_source  # noqa: E402


def best_of(fn, repeat: int) -> float:
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--json", default=None)
    ap.add_argument("--pattern", default="*.vl")
    ap.add_argument("--min-speedup", type=float, default=0.0,
                    help="fail if any benchmark speedup is below this factor")
    ap.add_argument("--min-geomean", type=float, default=0.0,
                    help="fail if geometric-mean speedup is below this factor")
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(ROOT, "bench", args.pattern)))
    excluded = {"run_bench.py", "vs_peers.py", "chart.py"}
    paths = [p for p in paths if os.path.basename(p) not in excluded]
    if not paths:
        print("no benchmarks found", file=sys.stderr)
        return 1

    print(f"Vela benchmark | {platform.processor() or 'x86-64'} | "
          f"CPython {platform.python_version()} | best of {args.repeat}")
    print()
    print(f"{'benchmark':<14}{'CPython':>12}{'Vela':>12}{'speedup':>10}"
          f"{'compile':>10}{'image':>10}")
    print("-" * 68)
    rows = []
    failures = 0
    for path in paths:
        name = os.path.splitext(os.path.basename(path))[0]
        with open(path, "r", encoding="utf-8-sig") as f:
            source = f.read()

        compile_times = []
        result = None
        for _ in range(3):
            t0 = time.perf_counter()
            result = compile_source(source, path)
            compile_times.append(time.perf_counter() - t0)
        compile_ms = min(compile_times) * 1000

        fd, exe = tempfile.mkstemp(prefix=f"vela_{name}_", suffix=".exe")
        os.close(fd)
        try:
            with open(exe, "wb") as f:
                f.write(result.exe)

            py = subprocess.run([sys.executable, path], capture_output=True, timeout=1200)
            native = subprocess.run([exe], capture_output=True, timeout=1200)
            if py.stdout != native.stdout or native.returncode != 0:
                failures += 1
                print(f"{name:<14}  OUTPUT MISMATCH")
                print("  python:", py.stdout[:200])
                print("  native:", native.stdout[:200])
                continue

            py_s = best_of(lambda: subprocess.run([sys.executable, path],
                                                  capture_output=True, timeout=1200), args.repeat)
            nat_s = best_of(lambda: subprocess.run([exe], capture_output=True,
                                                   timeout=1200), args.repeat)
            speedup = py_s / nat_s
            print(f"{name:<14}{py_s * 1000:>10.1f}ms{nat_s * 1000:>10.2f}ms"
                  f"{speedup:>9.1f}x{compile_ms:>9.2f}ms{len(result.exe) / 1024:>9.1f}KB")
            if args.min_speedup > 0 and speedup < args.min_speedup:
                failures += 1
                print(f"  FAIL TIMING GATE: {name} speedup {speedup:.2f}x < {args.min_speedup:.2f}x")
            rows.append({
                "name": name,
                "cpython_ms": py_s * 1000,
                "vela_ms": nat_s * 1000,
                "speedup": speedup,
                "compile_ms": compile_ms,
                "image_bytes": len(result.exe),
                "output": native.stdout.decode("utf-8", "replace"),
            })
        finally:
            try:
                os.unlink(exe)
            except OSError:
                pass

    print()
    if rows:
        import math
        geo = math.prod(r["speedup"] for r in rows) ** (1 / len(rows))
        total_py = sum(r["cpython_ms"] for r in rows)
        total_nat = sum(r["vela_ms"] for r in rows)
        print(f"geometric mean speedup: {geo:.1f}x   "
              f"total {total_py:.0f}ms -> {total_nat:.0f}ms")
        if args.min_geomean > 0 and geo < args.min_geomean:
            failures += 1
            print(f"FAIL TIMING GATE: geomean speedup {geo:.2f}x < {args.min_geomean:.2f}x")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"platform": platform.platform(), "python": platform.python_version(),
                       "rows": rows}, f, indent=2)
        print(f"json written to {args.json}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
