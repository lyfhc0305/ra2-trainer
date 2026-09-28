import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_core import HookMemory, ROOT, PE, Cs, CS_ARCH_X86, CS_MODE_32, disasm_checked, game_pe
from trainer import production, protection
from trainer.app import MainWindow


class NewHookTests(unittest.TestCase):
    def test_originals_match_complete_instructions_in_game(self):
        pe = game_pe(self)
        sites = list(production.SITES.values()) + list(protection.SITES.items())
        for address, original in sites:
            self.assertEqual(pe.read(address, len(original)), original)
            ins = list(Cs(CS_ARCH_X86, CS_MODE_32).disasm(original, address))
            self.assertEqual(sum(i.size for i in ins), len(original))

    def test_trampoline_branches_land_on_instruction_boundaries(self):
        base = 0x15000000
        codes = [production.speed_code(base, base - 0x100), production.queue_code(base)]
        codes += [protection.damage_code(base, entry, original)
                  for entry, original in protection.SITES.items()]
        for code in codes:
            ins = disasm_checked(self, code, base)

    def test_production_precedence_and_full_restoration(self):
        p = HookMemory()
        for address, original in production.SITES.values():
            p.patch(address, original)
        c = production.ProductionController(p)
        c.set("fast_build", True)
        self.assertEqual(p.read_u32(c.base), 1)
        c.set("instant_build", True)
        self.assertEqual(p.read_u32(c.base), 2)
        c.set("instant_build", False)
        self.assertEqual(p.read_u32(c.base), 1)
        c.set("unlimited_queue", True)
        c.close()
        for address, original in production.SITES.values():
            self.assertEqual(p.read(address, len(original)), original)

    def test_production_foreign_bytes_rejected(self):
        p = HookMemory()
        for address, original in production.SITES.values():
            p.patch(address, original)
        p.patch(0x4B9342, b"\xcc")
        before = dict(p.data)
        with self.assertRaises(RuntimeError):
            production.ProductionController(p).set("fast_build", True)
        self.assertEqual(before, p.data)

    def test_protection_partial_install_rolls_back(self):
        p = HookMemory()
        for address, original in protection.SITES.items():
            p.patch(address, original)
        c = protection.ProtectionController(p)
        p.fail_at = 0x43EE70
        with self.assertRaises(OSError):
            c.enable()
        self.assertFalse(c.owned)
        for address, original in protection.SITES.items():
            self.assertEqual(p.read(address, len(original)), original)
        c.enable()
        self.assertTrue(c.installed)
        c.close()
        for address, original in protection.SITES.items():
            self.assertEqual(p.read(address, len(original)), original)

    def test_protection_external_change_not_overwritten(self):
        p = HookMemory()
        for address, original in protection.SITES.items():
            p.patch(address, original)
        c = protection.ProtectionController(p)
        c.enable()
        address = next(iter(protection.SITES))
        p.patch(address, b"\xcc")
        with self.assertRaises(RuntimeError):
            c.close()
        self.assertEqual(p.read(address, 1), b"\xcc")


class RepairTests(unittest.TestCase):
    def test_increment_capped_and_boosted_hp_preserved(self):
        p = HookMemory()
        window = SimpleNamespace(proc=p, repair_amount=80)
        spec = dict(op="heal", kinds=[0x79CBBC])
        with patch("trainer.units.player_technos_cached", return_value=[(0x2000, 0x79CBBC)]), \
             patch("trainer.units.type_of", return_value=0x3000), \
             patch("trainer.units.player_house", return_value=0x4000), \
             patch("trainer.units.valid_techno", return_value=True):
            p.write_u32(0x30A0, 1000)
            for hp, amount, expected in [(100, 80, 180), (980, 80, 1000),
                                          (500, 0, 1000), (2000, 80, 2000), (0, 80, 0)]:
                p.write_u32(0x206C, hp)
                window.repair_amount = amount
                MainWindow._apply_loop(window, spec)
                self.assertEqual(p.read_u32(0x206C), expected)

    def test_invalid_repair_amount_retains_previous_value(self):
        window = SimpleNamespace(repair_amount=20, can_write=lambda: True,
                                 save_state=lambda: None, log=lambda text: None)
        for text in ("-1", "1000001", "nan", "1.5"):
            MainWindow.value_write(window, "repair_amount", text)
            self.assertEqual(window.repair_amount, 20)


if __name__ == "__main__":
    unittest.main()
