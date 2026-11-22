# Vela 当前能力矩阵（唯一真源：已实现 / 未实现 / 明确豁免）

> 最后更新：2026-11-22 · 当前开发版本：`0.7.0`（已发布稳定版：`v0.6.0`）
> 本文档是 Vela 语言特性、运行时与标准库状态的**唯一当前真源**。
> 历史阶段记录见 [V0.3-CLOSURE.md](V0.3-CLOSURE.md)、[V0.4-CLOSURE.md](V0.4-CLOSURE.md)、[V0.5-CLOSURE.md](V0.5-CLOSURE.md)、[V0.6-CLOSURE.md](V0.6-CLOSURE.md)、[V0.7-CLOSURE.md](V0.7-CLOSURE.md)；总体路线见 [V1.0-PLAN.md](V1.0-PLAN.md)。

## 1. 编译模式概览

| 模式 | CLI 开关 | 定位 |
| --- | --- | --- |
| 默认静态原生 Profile | 无额外开关 | 单态静态类型（`int64`/`float64`/`bool`/`str`/`None`），零装箱开销，数值计算极致性能（10–56x CPython） |
| 对象运行时 Profile | `--boxed` | 启用 32 字节引用计数对象头、精确帧链位图 GC + 循环终结器、容器、大整数、异常、模块、类与生成器 |
| 动态可提升整数 Profile | `--boxed-dyn` | 在 `--boxed` 基础上将全部 `int` 运算走 `rt_num_*`，int64 溢出自动无缝提升为任意精度 `bigint` |
| 保守栈扫描兼容模式 | `--gc-conservative` | 将 GC 栈根扫描从默认的精确调用帧位图切换为整段栈保守指针匹配 |

---

## 2. 已实现能力（Implemented · v0.1–v0.6 已交付并纳入 CI 门禁）

### 2.1 后端、PE 与优化管线

- **自有 x86-64 编码器与 PE32+ 直出**（`vela/x64.py`、`vela/pe.py`）：不依赖 LLVM、MSVC 或外部链接器。
- **Win64 ABI 与任意参数个数**：前 4 个参数走寄存器（`rcx/rdx/r8/r9` 或 `xmm0..xmm3`），第 5 个及以上参数走栈槽 `[rsp + 32 + (i - 4) * 8]`。
- **P1 常量折叠 + 代数化简 + 死代码消除**（`vela/opt.py`）。
- **P2 函数内联全覆盖**（`vela/inline.py`）：非递归函数全内联，递归函数深度 1 展开。
- **P3 栈帧优化**（`vela/emit.py`）：叶子函数免帧、按需影子空间、>4 KiB 大栈帧逐页探测。

### 2.2 运行时对象模型与精确 GC（`--boxed` / `--boxed-dyn`）

- **32 字节对象头**：`[0]` 类型描述符指针、`[8]` 引用计数、`[16]` GC 标记状态、`[24]` GC 链表指针；`+32` 起为 payload。
- **48 字节小对象自由链表池**：标量装箱对象（`int`/`float`/`bool`/`None`/`str`）释放时直接进入 O(1) 自由链表复用，消除循环内内核堆分配热点。
- **不朽单例**：`True`、`False`、`None` 与 `code` 函数代码对象均为不朽引用计数单例，移出 GC 清扫链表，支持 `is` 指针身份比较。
- **精确调用帧链与 live-OBJ 位图 GC**：
  - 每个含 `OBJ` 虚拟寄存器与调用点的函数生成 32 字节 GC 帧记录并链入 `gc_frame_top`（跨函数 `try/except` 展开时由 `rt_try_push`/`rt_raise` 自动恢复）。
  - 在每个 safepoint（`call`/`print`）按活跃变量区间写入 64 位 `live-OBJ` 位图。
  - `rt_gc_collect` 默认沿活动帧链 + 显式根栈 + 全局槽 + 容器子对象（`list`/`tuple`/`set`/`dict`/`code` 闭包环境）传递标记精确回收不可达对象与不可达环。
