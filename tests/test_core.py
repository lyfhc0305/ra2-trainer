import ctypes
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from trainer.mem import Process, build_call_stub
from trainer.patches import PatchManager
from trainer.addresses import PATCHES
from trainer import units
from trainer.executor import build_trampoline, HOOK, ORIGINAL
from trainer import power
from pe import PE
from capstone import Cs, CS_ARCH_X86, CS_MODE_32


def disasm_checked(test, code, base):
    """Disassemble code; it must decode fully and in-block jumps must hit instruction starts."""
    ins = list(Cs(CS_ARCH_X86, CS_MODE_32).disasm(code, base))
    test.assertEqual(sum(i.size for i in ins), len(code))
    starts = {i.address for i in ins}
    for i in ins:
        if i.mnemonic.startswith("j"):
            target = int(i.op_str, 16)
            if base <= target < base + len(code):
                test.assertIn(target, starts)
    return ins


class Memory:
    is32 = True

    def __init__(self):
        self.data = {}
        self.fail_at = None

    def read(self, address, length):
        if any(address + i not in self.data for i in range(length)):
            return None
        return bytes(self.data[address + i] for i in range(length))

    def patch(self, address, data):
        if address == self.fail_at:
            self.fail_at = None
            return False
        self.data.update({address + i: v for i, v in enumerate(data)})
        return True

    def patch_quiescent(self, address, expected, replacement):
        # Mirrors Process.patch_quiescent: verify, write, restore on failure.
        if self.read(address, len(expected)) != expected:
            raise RuntimeError("changed bytes")
        if not self.patch(address, replacement):
            self.patch(address, expected)
            raise OSError("write failure")

    def code_idle(self, _address, _size):
        return True

    def read_u32(self, a):
        b = self.read(a, 4)
        return struct.unpack("<I", b)[0] if b else None

    read_i32 = read_u32


class HookMemory(Memory):
    def alloc(self, _size):
        return 0x15000000

    def free(self, _address):
        pass

    def patch_quiescent(self, address, expected, replacement):
        if self.read(address, len(expected)) != expected:
            raise RuntimeError("changed bytes")
        if not self.patch(address, replacement):
            raise OSError("write failure")

    def write_u32(self, address, value):
        return self.patch(address, struct.pack("<I", value))

    write_i32 = write_u32


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.p = Memory()
        for spec in PATCHES.values():
            for va, off, _on in spec["sites"]:
                self.p.patch(va, bytes.fromhex(off))
        self.manager = PatchManager(self.p)

    def test_wrong_version_refuses_write(self):
        self.p.patch(0x4E53A9, b"\xcc\xcc")
        before = dict(self.p.data)
        with self.assertRaises(RuntimeError):
            self.manager.enable("reveal_map")
        self.assertEqual(before, self.p.data)

    def test_multisite_failure_rolls_back(self):
        before = dict(self.p.data)
        self.p.fail_at = 0x49BC2A
        with self.assertRaises(OSError):
            self.manager.enable("build_anywhere")
        self.assertEqual(before, self.p.data)

    def test_does_not_restore_another_trainers_patch(self):
        va, _off, on = PATCHES["reveal_map"]["sites"][0]
        self.p.patch(va, bytes.fromhex(on))
        self.manager.enable("reveal_map")
        self.manager.close()
        self.assertEqual(self.p.read(va, 2), bytes.fromhex(on))

    def test_own_patch_restored(self):
        before = dict(self.p.data)
        self.manager.enable("build_anywhere")
        self.manager.close()
        self.assertEqual(before, self.p.data)

    def test_adopt_releases_bytes_left_by_abnormal_exit(self):
        before = dict(self.p.data)
        for va, _off, on in PATCHES["build_anywhere"]["sites"]:
            self.p.patch(va, bytes.fromhex(on))
        self.manager.enable("build_anywhere", adopt=True)
        self.manager.disable("build_anywhere")
        self.assertEqual(before, self.p.data)


