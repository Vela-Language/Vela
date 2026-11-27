# Vela Native Profile（v0.1 静态基线说明）

> **当前能力真源提示**：本文档仅描述 v0.1 默认无装箱静态子集。包含 `--boxed` / `--boxed-dyn` 对象运行时、精确 GC、大整数、容器、异常、类与生成器在内的**完整当前能力表（已实现 / 未实现 / 明确豁免）请以 [CAPABILITIES.md](CAPABILITIES.md) 为准**。
> **Bilingual Summary**: The Vela Native Profile is a statically typed subset of Python (`int64`, `float64`, `bool`, `str`, `None`) compiled directly to standalone Windows x86-64 PE32+ executables; passing `--boxed` enables the full pure-Python 3.15 object runtime.
> 对标版本：Vela v0.10.0 · 语法前端：宿主 CPython `ast`

native profile 是 Python 的一个**静态类型子集**：落在子集内的代码被编译为原生机器码，
子集外的代码在 `vela build` 下直接报错，在 `vela run` 下显式回退 CPython。

源码后缀：`.vl`（推荐）或 `.py`，内容都是普通 Python 语法；语法版本由宿主解释器决定
（3.13.3 与 3.15.2 均已验证）。

## 1. 类型

| 类型 | 表示 | 字面量 |
| --- | --- | --- |
| `int` | 有符号 int64 | `0`, `42`, `-7` |
| `float` | IEEE-754 binary64 | `0.5`, `1e3` |
| `bool` | i64 0/1 | `True`, `False` |
| `str` | UTF-8 字节串（`[u64 len][bytes]`） | `"hello"`, `'你好'` |
| `None` | 无值 | `None` |

规则：

- 每个变量、函数参数、返回值**只有一个静态类型**；
- 允许放宽：`bool → int`、`int → float`（赋值、传参、返回时自动转换）；
- 禁止收窄：`float → int` 必须显式 `int(x)`；`int → bool` 必须显式 `bool(x)`；
- 未标注的函数类型由固定点推断；推断冲突报类型错误并给出行号。

```python
def add(a: int, b: int) -> int:   # 标注可选；不标注也能推断为 int
    return a + b

x: float = 1        # int -> float，允许
y = 1.5
y = 2               # 允许：变量已是 float，2 被转换为 2.0
```

## 2. 语句

| 语法 | 说明 |
| --- | --- |
| `x = expr` | 单目标赋值；链式/元组解包不支持 |
| `x: T = expr` | 类型标注；`T ∈ {int,float,bool,str,None}` |
| `x += expr` 等 | `+= -= *= /= //= %= **= &= |= ^= <<= >>=` |
| `if / elif / else` | 条件要求 `bool` 或 `int` |
| `while cond` | 不支持 `while ... else` |
| `for i in range(...)` | 1–3 个参数；`else` 不支持 |
| `break` / `continue` / `pass` | 支持 |
| `return [expr]` | 必须与函数返回类型一致 |
| `def f(...)` | 顶层函数；最多 4 个参数；不支持默认值/可变参数/关键字参数/装饰器 |
| `print(...)` | 仅位置参数；`sep`/`end` 等关键字不支持 |

明确不支持（编译期报错）：`class`、`import`、`try/except/finally`、`with`、
`yield`、`async/await`、`match`、`lambda`、推导式、`global`/`nonlocal`、
嵌套函数、生成器、装饰器、`assert`、`del`、`raise`、`while/for else`。

## 3. 表达式

| 语法 | 类型规则 |
| --- | --- |
| `+ - *` | int×int→int；任一 float→float（自动提升） |
| `/` | 恒为 float（与 Python 一致） |
| `// %` | 仅 int；向下取整语义；除数为 2 的幂时编译为移位/掩码 |
| `**` | int 底/指数；负指数运行期报错；溢出报错 |
| `& \| ^ << >>` | int；`>>` 为算术右移（Python 对负数语义） |
| `== != < <= > >=` | int/float/bool；链式比较支持 |
| `and / or / not` | 操作数 bool/int；**结果规范化为 bool** |
| `a if c else b` | 两分支类型必须可统一 |
| `f(...)` | 顶层函数或内建；最多 4 个位置参数 |
| `x` | 局部变量；未赋值使用报错 |

