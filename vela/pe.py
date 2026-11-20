"""Minimal PE32+ (x86-64, Windows console) image writer.

No linker, no object files: Vela emits a complete, loadable .exe directly.
Sections are laid out as .text / .rdata / .data / .idata with the usual
0x1000 / 0x200 alignment, a fixed image base (ASLR off for v0.1) and a
hand-built import directory.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

IMAGE_BASE = 0x140000000
SECTION_ALIGN = 0x1000
FILE_ALIGN = 0x200
SUBSYSTEM_CONSOLE = 3

SCN_CNT_CODE = 0x00000020
SCN_CNT_INITIALIZED_DATA = 0x00000040
SCN_MEM_EXECUTE = 0x20000000
SCN_MEM_READ = 0x40000000
SCN_MEM_WRITE = 0x80000000

DOS_STUB = (b"\x0e\x1f\xba\x0e\x00\xb4\x09\xcd\x21\xb8\x01\x4c\xcd\x21"
            b"This program cannot be run in DOS mode.\r\r\n$")


def align_up(v: int, a: int) -> int:
    return (v + a - 1) // a * a


class DataBuilder:
    """Byte sink for .rdata/.data with alignment and offset bookkeeping."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def align(self, n: int = 8) -> None:
        while len(self.buf) % n:
            self.buf.append(0)

    def add(self, data: bytes, align: int = 8) -> int:
        self.align(align)
        off = len(self.buf)
        self.buf += data
        return off

    def add_cstr(self, s: str) -> int:
        return self.add(s.encode("utf-8") + b"\x00", align=1)

    def add_str_descriptor(self, s: str) -> int:
        """Vela string: [u64 length][utf-8 bytes] (not NUL terminated)."""
        raw = s.encode("utf-8")
        self.align(8)
        off = len(self.buf)
        self.buf += len(raw).to_bytes(8, "little")
        self.buf += raw
        return off

    def size(self) -> int:
        return len(self.buf)


class ImportTable:
    """Import directory layout, known before code emission.

    Offsets are relative to the start of .idata so that the code generator can
    reference IAT slots before the final RVA is known.
    """

    def __init__(self, dll_funcs: dict[str, list[str]]) -> None:
        self.dll_funcs = {d: list(fs) for d, fs in dll_funcs.items() if fs}
        self.dll_names = list(self.dll_funcs)
        ndll = len(self.dll_names)
        self.desc_size = 20 * (ndll + 1)
        self.int_off: list[int] = []
        self.iat_off: list[int] = []
        self.hint_off: dict[tuple[str, str], int] = {}
        self.dll_off: dict[str, int] = {}
        self.total_iat_size = 0

        cur = self.desc_size
        for d in self.dll_names:
            self.int_off.append(cur)
            cur += 8 * (len(self.dll_funcs[d]) + 1)
        for d in self.dll_names:
            self.iat_off.append(cur)
            cur += 8 * (len(self.dll_funcs[d]) + 1)
            self.total_iat_size += 8 * (len(self.dll_funcs[d]) + 1)
        self.iat_start = self.iat_off[0] if self.iat_off else 0
        for d in self.dll_names:
            for f in self.dll_funcs[d]:
                self.hint_off[(d, f)] = cur
                cur += 2 + len(f) + 1
                cur += cur & 1
        for d in self.dll_names:
            self.dll_off[d] = cur
            cur += len(d) + 1
        self.size = cur

    def slot(self, dll: str, func: str) -> int:
        """Offset (within .idata) of the IAT slot for func."""
        i = self.dll_names.index(dll)
        j = self.dll_funcs[dll].index(func)
        return self.iat_off[i] + 8 * j

    def build(self, idata_rva: int) -> bytes:
        b = bytearray(self.size)
        for i, d in enumerate(self.dll_names):
            base = i * 20
            struct.pack_into("<IIIII", b, base,
                             idata_rva + self.int_off[i], 0, 0,
                             idata_rva + self.dll_off[d],
                             idata_rva + self.iat_off[i])
            for j, f in enumerate(self.dll_funcs[d]):
                rva = idata_rva + self.hint_off[(d, f)]
                struct.pack_into("<Q", b, self.int_off[i] + 8 * j, rva)
                struct.pack_into("<Q", b, self.iat_off[i] + 8 * j, rva)
                name_off = self.hint_off[(d, f)]
                b[name_off:name_off + 2] = b"\x00\x00"
                nb = f.encode("ascii") + b"\x00"
                b[name_off + 2:name_off + 2 + len(nb)] = nb
            struct.pack_into("<Q", b, self.int_off[i] + 8 * len(self.dll_funcs[d]), 0)
            struct.pack_into("<Q", b, self.iat_off[i] + 8 * len(self.dll_funcs[d]), 0)
        for d in self.dll_names:
            off = self.dll_off[d]
            nb = d.encode("ascii") + b"\x00"
            b[off:off + len(nb)] = nb
        return bytes(b)


