import unittest

from test_core import HookMemory
from test_core import Cs, CS_ARCH_X86, CS_MODE_32, game_exe
from trainer import all_target


class AllTargetTests(unittest.TestCase):
    def setUp(self):
        self.p = HookMemory()
        for address, original in all_target.SITES.values():
            self.p.patch(address, original)

    def test_binary_sites_and_branches(self):
        pe = game_exe()
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        for i, (name, (address, original)) in enumerate(all_target.SITES.items()):
            self.assertEqual(pe.read(address, len(original)), original, name)
            base = 0x15000000 + (i + 1) * 0x200
            code = all_target.target_code(name, base)
            self.assertLess(len(code), 0x200)
            ins = list(cs.disasm(code, base))
            self.assertEqual(sum(x.size for x in ins), len(code), name)
            boundaries = {x.address for x in ins}
            for x in ins:
                if x.mnemonic.startswith("j"):
                    target = int(x.op_str, 16)
                    if base <= target < base + len(code):
                        self.assertIn(target, boundaries, name)

    def test_install_and_restore(self):
        c = all_target.AllTargetController(self.p)
        c.set(True)
        self.assertEqual(len(c.installed), 4)
        c.close()
        for address, original in all_target.SITES.values():
            self.assertEqual(self.p.read(address, len(original)), original)

    def test_failed_install_rolls_back(self):
        c = all_target.AllTargetController(self.p)
        self.p.fail_at = all_target.SITES["bullet_air"][0]
        with self.assertRaises(OSError):
            c.set(True)
        self.assertFalse(c.installed)
        for address, original in all_target.SITES.values():
            self.assertEqual(self.p.read(address, len(original)), original)


if __name__ == "__main__":
    unittest.main()
