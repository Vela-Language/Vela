"""Diagnostics for the Vela compiler."""
from __future__ import annotations

import ast

from . import i18n


class VelaError(Exception):
    """Base class for all user-facing compiler errors."""

    kind = "error"

    def __init__(self, message: str, node: ast.AST | None = None,
                 filename: str | None = None, *, key: str | None = None,
                 **params: object) -> None:
        if key is not None:
            message = i18n.EN.get(key, message).format(**params)
        super().__init__(message)
        self.message = message
        self.node = node
        self.filename = filename
        self.key = key
        self.params = params

    @property
    def lineno(self) -> int | None:
        return getattr(self.node, "lineno", None)

    @property
    def col(self) -> int | None:
        col = getattr(self.node, "col_offset", None)
        return col + 1 if col is not None else None

    def localized_message(self) -> str:
        if self.key is not None:
            return i18n.t(self.key, **self.params)
        return i18n.translate_diagnostic(self.message)

    def format(self, filename: str | None = None) -> str:
        fn = self.filename or filename or "<input>"
        kind = i18n.kind_label(self.kind)
        message = self.localized_message()
        if self.lineno is None:
            return f"{fn}: {kind}: {message}"
        return f"{fn}:{self.lineno}:{self.col}: {kind}: {message}"


class UnsupportedError(VelaError):
    kind = "error"

    def __init__(self, feature: str, node: ast.AST | None = None,
                 filename: str | None = None) -> None:
        msg = f"unsupported by the Vela native profile (v0.1): {feature}"
        super().__init__(msg, node, filename)
        self.feature = feature

    def localized_message(self) -> str:
        if i18n.get_lang() == "zh":
            return i18n.t("err.unsupported", feature=i18n.feature_text(self.feature))
        return self.message


class VelaTypeError(VelaError):
    kind = "type error"


class VelaCompileError(VelaError):
    kind = "error"


class VelaInternalError(VelaError):
    kind = "internal error"