- **PEP 442 四阶段循环终结器**（`rt_set_finalizer`）：
  - 不可达环在释放前执行终结回调（每个对象至多执行一次）。
  - 内置 GC 重入保护与终结器异常捕获（`rt_gc_finalizer_errors`）。
  - 支持终结器内重新挂接（resuscitation 到全局或存活容器）后的二次传递标记保活。

### 2.3 数值、字符串与容器（`--boxed` / `--boxed-dyn`）

- **任意精度大整数 `bigint`**：超 int64 字面量、任意精度 `+ - * // %`（运行时十进制长除法，覆盖四种正负符号组合的 Python 向下取整语义）、六种比较、`abs()`、`min/max`、`str()`；`--boxed-dyn` 下 int64 溢出自动提升为 `bigint`。
- **字符串 `str` 与 `bytes`**：UTF-8 描述符、拼接 `+`、`==`/`!=`、`len()`（Unicode 码点计数）、正负索引 `s[i]`、任意步长切片 `s[start:stop:step]`（含 `s[::-1]`）、`upper()`、`lower()`、`startswith()`、`endswith()`、`find()`、f-string `f"...{expr}..."` 任意类型插值、`bytes` 字面量打印。
- **容器 `list` / `tuple` / `set` / `dict`**：
  - `list`：任意表达式字面量、正负索引读写、任意步长切片 `xs[a:b:c]`、`append`、`extend`、`pop([idx])`、`count`、`index`、`in`/`not in`、`for` 迭代、`len()`、`sum()`、`list()`。
  - `tuple`：字面量、正负索引、步长切片、多重/嵌套/星号解包 `a, *mid, b = ...`、`len()`。
  - `set`：字面量、`add()`、并集 `|`、交集 `&`、`in`/`not in`、`len()`。
  - `dict`：`int`/`str` 键、下标读写、`get(k, default)`、`keys()`、`values()`、`items()`、`in`/`not in`、`len()`、`for` 迭代。
  - **推导式**：列表推导式 `[...]`、集合推导式 `{...}`、字典推导式 `{k: v ...}`、生成器表达式 `(...)`（支持多重 `for` 与多重 `if` 过滤）。

### 2.4 函数、闭包、装饰器、类、dunder 与生成器（`--boxed`）

- **一等函数对象与词法闭包**：
  - 模块函数（`m.f`）、`sys.modules["m"].f`、局部函数与 `lambda` 均编译为 `code` 对象，搭配统一 `fn_wrap_<name>(argv)` wrapper 与 `rt_code_call_argv`，支持 0–4+ 任意参数个数与任意返回类型的运行期多目标动态调用。
  - 词法闭包（`rt_closure_new`）：嵌套函数可捕获外层只读变量与 `nonlocal` 可变 cell 并作为返回值传出，多次调用实例状态完全隔离。
- **参数协议与装饰器**：位置默认参数、关键字参数重排与默认值回填、`*args` 列表打包与 `*seq` 调用解包、`**kwargs` 字典打包与 `**mapping` 调用解包、单层/多层 `@dec` 函数装饰器与 `@dec(arg)` 带参装饰器工厂。
- **类与面向对象**：
  - `class` 定义、`__init__` 构造、实例属性读写 `self.x`、类常量属性、方法调用。
  - 单继承与多继承 **C3 线性化 MRO**（`Cls.mro()`）、子类方法重写、`isinstance(obj, Cls)`、钻石继承协同 `super()` 解析。
  - `@property` / `@x.setter` 属性拦截、自定义描述符类（`__get__`/`__set__`）、`__slots__` 属性白名单编译期/运行期校验。
