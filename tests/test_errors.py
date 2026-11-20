"""Diagnostics: unsupported constructs and type errors must be rejected cleanly."""
from __future__ import annotations

import unittest

from support import VelaTestCase

from vela.analyze import Analyzer
from vela.driver import analyze_source
from vela.errors import UnsupportedError, VelaCompileError, VelaTypeError


def analyze(src: str) -> Analyzer:
    return analyze_source(src, "t.py")


class ErrorTests(VelaTestCase):
    def assertRejects(self, src: str, needle: str, exc_type=None) -> None:
        with self.assertRaises(Exception) as ctx:
            analyze(src)
        exc = ctx.exception
        if exc_type is not None:
            self.assertIsInstance(exc, exc_type)
        self.assertIn(needle, str(exc))

    def test_containers(self):
        self.assertRejects("xs = [1, 2]\n", "container literals", UnsupportedError)

    def test_attribute(self):
        self.assertRejects("import os\n", "Import", UnsupportedError)

    def test_method_call(self):
        self.assertRejects("print('a'.upper())\n", "calling an attribute", UnsupportedError)

    def test_fstring(self):
        self.assertRejects("x = 1\nprint(f'{x}')\n", "f-strings", UnsupportedError)

    def test_lambda(self):
        self.assertRejects("f = lambda x: x\n", "lambda", UnsupportedError)

    def test_global(self):
        self.assertRejects("def f():\n    return G\n", "global variable 'G'", UnsupportedError)

    def test_nested_def(self):
        self.assertRejects("def f():\n    def g():\n        pass\n",
                           "nested function", UnsupportedError)

    def test_import(self):
        self.assertRejects("import sys\n", "Import", UnsupportedError)

    def test_try(self):
        self.assertRejects("try:\n    pass\nexcept:\n    pass\n", "Try", UnsupportedError)

    def test_class(self):
        self.assertRejects("class A:\n    pass\n", "ClassDef", UnsupportedError)

    def test_type_conflict(self):
        self.assertRejects("x = 1\nx = 'a'\n", "one static type", VelaTypeError)

    def test_use_before_assignment(self):
        self.assertRejects("print(x)\nx = 1\n", "used before assignment", VelaCompileError)
        self.assertRejects("def f():\n    return y\n", "global variable 'y'",
                           UnsupportedError)

    def test_return_none_from_int_function(self):
        self.assertRejects("def f() -> int:\n    pass\n", "may fall off", VelaTypeError)

    def test_arg_count(self):
        self.assertRejects("def f(a):\n    return a\nf(1, 2)\n", "takes 1 argument",
                           VelaCompileError)

    def test_print_container(self):
        self.assertRejects("print([1])\n", "container literals", UnsupportedError)

    def test_float_floor_division(self):
        self.assertRejects("print(1.5 // 2.0)\n", "float floor division", UnsupportedError)

    def test_default_arg(self):
        self.assertRejects("def f(a=1):\n    return a\n", "default argument", UnsupportedError)

    def test_annotation(self):
        self.assertRejects("def f(a: list) -> int:\n    return 1\n",
                           "type annotation", UnsupportedError)

    def test_valid_annotations(self):
        an = analyze("def f(a: int, b: float) -> float:\n    return a + b\n"
                     "x: bool = True\nprint(f(1, 2.0), x)\n")
        self.assertEqual(an.funcs["f"].ret_type, "float")
        self.assertEqual(an.funcs["f"].param_types, ["int", "float"])


if __name__ == "__main__":
    unittest.main()