@dataclass
class Section:
    name: str
    data: bytes
    characteristics: int
    rva: int = 0
    raw_ptr: int = 0
    virtual_size: int = 0


@dataclass
class ImageLayout:
    rvas: dict[str, int]
    idata: bytes
    sections: list[Section] = field(default_factory=list)
    size_of_image: int = 0
    headers_size: int = 0


def layout_image(text: bytes, rdata: bytes, data: bytes,
                 imports: ImportTable) -> ImageLayout:
    secs = [
        Section(".text", text, SCN_CNT_CODE | SCN_MEM_EXECUTE | SCN_MEM_READ),
        Section(".rdata", rdata, SCN_CNT_INITIALIZED_DATA | SCN_MEM_READ),
        Section(".data", data, SCN_CNT_INITIALIZED_DATA | SCN_MEM_READ | SCN_MEM_WRITE),
        Section(".idata", b"", SCN_CNT_INITIALIZED_DATA | SCN_MEM_READ | SCN_MEM_WRITE),
    ]
    headers = 0x40 + len(DOS_STUB) + 4 + 20 + 240 + 40 * len(secs)
    headers_size = align_up(headers, FILE_ALIGN)
    rva = align_up(headers_size, SECTION_ALIGN)
    rvas: dict[str, int] = {}
    for s in secs:
        s.rva = rva
        rvas[s.name[1:]] = rva
        # .idata size is RVA independent; build after RVAs are assigned
        size = imports.size if s.name == ".idata" else len(s.data)
        s.virtual_size = max(size, 1)
        rva = align_up(rva + max(size, 1), SECTION_ALIGN)
    idata = imports.build(rvas["idata"])
    secs[3].data = idata
    secs[3].virtual_size = len(idata)
    size_of_image = align_up(rva, SECTION_ALIGN)

    raw = headers_size
    for s in secs:
        s.raw_ptr = raw
        raw += align_up(max(len(s.data), 1), FILE_ALIGN)
    return ImageLayout(rvas=rvas, idata=idata, sections=secs,
                       size_of_image=size_of_image, headers_size=headers_size)


