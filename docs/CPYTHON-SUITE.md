# 为什么不直接抄 CPython 的全量回归测试集

> 对标版本：Vela v0.1.0 · 调查对象：CPython 3.15.2 `Lib/test` · 日期：2026-11-21

## 结论

**抄过来不能用。** CPython 的 `Lib/test` 测的是解释器实现（导入系统、对象模型、
异常、GC、线程、C API……），不是"一个静态类型原生子集"。用 Vela 的 analyzer 逐文件
扫描 394 个 `test_*.py`：

| 结果 | 数量 |
| --- | ---: |
| 完全落在 native profile 内、可编译 | **0** |
| 首个不支持构造即失败 | 384 |
| 文件本身是"故意非法语法"（测 `compile()` 用） | 5 |
| 类型/其他编译错误 | 5 |

前 12 类阻断原因（按文件计，只记首个错误）：

| 构造 | 文件数 |
| --- | ---: |
| `import` | 149 |
| 调用属性/表达式（如 `self.assertEqual`） | 48 |
| 默认参数值 | 36 |
| `from ... import` | 30 |
| `*args` / 关键字参数 | 28 |
| 装饰器 | 18 |
| 容器字面量 | 12 |
| 嵌套函数 | 8 |
| `global` | 6 |
| `for` 遍历非 `range` 对象 | 6 |
| 属性访问 / 方法调用 | 5 |
| `try` | 4 |

这些不是"零散边缘情况"，而是测试框架的地基：`unittest` 本身要 import、类、继承、
装饰器、异常。即便把 394 个文件全部拷进 `tests/`，结果也只是 394 条
"unsupported by the native profile"，没有一条真正执行被测代码——这不叫回归测试，
只会把真实回归淹掉。

## 那应该怎么扩回归

1. **差分语料（已落地）**：`tests/differential.py` 对 `tests/corpus/*.vl` 逐个
   编译、运行，并与 CPython 的同文件输出**逐字节比对**；超纲文件记为 skipped。
   当前 11 个语料全部通过，覆盖 int64 边界、负数整除/取模、浮点比较、嵌套循环、
   `range` 动态步长、递归、Unicode `len`、布尔、类型转换、寄存器溢出压力、4 参数调用。
   ```powershell
   python tests\differential.py
   ```
2. **CPython 语义移植**：从 `test_int` / `test_float` / `test_operator` 里挑纯计算断言，
   机械改写为 `print(...)` 差分用例（不带 unittest 脚手架），逐条加入 corpus。
   这需要人工判断，不能整文件拷贝。
3. **等运行时具备对象/异常/import 后**：v0.3 计划嵌入 CPython 作为兼容运行时，
   届时才可能把 CPython 测试集跑在 Vela 的运行时上；在此之前，把它当"目标清单"
   而不是"回归套件"。
4. **编译器自身的回归**：`tests/` 下 44 个 unittest + 差分 corpus + 10 张基准图，
   全部能一条命令复现。

## 复现调查

```powershell
python tools\survey_cpython_suite.py
```

脚本只读本机 CPython 3.15 的 `Lib/test`（可用 `VELA_TEST_DIR` 指定），输出 `temp/cpython_suite_survey.json`，
不改动 CPython 安装目录。

## 原则

> 对明确的编程错误，宁可报错退出也不静默跳过。

把 394 个必然失败的测试塞进 CI，等价于把"不支持"当成"通过"——这比没有测试更危险。
回归集必须全部是**真正执行并通过**的用例；覆盖率的提升跟着 profile 扩展走，
而不是靠拷贝别人的解释器测试。
