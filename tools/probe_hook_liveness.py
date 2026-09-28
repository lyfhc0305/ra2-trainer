"""Offline check (local game.exe only) that two hooks leave nothing live behind.

1. Range multiplier, weapons.py "in_range" at 0x6C4BD6: the stub clobbers EAX,
   ECX, EDX and flags without saving them. From the resume point 0x6C4BDC on
   every path, each of them must be written before it is read.
2. build_unlock.py "prerequisites" bypass: it jumps from 0x4E38FC to 0x4E3C0C
   and skips the native `mov [esp+0x54],ebp`. No instruction reachable from
   0x4E3C0C (including the type-kind jump table) may access that stack slot.

    python tools/probe_hook_liveness.py [path\\to\\game.exe]
"""
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from capstone import CS_AC_READ, CS_AC_WRITE, CS_ARCH_X86, CS_MODE_32, Cs  # noqa: E402
from capstone.x86 import X86_OP_IMM, X86_OP_MEM, X86_REG_ESP  # noqa: E402
from pe import PE  # noqa: E402

FAMILY = {"eax": {"eax", "ax", "al", "ah"}, "ecx": {"ecx", "cx", "cl", "ch"},
          "edx": {"edx", "dx", "dl", "dh"}, "flags": {"eflags"}}


class Code:
    def __init__(self, pe):
        self.pe = pe
        self.md = Cs(CS_ARCH_X86, CS_MODE_32)
        self.md.detail = True

    def at(self, address):
        return next(self.md.disasm(self.pe.read(address, 16), address))


def family(name):
    return next((f for f, names in FAMILY.items() if name in names), None)


def live_registers(code, start):
    """{register: addresses where it is read before any write, over every path}."""
    found = {f: set() for f in FAMILY}
    seen, stack = set(), [(start, frozenset())]
    while stack:
        address, defined = stack.pop()
        if (address, defined) in seen:
            continue
        seen.add((address, defined))
        i = code.at(address)
        read, written = i.regs_access()
        reads = {family(i.reg_name(r)) for r in read} - {None}
        writes = {family(i.reg_name(r)) for r in written} - {None}
        ops = i.op_str.split(", ")
        if i.mnemonic in ("xor", "sub") and len(ops) == 2 and ops[0] == ops[1]:
            reads -= writes  # xor r,r only defines r
        if i.mnemonic == "call":
            reads = {"ecx"} - defined  # thiscall: ECX is an argument
            writes = set(FAMILY)  # caller-saved
        if i.mnemonic.startswith("ret"):
            reads = {"eax"}
        for f in reads - defined:
            found[f].add(address)
        defined |= writes
        if i.mnemonic.startswith("ret"):
            continue
        if i.mnemonic.startswith("j"):
            op = i.operands[0]
            if op.type != X86_OP_IMM:
                raise RuntimeError(f"unresolved indirect jump at {address:08X}")
            stack.append((op.imm, defined))
            if i.mnemonic == "jmp":
                continue
        stack.append((address + i.size, defined))
    return found


def stack_slot_accesses(code, start, function, jump_tables):
    """[(address, slot, access)] for ESP-relative operands, slots relative to ESP at start.

    Returns also the set of reachable addresses and the depths seen at `ret`."""
    out, seen, rets = [], set(), set()
    stack = [(start, 0, 0)]
    while stack:
        address, depth, pushed = stack.pop()
        if (address, depth) in seen or not function[0] <= address < function[1]:
            continue
        seen.add((address, depth))
        i = code.at(address)
        m = i.mnemonic
        for op in i.operands:
            if op.type == X86_OP_MEM and op.mem.base == X86_REG_ESP:
                access = "addr" if m == "lea" else (("W" if op.access & CS_AC_WRITE else "")
                                                   + ("R" if op.access & CS_AC_READ else ""))
                out.append((address, op.mem.disp + depth, access))
        after = address + i.size
        if m == "push":
            depth, pushed = depth - 4, pushed + 4
        elif m == "pop":
            depth += 4
        elif m in ("add", "sub") and i.op_str.startswith("esp,"):
            depth += i.operands[1].imm if m == "add" else -i.operands[1].imm
            pushed = 0
        elif m == "call":
            nxt = code.at(after)
            if not (nxt.mnemonic == "add" and nxt.op_str.startswith("esp,")):
                depth += pushed  # thiscall/stdcall callee popped its arguments
            pushed = 0
        if m.startswith("ret"):
            rets.add(depth)
            continue
        if m.startswith("j"):
            op = i.operands[0]
            if op.type == X86_OP_IMM:
                stack.append((op.imm, depth, pushed))
            elif address in jump_tables:
                stack.extend((t, depth, pushed) for t in jump_tables[address])
            else:
                raise RuntimeError(f"unresolved indirect jump at {address:08X}")
            if m == "jmp":
                continue
        stack.append((after, depth, pushed))
    return out, {a for a, _ in seen}, rets


def main():
    game = Path(sys.argv[1] if len(sys.argv) > 1 else
                os.environ.get("RA2_GAME_EXE") or ROOT.parent / "游戏本体" / "game.exe")
    pe = PE(str(game))
    code = Code(pe)
    ok = True

    live = {r: a for r, a in live_registers(code, 0x6C4BDC).items() if a}
    print("in_range 0x6C4BDC: registers live after the hook:", live or "none")
    ok &= not live

    index = pe.read(0x4E405C, 0x26)
    targets = struct.unpack(f"<{max(index) + 1}I", pe.read(0x4E4048, 4 * (max(index) + 1)))
    accesses, reachable, rets = stack_slot_accesses(
        code, 0x4E3C0C, (0x4E3660, 0x4E4048), {0x4E3C75: sorted(set(targets))})
    slot = [(hex(a), access) for a, s, access in accesses if s == 0x54]
    print("prerequisites bypass 0x4E3C0C: accesses to [esp+0x54]:", slot or "none",
          "| stack depth at ret:", sorted(hex(d) for d in rets))
    ok &= not slot and rets == {0x4C}  # 4 pushes + 0x3C locals: tracking stayed in step

    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
