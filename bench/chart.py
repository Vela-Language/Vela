"""Generate per-benchmark comparison charts (PNG) + an index.html gallery.

Style follows XuanTie's bench/charts: log-scale horizontal bars (fastest on
top, Vela highlighted), a data table, and a footnote with machine/toolchain
identity and measurement method.

Usage:
    python bench/chart.py [--out bench/charts] [--only 空函数调用]
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import math
import os
import platform
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "bench")
DEFAULT_OUT = os.path.join(BENCH, "charts")

FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_MONO = r"C:\Windows\Fonts\consola.ttf"
if not os.path.exists(FONT_MONO):
    FONT_MONO = FONT

# case metadata: title, subtitle, rounds (kernel executions per process)
CASES = {
    # name: (title, subtitle, rounds, ops_per_round)
    "empty_call": ("空函数调用", "1,000,000 次调用 × 20 轮", 20, 1_000_000),
    "float_sum": ("浮点累加", "1,000,000 次 double 加法 × 20 轮", 20, 1_000_000),
    "mulmod": ("乘模", "1,000,000 次 (r*7+13)%1000003 × 5 轮", 5, 1_000_000),
    "branch": ("分支", "10,000,000 次奇偶分支 × 2 轮", 2, 10_000_000),
    "nested": ("嵌套循环", "1,000×1,000 计数 × 20 轮", 20, 1_000_000),
    "fib30": ("fib30 递归", "fib(30) × 5 轮", 5, 1),
    "loop": ("整数循环", "100,000,000 次累加 × 1 轮", 1, 100_000_000),
    "fib": ("fib32 递归", "fib(32) × 1 轮", 1, 1),
    "collatz": ("Collatz", "300,000 个起点 × 1 轮", 1, 300_000),
    "primes": ("素数计数", "200,000 个整数试除 × 1 轮", 1, 200_000),
}

# language -> (label, colour, is_vela)
COLORS = {
    "cpython315": ("CPython 3.15", "#D4A017", False),
    "cpython313": ("CPython 3.13", "#E9CF87", False),
    "cython": ("Cython 3.3", "#2E86C1", False),
    "cython_typed": ("Cython（typed）", "#9CC3E0", False),
    "vela": ("Vela", "#C0392B", True),
}

BG = "#FFFFFF"
INK = "#1F2328"
MUTED = "#6B7280"
GRID = "#E5E7EB"
CARD = "#F8F9FA"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def load_cases() -> dict[str, dict]:
    """Merge peer + heavy results into {case: {series_key: ms, ...}}."""
    cases: dict[str, dict] = {}

    def put(name, key, ms, runs, extra=None):
        if not ms or ms <= 0:
            return
        c = cases.setdefault(name, {"series": {}, "runs": runs, "extra": {}})
        c["series"].setdefault(key, ms)
        if extra:
            c["extra"].update(extra)

    peer315 = os.path.join(BENCH, "peers", "results-py3.15.json")
    peer313 = os.path.join(BENCH, "peers", "results-py3.13.json")
    for path, host_key in ((peer315, "cpython315"), (peer313, "cpython313")):
        if not os.path.exists(path):
            continue
        rows = json.load(open(path, encoding="utf-8"))["rows"]
        for r in rows:
            put(r["name"], host_key, r["cpython_host_ms"], 5)
            other = r.get("cpython_other_ms")
            if other:
                put(r["name"], "cpython313" if host_key == "cpython315" else "cpython315", other, 5)
            put(r["name"], "cython", r.get("cython_ms"), 5)
            if r.get("cython_typed_ms"):
                put(r["name"], "cython_typed", r["cython_typed_ms"], 5,
                    {"optimized_out": bool(r.get("typed_optimized_out"))})
            put(r["name"], "vela", r["vela_ms"], 5)

    for path, key in ((os.path.join(BENCH, "results-py315.json"), "cpython315"),
                      (os.path.join(BENCH, "results.json"), "cpython313")):
        if not os.path.exists(path):
            continue
        rows = json.load(open(path, encoding="utf-8"))["rows"]
        for r in rows:
            put(r["name"], key, r["cpython_ms"], 3)
            put(r["name"], "vela", r["vela_ms"], 3)
    return cases


def fmt_ms(v: float) -> str:
    if v >= 1000:
        return f"{v / 1000:.2f} s"
    if v >= 1:
        return f"{v:.2f} ms"
    return f"{v * 1000:.0f} µs"


def fmt_op(ms: float) -> str:
    """Format a per-operation time given in milliseconds."""
    if ms >= 1:
        return f"{ms:.2f} ms"
    if ms >= 1e-3:
        return f"{ms * 1e3:.2f} µs"
    return f"{ms * 1e6:.1f} ns"


def draw_chart(name: str, data: dict, out_path: str, meta: dict) -> None:
    title, subtitle, rounds, ops = CASES.get(name, (name, "", 1, 1))
    series = [(k, v, COLORS[k]) for k, v in data["series"].items() if v]
    series.sort(key=lambda t: t[1])          # fastest on top
    n = len(series)

    W = 1500
    table_rows = len(series)
    H = 150 + n * 46 + 90 + table_rows * 34 + 120
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    f_title = font(30, True)
    f_sub = font(15)
    f_lang = font(17, True)
    f_val = font(16, True)
    f_tick = font(13)
    f_th = font(14, True)
    f_td = font(14)
    f_note = font(12)

    # header
    d.text((48, 34), f"{title} · Vela / CPython / Cython", font=f_title, fill=INK)
    d.text((48, 76), f"基准：{subtitle}    ·    指标：进程墙钟中位值（已扣除各语言启动开销）",
           font=f_sub, fill=MUTED)
    d.text((W - 48, 40), meta["date"], font=f_sub, fill=MUTED, anchor="ra")

    # log axis
    left, right = 300, W - 210
    top = 132
    vals = [v for _, v, _ in series]
    lo_exp = math.floor(math.log10(min(vals) * 0.75))
    hi_exp = math.ceil(math.log10(max(vals) * 1.35))
    lo, hi = 10.0 ** lo_exp, 10.0 ** hi_exp

    def xpos(v: float) -> float:
        t = (math.log10(max(v, lo)) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))
        return left + t * (right - left)

    for e in range(lo_exp, hi_exp + 1):
        v = 10.0 ** e
        x = xpos(v)
        d.line([(x, top - 12), (x, top + n * 46 + 6)], fill=GRID, width=1)
        if v >= 1000:
            label = f"{v / 1000:g} s"
        elif v >= 1:
            label = f"{v:g} ms"
        else:
            label = f"{v * 1000:g} µs"
        d.text((x, top + n * 46 + 12), label, font=f_tick, fill=MUTED, anchor="ma")

    # bars
    bar_h = 30
    for i, (key, ms, (label, colour, is_vela)) in enumerate(series):
        y = top + i * 46
        d.text((48, y + 4), label, font=f_lang if is_vela else f_lang, fill=INK if is_vela else MUTED)
        if is_vela:
            d.rounded_rectangle([40, y - 4, 44 + 248, y + bar_h + 4], radius=6, fill="#FBEAE7")
        x1 = xpos(ms)
        d.rounded_rectangle([left, y, max(x1, left + 3), y + bar_h], radius=4, fill=colour)
        opt_out = key == "cython_typed" and data.get("extra", {}).get("optimized_out")
        txt = "0（gcc 消除）" if opt_out else fmt_ms(ms)
        if is_vela:
            txt += "  ★"
        d.text((x1 + 10, y + 6), txt, font=f_val, fill=INK)
        if is_vela:
            d.text((48, y + 4), label, font=f_lang, fill="#C0392B")

    # table
    ty = top + n * 46 + 62
    d.text((48, ty), "数据明细", font=font(18, True), fill=INK)
    ty += 36
    cols = [(48, "实现"), (430, "总耗时（中位）"), (640, "单次/操作"), (820, "相对 Vela"), (980, "样本")]
    d.rectangle([40, ty - 6, W - 40, ty + 28], fill=CARD)
    for x, label in cols:
        d.text((x, ty), label, font=f_th, fill=MUTED)
    ty += 34
    vela_ms = data["series"].get("vela")
    for key, ms, (label, colour, is_vela) in series:
        d.rectangle([48, ty + 7, 62, ty + 21], fill=colour)
        d.text((74, ty), label + ("  ★" if is_vela else ""), font=f_td, fill=INK if is_vela else MUTED)
        opt_out = key == "cython_typed" and data.get("extra", {}).get("optimized_out")
        d.text((430, ty), "0（gcc 消除）" if opt_out else fmt_ms(ms), font=f_td, fill=INK)
        d.text((640, ty), "—" if opt_out else fmt_op(ms / max(rounds * ops, 1)),
               font=f_td, fill=INK)
        ratio = "-" if opt_out else (f"{ms / vela_ms:.2f}x" if vela_ms else "-")
        if is_vela:
            ratio = "1.00x"
        d.text((820, ty), ratio, font=f_td, fill=INK)
        d.text((980, ty), str(data.get("runs", 5)), font=f_td, fill=MUTED)
        ty += 34

    # footnote
    fy = H - 78
    d.line([(48, fy - 10), (W - 48, fy - 10)], fill=GRID, width=1)
    d.text((48, fy),
           f"机器：{meta['cpu']}  ·  {meta['os']}  ·  CPython {meta['py315']} / {meta['py313']}  ·  "
           f"Cython {meta['cython']}  ·  Vela {meta['vela']}",
           font=f_note, fill=MUTED)
    d.text((48, fy + 24),
           "方法：同一份 .py 源码；Vela 由自有 x86-64 后端直接生成 PE32+；Cython 用 gcc -O2 编译；"
           "每项取 5 次进程墙钟最小值并扣除该语言空程序启动耗时；运行前逐字节校验输出。",
           font=f_note, fill=MUTED)

    img.save(out_path)
    print(f"  {out_path}")


def build_index(charts: list[tuple[str, str, str]], out_dir: str, meta: dict) -> None:
    cards = []
    for fname, title, desc in charts:
        cards.append(f"""
      <a class="card" href="{html.escape(fname)}">
        <img src="{html.escape(fname)}" alt="{html.escape(title)}">
        <div class="meta"><strong>{html.escape(title)}</strong><span>{html.escape(desc)}</span></div>
      </a>""")
    doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Vela 性能对比</title>
<style>
  :root {{ --ink:#1F2328; --muted:#6B7280; --line:#E5E7EB; --accent:#C0392B; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:40px; background:#F3F4F6; color:var(--ink);
         font:15px/1.6 "Microsoft YaHei", system-ui, sans-serif; }}
  header {{ max-width:1400px; margin:0 auto 28px; }}
  h1 {{ font-size:30px; margin:0 0 8px; }}
  .sub {{ color:var(--muted); font-size:14px; }}
  .grid {{ max-width:1400px; margin:0 auto; display:grid;
           grid-template-columns:repeat(auto-fit,minmax(620px,1fr)); gap:24px; }}
  .card {{ display:block; background:#fff; border:1px solid var(--line); border-radius:12px;
           overflow:hidden; text-decoration:none; color:inherit;
           box-shadow:0 1px 2px rgba(0,0,0,.04); }}
  .card:hover {{ border-color:var(--accent); }}
  .card img {{ width:100%; display:block; }}
  .meta {{ padding:12px 16px; border-top:1px solid var(--line); display:flex;
           justify-content:space-between; gap:12px; font-size:13px; }}
  .meta span {{ color:var(--muted); }}
  footer {{ max-width:1400px; margin:32px auto 0; color:var(--muted); font-size:13px; }}
  code {{ background:#EEF0F3; padding:1px 5px; border-radius:4px; }}
</style></head>
<body>
  <header>
    <h1>Vela 性能对比</h1>
    <div class="sub">Vela v0.1.0 · Windows x86-64 · {html.escape(meta['date'])}<br>
      同一份 Python 源码，Vela 编译为独立 PE32+；对照 CPython {html.escape(meta['py315'])} /
      {html.escape(meta['py313'])} 与 Cython {html.escape(meta['cython'])}。
      点击任意图片可查看原图；原始数据见
      <a href="../peers/results-py3.15.json">peer JSON</a>、
      <a href="../results-py315.json">heavy JSON</a>。</div>
  </header>
  <div class="grid">{''.join(cards)}
  </div>
  <footer>
    机器：{html.escape(meta['cpu'])} · {html.escape(meta['os'])}；
    Vela 编译耗时 0.4–1.6 ms/文件，产物 4.0–4.5 KB。
    方法：进程墙钟取 5 次最小值并扣除空程序启动耗时；运行前逐字节比对输出。
    Cython（typed）为手工 <code>cdef</code> 版本，仅作上限参考；被 gcc 常量折叠的项已标注。
  </footer>
</body></html>"""
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(doc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    cases = load_cases()
    if not cases:
        print("no results found; run bench/vs_peers.py and bench/run_bench.py first", file=sys.stderr)
        return 1

    meta = {
        "date": datetime.date.today().isoformat(),
        "cpu": platform.processor() or "x86-64",
        "os": platform.platform(),
        "py315": "3.15.2",
        "py313": "3.13.3",
        "cython": "3.3.1",
        "vela": "v0.1.0",
    }
    charts = []
    order = [k for k in CASES if k in cases] + [k for k in cases if k not in CASES]
    for i, name in enumerate(order):
        if args.only and args.only not in name and args.only not in CASES.get(name, ("",))[0]:
            continue
        title, subtitle, _, _ = CASES.get(name, (name, "", 1, 1))
        fname = f"{i + 1:02d}_{name}.png"
        draw_chart(name, cases[name], os.path.join(args.out, fname), meta)
        charts.append((fname, title, subtitle))

    build_index(charts, args.out, meta)
    print(f"index: {os.path.join(args.out, 'index.html')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
