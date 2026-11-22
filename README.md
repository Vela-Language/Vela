<p align="center">
  <img src="assets/vela-logo.png" alt="Vela" width="520">
</p>

# Vela（帆）

[![Release](https://github.com/Vela-Language/Vela/actions/workflows/release.yml/badge.svg)](https://github.com/Vela-Language/Vela/actions/workflows/release.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

> v0.9.0 开发中 · Windows x86-64 · Python 语法 · 不依赖 LLVM / 不依赖 CPython 运行时

Vela 是一个把 Python 源码直接编译成独立 `.exe` 的 AOT 编译器。前端复用宿主 CPython 的
parser（语法版本 = 宿主解释器版本），中端做单态类型推断与静态检查，后端完全从零实现：
自有 x86-64 指令编码器 → 线性扫描寄存器分配 → 直接写出 PE32+ 可执行文件。

```text
fib.vl ──ast──> 类型推断 ──IR──> 寄存器分配 ──> x86-64 机器码 ──> PE32+ ──> fib.exe
                                                                 (4 KB, 无运行时依赖)
```

`.vl` 是 Vela 源码后缀，文件内容就是普通 Python 语法；`.py` 同样可以直接编译，
便于把现有脚本零改动接入。

## 为什么是 Python 语法

- 主流旗舰模型的训练分布严重偏向 Python，用 Python 语法写代码对模型最"顺手"；
- Python 生态（库、工具、习惯）无需重学；
- 语法成本为零，性能收益由编译器拿走：同一份源码，解释执行 vs 原生执行。

## v0.1 能做什么

| 能力 | 状态 |
| --- | --- |
| `int`（int64）/ `float`（f64）/ `bool` / `str` / `None` | 支持 |
| 函数、递归、4 参数以内、Win64 调用约定 | 支持 |
| `if/elif/else`、`while`、`for range(...)`、`break/continue` | 支持 |
| `+ - * / // % **`、位运算、比较、短路布尔、三元表达式 | 支持 |
| `print`、`len`（按 Unicode 码点）、`abs`、`min`、`max`、`int/float/bool` | 支持 |
| 列表/字典/类/异常/生成器/f-string/import/闭包 | 不支持（v0.1 直接编译期报错） |
| 超出 native profile 的 `.py` | `vela run` 自动回退 CPython 解释执行 |

## 性能（同一份源码，best of 3）

| 基准 | CPython 3.13 | Vela | 加速 | CPython 3.15 | Vela | 加速 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 亿次整数循环 | 4065.6 ms | 76.8 ms | **52.9x** | 3501.9 ms | 68.1 ms | **51.4x** |
| Collatz 30 万 | 2222.8 ms | 115.7 ms | **19.2x** | 1500.9 ms | 111.5 ms | **13.5x** |
| 20 万内素数计数 | 368.0 ms | 26.7 ms | **13.8x** | 237.5 ms | 24.5 ms | **9.7x** |
| `fib(32)` 递归 | 245.3 ms | 24.7 ms | **9.9x** | 181.7 ms | 24.2 ms | **7.5x** |

几何平均 **19.3x**（3.13）/ **15.0x**（3.15）；编译 **0.4–1.6 ms/文件**，产物
**4.0–4.5 KB**，零外部依赖。

玄铁 bench 同构语料（6 项，Vela 可编译部分）对照：**vs CPython 3.15 14.3x**、
vs Cython(pure) 7.2x、vs Cython(typed) 0.7x（纯数值核落后 C 1.7–3.5x，调用密集快 8.6x）。
在线对比页：<https://vela.markjy.com/benchmark/>（10 张图 + 数据表）；
仓库内页面：[bench/charts/index.html](bench/charts/index.html)。方法见
[docs/PERFORMANCE.md](docs/PERFORMANCE.md)。

> 诚实说明：v0.1 的代码生成是"直白但正确"的（每次算术带溢出检查、无内联、无向量化），
> 因此**尚未**达到 rustc -O 的代码质量。9–53x 是对 CPython 解释器的胜利，不是对 Rust 的胜利；
> 追赶路径见 [docs/ROADMAP.md](docs/ROADMAP.md)。

## 快速开始

要求：Windows 10/11 x86-64，Python 3.10+（3.13.3 与 3.15.2 均通过全部测试）。

```powershell
# 编译成独立 exe（.vl 是 Vela 源码后缀，内容就是 Python 语法；.py 同样支持）
python -m vela build examples/hello.vl -o hello.exe
.\hello.exe

# 编译并执行（超纲文件自动回退 CPython）
python -m vela run bench/fib.vl --stats

# 只做静态检查：是否落在 native profile 内
python -m vela check examples/fib.vl -v
```

仓库根目录的 `vela.cmd` 可直接当作命令使用：

```powershell
.\vela.cmd build bench/fib.vl -o fib.exe
```

## 中英双语（i18n）

```powershell
python -m vela --help --lang zh
python -m vela check examples/fib.vl --lang zh
$env:VELA_LANG = "zh"; python -m vela run examples/fib.vl
```

当前覆盖：帮助/用法、CLI 状态消息、诊断类别、"native profile 不支持"包装与常见特性名。
类型/编译错误的细节文本、生成 exe 的运行时错误仍为英文，后续迁移到消息 ID。

## 测试与基准

```powershell
python -m unittest discover -s tests      # 44 个 unittest（编码器/PE/语言/诊断/CLI/差分）
python tests\differential.py              # 11 个 corpus 与 CPython 逐字节比对
python bench\run_bench.py --repeat 3      # 4 个重负载基准，校验输出一致
python bench\vs_peers.py --runs 5         # 玄铁语料 vs CPython 3.13/3.15 / Cython
python bench\chart.py                     # 生成 10 张对比图 + charts/index.html
```

`tests/corpus/` 是差分语料：同一份 `.vl` 分别由 Vela 与 CPython 执行并比对输出。
为什么不直接抄 CPython 的 `Lib/test`：见 [docs/CPYTHON-SUITE.md](docs/CPYTHON-SUITE.md)
（394 个测试文件中有 0 个落在当前 profile 内）。

## 文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：编译流水线、IR、寄存器分配、PE 布局、ABI、运行时
- [docs/PROFILE.md](docs/PROFILE.md)：native profile 的语法/类型规则与**语义差异**
- [docs/PERFORMANCE.md](docs/PERFORMANCE.md)：基准方法、CPython/Cython 对照、瓶颈分析
- [docs/CPYTHON-SUITE.md](docs/CPYTHON-SUITE.md)：CPython 测试集覆盖调查与回归策略
- [docs/ROADMAP.md](docs/ROADMAP.md)：从 v0.1 到"全语法 + 生态互通 + 接近 rustc"的路线
- [docs/RELEASE.md](docs/RELEASE.md)：SemVer 规则、develop/master 分支模型、自动发布与安装包
- [docs/V1.0-PLAN.md](docs/V1.0-PLAN.md)：到 v1.0（Python 3.15.2 等价）的里程碑任务清单与验收门槛
- [bench/charts/index.html](bench/charts/index.html)：10 张性能对比图

## 目录结构

```
vela/            编译器实现（无第三方依赖）
  analyze.py     覆盖检查 + 单态类型推断
  lower.py       Python AST -> Vela IR
  regalloc.py    活跃变量分析 + 线性扫描分配
  emit.py        IR -> x86-64（Win64 ABI）
  x64.py         指令编码器（自有汇编器）
  pe.py          PE32+ 写出器（含导入表）
  runtime.py     手写运行时（打印/整除/溢出诊断）
  cli.py         build / run / check / bench
tests/           unittest 套件 + corpus 差分语料
bench/           .vl 基准、玄铁语料对照（peers/）、图表（charts/）
tools/           CPython 测试集覆盖调查
examples/        示例程序（.vl）
```

## 许可

[Apache License 2.0](LICENSE)。Copyright 2026 MARKJY-China。

CI 在每次 push / PR 时用 CPython 3.13 与 3.15 跑全量回归（单测、差分语料、构建并运行
原生 exe、基准输出校验），见 [.github/workflows/ci.yml](.github/workflows/ci.yml)。
