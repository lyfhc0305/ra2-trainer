"""Hook lifecycle regressions: failed installs, abnormal exits, reused type addresses."""
import struct
import unittest
import unittest.mock

from test_core import HookMemory, Memory
from test_airport_slots import FakeExecutor
from trainer import airport_slots as ap
from trainer import build_unlock, chrono_landing, power, production, protection, typeedit, units
from trainer.hooks import HookSites, assemble
from trainer.executor import relative_jump, release_orphan
from trainer.objscan import TYPE_VT


class AllocMemory(HookMemory):
    def __init__(self):
        super().__init__()
        self.next_alloc = 0x15000000
        self.freed = []

    def alloc(self, _size):
        address = self.next_alloc
        self.next_alloc += 0x1000
        return address

    def free(self, address):
        self.freed.append(address)


class PowerTests(unittest.TestCase):
    def setUp(self):
        self.p = AllocMemory()
        self.p.patch(power.HOOK, power.ORIGINAL)
        self.p.patch(0x4E44F0, bytes.fromhex("8b86d452000085c00f9ec24a23c2"))
        self.c = power.PowerController(self.p, executor=None)
        self.c.refresh = lambda: None

    def test_failed_hook_write_is_freed_and_never_reused(self):
        self.p.fail_at = 0x15000000
        with self.assertRaises(OSError):
            self.c.enable()
        self.assertIsNone(self.c.base)
        self.assertEqual(self.p.freed, [0x15000000])
        self.c.enable()  # a fresh, fully written block
        self.assertEqual(self.c.base, 0x15001000)
        self.assertEqual(self.p.read(self.c.base, 3), power.build_power_hook(self.c.base)[:3])

    def test_refresh_failure_reports_hook_state(self):
        def timeout():
            raise TimeoutError("paused")
        self.c.refresh = timeout
        self.c.enable()  # never waits for a game frame on the caller's thread
        self.assertTrue(self.c.installed)
        self.assertEqual(self.c.pending_refresh, "已开启")
        with self.assertRaisesRegex(RuntimeError, "已开启"):
            self.c.flush_refresh()  # the background job reports the hook state
        self.assertIsNone(self.c.pending_refresh)
        self.assertTrue(self.c.installed)

    def test_releases_own_hook_left_by_abnormal_exit(self):
        self.c.enable()
        fresh = power.PowerController(self.p, executor=None)
        fresh.refresh = lambda: None
        fresh.enable()
        self.assertTrue(fresh.installed)
        self.assertNotEqual(fresh.base, self.c.base)
        fresh.close()
        self.assertEqual(self.p.read(power.HOOK, len(power.ORIGINAL)), power.ORIGINAL)


class OrphanTests(unittest.TestCase):
    def test_foreign_hook_is_left_alone(self):
        p = AllocMemory()
        site, original = chrono_landing.LANDING_SITE, chrono_landing.ORIGINAL
        foreign = relative_jump(site, 0x16000000) + b"\x90" * (len(original) - 5)
        p.patch(site, foreign)
        p.patch(0x16000000, b"\xcc" * 64)  # not our code
        self.assertFalse(release_orphan(p, site, original, chrono_landing.landing_code))
        self.assertEqual(p.read(site, len(original)), foreign)
        with self.assertRaises(RuntimeError):
            chrono_landing.ChronoLandingController(p).enable()

    def test_protection_releases_every_site(self):
        p = AllocMemory()
        for entry, original in protection.SITES.items():
            p.patch(entry, original)
        protection.ProtectionController(p).enable()
        fresh = protection.ProtectionController(p)
        fresh.enable()
        self.assertTrue(fresh.installed)
        fresh.close()
        for entry, original in protection.SITES.items():
            self.assertEqual(p.read(entry, len(original)), original)


class AirportCloseTests(unittest.TestCase):
    def test_close_at_menu_resets_factor_without_executor(self):
        p = HookMemory()
        for address, raw, _kind in ap.SITES.values():
            p.patch(address, bytes.fromhex(raw))
        p.patch(ap.PLAYER_PTR, struct.pack("<I", 0x14000000))
        ex = FakeExecutor(p)
        c = ap.AirportSlotsController(p, ex)
        c.set_factor(2)
        p.patch(ap.PLAYER_PTR, bytes(4))  # back at the main menu
        calls = len(ex.calls)
        c.close()
        self.assertEqual(len(ex.calls), calls)
        self.assertEqual(p.read_u32(c.base + ap.FACTOR_OFFSET), 1)
        self.assertEqual(c.factor, 1)


class TypeMemory(Memory):
    def read_u8(self, a):
        b = self.read(a, 1)
        return b[0] if b else None

    def read_cstr(self, a, size=64):
        b = self.read(a, size)
        return None if b is None else b.split(b"\0")[0].decode("latin1")

    def read_value(self, a, kind):
        return getattr(self, "read_" + kind)(a)

    def write_value(self, a, v, kind):
        return self.patch(a, struct.pack({"i32": "<i", "u8": "<B"}[kind], int(v)))


