"""End-to-end language tests: compile a program, run the .exe, compare stdout."""
from __future__ import annotations

import unittest

from support import VelaTestCase, run_native, out_text

CASES = {
    "arith": ("print(1 + 2 * 3 - 4)\n", "3\n"),
    "precedence": ("print((1 + 2) * (3 - 1) ** 2)\n", "12\n"),
    "unary": ("print(-5 + +3, -(-7))\n", "-2 7\n"),
    "divmod_signs": ("print(7 // 2, 7 % 2, -7 // 2, -7 % 2, 7 // -2, 7 % -2)\n",
                     "3 1 -4 1 -4 -1\n"),
    "pow": ("print(2 ** 10, 3 ** 4, 1 ** 0)\n", "1024 81 1\n"),
    "bitwise": ("print(6 & 3, 6 | 3, 6 ^ 3, 1 << 10, 1024 >> 3)\n",
                "2 7 5 1024 128\n"),
    "sign_shift": ("print(-8 >> 1)\n", "-4\n"),
    "comparisons": ("print(1 < 2, 2 <= 2, 3 > 4, 4 >= 4, 5 == 5, 5 != 5)\n",
                    "True True False True True False\n"),
    "chained_cmp": ("print(1 < 2 < 3, 3 < 2 < 4, 1 == 1 == 1)\n",
                    "True False True\n"),
    "boolops": ("print(1 and 0, 1 or 0, 0 or 1, 0 and 1)\n",
                "False True True False\n"),
    "not": ("print(not 0, not 1, not True)\n", "True False False\n"),
    "if_elif": ("""def sign(n):
    if n > 0:
        return 1
    elif n < 0:
        return -1
    return 0
print(sign(5), sign(-5), sign(0))
""", "1 -1 0\n"),
    "while_break": ("""def f():
    i = 0
    total = 0
    while i < 100:
        i = i + 1
        if i % 2 == 0:
            continue
        if i > 9:
            break
        total = total + i
    return total
print(f())
""", "25\n"),
    "for_break_continue": ("""def f():
    total = 0
    for i in range(20):
        if i % 3 == 0:
            continue
        if i > 12:
            break
        total = total + i
    return total
print(f())
""", "48\n"),
    "for_step": ("""def count(a, b, s):
    n = 0
    for i in range(a, b, s):
        n = n + 1
    return n
print(count(10, 0, -2), count(0, 10, 2), count(0, 0, 3))
""", "5 5 0\n"),
    "for_range_empty": ("for i in range(5, 0):\n    print(i)\nprint('done')\n",
                        "done\n"),
    "nested_loops": ("""def f():
    c = 0
    for i in range(5):
        for j in range(5):
            if j == 2:
                break
            c = c + 1
    return c
print(f())
""", "10\n"),
    "for_body_reassign": ("""def f():
    total = 0
    for i in range(5):
        i = i * 10
        total = total + i
    return total
print(f())
""", "100\n"),
    "funcs": ("""def add(a, b):
    return a + b

def four(a, b, c, d):
    return a * 1000 + b * 100 + c * 10 + d

print(add(2, 3), four(1, 2, 3, 4))
""", "5 1234\n"),
    "recursion": ("""def fact(n):
    if n <= 1:
        return 1
    return n * fact(n - 1)
print(fact(10))
""", "3628800\n"),
    "returns_none": ("""def f():
    print('inside')

print(f())
""", "inside\nNone\n"),
    "float_arith": ("print(1.5 + 2.25, 10.0 / 4.0, 2.5 * 2.0, 7.0 - 0.5)\n",
                    "3.75 2.5 5.0 6.5\n"),
    "float_cmp": ("print(1.5 < 2.5, 2.5 <= 2.5, 3.5 > 4.5, 1.0 == 1.0)\n",
                  "True True False True\n"),
    "float_int_mix": ("x = 1\nx = x + 0.5\nprint(x)\n", "1.5\n"),
    "float_conv": ("print(int(3.99), int(-3.99), float(7), float(True))\n",
                   "3 -3 7.0 1.0\n"),
    "float_neg": ("print(-2.5, -0.5 + 1.0)\n", "-2.5 0.5\n"),
    "float_abs": ("print(abs(-2.5), abs(2.5))\n", "2.5 2.5\n"),
    "float_intdiv_unsupported": None,
    "strings": ("print('hello', 'world')\nprint(len('hello'))\n", "hello world\n5\n"),
    "string_escapes": ("print('a\\tb\\nc')\n", "a\tb\nc\n"),
    "string_unicode": ("print('你好，Vela')\n", "你好，Vela\n"),
    "string_len_unicode": ("print(len('你好'))\n", "2\n"),
    "none": ("x = None\nprint(x)\n", "None\n"),
    "minmax": ("print(min(3, 7), max(3, 7), min(-1, -5), max(2.5, 1.5))\n",
               "3 7 -5 2.5\n"),
    "abs": ("print(abs(-42), abs(0), abs(42))\n", "42 0 42\n"),
    "augassign": ("x = 10\nx += 5\nx -= 3\nx *= 2\nx //= 3\nx %= 7\nprint(x)\n",
                  "1\n"),
    "ifexp": ("def f(n):\n    return 10 if n > 0 else 20\nprint(f(1), f(-1))\n",
              "10 20\n"),
    "cond_expr_nested": ("print(1 if 0 else 2 if 0 else 3)\n", "3\n"),
    "print_empty": ("print()\nprint('x')\n", "\nx\n"),
    "pass_stmt": ("if 1 > 0:\n    pass\nprint('ok')\n", "ok\n"),
    "shadow_assign": ("x = 1\nx = 2\nx = x + 3\nprint(x)\n", "5\n"),
    "branch_assign": ("""def f(c):
    if c:
        x = 1
    else:
        x = 2
    return x
print(f(True), f(False))
""", "1 2\n"),
    "spill_int": ("""def stress(a):
    v1 = a + 1
    v2 = a + 2
    v3 = a + 3
    v4 = a + 4
    v5 = a + 5
    v6 = a + 6
    v7 = a + 7
    v8 = a + 8
    v9 = a + 9
    v10 = a + 10
    v11 = a + 11
    v12 = a + 12
    return v1 + v2 + v3 + v4 + v5 + v6 + v7 + v8 + v9 + v10 + v11 + v12
print(stress(100))
""", "1278\n"),
    "spill_float": ("""def stress(a):
    v1 = a + 1.5
    v2 = a + 2.5
    v3 = a + 3.5
    v4 = a + 4.5
    v5 = a + 5.5
    v6 = a + 6.5
    v7 = a + 7.5
    v8 = a + 8.5
    v9 = a + 9.5
    v10 = a + 10.5
    v11 = a + 11.5
    v12 = a + 12.5
    return v1 + v2 + v3 + v4 + v5 + v6 + v7 + v8 + v9 + v10 + v11 + v12
print(stress(100.0))
""", "1284.0\n"),
    "loop_carried": ("""def f(n):
    a = 0
    b = 1
    i = 0
    while i < n:
        t = a + b
        a = b
        b = t
        i = i + 1
    return a
print(f(20))
""", "6765\n"),
    "mutual_calls": ("""def is_even(n):
    if n == 0:
        return True
    return is_odd(n - 1)

def is_odd(n):
    if n == 0:
        return False
    return is_even(n - 1)

print(is_even(10), is_odd(7))
""", "True True\n"),
}


class LanguageTests(VelaTestCase):
    def test_cases(self):
        for name, case in CASES.items():
            if case is None:
                continue
            src, expected = case
            with self.subTest(name=name):
                self.assertOutput(src, expected)


class RuntimeErrorTests(VelaTestCase):
    def test_division_by_zero(self):
        proc = run_native("print(1 // 0)\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"division by zero", proc.stderr)

    def test_modulo_by_zero(self):
        proc = run_native("print(1 % 0)\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"division by zero", proc.stderr)

    def test_overflow(self):
        proc = run_native("x = 9223372036854775807\nprint(x + 1)\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"integer overflow", proc.stderr)

    def test_negative_exponent(self):
        proc = run_native("x = -2\nprint(2 ** x)\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"negative integer exponent", proc.stderr)

    def test_range_step_zero_dynamic(self):
        proc = run_native("s = 0\nfor i in range(0, 10, s):\n    print(i)\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"step must not be zero", proc.stderr)


if __name__ == "__main__":
    unittest.main()
