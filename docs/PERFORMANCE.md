# Vela 性能报告

> 对标版本：Vela v0.1.0 · 测量日期：2026-11-21 · 平台：Windows 11 x86-64

## 1. 环境与方法

| 项 | 值 |
| --- | --- |
| CPU | Intel64 Family 6 Model 151 Stepping 5（12 逻辑核） |
| OS | Windows 11 (10.0.22621) |
| CPython | 3.15.2 与 3.13.3 |
| Cython | 3.3.1（两套解释器各装一份） |
| C 编译器 | MinGW gcc（MSYS2 ucrt64），`-O2 -static-libgcc` |
| Vela | v0.1.0，自有 x86-64 后端 |
| 计时 | `time.perf_counter()` 包住 `subprocess.run`，取 N 次最小值 |
| 正确性 | 计时前逐字节比对 Vela 与 CPython 的 stdout |

复现：

```powershell
python bench\run_bench.py --repeat 3                 # 4 个重负载基准（.vl）
python bench\run_bench.py --repeat 3   # 用 3.15 解释器运行即对比 3.15
python bench\vs_peers.py --runs 5                    # 玄铁语料 6 项 vs CPython/Cython
python bench\chart.py                                # 10 张对比图 + bench/charts/index.html
```

> 计时包含进程启动（CPython 约 25–30 ms、Vela 约 5 ms），peer 对比逐语言扣除了
> 空程序启动开销；图表页脚写明了这一点。

## 2. 对 CPython：4 个重负载基准

| 基准 | 内容 | CPython 3.13 | Vela | 加速 | CPython 3.15 | Vela | 加速 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `loop` | 1 亿次整数累加 | 4065.6 ms | 76.8 ms | **52.9x** | 3501.9 ms | 68.1 ms | **51.4x** |
| `collatz` | 30 万以内最长 Collatz 链 | 2222.8 ms | 115.7 ms | **19.2x** | 1500.9 ms | 111.5 ms | **13.5x** |
| `primes` | 20 万以内素数计数（试除） | 368.0 ms | 26.7 ms | **13.8x** | 237.5 ms | 24.5 ms | **9.7x** |
| `fib` | `fib(32)` 朴素递归 | 245.3 ms | 24.7 ms | **9.9x** | 181.7 ms | 24.2 ms | **7.5x** |

几何平均：**19.3x**（3.13）、**15.0x**（3.15）。
Vela 产物在两种解释器下是同一个 exe，差异全部来自 CPython 自身变快。

## 3. 玄铁语料：Vela / CPython / Cython

语料取自 [玄铁 bench](../../XuanTie/bench) 中 Vela 可编译的 6 项，迭代次数一致：

| 用例 | CPython 3.15 | Cython(pure) | Cython(typed) | Vela | vs 3.15 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 分支 1e7 | 681.8 ms | 231.2 ms | 7.3 ms* | 25.8 ms | 26.4x |
| 空函数调用 1e6×20 | 536.9 ms | 347.1 ms | 201.4 ms | 23.5 ms | 22.9x |
| fib30×5 | 329.3 ms | 219.2 ms | 18.9 ms | 35.4 ms | 9.3x |
| 浮点累加 1e6×20 | 455.8 ms | 151.8 ms | 8.0 ms* | 30.1 ms | 15.2x |
| 乘模 1e6×5 | 204.7 ms | 133.2 ms | 16.4 ms | 29.1 ms | 7.0x |
| 嵌套循环 1e6×20 | 399.4 ms | 178.8 ms | 0（gcc 消除） | 13.9 ms | 28.8x |

\* gcc 对 typed 版本做了自动向量化/归纳变量化简，低于逐次执行的物理下限，只能当上限参考。

几何平均：**vs CPython 3.15 14.3x**、vs CPython 3.13 19.3x、vs Cython(pure) 7.2x。
对 Cython(typed) 为 **0.7x**：Vela 在纯数值核上慢 1.7–3.5x，在 Python 调用密集的
`空函数调用` 上快 8.6x。

详细方法、逐项数字与注意事项见 [bench/peers/README.md](../bench/peers/README.md)；
对比图与 HTML 画廊见 [bench/charts/index.html](../bench/charts/index.html)。

## 4. 逐项分析

### `loop`（51–53x）

热点是 `total += i; i += 1; i < N`：寄存器内 int64、无装箱、无字节码分派。
`cmp + br` 已融合，每轮只多一条 `jo` 溢出检查。

### `fib`（7.5–9.9x）

`fib(32)` 约 705 万次调用，24.2 ms ≈ **3.4 ns/次**；CPython 3.15 约 26 ns/次。
调用固定成本（`push rbp`、保存实际用到的 callee-saved、参数/返回值搬运）已接近手写
汇编量级；再往上要靠内联。

### `collatz`（13.5–19.2x）

`//2` / `%2` 被强度削减为 `sar` / `and 1`（优化前只有 8.5x），说明中端优化比后端微调
更划算。

### `mulmod`（7.0x）与 `primes`（9.7x）

除数是变量，走 `rt_imod` 运行时调用（含 Python 符号修正），是这批用例里最慢的两项；
"不变除数倒数乘法"是下一步最明确的收益点。

### `空函数调用`（22.9x）

Vela 每次调用约 1.2 ns；Cython(pure) 每次约 17 ns（PyObject 调用）。这是原生调用约定
对解释器调用协议的胜利。

### 对 Cython(typed) 的落后（0.3–0.6x）

Cython + gcc -O2 有向量化、归纳变量化简、内联；Vela v0.1 没有。fib30 的 0.5x 主要差在
C 递归没有帧指针开销，而 Vela 每个函数都有 `push rbp / sub rsp` 与保存寄存器。

## 5. 编译器自身性能

- 单文件编译 0.4–1.6 ms（不含 Python 解释器启动）；
- 端到端 `vela build` 约 40–60 ms，其中约 25–30 ms 是 `python.exe` 启动；
- 产物 4.0–4.5 KB，无运行时 DLL 依赖；冷启动约 1–3 ms（CPython 约 25 ms）。

## 6. 与 rustc / C 的差距（诚实评估）

v0.1 没有内联、常量折叠、CSE、循环优化、逃逸分析，寄存器分配是保守的线性扫描。
因此：

- 对 **CPython 解释器**：7.5–51x，结论可靠（同机、同源码、输出逐字节校验）；
- 对 **Cython(typed) / C**：纯数值核慢 1.7–3.5x，fib 类递归慢约 2x；**没有测 rustc**，
  也不宣称超过 Rust。

要宣称"比 Rust 好"，至少需要：同机同算法的对照基准、覆盖调用/分支/内存三类负载、
公开的测量脚本与原始数据。这三项目前只完成了前两项的一半。

## 7. 下一步优化（按预期收益排序）

1. **内联**小函数（fib 这类调用密集场景，预期 1.5–3x）；
2. **常量传播 + 折叠 + 死代码消除**（去掉冗余 mov 与边界检查）；
3. **一般除法的倒数乘法**（mulmod/primes）；
4. **循环不变量外提 + 归纳变量强度削减**；
5. **寄存器分配改进**：move 合并、活跃区间分裂、减少溢出；
6. **块重排**：热路径 fallthrough，冷路径（溢出诊断）移出；
7. **栈帧优化**：省略帧指针、按需影子空间；
8. **大整数提升**：溢出时走堆上 bigint，而不是终止。
