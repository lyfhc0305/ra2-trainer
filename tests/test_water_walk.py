import unittest

from test_core import HookMemory, game_pe
from test_core import ROOT, PE, Cs, CS_ARCH_X86, CS_MODE_32, disasm_checked
from trainer import water_walk as ww
from trainer.features import FEATURES


class WaterWalkTests(unittest.TestCase):
    def setUp(self):
        self.p = HookMemory()
        for address, raw, _kind in ww.SITES.values():
            self.p.patch(address, bytes.fromhex(raw))

    def test_native_sites_and_trampoline_boundaries(self):
        pe = game_pe(self)
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        base = 0x15000000
        helper = ww.qualifier_code(base + ww.HELPER_OFFSET,
                                   base + ww.MODE_OFFSET)
        all_codes = [(base + ww.HELPER_OFFSET, helper)]
        for i, (name, (address, raw, _kind)) in enumerate(ww.SITES.items()):
            self.assertEqual(pe.read(address, len(raw) // 2), bytes.fromhex(raw), name)
            code_at = base + ww.site_offset(i)
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
            code_at = base + ww.site_offset(list(ww.SITES).index(name))
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
            code_at = base + ww.site_offset(list(ww.SITES).index(name))
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

    def test_previous_versions_preserve_existing_stubs(self):
        for magic, count in ((ww.ATTACK_MAGIC, ww.ATTACK_SITE_COUNT),
                             (ww.RALLY_MAGIC, ww.RALLY_SITE_COUNT),
                             (ww.DESTINATION_MAGIC, 21), (ww.CLONING_MAGIC, 21)):
            with self.subTest(magic=magic):
                self.setUp()
                c = ww.WaterWalkController(self.p)
                c.enable()
                base = c.base
                end = ww.SITE_OFFSET + count * ww.SITE_STRIDE
                for name in list(ww.SITES)[count:]:
                    c.hooks.switch(name, False)
                legacy = bytearray(ww.block_bytes(base, cloning=magic == ww.CLONING_MAGIC))
                legacy[ww.MODE_OFFSET] = ww.MODE_ON
                self.p.patch(base, bytes(legacy))
                self.p.patch(base, magic)
                self.p.patch(base + end, bytes(ww.BLOCK_SIZE - end))
                old_stubs = self.p.read(base + ww.SITE_OFFSET, end - ww.SITE_OFFSET)
                upgraded = ww.WaterWalkController(self.p)
                self.assertIsNone(upgraded.adopt_error)
                upgraded.enable()
                self.assertEqual(upgraded.base, base)
                shared = (ww.ATTACK_SITE_COUNT * ww.SITE_STRIDE)
                self.assertEqual(self.p.read(base + ww.SITE_OFFSET, shared), old_stubs[:shared])
                self.assertEqual(set(upgraded.installed), set(ww.SITES))

    def test_rally_zone_only_for_enabled_owned_land_factories(self):
        from test_batch_emulated import HAVE_UNICORN, machine
        if not HAVE_UNICORN:
            self.skipTest('unicorn not installed')
        import struct
        from unicorn.x86_const import (UC_X86_REG_EAX, UC_X86_REG_EBX,
            UC_X86_REG_ECX, UC_X86_REG_EDX, UC_X86_REG_ESI, UC_X86_REG_EDI,
            UC_X86_REG_EBP, UC_X86_REG_ESP, UC_X86_REG_EFLAGS)
        base, obj, typ, player = 0x10000000, 0x30010000, 0x30020000, 0x30030000
        defaults = dict(mode=ww.MODE_ON, owner=player, factory=16, naval=0,
                        vt=ww.BUILDING_VT, health=100, active=1, limbo=0,
                        caller=0x443ca1, zone=0, cloning=0)
        cases = [({}, 5), ({'factory': 40}, 5), ({'caller': 0x443b2e}, 5),
                 ({'mode': ww.MODE_DRAIN}, 0), ({'mode': ww.MODE_OFF}, 0),
                 ({'owner': player + 4}, 0), ({'naval': 1, 'factory': 40}, 0),
                 ({'factory': 3}, 0), ({'factory': 6}, 0),
                 ({'vt': ww.UNIT_VT}, 0), ({'health': 0}, 0),
                 ({'active': 0}, 0), ({'limbo': 1}, 0),
                 ({'caller': 0x6c0000}, 0), ({'zone': 4}, 4),
                 ({'factory': 0, 'cloning': 1}, 5),
                 ({'factory': 0, 'cloning': 0}, 0),
                 ({'factory': 0, 'cloning': 1, 'owner': player + 4}, 0),
                 ({'factory': 0, 'cloning': 1, 'mode': ww.MODE_DRAIN}, 0),
                 ({'factory': 0, 'cloning': 1, 'mode': ww.MODE_OFF}, 0),
                 ({'factory': 0, 'cloning': 1, 'naval': 1}, 0)]
        import itertools
        for (overrides, expected), site in itertools.product(cases, ('rally_zone', 'rally_destination')):
            with self.subTest(overrides=overrides, site=site):
                d = defaults | overrides
                mu = machine()
                def put(a, v):
                    mu.mem_write(a, struct.pack('<I', v))
                mu.mem_write(base, ww.block_bytes(base))
                mu.mem_write(base + ww.MODE_OFFSET, bytes([d['mode']]))
                put(ww.PLAYER_PTR, player)
                put(obj, d['vt'])
                put(obj + 0x418, typ)
                put(obj + 0x1b4, d['owner'])
                put(obj + 0x6c, d['health'])
                mu.mem_write(obj + 0x7d, bytes([d['active']]))
                mu.mem_write(obj + 0x76, bytes([d['limbo']]))
                put(typ + 0x500, d['zone'])
                put(typ + 0xc44, d['factory'])
                mu.mem_write(typ + 0x134b, bytes([d['cloning']]))
                mu.mem_write(typ + 0xada, bytes([d['naval']]))
                registers = {UC_X86_REG_EAX: typ, UC_X86_REG_ESI: obj,
                    UC_X86_REG_EBX: 123, UC_X86_REG_ECX: 456, UC_X86_REG_EDX: 789,
                    UC_X86_REG_EDI: 321, UC_X86_REG_EBP: 654,
                    UC_X86_REG_ESP: 0x7f080000, UC_X86_REG_EFLAGS: 0x246}
                put(registers[UC_X86_REG_ESP] + 0x20, d['caller'])
                for r, v in registers.items():
                    mu.reg_write(r, v)
                if site == 'rally_destination':
                    registers[UC_X86_REG_EBP] = obj
                    registers[UC_X86_REG_ESI] = d['zone']
                    mu.reg_write(UC_X86_REG_EBP, obj)
                    mu.reg_write(UC_X86_REG_ESI, d['zone'])
                    put(registers[UC_X86_REG_ESP] + 0x20, 0)
                addr, raw, _ = ww.SITES[site]
                start = base + ww.site_offset(list(ww.SITES).index(site))
                mu.emu_start(start, addr + len(raw) // 2, count=1000)
                if site == 'rally_destination':
                    if overrides == {'caller': 0x6c0000}:
                        expected = 5  # Dedicated setter has no caller restriction.
                    registers[UC_X86_REG_ESI] = expected
                    registers[UC_X86_REG_EDX] = obj + 0x88
                    speed = struct.unpack('<I', mu.mem_read(registers[UC_X86_REG_ESP] + 0x20, 4))[0]
                    self.assertEqual(speed, 6 if expected == 5 else 0)
                else:
                    registers[UC_X86_REG_EDI] = expected
                for r, v in registers.items():
                    self.assertEqual(mu.reg_read(r), v)

    def test_version_two_block_is_upgraded_in_place(self):
        c = ww.WaterWalkController(self.p)
        c.enable()
        base = c.base
        end = ww.SITE_OFFSET + ww.PREVIOUS_SITE_COUNT * ww.SITE_STRIDE
        for name in list(ww.SITES)[ww.PREVIOUS_SITE_COUNT:]:
            c.hooks.switch(name, False)
        self.p.patch(base, ww.PREVIOUS_MAGIC)
        self.p.patch(base + end, bytes(ww.BLOCK_SIZE - end))
        upgraded = ww.WaterWalkController(self.p)
        self.assertIsNone(upgraded.adopt_error)
        self.assertEqual(upgraded.base, base)
        self.assertFalse(upgraded.enabled)
        upgraded.enable()
        self.assertEqual(set(upgraded.installed), set(ww.SITES))
        self.assertEqual(self.p.read(base, len(ww.MAGIC)), ww.MAGIC)

    def test_firing_position_uses_shooter_ebp_for_infantry_and_tanks(self):
        from test_batch_emulated import HAVE_UNICORN, machine
        if not HAVE_UNICORN:
            self.skipTest("unicorn not installed")
        import struct
        from unicorn.x86_const import (UC_X86_REG_EAX, UC_X86_REG_EBP,
            UC_X86_REG_EBX, UC_X86_REG_ECX, UC_X86_REG_EDI,
            UC_X86_REG_EDX, UC_X86_REG_ESI, UC_X86_REG_ESP,
            UC_X86_REG_EFLAGS)
        base, obj, typ, player = 0x10000000, 0x30010000, 0x30020000, 0x30030000
        for vt, offset in ((ww.INFANTRY_VT, 0x5a8), (ww.UNIT_VT, 0x5ac)):
            for mode, land, owner, expected_zone in (
                    (ww.MODE_ON, 2, player, 4),
                    (ww.MODE_DRAIN, 2, player, 4),
                    (ww.MODE_DRAIN, 6, player, 4),
                    (ww.MODE_DRAIN, 0, player, 1),
                    (ww.MODE_OFF, 2, player, 1),
                    (ww.MODE_ON, 2, player + 4, 1)):
                for name in (list(ww.SITES)[ww.PREVIOUS_SITE_COUNT:ww.ATTACK_SITE_COUNT]
                             + list(ww.SITES)[21:]):
                    with self.subTest(vt=vt, mode=mode, land=land, owner=owner, site=name):
                        mu = machine()
                        def put(a, v):
                            mu.mem_write(a, struct.pack('<I', v))
                        mu.mem_write(base, ww.block_bytes(base))
                        mu.mem_write(base + ww.MODE_OFFSET, bytes([mode]))
                        put(ww.PLAYER_PTR, player)
                        put(obj, vt)
                        put(obj + offset, typ)
                        put(obj + 0x1b4, owner)
                        put(obj + 0x6c, 100)
                        mu.mem_write(obj + 0x7d, b'\x01')
                        put(typ + 0x500, 1)
                        put(typ + 0x5a4, 1)
                        put(ww.CELL_ARRAY_PTR, 0x30040000)
                        put(0x30040000, 0x30050000)
                        put(0x30050000 + 0xec, land)
                        registers = {UC_X86_REG_EAX: typ, UC_X86_REG_EBP: obj,
                            UC_X86_REG_EBX: 123, UC_X86_REG_ECX: 456,
                            UC_X86_REG_EDI: 789, UC_X86_REG_EDX: 321,
                            UC_X86_REG_ESI: 7, UC_X86_REG_ESP: 0x7f080000,
                            UC_X86_REG_EFLAGS: 0x246}
                        for r, v in registers.items():
                            mu.reg_write(r, v)
                        address, raw, kind = ww.SITES[name]
                        start = base + ww.site_offset(list(ww.SITES).index(name))
                        mu.emu_start(start, address + len(raw) // 2, count=1000)
                        output = UC_X86_REG_ECX if kind == 'speed_ecx_ebp' else UC_X86_REG_EAX
                        registers[output] = (6 if expected_zone == 4 else 1) if kind.startswith('speed') else expected_zone
                        for r, v in registers.items():
                            self.assertEqual(mu.reg_read(r), v)

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
        # A block this version cannot adopt must not fail attach: the feature
        # is reported unavailable and stays inert until the game restarts.
        degraded = ww.WaterWalkController(self.p)
        self.assertIn("版本不匹配", degraded.adopt_error)
        self.assertIsNone(degraded.base)
        self.assertFalse(degraded.enabled)
        with self.assertRaisesRegex(RuntimeError, "版本不匹配"):
            degraded.enable()
        degraded.disable()
        degraded.close()
        self.assertEqual(self.p.read(c.base + 0x200, 1), b"\xcc")
        self.p.patch(c.base + 0x200, prior)
        self.assertIsNone(ww.WaterWalkController(self.p).adopt_error)
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
