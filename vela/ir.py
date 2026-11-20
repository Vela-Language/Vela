"""Vela IR: a small, explicitly typed, CFG-based, non-SSA IR.

Values are virtual registers with a static type.  Variables from the source
program are also virtual registers: assignments are `mov` into the variable's
vreg, so control-flow joins need no phi nodes.  The register allocator computes
liveness over the CFG and assigns one machine location per vreg.

Instruction set
---------------
  ("arg",   dst, (index,))              copy ABI argument -> dst
  ("const", dst, (value,))              int/float constant
  ("str",   dst, (rdata_offset,))       address of a string descriptor
  ("mov",   dst, (src,))
  ("bin",   dst, (op, a, b))            add sub mul floordiv mod and or xor shl
                                        shr fadd fsub fmul fdiv
  ("un",    dst, (op, a))               neg fneg not i2f f2i
  ("cmp",   dst, (op, a, b))            eq ne lt le gt ge (int or float)
  ("br",    None, (cond, then_label, else_label))
  ("jmp",   None, (label,))
  ("call",  dst|None, (label, (args...)))
  ("ret",   None, (value|None,))
  ("print", None, (kind, value|None))   int float bool str none space nl
"""
from __future__ import annotations

from dataclasses import dataclass, field

INT = "int"
FLOAT = "float"
BOOL = "bool"
STR = "str"
NONE = "none"
UNKNOWN = "unknown"

NUMERIC = (INT, FLOAT)
GPR_TYPES = (INT, BOOL, STR)
XMM_TYPES = (FLOAT,)

OP_INFIX = {
    "add": "+", "sub": "-", "mul": "*", "floordiv": "//", "mod": "%",
    "and": "&", "or": "|", "xor": "^", "shl": "<<", "shr": ">>",
    "fadd": "+", "fsub": "-", "fmul": "*", "fdiv": "/",
}
CMP_INFIX = {"eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}


@dataclass(eq=False)
class VReg:
    id: int
    ty: str
    name: str = ""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"%{self.id}:{self.ty}" + (f"({self.name})" if self.name else "")


@dataclass
class Instr:
    op: str
    dst: VReg | None
    args: tuple

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"{self.op} {self.dst if self.dst else ''} {self.args}"


class Block:
    __slots__ = ("label", "instrs")

    def __init__(self, label: str) -> None:
        self.label = label
        self.instrs: list[Instr] = []

    def add(self, op: str, dst: VReg | None = None, *args) -> Instr:
        ins = Instr(op, dst, args)
        self.instrs.append(ins)
        return ins

    @property
    def terminator(self) -> Instr | None:
        if self.instrs and self.instrs[-1].op in ("jmp", "br", "ret"):
            return self.instrs[-1]
        return None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Block {self.label} n={len(self.instrs)}>"


class Function:
    def __init__(self, name: str, ret_ty: str) -> None:
        self.name = name
        self.ret_ty = ret_ty
        self.params: list[VReg] = []
        self.blocks: list[Block] = []
        self._vreg_id = 0
        self._block_id = 0
        self.entry = self.new_block("entry")

    def new_vreg(self, ty: str, name: str = "") -> VReg:
        self._vreg_id += 1
        return VReg(self._vreg_id, ty, name)

    def new_block(self, hint: str = "") -> Block:
        label = f"L_{self.name}_{self._block_id}_{hint}" if hint else f"L_{self.name}_{self._block_id}"
        self._block_id += 1
        b = Block(label)
        self.blocks.append(b)
        return b

    @property
    def vreg_count(self) -> int:
        return self._vreg_id

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Function {self.name} params={self.params} ret={self.ret_ty}>"


@dataclass
class Program:
    functions: list[Function] = field(default_factory=list)
    main: Function | None = None
    strings: object = None          # DataBuilder for .rdata
    runtime: object = None          # Runtime
