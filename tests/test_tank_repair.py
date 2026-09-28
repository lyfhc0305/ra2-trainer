import struct
import unittest

from test_core import Memory, Cs, CS_ARCH_X86, CS_MODE_32, disasm_checked, game_exe
from trainer import tank_repair as repair


class RepairMemory(Memory):
    def __init__(self):
        super().__init__()
        self.next_alloc = 0x15000000
        self.freed = []
        self.idle = True
        self.rate = 0.01600000075995922
        self.patch(repair.AI_ENTRY, repair.AI_ORIGINAL)
        self.patch(repair.RULES_PTR, struct.pack("<I", 0x100000))
        self.patch(0x100000 + 0x1334, struct.pack("<i", 8))

    def read_f64(self, _address):
        return self.rate

    def alloc(self, size):
        address = self.next_alloc
        self.next_alloc += (size + 0xFFF) & ~0xFFF
        return address

    def free(self, address):
        self.freed.append(address)

    def code_idle(self, _address, _size):
        return self.idle

    def write(self, address, data):
        return self.patch(address, data)

    def patch_quiescent(self, address, expected, replacement):
        if self.read(address, len(expected)) != expected or not self.patch(address, replacement):
            raise OSError("failed patch")


class TankRepairTests(unittest.TestCase):
    def test_native_rules_and_original_entry(self):
        pe = game_exe()
        self.assertEqual(pe.read(repair.AI_ENTRY, len(repair.AI_ORIGINAL)), repair.AI_ORIGINAL)
        p = RepairMemory()
        self.assertEqual(repair.native_repair_settings(p), (8, 15))
        p.rate = float("nan")
        with self.assertRaises(RuntimeError):
            repair.native_repair_settings(p)

    def test_trampoline_instruction_boundaries_and_guards(self):
        base = 0x15000000
        code = repair.repair_code(base, 0x16000000, 8, 15)
        self.assertLess(len(code), 0x1000)
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        ins = disasm_checked(self, code, base)
        instructions = {f"{i.mnemonic} {i.op_str}" for i in ins}
        for expected in (
            "mov ebx, dword ptr [esi + 0x10]",  # instance generation
            "cmp byte ptr [ebx + 0xad8], 0",  # repairable
            "cmp byte ptr [ebx + 0xada], 0",  # non-naval
            "cmp dword ptr [ebx + 0x5a4], 1",  # tracks
            "cmp dword ptr [ebx + 0x5a4], 2",  # wheels
            "mov edx, dword ptr [ebx + 0xa0]",  # full positive displacement
            f"cmp edi, {hex(0x16000000 + repair.TABLE_SLOTS * repair.SLOT_BYTES)}",
            "mov edi, 0x16000000",  # wrap a collision at the final bucket
            "cmp byte ptr [ebx + 0x1349], 0",  # native repair bay
            "mov dword ptr [esi + 0x6c], eax",
            "mov dword ptr [esi + 0x70], eax",
        ):
            self.assertIn(expected, instructions)

    def test_disable_restores_entry_and_reenable_clears_timers(self):
        p = RepairMemory()
        c = repair.TankRepairController(p)
        c.enable()
        self.assertTrue(c.enabled)
        self.assertNotEqual(p.read(repair.AI_ENTRY, len(repair.AI_ORIGINAL)), repair.AI_ORIGINAL)
        old_table = c.table
        p.patch(old_table, b"\x5a" * 16)
        c.disable()
        self.assertEqual(p.read(repair.AI_ENTRY, len(repair.AI_ORIGINAL)), repair.AI_ORIGINAL)
        allocated = p.next_alloc
        c.enable()
        self.assertEqual(c.table, old_table)  # idle block is reused, not leaked
        self.assertEqual(p.next_alloc, allocated)
        self.assertEqual(p.read(c.table, 16), bytes(16))
        c.close()
        self.assertEqual(p.read(repair.AI_ENTRY, len(repair.AI_ORIGINAL)), repair.AI_ORIGINAL)

    def test_repeated_toggles_do_not_grow_memory(self):
        p = RepairMemory()
        c = repair.TankRepairController(p)
        c.enable()
        c.disable()
        allocated = p.next_alloc
        for _ in range(5):
            c.enable()
            c.disable()
        self.assertEqual(p.next_alloc, allocated)
        code_base, table = c.code_base, c.table
        c.close()
        self.assertEqual(set(p.freed), {code_base, table})

    def test_busy_old_block_is_retired_not_freed(self):
        p = RepairMemory()
        c = repair.TankRepairController(p)
        c.enable()
        c.disable()
        old = (c.code_base, c.table)
        p.idle = False
        c.enable()
        self.assertNotEqual(c.code_base, old[0])
        self.assertIn(old, c.retired)
        self.assertEqual(p.freed, [])
        c.disable()
        p.idle = True
        c.close()
        self.assertTrue(set(old) <= set(p.freed))

    def test_releases_own_hook_left_by_abnormal_exit(self):
        p = RepairMemory()
        c = repair.TankRepairController(p)
        c.enable()
        orphan_base = c.code_base
        # A new trainer session attaches to the same game.
        fresh = repair.TankRepairController(p)
        fresh.enable()
        self.assertNotEqual(fresh.code_base, orphan_base)
        self.assertTrue(fresh.enabled)
        fresh.close()
        self.assertEqual(p.read(repair.AI_ENTRY, len(repair.AI_ORIGINAL)), repair.AI_ORIGINAL)


if __name__ == "__main__":
    unittest.main()
