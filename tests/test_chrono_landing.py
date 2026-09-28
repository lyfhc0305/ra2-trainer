import unittest

from test_core import HookMemory, game_pe
from test_core import ROOT, PE, Cs, CS_ARCH_X86, CS_MODE_32, disasm_checked
from trainer import chrono_landing as chrono
from trainer.features import FEATURES


class ChronoLandingTests(unittest.TestCase):
    def setUp(self):
        self.p = HookMemory()
        self.p.patch(chrono.LANDING_SITE, chrono.ORIGINAL)

    def test_original_site_and_generated_branch_boundaries(self):
        pe = game_pe(self)
        self.assertEqual(pe.read(chrono.LANDING_SITE, len(chrono.ORIGINAL)), chrono.ORIGINAL)
        base = 0x15000000
        code = chrono.landing_code(base)
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        ins = disasm_checked(self, code, base)
        text = {f"{i.mnemonic} {i.op_str}" for i in ins}
        self.assertIn("cmp dword ptr [esi], 0x7ad1a8", text)
        self.assertIn("cmp dword ptr [eax], 0x7a3540", text)
        self.assertIn("cmp dword ptr [esi + 0x40], 1", text)
        self.assertIn("mov dword ptr [esi + 0x40], 1", text)
        self.assertIn("mov byte ptr [ecx + 0x205], 1", text)  # original flow

    def test_switch_restores_exact_entry_and_is_hotkey_eligible(self):
        c = chrono.ChronoLandingController(self.p)
        c.enable()
        self.assertTrue(c.enabled)
        c.enable()  # repeated state verification
        self.assertNotEqual(self.p.read(chrono.LANDING_SITE, len(chrono.ORIGINAL)), chrono.ORIGINAL)
        c.close()
        self.assertEqual(self.p.read(chrono.LANDING_SITE, len(chrono.ORIGINAL)), chrono.ORIGINAL)
        feature = next(f for f in FEATURES if f["id"] == "chrono_quick_land")
        self.assertEqual((feature["kind"], feature["status"]), ("patch", "ok"))

    def test_foreign_change_does_not_get_overwritten(self):
        c = chrono.ChronoLandingController(self.p)
        c.enable()
        self.p.patch(chrono.LANDING_SITE, b"\xcc")
        with self.assertRaises(RuntimeError):
            c.close()
        self.assertEqual(self.p.read(chrono.LANDING_SITE, 1), b"\xcc")


if __name__ == "__main__":
    unittest.main()
