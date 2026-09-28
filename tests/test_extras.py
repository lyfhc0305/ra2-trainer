"""Campaign, economy, weapon hooks and feature profiles."""
import struct
import unittest
from unittest.mock import Mock, patch

from test_core import HookMemory, ROOT, PE, disasm_checked, game_pe, needs
from trainer import campaign as C, economy as E, weapons as W, psionic as P


class Memory(HookMemory):
    def write(self, address, data):
        return self.patch(address, data)

    def read_cstr(self, address, size):
        raw = self.read(address, size) or b""
        return raw.split(b"\0", 1)[0].decode("latin1")


def call_target(pe, at):
    code = pe.read(at, 5)
    assert code[0] == 0xE8, hex(at)
    return (at + 5 + struct.unpack("<i", code[1:])[0]) & 0xFFFFFFFF


class GameBytesTests(unittest.TestCase):
    """Every site and call relationship the hooks depend on, checked in the local game.exe."""

    def test_sites_match_local_game(self):
        pe = game_pe(self)
        sites = [(C.FRAME_ENTRY, C.FRAME_ORIGINAL)]
        sites += [(a, o) for a, o, _ in E.SITES.values()]
        sites += [(a, o) for a, o, _ in W.SITES.values()]
        for address, original in sites:
            self.assertEqual(pe.read(address, len(original)), original, hex(address))

    def test_call_relationships(self):
        pe = game_pe(self)
        self.assertEqual(call_target(pe, 0x540504), C.FRAME_ENTRY)  # once per frame
        self.assertEqual(call_target(pe, 0x6AEBC4), C.HOUSE_WIN)  # trigger "Winner is"
        self.assertEqual(pe.read(0x4E80C4, 3), bytes.fromhex("c20400"))  # Win(bool)
        self.assertEqual(call_target(pe, 0x702400), 0x4E5100)  # refinery unload
        self.assertEqual(call_target(pe, 0x7014F1), E.REDUCE_TIBERIUM)
        self.assertEqual(call_target(pe, 0x44BC23), 0x4E5280)  # repair payment
        # Mission timer at Scenario+0x7C8: trigger "timer start" writes the start frame.
        self.assertEqual(pe.read(0x6AE672, 6), bytes.fromhex("8988c8070000"))
        # Campaign start reads Campaign.Scenario at +0x9C.
        self.assertEqual(pe.read(0x5142CC, 6), bytes.fromhex("81c19c000000"))
        # Weapon.ROF +0xB0 and Range +0xB4 inside GetROF / GetWeaponRange.
        self.assertEqual(pe.read(0x6C9D15, 6), bytes.fromhex("db87b0000000"))  # fild [edi+0xB0]
        self.assertEqual(pe.read(0x6CD823, 6), bytes.fromhex("8b80b4000000"))

    def test_stubs_decode_and_return_to_game(self):
        base = 0x15000000
        stubs = [(C.freeze_code(base), C.CODE_OFFSET)]
        stubs += [(E.income_code(base, base + 0x100), 0x100), (E.harvest_code(base, base + 0x200), 0x200),
                  (E.repair_code("repair_building", base, base + 0x300), 0x300),
                  (E.repair_code("repair_unit", base, base + 0x380), 0x380),
                  (E.refund_code(base, base + 0x400), 0x400)]
        stubs += [(W.rof_code(base, base + 0x100), 0x100), (W.in_range_code(base, base + 0x200), 0x200),
                  (W.weapon_range_code(base, base + 0x300), 0x300)]
        for code, offset in stubs:
            ins = disasm_checked(self, code, base + offset)
            last = ins[-1]
            self.assertIn(last.mnemonic, ("jmp", "ret"))
        # Each stub fits before the next one in its block.
        for sites in (E.SITES, W.SITES):
            offsets = sorted(o for _a, _b, o in sites.values())
            self.assertEqual(len(offsets), len(set(offsets)))


