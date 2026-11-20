"""PE image writer tests."""
from __future__ import annotations

import os
import subprocess
import unittest

from support import VelaTestCase

from vela.pe import IMAGE_BASE, ImportTable, build_exe, layout_image
from vela.x64 import Asm


class PeTests(VelaTestCase):
    def test_minimal_exit_code(self):
        imp = ImportTable({"kernel32.dll": ["ExitProcess"]})
        a = Asm()
        a.bind("_start")
        a.and_rsp(-16)
        a.sub_rsp(32)
        a.mov_ri("rcx", 42)
        a.call_iat(("idata", imp.slot("kernel32.dll", "ExitProcess")))
        lay = layout_image(bytes(a.b), b"", b"", imp)
        a.resolve(IMAGE_BASE, lay.rvas)
        exe = build_exe(bytes(a.b), b"", b"", imp, entry_off=a.labels["_start"])
        self.assertEqual(exe[:2], b"MZ")
        self.assertEqual(len(exe) % 0x200, 0)
        path = os.path.join(os.path.dirname(__file__), "..", "_pe_exit.exe")
        path = os.path.abspath(path)
        with open(path, "wb") as f:
            f.write(exe)
        try:
            proc = subprocess.run([path], capture_output=True, timeout=10)
            self.assertEqual(proc.returncode, 42)
        finally:
            os.unlink(path)

    def test_import_table_layout(self):
        imp = ImportTable({"kernel32.dll": ["ExitProcess", "Sleep"],
                           "msvcrt.dll": ["sprintf"]})
        self.assertGreater(imp.size, 0)
        data = imp.build(0x4000)
        self.assertEqual(len(data), imp.size)
        # descriptor count + terminator
        self.assertEqual(int.from_bytes(data[0:4], "little"), 0x4000 + imp.int_off[0])
        # every IAT slot starts as a hint/name RVA inside .idata
        for dll in imp.dll_names:
            for fn in imp.dll_funcs[dll]:
                off = imp.slot(dll, fn)
                rva = int.from_bytes(data[off:off + 8], "little")
                self.assertTrue(0x4000 <= rva < 0x4000 + imp.size)


if __name__ == "__main__":
    unittest.main()
