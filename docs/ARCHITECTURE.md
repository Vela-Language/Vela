# Vela 架构

> 对标版本：Vela v0.1.0 · 平台：Windows x86-64 · 宿主 Python ≥ 3.10

## 1. 设计约束

| 约束 | 决定 |
| --- | --- |
| 只做 Windows x86-64 | 直接产出 PE32+，使用 Win64 ABI，不抽象平台层 |
| 不依赖 LLVM / 汇编器 / 链接器 | 自有 x86-64 编码器、自有 PE 写出器 |
| 前端语法 = Python | 直接消费宿主 CPython 的 `ast`，语法兼容性由 CPython 保证 |
| 编译器自身零依赖 | 纯标准库，`python -m vela` 即可运行 |
| 明确的错误必须报错 | 不在编译期静默降级；`run` 的回退是显式的、有提示的 |

## 2. 流水线

```text
 .py
  │  ast.parse（宿主 CPython parser）
  ▼
 Module AST
  │  analyze.py：覆盖检查 + 单态类型推断（固定点迭代）
  ▼
 带类型标注的 AST + FuncInfo（参数/返回/局部变量类型）
  │  lower.py：表达式/控制流展开为 CFG
  ▼
 Vela IR（非 SSA，虚拟寄存器，显式类型）
  │  regalloc.py：活跃变量分析 + 线性扫描
  ▼
 带机器位置的 IR（物理寄存器 / 栈槽）
  │  emit.py：指令选择 + Win64 ABI + 帧布局
  ▼
 .text 机器码 + .rdata 字符串 + .data 句柄 + 导入符号
  │  pe.py：节布局、导入表、IAT、节头、可选头
  ▼
 独立 .exe
```

## 3. 前端

### 3.1 为什么复用 CPython 的 parser

Python 语法的边界情况（缩进、f-string 嵌套、软关键字、类型参数、match 模式）极多。
自写 parser 的收益是"可脱离 CPython"，代价是长期追语法。Vela 的定位是"让模型写的
Python 直接跑得快"，因此**语法兼容性优先**：`ast.parse` 的输入就是 CPython 接受的
全部语法，Vela 只需决定"哪些节点能编译"。

语法版本 = 运行编译器的 Python 版本。已在 CPython 3.13.3 与 3.15.2 上跑通全部测试；
换成 3.15 后自动获得 3.15 语法，前提是 Vela 的覆盖表认识新节点（如 PEP 750 的
`TemplateStr` / `Interpolation` 目前按不支持处理）。

源码后缀：`.vl` 为推荐后缀，内容仍是普通 Python 语法；`.py` 同样接受，便于零改动接入。

### 3.2 覆盖检查

`analyze.py` 遍历 AST，遇到不支持的节点立即抛 `UnsupportedError`，带 `行:列`。
不做"部分函数降级"——一个函数要么完整编译，要么整个文件失败。

### 3.3 类型系统

- 5 种值类型：`int`(i64)、`float`(f64)、`bool`、`str`(UTF-8 字节串描述符)、`None`。
- 每个变量/参数/返回值**恰好一个静态类型**（单态）。
- 方向性放宽：`bool → int`、`int → float` 允许隐式转换；反向不允许。
- 函数签名固定点推断：未标注的函数参数/返回值由调用点与函数体约束反复迭代求解，
  默认收敛到 `int`；无法收敛则报类型错误。
- 作用域：v0.1 只有函数局部变量；模块级变量仅在模块顶层（`__main__`）有效，
  函数内引用全局变量会报错。

### 3.4 明确的语义边界

`PROFILE.md` 列出全部差异。最关键的三条：

1. `int` 是 int64，溢出触发运行时诊断并退出（不是静默回绕）；
2. `and/or` 结果规范化为 `bool`（Python 返回最后一个操作数）；
3. 字符串是 UTF-8 字节串，`len()` 按 Unicode 码点计数（与 CPython 一致），
   但切片/索引/拼接在 v0.1 不支持。

