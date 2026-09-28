"""Per-frame player effects from TechnoClass::AI: elite rank and building repair.

UnitClass/InfantryClass/AircraftClass::AI reach TechnoClass::AI through
FootClass::AI (0x4C8FF9); BuildingClass::AI calls it directly (0x43CC4B).
One entry hook therefore sees every live techno once per game frame on the
simulation thread, so nothing needs to poll or enumerate objects.

The block's first bytes are settings the trainer rewrites in place, so a
switch or the repair amount changes without repatching the entry:
    +0 elite on, +4 repair on, +8 repair amount (0 = full), +C period (frames)
Code starts at CODE_OFFSET so an orphaned block can still be matched by code.
"""
import struct

from .addresses import (AIRCRAFT_VT, BUILDING_VT, CLASS_TYPE_OFF, FRAME, INFANTRY_VT,
                        PLAYER_PTR, TECHNO_FIELD, UNIT_VT)
from .executor import relative_jump
from .hooks import HookSites, assemble
from .operations import X86

AI_ENTRY = 0x6C74C0
AI_ORIGINAL = bytes.fromhex("83ec68535556")  # sub esp,68h; push ebx; push ebp; push esi
CODE_OFFSET = 0x100
ELITE = 0x40000000  # 2.0f
REPAIR_PERIOD = 15  # frames between partial repairs (about one second at normal speed)
MAX_STRENGTH = 1000000

FEATURES = ("u_vet3", "auto_repair")


def ai_code(base):
    """TechnoClass::AI trampoline; `base` is the block start (settings)."""
    elite_on, repair_on, amount, period = base, base + 4, base + 8, base + 0xC
    at = base + CODE_OFFSET
    a = X86()
    a.emit("9c 60")  # save EFLAGS and registers; original ECX (this) is [ESP+0x18]
    a.emit("8b 74 24 18")  # ESI = TechnoClass*
    a.imm("a1", PLAYER_PTR)
    a.emit("85 c0")
    a.jump("0f 84", "done")
    a.emit("39 86 b4 01 00 00")  # Owner == player
    a.jump("0f 85", "done")
    a.emit("83 7e 6c 00")  # living
    a.jump("0f 8e", "done")
    a.emit("8b 0e")  # ECX = vtable

    a.emit("83 3d")
    a.code += struct.pack("<I", elite_on) + b"\x00"
    a.jump("0f 84", "repair")
    for vt in (UNIT_VT, INFANTRY_VT):
        a.imm("81 f9", vt)
        a.jump("0f 84", "elite")
    a.imm("81 f9", AIRCRAFT_VT)
    a.jump("0f 85", "repair")
    a.label("elite")
    a.emit("c7 86")
    a.code += struct.pack("<II", TECHNO_FIELD["Veterancy"], ELITE)
    a.jump("e9", "done")  # foot units are never buildings

    a.label("repair")
    a.emit("83 3d")
    a.code += struct.pack("<I", repair_on) + b"\x00"
    a.jump("0f 84", "done")
    a.imm("81 f9", BUILDING_VT)
    a.jump("0f 85", "done")
    a.emit("8b 9e")
    a.code += struct.pack("<I", CLASS_TYPE_OFF[BUILDING_VT])  # EBX = BuildingType*
    a.emit("85 db")
    a.jump("0f 84", "done")
    a.emit("8b 9b a0 00 00 00")  # EBX = Type.Strength
    a.emit("85 db")
    a.jump("0f 8e", "done")
    a.imm("81 fb", MAX_STRENGTH)
    a.jump("0f 87", "done")
    a.emit("8b 46 6c 39 d8")  # EAX = Health; below max?
    a.jump("0f 8d", "done")
    a.imm("8b 0d", amount)  # ECX = amount
    a.emit("85 c9")
    a.jump("0f 84", "full")
    a.imm("8b 3d", period)  # EDI = period
    a.emit("85 ff")
    a.jump("0f 84", "done")
    a.imm("a1", FRAME)
    a.emit("31 d2 f7 f7 85 d2")  # FRAME % period
    a.jump("0f 85", "done")
    a.emit("8b 46 6c")  # EAX = Health
    a.emit("89 da 29 c2")  # EDX = max - health (> 0)
    a.emit("39 ca")
    a.jump("0f 86", "full")  # remaining <= amount
    a.emit("01 c8")
    a.jump("e9", "store")
    a.label("full")
    a.emit("89 d8")
    a.label("store")
    a.emit("89 46 6c 89 46 70")  # Health and EstimatedHealth

    a.label("done")
    a.emit("61 9d")
    a.code += AI_ORIGINAL
    code = a.finish()
    return code + relative_jump(at + len(code), AI_ENTRY + len(AI_ORIGINAL))


def build_block(base):
    return assemble(0x1000, [(CODE_OFFSET, ai_code(base))], "单位AI")


def settings_bytes(elite, repair, amount, period=REPAIR_PERIOD):
    return struct.pack("<IIII", int(bool(elite)), int(bool(repair)), amount, period)


class TechnoAIController:
    """Owns the TechnoClass::AI hook shared by 我军升三星 and 自动修理建筑."""

    def __init__(self, proc, repair_amount=0):
        self.proc = proc
        self.hooks = HookSites(proc, "单位AI", {"ai": (AI_ENTRY, AI_ORIGINAL, CODE_OFFSET)},
                               build_block)
        self.enabled = set()
        self.repair_amount = repair_amount

    @property
    def installed(self):
        return self.hooks.installed

    def _settings(self, enabled):
        return settings_bytes("u_vet3" in enabled, "auto_repair" in enabled,
                              self.repair_amount)

    def _sync(self, enabled):
        """Write settings first, then install or remove the entry to match."""
        if enabled:
            self.hooks.prepare()
            wanted = self._settings(enabled)
            if self.proc.read(self.hooks.base, len(wanted)) != wanted:
                if not self.proc.write(self.hooks.base, wanted):
                    raise OSError("单位AI设置写入失败")
            self.hooks.apply({"ai"})
        else:
            self.hooks.close()
            if self.hooks.base is not None:
                self.proc.write(self.hooks.base, self._settings(()))
        self.enabled = set(enabled)

    def set(self, fid, on):
        if fid not in FEATURES:
            raise ValueError(fid)
        enabled = self.enabled | {fid} if on else self.enabled - {fid}
        if enabled == self.enabled and (not on or self.installed):
            if on:  # cheap periodic check that the settings were not lost
                wanted = self._settings(enabled)
                if self.proc.read(self.hooks.base, len(wanted)) != wanted:
                    self._sync(enabled)
            return
        self._sync(enabled)

    def set_amount(self, amount):
        if not 0 <= amount <= MAX_STRENGTH:
            raise ValueError("修理量超出范围")
        # Commit locally only after the game accepted it: a rejected value must
        # not be pushed later by the periodic settings check.
        if self.hooks.base is not None and self.enabled:
            if not self.proc.write(self.hooks.base + 8, struct.pack("<I", amount)):
                raise OSError("修理量写入失败")
        self.repair_amount = amount

    def close(self):
        self._sync(())
