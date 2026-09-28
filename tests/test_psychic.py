import struct
import unittest

from test_core import HookMemory, disasm_checked, game_exe
from trainer import psychic as P


class PsychicScanTests(unittest.TestCase):
    def setUp(self):
        self.p = HookMemory()
        self.p.patch(P.DETECT_ENTRY, P.DETECT_ORIGINAL)

    def test_game_matches_detection_path(self):
        pe = game_exe()
        self.assertEqual(pe.read(P.DETECT_ENTRY, len(P.DETECT_ORIGINAL)), P.DETECT_ORIGINAL)
        # Render loop: non-player objects -> detection check -> action lines.
        for call, target in ((0x6A4FFB, P.DETECT_ENTRY), (0x6A5020, 0x4CAB90),
                             (0x438713, P.IS_ALLIED)):
            code = pe.read(call, 5)
            self.assertEqual(code[0], 0xE8)
            self.assertEqual(call + 5 + struct.unpack("<i", code[1:])[0], target)
        # Detection compares with BuildingType.PsychicDetectionRadius (+0x1390).
        self.assertEqual(pe.read(0x4387E4, 6), bytes.fromhex("8b9190130000"))
        # IsAlliedWith is thiscall with one stack argument.
        self.assertEqual(pe.read(0x4E5550, 3), bytes.fromhex("c20400"))

    def test_code_decodes_and_returns_bool(self):
        base = 0x15000000
        code = P.detect_code(base)
        ins = disasm_checked(self, code, base + P.CODE_OFFSET)
        text = "\n".join(f"{i.mnemonic} {i.op_str}".strip() for i in ins)
        self.assertIn("push ecx\npush edx\nmov ecx, eax", text)
        self.assertIn("call eax\npop ecx\ntest al, al", text)
        self.assertIn("mov al, 1\nret", text)
        self.assertIn("push ebp\nmov ebp, esp\nand esp, 0xfffffff8", text)
        self.assertEqual(int(ins[-1].op_str, 16), P.DETECT_ENTRY + len(P.DETECT_ORIGINAL))

    def test_install_and_restore(self):
        c = P.PsychicScanController(self.p)
        c.set(True)
        self.assertTrue(c.installed)
        self.assertEqual(self.p.read(P.DETECT_ENTRY, 1), b"\xe9")
        c.set(True)  # periodic reapply: no change
        c.close()
        self.assertEqual(self.p.read(P.DETECT_ENTRY, 6), P.DETECT_ORIGINAL)

    def test_foreign_entry_is_not_overwritten(self):
        self.p.patch(P.DETECT_ENTRY, b"\xcc" * 6)
        with self.assertRaises(RuntimeError):
            P.PsychicScanController(self.p).set(True)
        self.assertEqual(self.p.read(P.DETECT_ENTRY, 6), b"\xcc" * 6)


if __name__ == "__main__":
    unittest.main()