## 4. 中端 IR

`vela/ir.py`。非 SSA、显式类型、按基本块线性排列：

```text
%1 = arg0
%2 = const 10
%3 = cmp.lt %1, %2
br %3, L_then, L_else
```

- **每个源变量是一个虚拟寄存器**，赋值是 `mov` 到同一 vreg。
  因此控制流汇合处不需要 phi：活跃变量分析覆盖定义-使用区间即可。
- 指令集刻意很小：`arg/const/str/mov/bin/un/cmp/jmp/br/call/ret/print`。
- 所有控制流（`if`、`while`、`for range`、短路 `and/or`、三元表达式、链式比较）
  都在 `lower.py` 展开为基本块和跳转，IR 层没有结构化控制流。

### 4.1 `for range` 的展开

```python
for i in range(a, b, s): body
```

展开为：

```text
i = a
loop:
  if s > 0: cond = i < b
  else:     cond = i > b
  br cond, body, exit
body:
  i_target = i
  ... body ...
inc:
  i = i + s
  jmp loop
```

`s` 为字面量时只生成一个比较分支；`s` 为变量时在每个迭代头判断符号（与 Python 语义一致）。
`s == 0` 在编译期（字面量）或运行期（变量）报错。

## 5. 寄存器分配

`regalloc.py`：经典线性扫描，按 CFG 做活跃变量固定点，构造每个 vreg 的 `[start, end]`
区间（对 `live_in/live_out` 的块扩展到块边界，循环回边因此被覆盖），再扫描分配。

关键设计：**分配池只包含非易失寄存器**。

| 类别 | 分配池（callee-saved） | 暂存（volatile） |
| --- | --- | --- |
| GPR | rbx, rsi, rdi, r12–r15 | rax, r10, r11 |
| XMM | xmm6–xmm15 | xmm0–xmm5 |

由此得到一个重要不变量：**调用（用户函数/运行时/Windows API）不会破坏任何已分配值**，
所以调用点不需要"保存活跃寄存器"的额外逻辑。代价是池子小（7 个 GPR），
复杂表达式会溢出到栈槽——测试用例 `spill_int`/`spill_float` 专门覆盖这条路径。

## 6. 代码生成

`emit.py`。栈帧：

```text
[rbp-8 ...]   仅保存实际用到的 callee-saved 寄存器
[...]         溢出槽（8 字节/个）
[rbp-frame]   32 字节影子空间（Win64 调用要求）
```

- 入口：`push rbp; mov rbp,rsp; sub rsp,frame;` 保存用到的寄存器；
- 调用前 `rsp` 保持 16 字节对齐，影子空间常驻；
- 出口：恢复寄存器、`mov rsp,rbp; pop rbp; ret`。

### 6.1 指令选择

- 二元运算：`load/op/store`，dst 在寄存器时尽量原地运算；
- 除法/取模：调用运行时 `rt_idiv/rt_imod`（Python 向下取整语义）；
- 浮点比较：`cmpsd` 产生掩码 → `movq` → `and 1`，正确处理 NaN（`nan < x` 为 False，
  `nan != x` 为 True，与 Python 一致）；
- 整数比较：`cmp + setcc + movzx`；若结果只被一个 `br` 使用，则**融合为 `cmp + jcc`**
  （见 §7）。

### 6.2 Win64 ABI

| 项 | 约定 |
| --- | --- |
| 整数参数 | rcx, rdx, r8, r9（按位置） |
| 浮点参数 | xmm0–xmm3（按位置） |
| 返回值 | rax / xmm0 |
| 影子空间 | 32 字节，调用者预留 |
| 栈对齐 | 调用点 `rsp % 16 == 0` |
| 非易失 | rbx, rsi, rdi, r12–r15, xmm6–xmm15 |

v0.1 限制最多 4 个参数（不实现栈传参），超过在编译期报错。

## 7. 已实现的优化