class ObjectTests(unittest.TestCase):
    def test_destroyed_or_transferred_object_not_returned_from_cache(self):
        p = Memory()
        p.patch(units.PLAYER_PTR, struct.pack("<I", 0x1000))
        p.patch(0x2000, struct.pack("<I", 0x7ADDF8))
        p.patch(0x2000 + 0x1B4, struct.pack("<I", 0x1000))
        p.patch(0x2000 + 0x6C, struct.pack("<I", 100))
        with patch.object(units, "player_technos", return_value=[(0x2000, 0x7ADDF8)]):
            self.assertEqual(len(units.player_technos_cached(p)), 1)
            p.patch(0x2000 + 0x1B4, struct.pack("<I", 0x3000))
            self.assertEqual(units.player_technos_cached(p), [])
            p.patch(0x2000 + 0x1B4, struct.pack("<I", 0x1000))
            p.patch(0x2000 + 0x6C, bytes(4))
            self.assertEqual(units.player_technos_cached(p), [])

    def test_cache_is_per_connection(self):
        p, q = Memory(), Memory()
        for proc in (p, q):
            proc.patch(units.PLAYER_PTR, struct.pack("<I", 0x1000))
        with patch.object(units, "player_technos", return_value=[]) as scan:
            units.player_technos_cached(p)
            units.player_technos_cached(q)
            self.assertEqual(scan.call_count, 2)


    def test_local_executable_appends_technos_to_array(self):
        # TechnoClass ctor: Items[Count++] = this, on the vector read by units.
        pe = PE(str(ROOT.parent / "游戏本体" / "game.exe"))
        code = pe.read(0x6C121C, 0x18)
        count = struct.pack("<I", units.TECHNO_COUNT).hex()
        items = struct.pack("<I", units.TECHNO_ARRAY).hex()
        self.assertEqual(code.hex(), f"8b0d{count}8bc141890d{count}8b0d{items}893481")

    def test_technos_come_from_game_array_not_heap(self):
        p = Memory()
        house = 0x1000
        p.patch(units.PLAYER_PTR, struct.pack("<I", house))
        p.patch(units.TECHNO_COUNT, struct.pack("<I", 4))
        p.patch(units.TECHNO_ARRAY, struct.pack("<I", 0x8000))
        objects = [(0x2000, 0x7ADDF8, house), (0x3000, 0x79CBBC, house),
                   (0x4000, 0x7ADDF8, 0x9000), (0x5000, 0x12345678, house)]
        p.patch(0x8000, struct.pack("<4I", *(a for a, _, _ in objects)))
        for a, vt, owner in objects:
            p.patch(a, struct.pack("<I", vt) + bytes(0x1B0) + struct.pack("<I", owner))
        with patch.object(units.O, "iter_objects", side_effect=AssertionError):
            self.assertEqual(units.player_technos(p),
                             [(0x2000, 0x7ADDF8), (0x3000, 0x79CBBC)])
            self.assertEqual(units.player_technos(p, kinds={0x79CBBC}), [(0x3000, 0x79CBBC)])
            self.assertEqual(len(list(units.iter_technos(p))), 3)

    def test_bad_techno_array_count_is_reported(self):
        p = Memory()
        p.patch(units.TECHNO_COUNT, struct.pack("<I", 0x7FFFFFFF))
        with self.assertRaises(RuntimeError):
            units.techno_pointers(p)
        p.patch(units.TECHNO_COUNT, bytes(4))
        self.assertEqual(units.techno_pointers(p), [])


RET = bytes([0xC3])


class CommandProc:
    """Executor mailbox with scripted state reads; everything else succeeds.

    run_block and call each read the idle state once before submitting."""
    BASE, BLOCK = 0x1F000000, 0x10000000

    def __init__(self, states, submit=True, alive=True, result=7):
        self.states, self.submit, self._alive, self.result = list(states), submit, alive, result
        self.freed = []

    def alloc(self, _size):
        return self.BLOCK

    def free(self, address):
        self.freed.append(address)

    def patch(self, _address, _data):
        return True

    def read(self, address, size):
        return bytes(size)

    def write_u32(self, address, value):
        return self.submit if address == self.BASE else True

    def read_u32(self, address):
        if address == self.BASE:
            return self.states.pop(0) if self.states else 3
        if address == self.BASE + 4:
            return self.result
        return 0

    def alive(self):
        return self._alive