- **dunder 协议**：
  - 算术：`__add__`、`__sub__`、`__mul__`、`__floordiv__`、`__mod__`、`__neg__`。
  - 比较：`__eq__`、`__ne__`、`__lt__`、`__le__`、`__gt__`、`__ge__`。
  - 容器与调用：`__len__`、`__getitem__`、`__setitem__`、`__contains__`、`__call__`、`__str__`。
  - 迭代与上下文管理：`__iter__`/`__next__`（配合 `for x in obj:`、`iter()`、`next()`、`list()`、`sum()`）、`with expr as var:`（`__enter__`/`__exit__`）。
- **生成器协程**：`yield`、`yield from`、`next(g)`、`g.send(val)` 双向通信、`g.throw(...)` 异常注入（可在生成器内 `try/except` 捕获）与 `g.close()` 关闭（触发 `finally` 清理）。
- **异常与模块**：`try/except/else/finally`、`raise ... [from ...]`、`assert`、跨帧栈展开与未捕获异常完整调用栈回溯（文件 + 行号 + 函数名 + 直接 cause）；纯 Python 模块 `import` / `from ... import` 与运行时 `sys.modules` 字典。

### 2.5 现代语法、解释器服务与 A3 标准库（v0.7 已实现）

- **Python 3.10–3.15 现代语法**（详见 [SYNTAX-3.15.md](SYNTAX-3.15.md)）：`match/case`（值/单例/或/捕获/序列+`*`/映射/类模式与 `if guard`）、`async/await`/`async for`/`async with`/异步生成器、PEP 695 泛型类型参数（`type Alias[T] = ...`、`def f[T](...):` 调用点多态单态化特化、`class C[T]:`）、`except*`、PEP 750 t-string、海象运算符 `:=`、`del`。
- **解释器服务**：`eval`/`exec`/`compile`、`__import__`/`importlib.import_module`、`gc`、`weakref`、`sys.getrefcount`、`sys.settrace`/`setprofile`、`inspect`、`threading.Thread`/`Lock`、`signal`、`faulthandler`。
- **A3 标准库与 `unittest`**：`math`（含硬件 SSE2 `sqrtsd`）、`json`、`random`、`collections`、`itertools`、`functools`、`dataclasses`、`typing`、`re`（含 `rt_str_cmp` 字典序比较）、`datetime`、`unittest`（`TestCase` + `unittest.main()` 原生运行）；支持 50+ 纯 Python 单文件与 `__init__.py` 包目录导入，缺失 C 扩展模块显式报 `ImportError`。

---

## 3. 未实现能力（Unimplemented · 按 v0.8–v0.10 路线推进）

| 能力项 | 目标阶段 | 当前状态 |
| --- | --- | --- |
| 多级异常链与完整 CPython `Lib/test` 纯 Python 子集 ≥99.5% 攻坚、10,000 随机 AST 差分 | v0.8 | 当前已达 800 随机 AST + 200 CPython 子集 |
| 后端优化 P4–P7：寄存器分配升级、块重排、循环不变量外提、常量除法、`--emit-asm` | v0.9 | P1–P3 已完成，P4–P7 待实现 |
| 全面双语（i18n 100% 键覆盖、中文 exe 运行时错误、安装包中文界面） | v0.10 | 基础 CLI/诊断分类已双语，全量文案迁移在 v0.10 |

---

## 4. 明确豁免项（Explicitly Exempted in v1.0 · 留至 v2.0+）

以下能力在 [V1.0-PLAN.md](V1.0-PLAN.md) 中**明确不属于 v1.0 范围**（v2.0 复刻 CPython 3.15 C ABI 后支持）：

1. **C 扩展模块（`.pyd` / `.dll`）**：`numpy`、`pygame`、`Dear PyGui`、`sqlite3` 等二进制 wheel。
2. **CPython C API / ABI 与 `python315.dll` 二进制兼容**：`PyObject` C 结构体二进制兼容、`ctypes` 加载任意外部 C DLL。
3. **依赖底层 OS 套接字/加密 C 层的标准库**：`socket`、`ssl`、`sqlite3`、`_json`（C 加速层，v1.0 用纯 Python 实现 `json`）。