| 优化 | 位置 | 效果 |
| --- | --- | --- |
| `cmp + br` 融合 | emit.py | 去掉 setcc/movzx/test，条件跳转少 3 条指令 |
| 2 的幂除法/取模强度削减 | lower.py | `x // 2` → `sar`，`x % 2` → `and 1`（Collatz 8.5x → 19x） |
| 只保存实际使用的 callee-saved 寄存器 | emit.py | 减少函数序言/尾声开销 |
| 非易失分配池 | regalloc.py | 调用点零保存/恢复 |
| 字符串常量去重、编译期求值字符串长度之外的常量 | lower.py/rdata | 产物 4 KB 级别 |

尚未实现（路线图）：内联、常量折叠、CSE、循环不变量外提、逃逸分析、大整数、向量化。

## 8. PE32+ 写出

`pe.py`，不经过链接器：

- 固定基址 `0x140000000`，关闭 ASLR（不生成 `.reloc`，`RELOCS_STRIPPED`）；
- 节：`.text`(RX) / `.rdata`(R) / `.data`(RW) / `.idata`(RW)；
- 导入表：`kernel32.dll`（GetStdHandle / WriteFile / ExitProcess）、`msvcrt.dll`（sprintf）；
- 代码中的导入调用：`mov r11, imm64(IAT槽); call qword ptr [r11]`，装载器负责填 IAT；
- 绝对地址（字符串描述符、句柄变量）在布局完成后由 `Asm.resolve()` 回填。

## 9. 运行时

`runtime.py`，全部用手写机器码（经自有编码器）生成，遵循 Win64 ABI，只使用 volatile 寄存器。

| 例程 | 功能 |
| --- | --- |
| `rt_init` | 取 stdout/stderr 句柄存入 `.data` |
| `rt_print_int` | 无符号除法逐位生成十进制，处理 INT64_MIN |
| `rt_print_float` | `sprintf("%.15g")` + 补 `.0`（对齐 Python 的 `7.0`） |
| `rt_print_str` | 写 `[len][bytes]` 描述符 |
| `rt_print_bool/none/nl/space` | 常量输出；换行为 CRLF（对齐 CPython 文本模式） |
| `rt_idiv/rt_imod` | x86 `idiv` + 符号修正，得到 Python 的向下取整除法/取模 |
| `rt_ipow` | 快速幂 + `jo` 溢出检查；负指数报错 |
| `rt_overflow/rt_range_error/rt_fail` | 写 stderr 后 `ExitProcess(1)` |

## 10. 正确性策略

- 编码器单测：`VirtualAlloc` 分配可执行内存，`ctypes` 直接调用生成的机器码；
- PE 单测：最小 exe 的进程退出码；
- 语言测试：45 个编译-运行用例，逐字节比对期望输出（含 CRLF）；
- 差分语料：`tests/corpus/*.vl` 共 11 个程序，Vela 与 CPython 同文件执行并比对 stdout；
- 运行时错误测试：除零、溢出、负指数、动态 `range` 步长 0 必须退出码 1；
- 基准脚本：先比对 Vela 与 CPython 的 stdout 完全一致，再计时。
- CPython `Lib/test` 覆盖调查：394 个文件 0 个落在 profile 内，故不作为回归集，
  原因与替代方案见 [CPYTHON-SUITE.md](CPYTHON-SUITE.md)。

## 11. 已知限制

1. `int` 为 int64，非任意精度（溢出即报错，不会静默截断）；
2. 无堆分配：列表/字典/字符串拼接/闭包不可用；
3. 无异常：`try/except/raise` 未实现，运行时错误直接退出；
4. 字符串 `len` 正确，但索引/切片/格式化未实现；
5. 浮点打印用 `%.15g`，与 CPython 的最短往返表示不总是逐字符一致；
6. `and/or` 结果规范化为 bool；
7. 模块级变量不能被函数引用；
8. 生成代码未做内联/循环优化，距离 rustc -O 有差距。
