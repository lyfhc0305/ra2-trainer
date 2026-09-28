"""Player-only rate-of-fire and weapon-range multipliers.

    TechnoClass::GetROF(int weapon) 0x6C9C00 (virtual, ret 4): delay =
        Weapon.ROF(+0xB0) * house ROF + random, veteran/garrison adjusted;
        every normal path ends at 0x6C9DD3 `mov eax,ebp; pop x4; ret 4`.
    TechnoClass in-range test 0x6C4BB0: reads Weapon.Range(+0xB4) at
        0x6C4BD6 (`mov ebx,[edi+0xB4]`), THIS saved at [ESP+0xC].
    TechnoClass::GetWeaponRange(int weapon) 0x6CD810 (virtual slot 0x154,
        ret 4): target searches use it.

Settings: +0 delay scale (float, 1/multiplier), +4 range percent (int).
"""
import math
import struct

from .addresses import PLAYER_PTR
from .executor import relative_jump
from .hooks import HookSites, assemble
from .operations import X86

ROF_SCALE, RANGE_PCT = 0, 4
OWNER = 0x1B4
SPECIAL_RANGE = 0xFFFFFE00  # -2 cells: "unlimited" in the native test
SITES = {
    "rof": (0x6C9DD3, bytes.fromhex("8bc55d5b5f5ec20400"), 0x100),
    "in_range": (0x6C4BD6, bytes.fromhex("8b9fb4000000"), 0x200),
    "weapon_range": (0x6CD810, bytes.fromhex("8b5424048b01"), 0x300),
}
FEATURE_SITES = {"rof": ("rof",), "range": ("in_range", "weapon_range")}


def _is_player(a, owner_modrm, skip):
    a.imm("8b 15", PLAYER_PTR)
    a.emit("85 d2")
    a.jump("0f 84", skip)
    a.emit(owner_modrm)
    a.code += struct.pack("<I", OWNER)
    a.jump("0f 85", skip)


def _scale_eax(a, base):
    """EAX = EAX * percent / 100 (EDX, ECX clobbered)."""
    a.emit("f7 2d")
    a.code += struct.pack("<I", base + RANGE_PCT)
    a.emit("b9 64 00 00 00 f7 f9")


def rof_code(base, at):
    """Function tail: ESI = techno, EBP = reload delay."""
    a = X86()
    a.emit("8b c5")
    _is_player(a, "39 96", "out")
    a.emit("50 db 04 24 d8 0d")
    a.code += struct.pack("<I", base + ROF_SCALE)
    a.emit("db 1c 24 58")  # delay *= scale (rounded)
    a.emit("83 f8 01")
    a.jump("0f 8d", "out")
    a.emit("b8 01 00 00 00")
    a.label("out")
    a.emit("5d 5b 5f 5e c2 04 00")
    return a.finish()


def in_range_code(base, at):
    # Mid-function, yet EAX/ECX/EDX and flags need no saving: from 0x6C4BDC on
    # every native path writes them before reading (tools/probe_hook_liveness.py).
    address, original, _ = SITES["in_range"]
    back = address + len(original)
    a = X86()
    a.code += original  # EBX = weapon range
    a.imm("81 fb", SPECIAL_RANGE)
    a.jump("0f 84", "back")
    a.emit("85 db")
    a.jump("0f 8e", "back")
    a.emit("8b 44 24 0c 85 c0")  # THIS
    a.jump("0f 84", "back")
    _is_player(a, "39 90", "back")
    a.emit("89 d8")
    _scale_eax(a, base)
    a.emit("89 c3")
    a.label("back")
    code = a.finish()
    return code + relative_jump(at + len(code), back)


def weapon_range_code(base, at):
    """Whole replacement of GetWeaponRange(int) with the same result, scaled for the player."""
    a = X86()
    a.emit("56 89 ce ff 74 24 08")  # keep THIS in ESI; push weapon index
    a.emit("8b 06 89 f1 ff 90 9c 03 00 00")  # GetWeapon(index) -> WeaponStruct*
    a.emit("8b 00 85 c0")
    a.jump("0f 84", "zero")
    a.emit("8b 80 b4 00 00 00 85 c0")
    a.jump("0f 8e", "out")
    _is_player(a, "39 96", "out")
    _scale_eax(a, base)
    a.label("out")
    a.emit("5e c2 04 00")
    a.label("zero")
    a.emit("31 c0 5e c2 04 00")
    return a.finish()


def build_block(base):
    builders = {"rof": rof_code, "in_range": in_range_code, "weapon_range": weapon_range_code}
    return assemble(0x1000, [(offset, builders[name](base, base + offset))
                             for name, (_a, _o, offset) in SITES.items()], "武器")


class WeaponController:
    def __init__(self, proc):
        self.proc = proc
        self.hooks = HookSites(proc, "武器", SITES, build_block)
        self.rof = 1.0
        self.range = 1.0

    @property
    def installed(self):
        return self.hooks.installed

    def _settings(self):
        return struct.pack("<fi", 1.0 / self.rof, round(self.range * 100))

    def _sync(self):
        active = {"rof"} if self.rof != 1.0 else set()
        if self.range != 1.0:
            active.add("range")
        wanted = {site for key in active for site in FEATURE_SITES[key]}
        if wanted:
            self.hooks.prepare()
            settings = self._settings()
            if self.proc.read(self.hooks.base, len(settings)) != settings:
                if not self.proc.write(self.hooks.base, settings):
                    raise OSError("武器设置写入失败")
        self.hooks.apply(wanted) if wanted else self.hooks.close()

    def _set(self, attr, value, lo, hi, label):
        value = float(value)
        if not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"{label}须在{lo}到{hi}之间")
        previous = getattr(self, attr)
        setattr(self, attr, value)
        try:
            self._sync()
        except Exception:
            setattr(self, attr, previous)
            raise

    def set_rof(self, multiplier):
        self._set("rof", multiplier, 1, 10, "射速倍率")

    def set_range(self, multiplier):
        self._set("range", multiplier, 0.5, 5, "射程倍率")

    def close(self):
        previous = self.rof, self.range
        self.rof = self.range = 1.0
        try:
            self._sync()
        except Exception:
            self.rof, self.range = previous  # hooks still live: keep reporting them
            raise