class CommandLifecycleTests(unittest.TestCase):
    """A submitted command whose outcome is unknown keeps its code block."""

    def run_block(self, proc):
        from trainer.operations import GameOperations
        from trainer.executor import MainThreadExecutor
        ops = GameOperations.__new__(GameOperations)
        ops.proc = proc
        ops.executor = MainThreadExecutor(proc)
        ops.executor.base, ops.executor.installed = proc.BASE, True
        return ops.run_block(0x100, lambda base: RET, 0, 4, timeout=1.0)

    def test_unreadable_state_while_game_alive_keeps_block(self):
        from trainer.executor import CommandPending
        for alive in (True, None):  # alive, or process state unknown
            proc = CommandProc([0, 0, None], alive=alive)
            with self.assertRaises(CommandPending):
                self.run_block(proc)
            self.assertEqual(proc.freed, [], alive)

    def test_failed_submit_write_keeps_block(self):
        from trainer.executor import CommandPending
        proc = CommandProc([0, 0], submit=False)
        with self.assertRaises(CommandPending):
            self.run_block(proc)
        self.assertEqual(proc.freed, [])

    def test_confirmed_exit_or_completion_releases_block(self):
        from trainer.executor import CommandPending, ProcessExited
        proc = CommandProc([0, 0, None], alive=False)
        with self.assertRaises(ProcessExited):
            self.run_block(proc)
        self.assertEqual(proc.freed, [proc.BLOCK])
        proc = CommandProc([0, 0, 3], result=None)  # finished, return value unreadable
        with self.assertRaises(OSError) as caught:
            self.run_block(proc)
        self.assertNotIsInstance(caught.exception, CommandPending)
        self.assertEqual(proc.freed, [proc.BLOCK])
        proc = CommandProc([0, 0, 1, 3])
        self.assertEqual(self.run_block(proc), bytes(4))
        self.assertEqual(proc.freed, [proc.BLOCK])

    def test_timeout_is_still_a_timeout(self):
        from trainer.executor import CommandPending
        proc = CommandProc([0, 0] + [1] * 100000)
        ops_timeout = 0.05
        from trainer.operations import GameOperations
        from trainer.executor import MainThreadExecutor
        ops = GameOperations.__new__(GameOperations)
        ops.proc, ops.executor = proc, MainThreadExecutor(proc)
        ops.executor.base, ops.executor.installed = proc.BASE, True
        with self.assertRaises(TimeoutError) as caught:
            ops.run_block(0x100, lambda base: RET, 0, 4, timeout=ops_timeout)
        self.assertIsInstance(caught.exception, CommandPending)
        self.assertEqual(proc.freed, [])


class RemoteCallTests(unittest.TestCase):
    def test_timeout_does_not_free_running_code(self):
        p = Process.__new__(Process)
        p.is32, p.h, p._pending_calls = True, 1, []
        with patch.object(p, "alloc", return_value=0x10000), \
             patch.object(p, "write", return_value=True), \
             patch.object(p, "free") as free, \
             patch("trainer.mem.kernel32.FlushInstructionCache", return_value=True), \
             patch("trainer.mem.kernel32.CreateRemoteThread", return_value=2), \
             patch("trainer.mem.kernel32.WaitForSingleObject", return_value=0x102):
            with self.assertRaises(TimeoutError):
                p.call(0x400000, timeout=0)
            free.assert_not_called()
            self.assertEqual(p._pending_calls, [(2, 0x10000)])
        with patch("trainer.mem.kernel32.WaitForSingleObject", return_value=0), \
             patch("trainer.mem.kernel32.CloseHandle"), patch.object(p, "free") as free:
            p.reap_calls()
            free.assert_called_once_with(0x10000)
            self.assertEqual(p._pending_calls, [])

    def test_stub_thread_abi_and_size(self):
        code = build_call_stub(0x400000, 0x1234, tuple(range(32)))
        instructions = list(Cs(CS_ARCH_X86, CS_MODE_32).disasm(code, 0))
        self.assertEqual(sum(i.size for i in instructions), len(code))
        self.assertEqual((instructions[-1].mnemonic, instructions[-1].op_str), ("ret", "4"))
        self.assertLess(len(code), 0x200)
        with self.assertRaises(ValueError):
            build_call_stub(0x400000, args=tuple(range(33)))


class HookTests(unittest.TestCase):
    def test_local_executable_matches_hook_signatures(self):
        p = PE(str(ROOT.parent / "游戏本体" / "game.exe"))
        self.assertEqual(p.read(HOOK, len(ORIGINAL)), ORIGINAL)
        self.assertEqual(p.read(power.HOOK, len(power.ORIGINAL)), power.ORIGINAL)

    def test_power_relocated_call_and_return(self):
        base = 0x15000000
        code = power.build_power_hook(base)
        ins = list(Cs(CS_ARCH_X86, CS_MODE_32).disasm(code, base))
        self.assertEqual(sum(i.size for i in ins), len(code))
        calls = [int(i.op_str, 16) for i in ins if i.mnemonic == "call"]
        self.assertEqual(calls, [0x44FA40])
        self.assertEqual(int(ins[-1].op_str, 16), power.HOOK + len(power.ORIGINAL))
        conditional = next(i for i in ins if i.mnemonic == "jne")
        target = next(i for i in ins if i.address == int(conditional.op_str, 16))
        self.assertEqual((target.mnemonic, target.op_str), ("pop", "eax"))

    def test_frame_trampoline_restores_state_and_returns(self):
        base = 0x15000000
        code = build_trampoline(base)
        ins = list(Cs(CS_ARCH_X86, CS_MODE_32).disasm(code, base + 0x300))
        self.assertEqual(sum(i.size for i in ins), len(code))
        self.assertTrue(any(i.mnemonic == "fxsave" for i in ins))
        self.assertTrue(any(i.mnemonic == "fxrstor" for i in ins))
        self.assertEqual(int(ins[-1].op_str, 16), HOOK + len(ORIGINAL))


if __name__ == "__main__":
    unittest.main()
