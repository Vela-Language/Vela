# Vela v1.0 与 CPython 3.15.2 已知差异清单（Known Differences）

> 适用版本：Vela `v1.0.0` · 对标基准：CPython `3.15.2` / `3.13.3`
> 关联文档：[CAPABILITIES.md](CAPABILITIES.md) · [CPYTHON-EXEMPTIONS.md](CPYTHON-EXEMPTIONS.md) · [SYNTAX-3.15.md](SYNTAX-3.15.md) · [V1.0-PLAN.md](V1.0-PLAN.md)

Vela v1.0 是将纯 Python 源码直接编译为独立 Windows x86-64 PE32+ 可执行文件（`.exe`）的 AOT 原生编译器，**不内嵌 CPython 解释器，也不依赖 `python315.dll`**。以下列出 Vela v1.0 与 CPython 3.15.2 的全部已知边界与行为差异。

---

## 1. 无 C 扩展与 CPython C API/ABI（明确留至 v2.0）

- **不支持加载 `.pyd` / C 扩展二进制模块**：
  - Vela v1.0 不提供 `PyObject*` C 结构体二进制兼容层或 `python315.dll` 导出符号表。
  - 依赖 C 扩展的第三方库（如 `numpy`、`pygame`、`Dear PyGui`、`scipy`）以及依赖底层 C 套接字/数据库/FFI 的标准库（如 `ctypes`、`socket`、`ssl`、`sqlite3`、`zlib`、`_testcapi`）在 v1.0 中**明确不支持**，编译期会给出明确的 `ImportError`（`找不到纯 Python 模块 '...'` / `cannot find pure Python module '...'`），绝不静默跳过或崩溃。
  - CPython C ABI 兼容层（直接加载 `cp315` `.pyd` wheel）规划于 [V1.0-PLAN.md](V1.0-PLAN.md) §4 的 **v2.0 阶段**。

## 2. 编译期单态类型收敛（Static Monomorphic Variable Discipline）

- **同一变量名需保持单一静态类型**：
  - Vela 在中端通过不动点类型推断为每个变量分配确定的机器表示（`int64`、`float64`、`bool`、`str` 或特定容器/类实例 `OBJ`）。
  - 允许数值拓宽（`bool -> int`、`int -> float`），但禁止将同一变量先后赋值为不兼容类型（例如先赋 `x = 1` 再赋 `x = "hello"`），编译器会在编译期报 `VelaTypeError`。
- **整数表示与溢出语义**：
  - 默认原生 Profile 与 `--boxed` 下，`int` 局部变量采用硬件有符号 64 位整数（`int64`），算术溢出立即触发运行时错误（退出码 `1`），超 `int64` 字面量自动使用任意精度 `bigint`。
  - 开启 `--boxed-dyn` 时，全部整数加/减/乘在 `int64` 溢出时自动无缝提升为任意精度 `bigint`。

## 3. AOT 元编程与解释器内省边界

- **无 CPython 字节码流（`co_code`）**：
  - Vela 直接发射 x86-64 机器码，函数对象（`code`）不携带 CPython 字节码指令序列，因此不支持 `dis` 字节码反汇编或修改 `f_code.co_code`；代码审查与反汇编请使用 `vela build --emit-ir` 与 `vela build --emit-asm`。
- **`eval` / `exec` / `compile` 与动态导入**：
  - 在编译期可静态折叠的字符串字面量或常量变量上完整支持 `eval()`、`exec()`、`compile()` 与 `__import__()` / `importlib.import_module()`；不支持运行期从外部网络/标准输入读取任意未知源码并动态 JIT 编译。
- **类继承图固定**：
  - 支持多继承 C3 线性化 MRO、钻石继承协同 `super()`、`@property`、描述符与 `__slots__`，但类的基类列表与方法表在编译期确定，不支持运行期动态篡改 `__bases__` 或向已编译类动态注入新方法。

## 4. 标准库范围（Vela A3 纯 Python 标准库）

- Vela v1.0 内置自研 A3 标准库实现：`math`（含硬件 SSE2 `sqrtsd`）、`json`、`random`、`collections`、`itertools`、`functools`、`dataclasses`、`typing`、`re`、`datetime`、`unittest`、`traceback`、`gc`、`weakref`、`inspect`、`threading`、`signal`、`faulthandler`、`importlib`、`asyncio`、`sys`。
- 项目本地及第三方**纯 Python** 模块与 `__init__.py` 包目录可零改动导入并编译进单一 `.exe`。