class PsionicTests(unittest.TestCase):
    def test_sites_and_callers_in_game(self):
        pe = game_pe(self)
        for address, original, _offset, _cmp in P.SITES.values():
            self.assertEqual(pe.read(address, len(original)), original, hex(address))
        self.assertEqual(call_target(pe, 0x6C988E), 0x467F80)  # targeting -> CanCapture
        self.assertEqual(call_target(pe, 0x462335), 0x467FF0)  # warhead -> Capture
        self.assertEqual(pe.read(0x6CDECD, 7), bytes.fromhex("8a8564010000") + b"\x84")  # MindControl warhead

    def test_stubs_decode(self):
        base = 0x15000000
        for name, (_a, _o, offset, _c) in P.SITES.items():
            code = P.compare_code(base + offset) if name == "capture" else P.load_code(name, base + offset)
            ins = disasm_checked(self, code, base + offset)
            self.assertEqual(ins[-1].mnemonic, "jmp")

    def test_install_and_restore(self):
        p = Memory()
        for address, original, _o, _c in P.SITES.values():
            p.patch(address, original)
        c = P.PsionicShieldController(p)
        c.set(True)
        self.assertEqual(set(c.installed), set(P.SITES))
        c.close()
        for address, original, _o, _c in P.SITES.values():
            self.assertEqual(p.read(address, len(original)), original)


class ControllerTests(unittest.TestCase):
    def memory(self, sites):
        p = Memory()
        for address, original in sites:
            p.patch(address, original)
        return p

    def test_economy_installs_only_needed_sites(self):
        p = self.memory([(a, o) for a, o, _ in E.SITES.values()])
        c = E.EconomyController(p)
        c.set("repair_free", True)
        self.assertEqual(set(c.installed), {"repair_building", "repair_unit"})
        c.set_income(3)
        self.assertEqual(set(c.installed), {"repair_building", "repair_unit", "income"})
        self.assertEqual(struct.unpack("<fIII", p.read(0x15000000, 16)), (3.0, 0, 1, 0))
        c.set("repair_free", False)
        c.set_income(1)
        self.assertFalse(c.installed)
        for address, original, _ in E.SITES.values():
            self.assertEqual(p.read(address, len(original)), original)
        with self.assertRaises(ValueError):
            c.set_income(1000)

    def test_weapons_and_close(self):
        p = self.memory([(a, o) for a, o, _ in W.SITES.values()])
        c = W.WeaponController(p)
        c.set_rof(2)
        c.set_range(1.5)
        self.assertEqual(set(c.installed), {"rof", "in_range", "weapon_range"})
        self.assertEqual(struct.unpack("<fi", p.read(0x15000000, 8)), (0.5, 150))
        c.close()
        self.assertFalse(c.installed)
        with self.assertRaises(ValueError):
            c.set_rof(0.5)

    def test_timer_freeze_toggle(self):
        p = self.memory([(C.FRAME_ENTRY, C.FRAME_ORIGINAL)])
        c = C.CampaignController(p, Mock())
        c.set_freeze(True)
        self.assertTrue(c.installed)
        self.assertEqual(p.read_u32(0x15000000), 1)
        c.set_freeze(True)  # periodic reapply keeps state
        c.close()
        self.assertEqual(p.read(C.FRAME_ENTRY, 9), C.FRAME_ORIGINAL)

    def test_mission_select_rewrites_and_restores(self):
        p = Memory()
        entries = {"TUT1": 0x5000000, "ALL1": 0x5001000, "SOV1": 0x5002000}
        p.patch(C.CAMPAIGN_ITEMS, struct.pack("<I", 0x4000000))
        p.patch(C.CAMPAIGN_COUNT, struct.pack("<I", 3))
        p.patch(0x4000000, struct.pack("<III", *entries.values()))
        for ident, entry in entries.items():
            p.patch(entry + C.CAMPAIGN_ID, ident.encode() + b"\0" * 21)
            p.patch(entry + C.CAMPAIGN_SCENARIO, f"{ident[:3]}01T.MAP".encode() + b"\0" * 24)
        c = C.CampaignController(p, Mock())
        self.assertEqual(c.set_start("SOV1", 9), "SOV09U")
        self.assertEqual(p.read_cstr(entries["SOV1"] + C.CAMPAIGN_SCENARIO, 32), "SOV09U.MAP")
        self.assertEqual(c.start_of("SOV1"), 9)
        with self.assertRaises(ValueError):
            c.set_start("ALL1", 13)
        c.restore_starts()
        self.assertEqual(p.read_cstr(entries["SOV1"] + C.CAMPAIGN_SCENARIO, 32), "SOV01T.MAP")

    def campaigns(self):
        p = Memory()
        entries = {"ALL1": 0x5001000, "SOV1": 0x5002000}
        p.patch(C.CAMPAIGN_ITEMS, struct.pack("<I", 0x4000000))
        p.patch(C.CAMPAIGN_COUNT, struct.pack("<I", 2))
        p.patch(0x4000000, struct.pack("<II", *entries.values()))
        for ident, entry in entries.items():
            p.patch(entry + C.CAMPAIGN_ID, ident.encode() + b"\0" * 21)
            p.patch(entry + C.CAMPAIGN_SCENARIO, f"{ident[:3]}01T.MAP".encode() + b"\0" * 24)
        p.write = lambda a, d: p.patch(a, d)
        c = C.CampaignController(p, Mock())
        c.set_start("ALL1", 3)
        c.set_start("SOV1", 9)
        return p, c, entries

    def test_failed_start_restore_keeps_the_record(self):
        p, c, entries = self.campaigns()
        sov = entries["SOV1"]
        p.write = lambda a, d: False if a == sov + C.CAMPAIGN_SCENARIO else p.patch(a, d)
        with self.assertRaises(OSError):
            c.restore_starts()
        self.assertEqual(p.read_cstr(entries["ALL1"] + C.CAMPAIGN_SCENARIO, 32), "ALL01T.MAP")
        self.assertEqual(list(c.scenario_originals), [sov])  # retry possible
        p.write = lambda a, d: p.patch(a, d)
        c.restore_starts()
        self.assertEqual(p.read_cstr(sov + C.CAMPAIGN_SCENARIO, 32), "SOV01T.MAP")
        self.assertEqual((c.scenario_originals, c.starts), ({}, {}))

    def test_external_start_change_is_not_overwritten(self):
        p, c, entries = self.campaigns()
        all1 = entries["ALL1"]
        p.patch(all1 + C.CAMPAIGN_SCENARIO, b"MYMOD.MAP\0")
        with self.assertRaises(RuntimeError):
            c.restore_starts()
        self.assertEqual(p.read_cstr(all1 + C.CAMPAIGN_SCENARIO, 32), "MYMOD.MAP")
        self.assertEqual(p.read_cstr(entries["SOV1"] + C.CAMPAIGN_SCENARIO, 32), "SOV01T.MAP")

    def test_win_calls_native_on_player(self):
        p = Memory()
        p.patch(C.PLAYER_PTR, struct.pack("<I", 0x6000000))
        p.patch(0x6000000 + 0x13E, b"\0\0\0")
        executor = Mock()
        C.CampaignController(p, executor).win()
        executor.call.assert_called_once_with(C.HOUSE_WIN, this=0x6000000, args=(0,))
        p.patch(0x6000000 + 0x13F, b"\1")
        with self.assertRaises(RuntimeError):
            C.CampaignController(p, executor).win()


