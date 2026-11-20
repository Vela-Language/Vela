"""Diagnostics for the Vela compiler."""
from __future__ import annotations

import ast


class VelaError(Exception):
    """Base class for all user-facing compiler errors."""

    kind = "error"

    def __init__(self, message: str, node: ast.AST | None = None,
                 filename: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.node = node
        self.filename = filename

    @property
    def lineno(self) -> int | None:
        return getattr(self.node, "lineno", None)

    @property
    def col(self) -> int | None:
        col = getattr(self.node, "col_offset", None)
        return col + 1 if col is not None else None

    def format(self, filename: str | None = None) -> str:
        fn = self.filename or filename or "<input>"
        if self.lineno is None:
            return f"{fn}: {self.kind}: {self.message}"
        return f"{fn}:{self.lineno}:{self.col}: {self.kind}: {self.message}"


class UnsupportedError(VelaError):
    kind = "error"

    def __init__(self, feature: str, node: ast.AST | None = None,
                 filename: str | None = None) -> None:
        msg = f"unsupported by the Vela native profile (v0.1): {feature}"
        super().__init__(msg, node, filename)
        self.feature = feature


class VelaTypeError(VelaError):
    kind = "type error"


class VelaCompileError(VelaError):
    kind = "error"


class VelaInternalError(VelaError):
    kind = "internal error"
