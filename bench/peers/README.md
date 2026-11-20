# 玄铁语料对照：Vela / CPython / Cython

> 对标版本：Vela v0.1.0 · 数据：2026-11-21 · 机器：Intel64 Family 6 Model 151 Stepping 5

语料取自 [玄铁 bench](../../../XuanTie/bench)（`cases/python_bench.py`）中 **Vela native
profile 可编译**的 6 项，迭代次数与原语料一致：空函数调用 1e6、浮点累加 1e6、乘模 1e6、
分支 1e7、嵌套循环 1e6、fib30。字符串拼接/列表/字典三项需要堆对象，v0.1 尚不支持，未纳入。

## 文件

```
cases/*.py          同一份源码：CPython 直接跑、Cython 编译成模块、Vela 编译成 exe
cases/typed/*.pyx   手工加 cdef 的 Cython 版本（上限参考，非同一份源码）
vs_peers.py         构建 + 计时 + 输出校验（stdout 必须逐字节一致）
../charts/         由 chart.py 生成的对比图与 index.html
```

## 方法

1. Cython 3.3.1 生成 C，MinGW gcc `-O2 -static-libgcc` 编译成 `.pyd`（本机无 MSVC）；
2. 进程墙钟计时，每项 5 次取最小值，再扣除该语言的空程序启动耗时；
3. 计时前先比对四种实现的 stdout，逐字节一致才计时；
4. Vela 与 CPython/Cython 使用同一份 `.py` 源码；Cython(typed) 单独标注。

复现：

```powershell
python bench\vs_peers.py --runs 5
python bench\vs_peers.py --runs 5   # 用目标 CPython 解释器运行
python bench\chart.py
```

## 结果（CPython 3.15.2 为主）

| 用例 | CPython 3.15 | CPython 3.13 | Cython 3.3 | Cython(typed) | Vela | vs 3.15 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 分支 1e7 | 681.8 ms | 918.3 ms | 231.2 ms | 7.3 ms* | 25.8 ms | 26.4x |
| 空函数调用 1e6×20 | 536.9 ms | 740.3 ms | 347.1 ms | 201.4 ms | 23.5 ms | 22.9x |
| fib30×5 | 329.3 ms | 414.1 ms | 219.2 ms | 18.9 ms | 35.4 ms | 9.3x |
| 浮点累加 1e6×20 | 455.8 ms | 665.1 ms | 151.8 ms | 8.0 ms* | 30.1 ms | 15.2x |
| 乘模 1e6×5 | 204.7 ms | 293.6 ms | 133.2 ms | 16.4 ms | 29.1 ms | 7.0x |
| 嵌套循环 1e6×20 | 399.4 ms | 633.5 ms | 178.8 ms | 0（消除）| 13.9 ms | 28.8x |

\* gcc 对 typed 版本做了自动向量化/归纳变量化简，耗时低于逐次执行的物理下限；
`嵌套循环` 被完全化简为常数，故不参与几何平均。

几何平均：**vs CPython 3.15 14.3x**、vs CPython 3.13 19.3x、vs Cython(pure) 7.2x。
对 Cython(typed) 的几何平均为 **0.7x**（Vela 在纯数值核上慢 1.7–3.5x，在 Python 函数
调用密集的 `空函数调用` 上快 8.6x）。

## 解读

- **对 CPython**：Vela 的收益来自去装箱、去字节码分派、寄存器内 int64/float64；
  连乘模这种每轮都要走运行时除法的用例仍有 7x。
- **对 Cython(pure)**：Cython 未加类型时仍是 PyObject 运算，Vela 快 5–13x；
  这条对比说明"编译"本身不等于"快"，类型信息才是。
- **对 Cython(typed)**：Cython + C 编译器拥有成熟的标量优化/向量化/内联，
  Vela v0.1 的直白代码生成在纯数值核上落后 1.7–3.5x。这是 v0.4 中端优化要补的差距，
  也是"比 Rust 快"必须跨过的门槛。

## 注意

- 该对比**不是**"Vela 比 Cython 快"的单一结论：取决于是否给 Cython 加类型、
  负载是调用密集还是数值密集；
- 所有数字都来自同一台机器、同一份源码、输出逐字节校验；
- 图表见 [../charts/index.html](../charts/index.html)。