try:
    import unicorn  # noqa: F401
    HAVE_UNICORN = True
except ImportError:
    HAVE_UNICORN = False


@needs(HAVE_UNICORN, "unicorn not installed")
class EmulatedStubTests(unittest.TestCase):
    """Run the generated code on a CPU emulator with a fake player and objects."""

    def setUp(self):
        from unicorn import Uc, UC_ARCH_X86, UC_MODE_32, UC_HOOK_CODE, x86_const as X
        self.X, self.HOOK = X, UC_HOOK_CODE
        self.mu = Uc(UC_ARCH_X86, UC_MODE_32)
        for address, size in ((0x400000, 0x800000), (0x15000000, 0x100000),
                              (0x20000000, 0x200000), (0x30000000, 0x100000), (0x7F000000, 0x100000)):
            self.mu.mem_map(address, size)
        self.mu.mem_write(C.PLAYER_PTR, struct.pack("<I", 0x20000000))

    def run_to(self, start, stop, esp, **regs):
        hits = []
        self.mu.hook_add(self.HOOK, lambda uc, a, _s, _d: (hits.append(a), uc.emu_stop()) if a == stop else None)
        self.mu.reg_write(self.X.UC_X86_REG_ESP, esp)
        for name, value in regs.items():
            self.mu.reg_write(getattr(self.X, f"UC_X86_REG_{name.upper()}"), value)
        self.mu.emu_start(start, 0, count=400)
        self.assertEqual(hits, [stop])
        return lambda name: self.mu.reg_read(getattr(self.X, f"UC_X86_REG_{name.upper()}"))

    def test_income_doubles_only_player(self):
        block = bytearray(E.build_block(0x15000000))
        block[0:16] = struct.pack("<fIII", 2.0, 0, 0, 0)
        self.mu.mem_write(0x15000000, bytes(block))
        self.mu.mem_write(0x7F080000, struct.pack("<If", 0, 10.0))
        reg = self.run_to(0x15000100, 0x4E510A, 0x7F080000, ecx=0x20000000, edx=7)
        self.assertEqual(struct.unpack("<f", self.mu.mem_read(0x7F080004, 4))[0], 20.0)
        self.assertEqual((reg("esp"), reg("edx")), (0x7F080000, 7))

    def test_range_scaled_for_player(self):
        block = bytearray(W.build_block(0x15000000))
        block[0:8] = struct.pack("<fi", 1.0, 200)
        self.mu.mem_write(0x15000000, bytes(block))
        self.mu.mem_write(0x30000000 + 0x1B4, struct.pack("<I", 0x20000000))
        self.mu.mem_write(0x7F08000C, struct.pack("<I", 0x30000000))
        self.mu.mem_write(0x30010000 + 0xB4, struct.pack("<I", 1024))
        reg = self.run_to(0x15000200, 0x6C4BDC, 0x7F080000, edi=0x30010000, esi=5)
        self.assertEqual((reg("ebx"), reg("esp"), reg("esi")), (2048, 0x7F080000, 5))

    def test_frozen_timer_keeps_time_left(self):
        self.mu.mem_write(0x15000000, C.build_block(0x15000000))
        self.mu.mem_write(0x15000000, struct.pack("<IIII", 1, 0, 0, 0))
        self.mu.mem_write(C.SCENARIO_PTR, struct.pack("<I", 0x30000000))
        self.mu.mem_write(0x30000000 + 0x7C8, struct.pack("<iii", 100, 0, 900))
        self.mu.mem_write(C.FRAME, struct.pack("<I", 400))
        reg = self.run_to(0x15000100, C.FRAME_ENTRY + 9, 0x7F080000, ecx=0x1234)
        start, _, time_left = struct.unpack("<iii", self.mu.mem_read(0x30000000 + 0x7C8, 12))
        self.assertEqual(time_left - (400 - start), 600)
        self.assertEqual((reg("ecx"), reg("esp")), (0x1234, 0x7F080000 - 0x28))