def build_exe(text: bytes, rdata: bytes, data: bytes, imports: ImportTable,
              entry_off: int, image_base: int = IMAGE_BASE,
              stack_reserve: int = 16 * 1024 * 1024,
              stack_commit: int = 64 * 1024) -> bytes:
    lay = layout_image(text, rdata, data, imports)
    rvas = lay.rvas
    secs = lay.sections

    # ---- DOS header ------------------------------------------------------
    dos = bytearray(0x40)
    dos[0:2] = b"MZ"
    dos[0x3C:0x40] = (0x40).to_bytes(4, "little")
    stub = DOS_STUB
    while (0x40 + len(stub)) % 4:
        stub += b"\x00"
    # e_lfanew points right after the DOS stub
    e_lfanew = 0x40 + len(stub)
    dos[0x3C:0x40] = e_lfanew.to_bytes(4, "little")
    headers_start = bytes(dos) + stub
    pe_off = e_lfanew
    assert len(headers_start) == pe_off

    # ---- PE signature + COFF header -------------------------------------
    coff = struct.pack(
        "<4sHHIIIHH",
        b"PE\x00\x00",
        0x8664,              # IMAGE_FILE_MACHINE_AMD64
        len(secs),
        0,                   # TimeDateStamp (deterministic builds)
        0,                   # PointerToSymbolTable
        0,                   # NumberOfSymbols
        240,                 # SizeOfOptionalHeader
        0x0002 | 0x0020 | 0x0001,  # EXECUTABLE_IMAGE | LARGE_ADDRESS_AWARE | RELOCS_STRIPPED
    )

    # ---- optional header (PE32+) ----------------------------------------
    text_sec = secs[0]
    initialized = sum(align_up(max(len(s.data), 1), FILE_ALIGN) for s in secs[1:])
    opt = bytearray(240)
    struct.pack_into("<H", opt, 0, 0x20B)          # magic
    opt[2] = 14                                     # linker major
    struct.pack_into("<I", opt, 4, align_up(len(text), FILE_ALIGN))   # SizeOfCode
    struct.pack_into("<I", opt, 8, initialized)     # SizeOfInitializedData
    struct.pack_into("<I", opt, 12, 0)              # SizeOfUninitializedData
    struct.pack_into("<I", opt, 16, text_sec.rva + entry_off)  # AddressOfEntryPoint
    struct.pack_into("<I", opt, 20, text_sec.rva)   # BaseOfCode
    struct.pack_into("<Q", opt, 24, image_base)     # ImageBase
    struct.pack_into("<I", opt, 32, SECTION_ALIGN)
    struct.pack_into("<I", opt, 36, FILE_ALIGN)
    struct.pack_into("<H", opt, 40, 6)              # MajorOperatingSystemVersion
    struct.pack_into("<H", opt, 42, 0)
    struct.pack_into("<H", opt, 44, 0)              # MajorImageVersion
    struct.pack_into("<H", opt, 46, 0)
    struct.pack_into("<H", opt, 48, 6)              # MajorSubsystemVersion
    struct.pack_into("<H", opt, 50, 0)
    struct.pack_into("<I", opt, 52, 0)              # Win32VersionValue
    struct.pack_into("<I", opt, 56, lay.size_of_image)
    struct.pack_into("<I", opt, 60, lay.headers_size)
    struct.pack_into("<I", opt, 64, 0)              # CheckSum
    struct.pack_into("<H", opt, 68, SUBSYSTEM_CONSOLE)
    struct.pack_into("<H", opt, 70, 0)              # DllCharacteristics: no ASLR
    struct.pack_into("<Q", opt, 72, stack_reserve)
    struct.pack_into("<Q", opt, 80, stack_commit)
    struct.pack_into("<Q", opt, 88, 1 << 20)        # heap reserve
    struct.pack_into("<Q", opt, 96, 64 * 1024)      # heap commit
    struct.pack_into("<I", opt, 104, 0)             # LoaderFlags
    struct.pack_into("<I", opt, 108, 16)            # NumberOfRvaAndSizes

    dirs = [(0, 0)] * 16
    dirs[1] = (rvas["idata"], imports.desc_size)                 # import table
    dirs[12] = (rvas["idata"] + imports.iat_start, imports.total_iat_size)  # IAT
    for i, (rva, size) in enumerate(dirs):
        struct.pack_into("<II", opt, 112 + 8 * i, rva, size)

    # ---- section headers -------------------------------------------------
    sh = bytearray()
    for s in secs:
        name = s.name.encode("ascii").ljust(8, b"\x00")
        sh += struct.pack("<8sIIIIIIHHI",
                          name, s.virtual_size, s.rva,
                          align_up(max(len(s.data), 1), FILE_ALIGN),
                          s.raw_ptr, 0, 0, 0, 0, s.characteristics)

    hdr = bytearray(headers_start + coff + bytes(opt) + bytes(sh))
    assert len(hdr) <= lay.headers_size, (len(hdr), lay.headers_size)
    hdr += b"\x00" * (lay.headers_size - len(hdr))

    # ---- raw section data ------------------------------------------------
    out = bytearray(hdr)
    for s in secs:
        out += s.data
        pad = align_up(max(len(s.data), 1), FILE_ALIGN) - len(s.data)
        out += b"\x00" * pad
    return bytes(out)
