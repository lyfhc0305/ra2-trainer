import unittest

from test_core import HookMemory
from test_core import Cs, CS_ARCH_X86, CS_MODE_32, disasm_checked, game_exe
from trainer import water_walk as ww
from trainer.features import FEATURES


class WaterWalkTests(unittest.TestCase):
    def setUp(self):
        self.p = HookMemory()
        for address, raw, _kind in ww.SITES.values():
            self.p.patch(address, bytes.fromhex(raw))

    def test_native_sites_and_trampoline_boundaries(self):
        pe = game_exe()
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        base = 0x15000000
        helper = ww.qualifier_code(base + ww.HELPER_OFFSET,
                                   base + ww.MODE_OFFSET)
        all_codes = [(base + ww.HELPER_OFFSET, helper)]
        for i, (name, (address, raw, _kind)) in enumerate(ww.SITES.items()):
            self.assertEqual(pe.read(address, len(raw) // 2), bytes.fromhex(raw), name)
            code_at = base + ww.SITE_OFFSET + i * ww.SITE_STRIDE
            all_codes.append((code_at, ww.site_code(name, code_at,
                             base + ww.HELPER_OFFSET, base + 0x28)))
        for start, code in all_codes:
            ins = disasm_checked(self, code, start)
        helper_text = [f"{i.mnemonic} {i.op_str}" for i in cs.disasm(helper, base + ww.HELPER_OFFSET)]
        self.assertIn("mov edi, dword ptr [esi + 0x5a8]", helper_text)  # Infantry
        self.assertIn("mov edi, dword ptr [esi + 0x5ac]", helper_text)  # Unit
        self.assertIn("mov eax, dword ptr [esi + 0x88]", helper_text)  # DRAIN, positive disp32
        self.assertIn("mov edx, dword ptr [esi + 0x8c]", helper_text)
        self.assertIn("cmp dword ptr [ebx*4 + 0x850ee8], 0", helper_text)
        self.assertIn("cmp byte ptr [edi + 0xada], 0", helper_text)  # Naval excluded

    def test_factor_is_only_water_or_beach_with_original_zero(self):
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        base = 0x15000000
        for name in ("infantry_enter", "infantry_level", "unit_enter"):
            code_at = base + ww.SITE_OFFSET + list(ww.SITES).index(name) * ww.SITE_STRIDE
            code = ww.site_code(name, code_at, base + ww.HELPER_OFFSET, base + 0x28)
            text = [f"{i.mnemonic} {i.op_str}".strip() for i in cs.disasm(code, code_at)]
            for boundary in (0x12, 0x14, 0x36, 0x38):
                self.assertIn(f"cmp eax, {hex(boundary)}", text)
            self.assertIn("cmp dword ptr [eax*4 + 0x850ea0], 0", text)
            self.assertIn("fld dword ptr [0x15000028]", text)
            self.assertIn("fld dword ptr [ecx*4 + 0x850ea0]" if name != "infantry_level"
                          else "fld dword ptr [edx*4 + 0x850ea0]", text)

    def test_attack_sites_substitute_the_shooters_zone(self):
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        base = 0x15000000
        expect = {"near_speed": ("mov ebx, dword ptr [eax + 0x5a4]", 4, 0x10),
                  "near_zone": ("mov edi, dword ptr [eax + 0x500]", 4, 0),
                  "shooter_zone": ("mov eax, dword ptr [eax + 0x500]", 0, 0x1c),
                  "target_zone": ("mov eax, dword ptr [eax + 0x500]", 0, 0x1c),
                  "candidate_zone": ("mov eax, dword ptr [eax + 0x500]", 4, 0x1c)}
        for name, (original, source, output) in expect.items():
            code_at = base + ww.SITE_OFFSET + list(ww.SITES).index(name) * ww.SITE_STRIDE
            code = ww.site_code(name, code_at, base + ww.HELPER_OFFSET, base + 0x28)
            text = [f"{i.mnemonic} {i.op_str}".strip() for i in cs.disasm(code, code_at)]
            self.assertEqual(text[0], original, name)  # native value first
            self.assertEqual(text[1:3], ["pushfd", "pushal"], name)
            self.assertIn("mov ecx, dword ptr [esp + 4]" if source else
                          "mov ecx, dword ptr [esp]", text, name)
            store = f"mov dword ptr [esp + {hex(output)}], eax" if output else "mov dword ptr [esp], eax"
            self.assertIn(store, text, name)
            self.assertEqual(text[-3:-1], ["popal", "popfd"], name)
            address, raw, _kind = ww.SITES[name]
            self.assertEqual(text[-1], f"jmp {hex(address + len(raw) // 2)}", name)

    def test_version_one_block_is_upgraded_in_place(self):
        c = ww.WaterWalkController(self.p)
        c.enable()
        c.disable()
        base = c.base
        legacy = list(ww.SITES)[:ww.LEGACY_SITE_COUNT]
        end = ww.SITE_OFFSET + ww.LEGACY_SITE_COUNT * ww.SITE_STRIDE
        for name in list(ww.SITES)[ww.LEGACY_SITE_COUNT:]:
            c.hooks.switch(name, False)
        self.p.patch(base, ww.LEGACY_MAGIC)
        self.p.patch(base + end, bytes(ww.BLOCK_SIZE - end))
        old = ww.WaterWalkController(self.p)
        self.assertEqual(old.base, base)
        self.assertEqual(set(old.installed), set(legacy))
        self.assertEqual(self.p.read(base, ww.BLOCK_SIZE), ww.block_bytes(base)[:ww.MODE_OFFSET]
                         + b"\x02" + ww.block_bytes(base)[ww.MODE_OFFSET + 1:])
        old.enable()
        self.assertEqual(set(old.installed), set(ww.SITES))
        self.assertTrue(old.enabled)

    def test_periodic_enable_does_not_rewrite_mode(self):
        c = ww.WaterWalkController(self.p)
        c.enable()
        from unittest.mock import patch as mock_patch
        with mock_patch.object(self.p, "patch", side_effect=AssertionError):
            c.enable()  # already on: reads only
        self.assertTrue(c.enabled)

    def test_on_drain_adopt_and_stale_on_sync(self):
        c = ww.WaterWalkController(self.p)
        c.enable()
        self.assertTrue(c.enabled)
        for name, (address, raw, _kind) in ww.SITES.items():
            self.assertEqual(self.p.read(address, len(raw) // 2), c._expected(name, c.base))
        c.disable()
        self.assertFalse(c.enabled)
        self.assertEqual(self.p.read(c.base + ww.MODE_OFFSET, 1), b"\x02")
        adopted = ww.WaterWalkController(self.p)
        self.assertEqual(adopted.base, c.base)
        adopted.enable()
        self.assertTrue(adopted.enabled)
        crashed_restart = ww.WaterWalkController(self.p)
        self.assertFalse(crashed_restart.enabled)  # saved-off UI cannot hide stale ON
        crashed_restart.enable()
        self.assertTrue(crashed_restart.enabled)
        crashed_restart.close()
        self.assertFalse(crashed_restart.enabled)
        feature = next(f for f in FEATURES if f["id"] == "water_walk")
        self.assertEqual((feature["kind"], feature["status"]), ("patch", "ok"))

    def test_external_tamper_or_bad_block_is_not_adopted(self):
        c = ww.WaterWalkController(self.p)
        c.enable()
        prior = self.p.read(c.base + 0x200, 1)
        self.p.patch(c.base + 0x200, b"\xcc")
        with self.assertRaisesRegex(RuntimeError, "版本不匹配"):
            ww.WaterWalkController(self.p)
        self.p.patch(c.base + 0x200, prior)
        first_addr, raw, _kind = next(iter(ww.SITES.values()))
        self.p.patch(first_addr, b"\xcc")
        with self.assertRaisesRegex(RuntimeError, "入口已被外部修改"):
            c.enable()
        self.assertEqual(self.p.read(first_addr, 1), b"\xcc")

    def test_mode_write_failure_is_reported(self):
        c = ww.WaterWalkController(self.p)
        c._prepare()
        self.p.fail_at = c.base + ww.MODE_OFFSET
        with self.assertRaises(OSError):
            c.enable()
        self.assertFalse(c.installed)
        for address, raw, _kind in ww.SITES.values():
            self.assertEqual(self.p.read(address, len(raw) // 2), bytes.fromhex(raw))


if __name__ == "__main__":
    unittest.main()