class PatchCloseTests(unittest.TestCase):
    def test_one_tampered_site_does_not_keep_others_patched(self):
        from trainer.addresses import PATCHES
        from trainer.patches import PatchManager
        p = Memory()
        p.is32 = True
        p.patch_quiescent = lambda va, before, after: p.patch(va, after)
        for spec in PATCHES.values():
            for va, off, _on in spec["sites"]:
                p.patch(va, bytes.fromhex(off))
        m = PatchManager(p)
        m.compatible = lambda: True
        for fid in PATCHES:
            m.enable(fid)
        first_fid = sorted(PATCHES)[0]
        va, _off, on = PATCHES[first_fid]["sites"][0]
        p.patch(va, b"\xeb\x00" + bytes(len(on) // 2 - 2))
        with self.assertRaises(RuntimeError):
            m.close()
        for fid, spec in PATCHES.items():
            for site_va, off, _on in spec["sites"]:
                if site_va != va:
                    self.assertEqual(p.read(site_va, len(off) // 2), bytes.fromhex(off), fid)
        self.assertEqual(set(m.owned), {va})


class ProfileTests(unittest.TestCase):
    def window(self):
        import os
        import tempfile
        from PySide6.QtWidgets import QApplication
        from trainer import app as app_module
        QApplication.instance() or QApplication([])
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = os.path.join(self.tmp.name, "state.json")
        patcher = patch.object(app_module, "STATE_FILE", state)
        patcher.start()
        self.addCleanup(patcher.stop)
        with patch.object(app_module.Process, "find_pids", return_value=[]):
            w = app_module.MainWindow()
        w.t_poll.stop()
        w.hotkey_timer.stop()
        return w, app_module

    def test_builtin_profiles_use_real_switches(self):
        w, app_module = self.window()
        switches = {f["id"] for f in app_module.FEATURES if f["kind"] == "patch" and f["status"] == "ok"}
        for profile in app_module.BUILTIN_PROFILES.values():
            self.assertTrue(set(profile["enabled"]) <= switches)
            self.assertEqual(w._valid_hook_values(profile["values"]), profile["values"])

    def test_apply_save_and_switch_profiles_offline(self):
        w, app_module = self.window()
        w.apply_profile("战役轻松档", app_module.BUILTIN_PROFILES["战役轻松档"])
        self.assertTrue(w.enabled["mission_timer_freeze"])
        self.assertEqual(w.hook_values, {"ore_income": 2.0})
        w.save_profile("我的")
        self.assertIn("我的", w._user_profiles())
        w.apply_profile("全部关闭", app_module.BUILTIN_PROFILES["全部关闭"])
        self.assertFalse(any(w.enabled.values()))
        self.assertEqual(w.hook_values, {})
        w.apply_profile("我的", w._user_profiles()["我的"])
        self.assertTrue(w.enabled["repair_free"])
        self.assertEqual(w.hook_values, {"ore_income": 2.0})
        w.delete_profile("我的")
        self.assertNotIn("我的", w._user_profiles())

    def test_save_state_keeps_profiles(self):
        w, _ = self.window()
        w.save_profile("我的")
        self.assertTrue(w.save_state())
        self.assertIn("我的", w._user_profiles())

    def test_unreadable_state_file_is_never_overwritten(self):
        w, app_module = self.window()
        w.save_profile("我的")
        with open(app_module.STATE_FILE, encoding="utf-8") as f:
            before = f.read()
        with patch.object(w, "_read_state", side_effect=PermissionError("locked")):
            self.assertFalse(w.save_state())
            self.assertFalse(w._save_hotkeys())
            w.save_profile("另一个")
        with open(app_module.STATE_FILE, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)

    def test_corrupt_state_file_is_kept_aside(self):
        import os
        w, app_module = self.window()
        with open(app_module.STATE_FILE, "w", encoding="utf-8") as f:
            f.write("{broken")
        w.save_profile("我的")
        with open(app_module.STATE_FILE + ".broken", encoding="utf-8") as f:
            self.assertEqual(f.read(), "{broken")
        self.assertIn("我的", w._user_profiles())
        self.assertTrue(os.path.exists(app_module.STATE_FILE))

    def test_attached_profile_turns_every_switch_off(self):
        w, app_module = self.window()
        w.proc = Mock()
        w.can_write = lambda: True
        for attr in ("build_unlock", "techno_ai", "water_walk", "economy", "weapons", "power"):
            setattr(w, attr, Mock(enabled=set()))
        w.reapply_all = Mock()
        # infinite_power sorts first: its disable leaves a pending refresh that
        # must not become a job, or every later switch would bail out as busy.
        switches = ("infinite_power", "tech_all", "u_vet3", "water_walk")
        for fid in switches:
            w.enabled[fid] = True
        w.apply_profile("全部关闭", app_module.BUILTIN_PROFILES["全部关闭"])
        self.assertFalse(any(w.enabled[fid] for fid in switches))
        w.power.disable.assert_called_once()
        w.techno_ai.set.assert_called_with("u_vet3", False)
        w.water_walk.disable.assert_called_once()
        w.reapply_all.assert_called_once_with(sidebar_refresh=True)
        self.assertFalse(w.jobs.busy)

    def test_saved_multipliers_are_validated(self):
        w, _ = self.window()
        self.assertEqual(w._valid_hook_values({"rof_mult": 50, "range_mult": 2, "x": 3,
                                               "ore_income": "9"}), {"range_mult": 2.0})


if __name__ == "__main__":
    unittest.main()
