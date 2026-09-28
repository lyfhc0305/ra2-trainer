import struct
import unittest
from unittest.mock import patch

from test_core import HookMemory, ROOT, PE, disasm_checked, game_pe
from trainer import techno_ai as T


class Memory(HookMemory):
    def write(self, address, data):
        return self.patch(address, data)


class TechnoAITests(unittest.TestCase):
    def setUp(self):
        self.p = Memory()
        self.p.patch(T.AI_ENTRY, T.AI_ORIGINAL)
        self.base = 0x15000000  # HookMemory.alloc

    def settings(self):
        return self.p.read(self.base, 16)

    def test_entry_matches_game_and_every_class_reaches_it(self):
        pe = game_pe(self)
        self.assertEqual(pe.read(T.AI_ENTRY, len(T.AI_ORIGINAL)), T.AI_ORIGINAL)
        # Slot 23 is AI: Unit/Infantry/Aircraft go through FootClass::AI.
        for vt, ai in ((0x7ADDF8, 0x6FB100), (0x7A3540, 0x502D20), (0x79B12C, 0x4147F0),
                       (0x79CBBC, 0x43C930), (0x7ACB80, T.AI_ENTRY)):
            self.assertEqual(pe.read(vt + 23 * 4, 4), struct.pack("<I", ai))
        for call, target in ((0x4C8FF9, T.AI_ENTRY), (0x43CC4B, T.AI_ENTRY),
                             (0x6FB37B, 0x4C8FF0), (0x502EFE, 0x4C8FF0), (0x4149D1, 0x4C8FF0)):
            code = pe.read(call, 5)
            self.assertEqual(code[0], 0xE8)
            self.assertEqual(call + 5 + struct.unpack("<i", code[1:])[0], target)

    def test_code_decodes_and_returns_to_entry(self):
        code = T.ai_code(self.base)
        start = self.base + T.CODE_OFFSET
        ins = disasm_checked(self, code, start)
        text = "\n".join(f"{i.mnemonic} {i.op_str}".strip() for i in ins)
        self.assertTrue(text.startswith("pushfd\npushal"))
        self.assertIn("popal\npopfd\nsub esp, 0x68\npush ebx\npush ebp\npush esi", text)
        self.assertEqual(ins[-1].mnemonic, "jmp")
        self.assertEqual(int(ins[-1].op_str, 16), T.AI_ENTRY + len(T.AI_ORIGINAL))
        self.assertIn("mov dword ptr [esi + 0x11c], 0x40000000", text)
        self.assertLess(T.CODE_OFFSET + len(code), 0x1000)

    def test_switches_share_one_hook(self):
        c = T.TechnoAIController(self.p, 25)
        c.set("u_vet3", True)
        self.assertEqual(self.settings(), T.settings_bytes(1, 0, 25))
        self.assertNotEqual(self.p.read(T.AI_ENTRY, 6), T.AI_ORIGINAL)
        c.set("auto_repair", True)
        self.assertEqual(self.settings(), T.settings_bytes(1, 1, 25))
        c.set_amount(0)
        self.assertEqual(self.settings(), T.settings_bytes(1, 1, 0))
        c.set("u_vet3", False)
        self.assertEqual(self.settings(), T.settings_bytes(0, 1, 0))
        self.assertTrue(c.installed)
        c.set("auto_repair", False)
        self.assertEqual(self.p.read(T.AI_ENTRY, 6), T.AI_ORIGINAL)
        self.assertEqual(self.settings(), T.settings_bytes(0, 0, 0))

    def test_rejected_amount_is_not_applied_later(self):
        c = T.TechnoAIController(self.p, 10)
        c.set("auto_repair", True)
        with patch.object(self.p, "write", return_value=False):
            with self.assertRaises(OSError):
                c.set_amount(50)
        self.assertEqual(c.repair_amount, 10)
        self.p.patch(self.base, bytes(16))  # settings lost: the periodic check rewrites them
        c.set("auto_repair", True)
        self.assertEqual(self.settings(), T.settings_bytes(0, 1, 10))

    def test_periodic_reapply_restores_lost_settings(self):
        c = T.TechnoAIController(self.p)
        c.set("auto_repair", True)
        self.p.patch(self.base, bytes(16))
        c.set("auto_repair", True)
        self.assertEqual(self.settings(), T.settings_bytes(0, 1, 0))

    def test_close_restores_entry(self):
        c = T.TechnoAIController(self.p)
        c.set("u_vet3", True)
        c.set("auto_repair", True)
        c.close()
        self.assertEqual(self.p.read(T.AI_ENTRY, 6), T.AI_ORIGINAL)
        self.assertFalse(c.installed)

    def test_foreign_entry_is_not_overwritten(self):
        self.p.patch(T.AI_ENTRY, b"\xcc" * 6)
        c = T.TechnoAIController(self.p)
        with self.assertRaises(RuntimeError):
            c.set("u_vet3", True)
        self.assertEqual(self.p.read(T.AI_ENTRY, 6), b"\xcc" * 6)


if __name__ == "__main__":
    unittest.main()