内建函数：

| 函数 | 说明 |
| --- | --- |
| `print(*args)` | 空格分隔、CRLF 结尾；`float` 用 `%.15g` + `.0` 补齐 |
| `len(s)` | str 的 Unicode 码点数（与 CPython 一致） |
| `abs(x)` | int/float；INT64_MIN 报溢出 |
| `min(a,b)` / `max(a,b)` | 两个同类型数值 |
| `int(x)` | float 截断取整（向零），bool→0/1 |
| `float(x)` | int/bool → double |
| `bool(x)` | int/float → 真值（NaN 为 True） |

## 4. 与 CPython 的语义差异

| 场景 | CPython | Vela v0.1 |
| --- | --- | --- |
| 整数范围 | 任意精度 | int64；溢出 → stderr 诊断 + 退出码 1 |
| `and/or` 结果 | 返回最后一个操作数（可能是 int） | 返回 `bool` |
| `x // y` 除零 | `ZeroDivisionError` | 诊断 + 退出码 1 |
| `range(..., 0)` | `ValueError` | 字面量：编译错误；变量：诊断 + 退出码 1 |
| `x = 1` 后 `x = 1.5` | 变量类型改变 | 变量固定为 float，`1` 被转为 `1.0` |
| 未赋值变量 | `NameError` | 编译期报错（保守的必定赋值分析） |
| `print` 换行 | 文本模式 `\r\n` | 与 CPython 一致（CRLF） |
| 浮点打印 | 最短往返表示（`repr`） | `%.15g`；整数补 `.0`；极端值可能与 CPython 不同 |
| 字符串索引/切片 | 支持 | 不支持 |
| 函数引用/高阶函数 | 支持 | 不支持 |
| 全局变量 | 支持 | 函数内不可读模块级变量 |

> v0.1 的目标是"**可编译子集的语义可信**"，不是"全 Python 语义等价"。
> 完整等价需要对象模型、异常、大整数与动态分派，见 [ROADMAP.md](ROADMAP.md)。

## 5. 错误处理原则

- 编译器对明确错误**一律报错退出**，不生成"可能能跑"的代码；
- 运行时错误统一写 stderr（`Vela runtime error: ...`）并以退出码 1 结束；
- `vela run` 的回退只针对"超出 profile"，且必须打印 `falling back to CPython`；
  `--strict` 禁止回退。

## 6. 回退机制

```powershell
python -m vela run script.py          # 能编译则原生执行，否则 CPython
python -m vela run script.py --strict # 超出 profile 直接失败
python -m vela check script.py -v     # 只检查，给出函数签名
```

回退使用当前 Python 解释器执行原文件，因此外部行为与直接 `python script.py` 一致
（包括 stdout 刷新、退出码、`sys.argv`）。

## 7. 双语诊断与语言开关（i18n / Language Switches）

- **语言优先级**：`--lang zh|en`（当次命令临时覆盖） > `VELA_LANG`（环境变量） > `--set-lang zh|en`（持久化用户配置 `~/.vela_lang`） > 默认 `en`。
- **运行时错误随编译期语言**：`vela build --lang zh`（或 `--set-lang zh` 默认中文下）编译出的 `.exe` 在发生除零、int64 溢出、负指数、`range()` 步长零等错误时输出中文 `Vela 运行时错误：...`；`--lang en` 编译出的 `.exe` 输出英文 `Vela runtime error: ...`。
- **统一术语对照**：
  - `native profile`：原生 profile
  - `type error`：类型错误
  - `syntax error`：语法错误
  - `internal error`：内部错误
  - `runtime error`：运行时错误
