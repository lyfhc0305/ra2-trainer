"""Failed code writes: rollback is verified and whatever stays behind is reported and owned."""
import unittest
from unittest.mock import patch

from test_core import HookMemory
from trainer.hooks import HookSites, entry_jump
from trainer.mem import PatchResidue, Process
from trainer.patches import PatchManager

A, B = 0x401000, 0x402000
ORIGINAL = bytes.fromhex("8b442404c3")


class ScriptedMemory(HookMemory):
    """patch() follows a per-address script, then succeeds.

    "stick": bytes land but the call reports failure (e.g. cache flush failed)
    "partial": only the first two bytes land   "fail": nothing lands
    patch_quiescent is the real Process implementation.
    """

    def __init__(self):
        super().__init__()
        self.script = {}

    def patch(self, address, data):
        steps = self.script.get(address)
        step = steps.pop(0) if steps else "ok"
        if step in ("ok", "stick"):
            self.data.update({address + i: v for i, v in enumerate(data)})
        elif step == "partial":
            self.data.update({address + i: v for i, v in enumerate(data[:2])})
        return step == "ok"

    patch_quiescent = Process.patch_quiescent

    def suspend_all(self):
        return [1]

    def resume_all(self, _held):
        pass

    def _eips(self, _held):
        return []


class ProcessPatchTests(unittest.TestCase):
    def process(self):
        p = Process.__new__(Process)
        p.h = 1
        return p

    def test_unrestored_protection_or_unflushed_cache_is_a_failure(self):
        for restore, flush in ((False, True), (True, False)):
            p = self.process()
            with patch.object(p, "virtual_protect", side_effect=[(True, 0x20), (restore, 0x40)]), \
                 patch.object(p, "write", return_value=True), \
                 patch("trainer.mem.kernel32.FlushInstructionCache", return_value=flush):
                self.assertFalse(p.patch(A, ORIGINAL), (restore, flush))

    def test_protection_is_restored_when_write_raises(self):
        p = self.process()
        with patch.object(p, "virtual_protect", return_value=(True, 0x20)) as protect, \
             patch.object(p, "write", side_effect=ValueError("bad data")):
            with self.assertRaises(ValueError):
                p.patch(A, ORIGINAL)
        self.assertEqual(protect.call_args_list[-1].args, (A, len(ORIGINAL), 0x20))

    def test_full_success(self):
        p = self.process()
        with patch.object(p, "virtual_protect", return_value=(True, 0x20)), \
             patch.object(p, "write", return_value=True), \
             patch("trainer.mem.kernel32.FlushInstructionCache", return_value=True):
            self.assertTrue(p.patch(A, ORIGINAL))


class QuiescentRollbackTests(unittest.TestCase):
    def setUp(self):
        self.p = ScriptedMemory()
        self.p.patch(A, ORIGINAL)
        self.new = bytes.fromhex("e9000000009090")[:len(ORIGINAL)]

    def test_verified_rollback_is_a_plain_failure(self):
        self.p.script[A] = ["partial"]
        with self.assertRaises(OSError) as caught:
            self.p.patch_quiescent(A, ORIGINAL, self.new)
        self.assertNotIsInstance(caught.exception, PatchResidue)
        self.assertEqual(self.p.read(A, 5), ORIGINAL)

    def test_failed_rollback_reports_the_bytes_left_behind(self):
        self.p.script[A] = ["partial", "fail"]
        with self.assertRaises(PatchResidue) as caught:
            self.p.patch_quiescent(A, ORIGINAL, self.new)
        self.assertEqual(caught.exception.current, self.new[:2] + ORIGINAL[2:])
        self.assertEqual(self.p.read(A, 5), caught.exception.current)


class HookSitesResidueTests(unittest.TestCase):
    def setUp(self):
        self.p = ScriptedMemory()
        self.p.patch(A, ORIGINAL)
        self.p.patch(B, ORIGINAL)
        self.hooks = HookSites(self.p, "测试", {"a": (A, ORIGINAL, 0x10), "b": (B, ORIGINAL, 0x20)},
                               lambda base: bytes(0x40))
        self.base = HookMemory.alloc(self.p, 0)
        self.jump_b = entry_jump(B, self.base + 0x20, len(ORIGINAL))

    def test_site_left_installed_is_owned_and_rolled_back(self):
        self.p.script[B] = ["stick", "fail"]  # written, reported failed, first rollback failed
        with self.assertRaises(PatchResidue):
            self.hooks.apply({"a", "b"})
        self.assertEqual(self.hooks.installed, {})
        self.assertEqual((self.p.read(A, 5), self.p.read(B, 5)), (ORIGINAL, ORIGINAL))

    def test_unremovable_site_stays_owned_and_is_reported(self):
        self.p.script[B] = ["stick", "fail", "fail", "fail"]
        with self.assertRaises(RuntimeError) as caught:
            self.hooks.apply({"a", "b"})
        self.assertIn("仍残留入口：b", str(caught.exception))
        self.assertEqual(self.hooks.installed, {"b": self.jump_b})
        self.assertEqual(self.p.read(A, 5), ORIGINAL)
        self.hooks.close()  # the site is still owned, so a later close removes it
        self.assertEqual((self.hooks.installed, self.p.read(B, 5)), ({}, ORIGINAL))

    def test_mixed_bytes_are_reported_not_owned(self):
        self.p.script[B] = ["partial", "fail"]
        with self.assertRaises(PatchResidue) as caught:
            self.hooks.apply({"a", "b"})
        self.assertIn("未能恢复", str(caught.exception))
        self.assertEqual(self.hooks.installed, {})
        self.assertEqual(self.p.read(A, 5), ORIGINAL)


class PatchManagerResidueTests(unittest.TestCase):
    def test_unrestorable_site_stays_owned_and_is_reported(self):
        p = ScriptedMemory()
        sites = [(A, ORIGINAL.hex(), "9090909090"), (B, ORIGINAL.hex(), "9090909090")]
        for va, off, _on in sites:
            p.patch(va, bytes.fromhex(off))
        p.script[B] = ["stick", "fail", "fail", "fail"]
        with patch.dict("trainer.patches.PATCHES", {"t": {"sites": sites}}, clear=True):
            manager = PatchManager(p)
            with self.assertRaises(RuntimeError) as caught:
                manager.enable("t")
            self.assertIn(f"0x{B:X}", str(caught.exception))
            self.assertEqual(set(manager.owned), {B})
            self.assertEqual(p.read(A, 5), ORIGINAL)
            manager.close()
            self.assertEqual((manager.owned, p.read(B, 5)), ({}, ORIGINAL))


if __name__ == "__main__":
    unittest.main()
