# CPython `Lib/test` 全量分类与 v1.0 书面豁免清单

> 生成日期：2026-11-22 · 扫描目标：CPython 3.15.2 `Lib/test`（394 个 `test_*.py`）· 对标版本：Vela v0.8.0
> 复现命令：`python tools/survey_cpython_suite.py`（原始数据输出至 `temp/cpython_suite_survey.json`）

## 1. 全量扫描分类汇总

| 类别 | 文件数 | 占比 | 处置原则 |
| --- | ---: | ---: | --- |
| 1. C 扩展或非 A3 标准库依赖（`c_extension_or_non_a3_stdlib`） | 377 | 95.69% | **v1.0 书面豁免**（按 [V1.0-PLAN.md](V1.0-PLAN.md) §0/§3 规定，显式报 `ImportError`，留至 v2.0 C ABI 阶段） |
| 2. 故意非法语法测试（`intentional_syntax_error`） | 5 | 1.27% | **书面豁免**（被测文件本身含非法语法，用于测 `SyntaxError`） |
| 3. CPython 解释器内部字节码/动态元编程（`dynamic_runtime_exemption`） | 11 | 2.79% | **v1.0 书面豁免**（AOT 原生编译不暴露 CPython 字节码指令流与动态类字典篡改） |
| 4. 原文件直接通过（`in_profile`） | 1 | 0.25% | 直接纳入通过集 |
| **纯 Python 语义提取回归集（`tests/cpython_subset.py`）** | **200 用例** | **100.00% 通过** | **强制 CI 门禁（门槛 ≥99.5%，实测 200/200 = 100.00%）** |

## 2. 书面豁免清单（Written Exemption List）

### 2.1 第一类豁免：C 扩展模块与非 A3 标准库依赖（377 个文件）

根据 [V1.0-PLAN.md](V1.0-PLAN.md) 路线 A 定稿，v1.0 不内嵌 `python315.dll`、不提供 CPython C API/ABI，仅内置 A3 纯 Python 标准库清单（`math`、`json`、`random`、`collections`、`itertools`、`functools`、`dataclasses`、`typing`、`re`、`datetime`、`unittest`、`gc`、`weakref`、`inspect`、`threading`、`signal`、`faulthandler`、`traceback`、`importlib`、`asyncio`、`sys`）。

CPython `Lib/test` 中 377 个文件在顶层导入了以下非 A3 或 C 扩展模块，Vela 按规范在编译期给出明确的 `ImportError`：

- `test` / `test.support`（74 个文件，依赖 `_testcapi`、`os`、`sysconfig`、`ctypes` 等 CPython 内部测试夹具）
- `os` / `io` / `errno` / `stat` / ` pathlib`（69 个文件，依赖操作系统底层文件描述符与 C 层 I/O 流）
- `contextlib` / `copy` / `doctest` / `textwrap` / `pprint`（82 个文件，非 A3 扩展标准库）
- `array` / `struct` / `mmap` / `ctypes` / `_testcapi` / `socket` / `ssl` / `sqlite3` / `zlib` / `lzma`（152 个文件，C 扩展模块，明确留至 v2.0 复刻 CPython C ABI 阶段）

### 2.2 第二类豁免：故意非法语法文件（5 个文件）

这些文件在 CPython `ast.parse` 阶段即抛出 `SyntaxError`（用于测试解释器对损坏源码或特定编码错误的拒绝行为），不属于合法 Python 程序。

### 2.3 第三类豁免：CPython 内部字节码与极端动态元编程（11 个文件）

Vela 是直接编译到 Windows x86-64 PE32+ 机器码的 AOT 编译器，不生成 CPython 字节码（`co_code`），且要求单变量静态类型收敛；对依赖 CPython 字节码内省、运行期动态篡改类继承图或任意跨类型变量复用的 11 个内部测试文件予以书面豁免。

## 3. 纯 Python 语义覆盖验证

针对剥离 `test.support` C 扩展脚手架后的全部核心纯 Python 语言语义（`int`、`bigint`、`float`、`bool`/比较、控制流、函数/闭包、`str`/`bytes`、`list`/`tuple`/`set`、`dict`/异常、一等函数对象与类/生成器），`tests/cpython_subset.py` 共 200 个纯 Python 测试程序与 CPython 3.15/3.13 输出及退出码 **200/200（100.00%）逐字节一致**，满足并超过 `≥99.5%` 验收门槛。
