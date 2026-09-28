import unittest

from test_core import HookMemory, Cs, CS_ARCH_X86, CS_MODE_32, game_exe
from trainer import build_unlock


class BuildUnlockTests(unittest.TestCase):
    def setUp(self):
        self.p = HookMemory()
        for _fid, address, original, _resume, _bypass in build_unlock.SITES.values():
            self.p.patch(address, bytes.fromhex(original))

    def test_originals_and_trampolines(self):
        pe = game_exe()
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        for index, (name, spec) in enumerate(build_unlock.SITES.items()):
            _fid, address, original, _resume, _bypass = spec
            raw = bytes.fromhex(original)
            self.assertEqual(pe.read(address, len(raw)), raw, name)
            self.assertEqual(sum(i.size for i in cs.disasm(raw, address)), len(raw))
            base = 0x15000000 + (index + 1) * 0x100
            code = build_unlock.site_code(name, base)
            ins = list(cs.disasm(code, base))
            self.assertEqual(sum(i.size for i in ins), len(code), name)
            boundaries = {i.address for i in ins}
            for i in ins:
                if i.mnemonic.startswith("j"):
                    target = int(i.op_str, 16)
                    if base <= target < base + len(code):
                        self.assertIn(target, boundaries, name)
            if name == "tech_level":
                self.assertIn("cmp dword ptr [ebx + 0x55c], 0xff",
                              [f"{i.mnemonic} {i.op_str}" for i in ins])
                text = [f"{i.mnemonic} {i.op_str}" for i in ins]
                self.assertIn("cmp dword ptr [ebx + 0x24], 0x49414147", text)  # GAAIRC
                self.assertIn("cmp dword ptr [ebx + 0x24], 0x41524d41", text)  # AMRADR
                self.assertIn("cmp dword ptr [eax + 0xb8], 0", text)  # country
                self.assertIn("jmp 0x4e3740", text)  # native safe rejection

    def test_independent_switches_and_restoration(self):
        c = build_unlock.BuildUnlockController(self.p)
        c.set("tech_all", True)
        self.assertEqual(set(c.installed), {"tech_level", "factory_owner", "prerequisites"})
        c.set("unlock_build", True)
        self.assertEqual(len(c.installed), 5)
        c.set("tech_all", False)
        self.assertEqual(set(c.installed), {"prerequisites", "canbuild_limit", "reached_limit"})
        c.close()
        for _fid, address, original, _resume, _bypass in build_unlock.SITES.values():
            self.assertEqual(self.p.read(address, len(original) // 2), bytes.fromhex(original))

    def test_shared_prerequisite_refcount_in_both_orders(self):
        for first, second in (("tech_all", "unlock_build"),
                              ("unlock_build", "tech_all")):
            p = HookMemory()
            for _fid, address, original, _resume, _bypass in build_unlock.SITES.values():
                p.patch(address, bytes.fromhex(original))
            c = build_unlock.BuildUnlockController(p)
            c.set(first, True)
            self.assertIn("prerequisites", c.installed)
            shared_patch = c.installed["prerequisites"]
            c.set(second, True)
            self.assertEqual(c.installed["prerequisites"], shared_patch)
            c.set(first, False)
            self.assertEqual(c.installed["prerequisites"], shared_patch)
            c.set(second, False)
            self.assertNotIn("prerequisites", c.installed)

    def test_install_failure_rolls_back(self):
        c = build_unlock.BuildUnlockController(self.p)
        self.p.fail_at = build_unlock.SITES["factory_owner"][1]
        with self.assertRaises(OSError):
            c.set("tech_all", True)
        self.assertFalse(c.installed)
        self.assertFalse(c.enabled)
        for _fid, address, original, _resume, _bypass in build_unlock.SITES.values():
            self.assertEqual(self.p.read(address, len(original) // 2), bytes.fromhex(original))

    def test_foreign_change_not_overwritten(self):
        c = build_unlock.BuildUnlockController(self.p)
        c.set("tech_all", True)
        address = build_unlock.SITES["tech_level"][1]
        self.p.patch(address, b"\xcc")
        with self.assertRaises(RuntimeError):
            c.close()
        self.assertEqual(self.p.read(address, 1), b"\xcc")


if __name__ == "__main__":
    unittest.main()
