"""Peer benchmark: Vela vs CPython vs Cython (pure + typed).

Workloads are the Vela-compatible subset of XuanTie's bench cases
(`I:/Project/XuanTie/bench/cases/python_bench.py`), same iteration counts:
empty call 1e6, float sum 1e6, mulmod 1e6, branch 1e7, nested 1e6, fib30.

Method
------
* Run this script with the CPython under test; Vela is compiled by that same
  interpreter (its frontend is the host `ast`), so syntax version matches.
* Vela / CPython / Cython(pure) run the *same* .py source (Cython compiles it
  as a module; importing the module executes the benchmark).
* Cython(typed) is a hand-annotated `.pyx` version (cdef long long / double),
  i.e. Cython at its intended best; it is a reference ceiling, not same-source.
  A C compiler may reduce some typed kernels to closed form (noted in output).
* Process wall time is measured (best of N); each language's startup cost is
  measured with an empty program and subtracted.
* Outputs (checksums) are compared byte-for-byte before timing.

Usage
-----
    python bench/vs_peers.py [--runs 5] [--json ...]
    H:/Python3.15.2/python.exe bench/vs_peers.py
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vela.driver import compile_source  # noqa: E402

PEERS = os.path.join(ROOT, "bench", "peers")
BUILD = os.path.join(ROOT, "temp", "peers_build")
HOST = sys.executable
HOST_VER = f"{sys.version_info.major}.{sys.version_info.minor}"
# Second CPython column; override with VELA_PY_OTHER if your install differs.
OTHER = os.environ.get("VELA_PY_OTHER") or (
    r"H:\Python3.15.2\python.exe" if sys.version_info.minor == 13
    else r"C:\Python313\python.exe")
GCC = shutil.which("gcc")


def run(cmd, cwd=None, timeout=600):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)


def best_time(cmd, cwd, runs: int) -> tuple[float, bytes]:
    best = float("inf")
    out = b""
    for _ in range(runs):
        t0 = time.perf_counter()
        p = run(cmd, cwd=cwd)
        dt = time.perf_counter() - t0
        if p.returncode != 0:
            raise RuntimeError(f"{cmd} failed: {p.stderr[:500]!r}")
        if dt < best:
            best = dt
            out = p.stdout
    return best, out


def build_cython(src: str, name: str) -> str:
    os.makedirs(BUILD, exist_ok=True)
    c_path = os.path.join(BUILD, name + ".c")
    pyd_path = os.path.join(BUILD, name + ".pyd")
    r = run([HOST, "-m", "cython", "-3", "--fast-fail", "-o", c_path, src])
    if r.returncode != 0:
        raise RuntimeError(f"cython failed for {name}: {r.stderr.decode('utf-8', 'replace')[:800]}")
    inc = sysconfig.get_paths()["include"]
    dll = os.path.join(sys.prefix, f"python{sys.version_info.major}{sys.version_info.minor}.dll")
    if not os.path.exists(dll):
        raise RuntimeError(f"python dll not found: {dll}")
    cmd = [GCC, "-shared", "-O2", "-static-libgcc", "-D_CRT_SECURE_NO_WARNINGS",
           f"-I{inc}", f"-I{os.path.join(inc, 'internal')}",
           c_path, "-o", pyd_path, dll]
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"gcc failed for {name}: {r.stderr.decode('utf-8', 'replace')[:1200]}")
    return pyd_path


def build_vela(src: str, name: str) -> str:
    with open(src, "r", encoding="utf-8-sig") as f:
        source = f.read()
    try:
        result = compile_source(source, src)
    except Exception:  # noqa: BLE001
        result = compile_source(source, src, boxed=True)
    exe = os.path.join(BUILD, name + ".exe")
    with open(exe, "wb") as f:
        f.write(result.exe)
    return exe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    if args.json is None:
        args.json = os.path.join(PEERS, f"results-py{HOST_VER}.json")

    if not GCC:
        print("gcc not found on PATH; install MSYS2/LLVM or add gcc to PATH", file=sys.stderr)
        return 1
    if not os.path.exists(OTHER):
        print(f"note: {OTHER} missing, second CPython column disabled", file=sys.stderr)
        other = None
    else:
        other = OTHER
        other_ver = run([other, "-c", "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"]).stdout.decode().strip()

    os.makedirs(BUILD, exist_ok=True)
    for pat in ("*.c", "*.pyd", "*.exe", "*.pyx", "*.py"):
        for p in glob.glob(os.path.join(BUILD, pat)):
            os.unlink(p)

    empty_py = os.path.join(BUILD, "empty_plain.py")
    with open(empty_py, "w", encoding="utf-8") as f:
        f.write("pass\n")
    empty_cy = os.path.join(BUILD, "empty_cy.py")
    with open(empty_cy, "w", encoding="utf-8") as f:
        f.write("pass\n")
    build_cython(empty_cy, "empty_cy")
    empty_typed = os.path.join(BUILD, "typed_empty.pyx")
    with open(empty_typed, "w", encoding="utf-8") as f:
        f.write("pass\n")
    build_cython(empty_typed, "typed_empty")
    empty_vela = build_vela(empty_py, "empty_vela")

    startup = {}
    startup["cpython_host"], _ = best_time([HOST, empty_py], None, 3)
    if other:
        startup["cpython_other"], _ = best_time([other, empty_py], None, 3)
    startup["cython"], _ = best_time([HOST, "-c", "import empty_cy"], BUILD, 3)
    startup["cython_typed"], _ = best_time([HOST, "-c", "import typed_empty"], BUILD, 3)
    startup["vela"], _ = best_time([empty_vela], None, 3)

    cases = sorted(glob.glob(os.path.join(PEERS, "*.py")))
    rows = []
    cyver = subprocess.run([HOST, "-m", "cython", "--version"],
                           capture_output=True).stdout.decode().strip()
    print(f"peer benchmark | CPython {platform.python_version()} | "
          f"Cython {cyver.replace('Cython version ', '')} | {platform.processor() or 'x86-64'}")
    print(f"startup: CPython{HOST_VER} {startup['cpython_host']*1000:.1f}ms"
          + (f" | CPython{other_ver} {startup['cpython_other']*1000:.1f}ms" if other else "")
          + f" | Cython {startup['cython']*1000:.1f}ms | Vela {startup['vela']*1000:.2f}ms")
    print()
    header = (f"{'case':<12}{'CPython'+HOST_VER:>12}"
              + (f"{'CPython'+other_ver:>12}" if other else "")
              + f"{'Cython':>11}{'Cy(typed)':>12}{'Vela':>10}{'vs py':>9}{'vs typed':>10}")
    print(header)
    print("-" * len(header))

    for src in cases:
        name = os.path.splitext(os.path.basename(src))[0]
        typed_src = os.path.join(PEERS, "typed", name + ".pyx")
        try:
            exe = build_vela(src, name)
            build_cython(src, name)
            have_typed = os.path.exists(typed_src)
            if have_typed:
                # Cython derives PyInit_<module> from the source file name, so
                # the copy must be named typed_<case>.pyx inside the build dir.
                staged = os.path.join(BUILD, "typed_" + name + ".pyx")
                shutil.copyfile(typed_src, staged)
                build_cython(staged, "typed_" + name)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"{name:<12} BUILD FAILED: {exc}")
            continue

        cmds = {
            "cpython_host": ([HOST, src], None),
            "cython": ([HOST, "-c", f"import {name}"], BUILD),
            "vela": ([exe], None),
        }
        if other:
            cmds["cpython_other"] = ([other, src], None)
        if have_typed:
            cmds["cython_typed"] = ([HOST, "-c", f"import typed_{name}"], BUILD)

        times, outputs = {}, {}
        try:
            for key, (cmd, cwd) in cmds.items():
                times[key], outputs[key] = best_time(cmd, cwd, args.runs)
        except RuntimeError as exc:
            print(f"{name:<12} RUN FAILED: {exc}")
            continue
        ref = outputs["cpython_host"]
        if any(o != ref for o in outputs.values()):
            print(f"{name:<12} OUTPUT MISMATCH: " +
                  ", ".join(f"{k}={v[:40]!r}" for k, v in outputs.items()))
            continue

        adj = {k: max(v - startup[k], 1e-6) for k, v in times.items()}
        speed_host = adj["cpython_host"] / adj["vela"]
        speed_typed = (adj["cython_typed"] / adj["vela"]) if "cython_typed" in adj else None
        opt_out = bool(speed_typed is not None and name in ("nested", "loop") and adj["cython_typed"] < 0.002)
        note = " [typed kernel optimized out by gcc]" if opt_out else ""
        other_cell = (f"{adj['cpython_other']*1000:>10.1f}ms" if other else "")
        typed = (f"{adj['cython_typed']*1000:>10.1f}ms" if speed_typed is not None
                 else f"{'n/a':>12}")
        print(f"{name:<12}{adj['cpython_host']*1000:>10.1f}ms{other_cell}"
              f"{adj['cython']*1000:>9.1f}ms{typed}{adj['vela']*1000:>8.2f}ms"
              f"{speed_host:>8.1f}x{(f'{speed_typed:.1f}x' if speed_typed else 'n/a'):>10}{note}")
        rows.append({
            "name": name,
            "cpython_host_ms": adj["cpython_host"] * 1000,
            "cpython_other_ms": adj.get("cpython_other", 0) * 1000,
            "cython_ms": adj["cython"] * 1000,
            "cython_typed_ms": adj.get("cython_typed", 0) * 1000,
            "vela_ms": adj["vela"] * 1000,
            "speedup_vs_cpython_host": speed_host,
            "speedup_vs_cpython_other": (adj.get("cpython_other", 0) / adj["vela"]) if other else None,
            "speedup_vs_cython": adj["cython"] / adj["vela"],
            "speedup_vs_cython_typed": speed_typed,
            "typed_optimized_out": opt_out,
            "output": ref.decode("utf-8", "replace").strip(),
        })

    print()
    if rows:
        def geo(key):
            vals = [r[key] for r in rows if r.get(key) and not r.get("typed_optimized_out", False)]
            return math.prod(vals) ** (1 / len(vals)) if vals else 0.0
        print(f"geometric mean: vs CPython{HOST_VER} {geo('speedup_vs_cpython_host'):.1f}x | "
              f"vs Cython(pure) {geo('speedup_vs_cython'):.1f}x | "
              f"vs Cython(typed) {geo('speedup_vs_cython_typed'):.1f}x "
              f"(excluding kernels gcc optimized out)")
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({
                "platform": platform.platform(),
                "python_host": platform.python_version(),
                "python_other": other_ver if other else None,
                "cython": cyver,
                "startup_ms": {k: v * 1000 for k, v in startup.items()},
                "rows": rows,
            }, f, indent=2, ensure_ascii=False)
        print(f"json: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