class TypeEditorTests(unittest.TestCase):
    TYP = 0x3000

    def setUp(self):
        self.p = TypeMemory()
        self.set_type(b"HTNK", speed=6)
        self.e = typeedit.TypeEditor(self.p)
        self.e.targets = lambda: [self.TYP]

    def set_type(self, name, speed):
        self.p.patch(self.TYP, struct.pack("<I", next(iter(TYPE_VT))))
        self.p.patch(self.TYP + typeedit.ID_OFFSET, name.ljust(25, b"\0"))
        self.p.patch(self.TYP + 0x5A0, struct.pack("<i", speed))

    def test_restore_skips_a_different_type_at_the_same_address(self):
        self.e.write("u_speed", "2")
        self.set_type(b"MTNK", speed=12)  # new match reused the address
        self.assertEqual(self.e.restore(), 0)
        self.assertEqual(self.p.read_u32(self.TYP + 0x5A0), 12)

    def test_restore_own_type(self):
        self.e.write("u_speed", "2")
        self.assertEqual(self.p.read_u32(self.TYP + 0x5A0), 12)
        self.assertEqual(self.e.restore(), 1)
        self.assertEqual(self.p.read_u32(self.TYP + 0x5A0), 6)

    def test_immobile_type_stays_immobile(self):
        self.set_type(b"GAWALL", speed=0)
        self.e.write("u_speed", "3")
        self.assertEqual(self.p.read_u32(self.TYP + 0x5A0), 0)

    def test_failed_second_write_keeps_the_committed_record(self):
        self.e.write("u_speed", "2")  # 6 -> 12
        with unittest.mock.patch.object(self.p, "write_value", return_value=False):
            with self.assertRaises(OSError):
                self.e.write("u_speed", "3")  # 18 never lands
        self.assertEqual(self.p.read_u32(self.TYP + 0x5A0), 12)
        self.assertEqual(self.e.restore(), 1)  # 12 is still recognised as ours
        self.assertEqual(self.p.read_u32(self.TYP + 0x5A0), 6)

    def test_unknown_write_outcome_accepts_either_value(self):
        self.e.write("u_speed", "2")  # 6 -> 12
        real_read = self.p.read_value
        with unittest.mock.patch.object(self.p, "write_value", return_value=False), \
             unittest.mock.patch.object(self.p, "read_value",
                                        side_effect=[12, None]):  # plan, read-back
            with self.assertRaises(OSError):
                self.e.write("u_speed", "3")
        self.assertEqual(self.e.originals[(self.TYP, 0x5A0, "i32")][1], (12, 18))
        self.p.patch(self.TYP + 0x5A0, struct.pack("<i", 18))  # it did land after all
        self.assertEqual(real_read(self.TYP + 0x5A0, "i32"), 18)
        self.assertEqual(self.e.restore(), 1)
        self.assertEqual(self.p.read_u32(self.TYP + 0x5A0), 6)

    def test_restore_continues_past_a_conflict(self):
        second = 0x4000
        self.p.patch(second, struct.pack("<I", next(iter(TYPE_VT))))
        self.p.patch(second + typeedit.ID_OFFSET, b"MTNK".ljust(25, b"\0"))
        self.p.patch(second + 0x5A0, struct.pack("<i", 6))
        self.e.targets = lambda: [self.TYP, second]
        self.e.write("u_speed", "2")
        self.p.patch(self.TYP + 0x5A0, struct.pack("<i", 99))  # changed by someone else
        with self.assertRaises(RuntimeError):
            self.e.restore()
        self.assertEqual(self.p.read_u32(second + 0x5A0), 6)
        self.assertEqual(self.p.read_u32(self.TYP + 0x5A0), 99)
        self.assertEqual(list(self.e.originals), [(self.TYP, 0x5A0, "i32")])  # kept for a retry


