import struct
import unittest

from test_core import HookMemory
from test_core import Cs, CS_ARCH_X86, CS_MODE_32, disasm_checked, game_exe
from trainer import airport_slots as ap
from trainer import build_unlock
from trainer.features import FEATURES


class FakeExecutor:
    def __init__(self, proc):
        self.proc = proc
        self.base = 0x14000000
        self.calls = []
        proc.patch(self.base, struct.pack("<I", 0))

    def install(self):
        pass

    def call(self, address, timeout=5.0):
        self.calls.append(address)
        code = self.proc.read(address, 15)
        factor = struct.unpack_from("<I", code, 10)[0]  # mov [esp],factor
        self.proc.patch(address - ap.RECONCILE_OFFSET + ap.FACTOR_OFFSET,
                        struct.pack("<I", factor))
        return 1


class AirportSlotsTests(unittest.TestCase):
    def setUp(self):
        self.p = HookMemory()
        for address, raw, _kind in ap.SITES.values():
            self.p.patch(address, bytes.fromhex(raw))
        self.p.patch(0xA35DB4, struct.pack("<I", 0x14000000))
        self.ex = FakeExecutor(self.p)

    def test_native_sites_and_branch_boundaries(self):
        pe = game_exe()
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        base = 0x15000000
        all_codes = [(base + ap.TYPE_HELPER, ap.type_helper_code()),
                     (base + ap.LIMIT_HELPER,
                      ap.limit_helper_code(base + ap.LIMIT_HELPER,
                                           base + ap.TYPE_HELPER,
                                           base + ap.FACTOR_OFFSET)),
                     (base + ap.RADIO_HELPER,
                      ap.radio_helper_code(base + ap.RADIO_HELPER,
                                           base + ap.LIMIT_HELPER))]
        for i, (name, (address, raw, _kind)) in enumerate(ap.SITES.items()):
            self.assertEqual(pe.read(address, len(raw) // 2), bytes.fromhex(raw), name)
            at = base + ap.SITE_OFFSET + i * ap.SITE_STRIDE
            code = (ap.full_site_code(at, base + ap.RADIO_HELPER,
                                      base + ap.FACTOR_OFFSET)
                    if name == "send_full" else
                    ap.site_code(name, at, base + ap.TYPE_HELPER,
                                 base + ap.LIMIT_HELPER, base + ap.RADIO_HELPER))
            all_codes.append((at, code))
        all_codes.append((base + ap.RECONCILE_OFFSET,
                          ap.reconcile_code(base + ap.RECONCILE_OFFSET,
                                            0x14000000, 2, base + ap.TYPE_HELPER,
                                            base + ap.FACTOR_OFFSET)))
        for at, code in all_codes:
            ins = disasm_checked(self, code, at)

    def test_radio_and_reconcile_critical_paths(self):
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        base = 0x15000000
        def text(name):
            at = base + ap.SITE_OFFSET + list(ap.SITES).index(name) * ap.SITE_STRIDE
            code = (ap.full_site_code(at, base + ap.RADIO_HELPER,
                                      base + ap.FACTOR_OFFSET)
                    if name == "send_full" else
                    ap.site_code(name, at, base + ap.TYPE_HELPER,
                                 base + ap.LIMIT_HELPER, base + ap.RADIO_HELPER))
            return [f"{i.mnemonic} {i.op_str}" for i in cs.disasm(code, at)]
        self.assertIn("mov edx, dword ptr [esp + 0x28]", text("can_request"))
        self.assertIn("jmp 0x636b49", text("send_full"))  # no forced slot-0 eviction
        self.assertIn("and dword ptr [esp + 0x1c], 3", text("dock_coords"))
        self.assertIn("cmp dword ptr [edx + 0xd0], 4", text("send_full"))
        at = base + ap.RECONCILE_OFFSET
        code = ap.reconcile_code(at, 0x14000000, 3,
                                 base + ap.TYPE_HELPER, base + ap.FACTOR_OFFSET)
        rows = [f"{i.mnemonic} {i.op_str}" for i in cs.disasm(code, at)]
        self.assertIn("call 0x636f40", rows)
        self.assertIn("cmp byte ptr [ebp + 0x1365], 0", rows)
        self.assertIn("mov dword ptr [edi + 0x218], edx", rows)
        self.assertIn("mov dword ptr [0x15000020], eax", rows)

    def test_factor_lifecycle_and_adopt(self):
        c = ap.AirportSlotsController(self.p, self.ex)
        self.assertEqual(c.set_factor(1), 1)
        self.assertFalse(c.installed)
        self.assertEqual(c.set_factor(2), 2)
        self.assertEqual(len(c.installed), len(ap.SITES))
        adopted = ap.AirportSlotsController(self.p, self.ex)
        self.assertEqual(adopted.factor, 2)
        adopted.close()
        self.assertEqual(adopted.factor, 1)
        self.assertEqual(len(adopted.installed), len(ap.SITES))  # high-slot guard retained
        feat = next(f for f in FEATURES if f["id"] == "airport_slots")
        self.assertEqual((feat["kind"], feat["status"]), ("value", "ok"))

    def test_pending_executor_does_not_overwrite_code(self):
        c = ap.AirportSlotsController(self.p, self.ex)
        c.set_factor(2)
        before = self.p.read(c.base + ap.RECONCILE_OFFSET, 50)
        self.p.patch(self.ex.base, struct.pack("<I", 1))
        with self.assertRaisesRegex(RuntimeError, "上一条"):
            c.set_factor(3)
        self.assertEqual(self.p.read(c.base + ap.RECONCILE_OFFSET, 50), before)
        self.assertEqual(c.factor, 2)

    def test_unlock_build_keeps_airport_aircraft_capacity(self):
        code = build_unlock.site_code("reached_limit", 0x15001000)
        cs = Cs(CS_ARCH_X86, CS_MODE_32)
        rows = [f"{i.mnemonic} {i.op_str}" for i in cs.disasm(code, 0x15001000)]
        self.assertIn("call dword ptr [edx + 0x2c]", rows)
        self.assertIn("cmp eax, 3", rows)
        self.assertIn("cmp byte ptr [eax + 0xba3], 0", rows)


if __name__ == "__main__":
    unittest.main()
