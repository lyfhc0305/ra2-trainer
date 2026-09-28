import unittest

from test_core import HookMemory
from test_core import Cs, CS_ARCH_X86, CS_MODE_32, disasm_checked, game_exe
from trainer import anti_stealth


class AntiStealthTests(unittest.TestCase):
    def setUp(self):
        self.p = HookMemory()
        for address, original in anti_stealth.SITES.items():
            self.p.patch(address, original)

    def test_binary_sites_and_branches(self):
        pe = game_exe()
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        for i, (address, original) in enumerate(anti_stealth.SITES.items()):
            self.assertEqual(pe.read(address, len(original)), original)
            base = 0x15000000 + (i + 1) * 0x100
            code = anti_stealth.sensor_code(base, address, original)
            ins = disasm_checked(self, code, base)

    def test_enable_and_restore(self):
        c = anti_stealth.AntiStealthController(self.p)
        c.set(True)
        self.assertEqual(len(c.installed), 2)
        c.set(True)
        c.close()
        for address, original in anti_stealth.SITES.items():
            self.assertEqual(self.p.read(address, len(original)), original)

    def test_partial_install_rolls_back(self):
        c = anti_stealth.AntiStealthController(self.p)
        self.p.fail_at = 0x47C300
        with self.assertRaises(OSError):
            c.set(True)
        self.assertFalse(c.installed)
        for address, original in anti_stealth.SITES.items():
            self.assertEqual(self.p.read(address, len(original)), original)


if __name__ == "__main__":
    unittest.main()