class HookSitesTests(unittest.TestCase):
    SITES = {"a": (0x401000, bytes.fromhex("8bec83ec10"), 0x100),
             "b": (0x402000, bytes.fromhex("5657535550"), 0x200)}

    def setUp(self):
        self.p = AllocMemory()
        for address, original, _offset in self.SITES.values():
            self.p.patch(address, original)
        self.hooks = HookSites(self.p, "测试", self.SITES, lambda base: assemble(
            0x1000, [(0x100, bytes.fromhex("90c3")), (0x200, bytes.fromhex("ccc3"))], "测试"))

    def test_failed_after_step_rolls_back_and_keeps_the_original_error(self):
        def fail():
            raise OSError("mode write failed")
        with self.assertRaisesRegex(OSError, "mode write failed"):
            self.hooks.apply(self.SITES, after=fail)
        self.assertFalse(self.hooks.installed)
        for address, original, _offset in self.SITES.values():
            self.assertEqual(self.p.read(address, len(original)), original)

    def test_close_restores_remaining_sites_past_a_foreign_change(self):
        self.hooks.apply(self.SITES)
        self.p.patch(0x402000, bytes.fromhex("cc"))  # someone else rewrote site b
        with self.assertRaisesRegex(RuntimeError, "外部修改"):
            self.hooks.close()
        self.assertEqual(self.p.read(0x401000, 5), self.SITES["a"][1])
        self.assertEqual(self.p.read(0x402000, 1), bytes.fromhex("cc"))

    def test_overlapping_stubs_rejected(self):
        with self.assertRaises(ValueError):
            assemble(0x1000, [(0x100, bytes.fromhex("90") * 0x101), (0x200, bytes.fromhex("c3"))], "测试")

    def test_every_block_controller_releases_its_own_leftovers(self):
        for module, make, switch_on in (
                (production, production.ProductionController,
                 lambda c: c.set("unlimited_queue", True) or c.set("fast_build", True)),
                (build_unlock, build_unlock.BuildUnlockController,
                 lambda c: c.set("tech_all", True) or c.set("unlock_build", True))):
            p = AllocMemory()
            for spec in module.SITES.values():
                address, original = ((spec[1], bytes.fromhex(spec[2])) if module is build_unlock
                                     else spec)
                p.patch(address, original)
            switch_on(make(p))  # this session "crashes" with hooks live
            fresh = make(p)
            switch_on(fresh)
            fresh.close()
            for spec in module.SITES.values():
                address, original = ((spec[1], bytes.fromhex(spec[2])) if module is build_unlock
                                     else spec)
                self.assertEqual(p.read(address, len(original)), original, module.__name__)


class SelectionTests(unittest.TestCase):
    def test_selection_vector_without_heap_scan(self):
        p = TypeMemory()
        house, unit, enemy = 0x5000, 0x6000, 0x7000
        p.patch(units.PLAYER_PTR, struct.pack("<I", house))
        p.patch(units.SELECTED_DATA, struct.pack("<I", 0x8000))
        p.patch(units.SELECTED_COUNT, struct.pack("<I", 3))
        p.patch(0x8000, struct.pack("<III", unit, enemy, unit))
        for obj, owner in ((unit, house), (enemy, 0x9000)):
            p.patch(obj, struct.pack("<I", 0x7ADDF8))
            p.patch(obj + 0x1B4, struct.pack("<I", owner))
            p.patch(obj + 0x6C, struct.pack("<I", 100))
            p.patch(obj + 0x77, bytes([1]))
        with unittest.mock.patch.object(units, "iter_technos", side_effect=AssertionError):
            self.assertEqual(units.selected_technos(p, own=True), [(unit, 0x7ADDF8)])
            self.assertEqual(len(units.selected_technos(p)), 2)

    def enemy_selected(self, player):
        p = TypeMemory()
        enemy = 0x7000
        if player is not None:
            p.patch(units.PLAYER_PTR, struct.pack("<I", player))
        p.patch(units.SELECTED_DATA, struct.pack("<I", 0x8000))
        p.patch(units.SELECTED_COUNT, struct.pack("<I", 1))
        p.patch(0x8000, struct.pack("<I", enemy))
        p.patch(enemy, struct.pack("<I", 0x7ADDF8))
        p.patch(enemy + 0x1B4, struct.pack("<I", 0x9000))
        p.patch(enemy + 0x6C, struct.pack("<I", 100))
        p.patch(enemy + 0x77, bytes([1]))
        return p, enemy

    def test_unknown_player_never_widens_own_selection(self):
        for player in (None, 0):  # read failed / not in a match
            p, enemy = self.enemy_selected(player)
            self.assertEqual(units.selected_technos(p, own=True), [], player)
            self.assertFalse(units.valid_techno(p, enemy, 0x7ADDF8, player))
            self.assertTrue(units.valid_techno(p, enemy, 0x7ADDF8))  # explicit "any owner"
            with self.assertRaises(RuntimeError):
                units.require_player_house(p)

    def test_write_fields_rechecks_owner_and_skips_unknown_house(self):
        from trainer.app import write_fields
        p, enemy = self.enemy_selected(0x5000)
        fields = [(0x6C, "i32")]
        self.assertEqual(write_fields(p, [(enemy, 0x7ADDF8)], fields, 1, None), 0)
        self.assertEqual(write_fields(p, [(enemy, 0x7ADDF8)], fields, 1, 0x5000), 0)
        self.assertEqual(p.read_u32(enemy + 0x6C), 100)
        self.assertEqual(write_fields(p, [(enemy, 0x7ADDF8)], fields, 1, 0x9000), 1)


if __name__ == "__main__":
    unittest.main()
