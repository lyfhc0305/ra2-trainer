"""Player-scoped technology and build-limit hooks for stock RA2 1.006."""
from .addresses import PLAYER_PTR
from .executor import relative_jump
from .hooks import HookSites, assemble
from .operations import X86


# Each entry is a complete instruction sequence from the local game.exe.
# The prerequisite site is shared: either technology or build-limit mode may
# need it, while quantity-limit sites remain exclusive to unlock_build.
SITES = {
    "tech_level": ("tech_all", 0x4E372A, "8bb71c010000896c2414", 0x4E3734, 0x4E3832),
    "factory_owner": ("tech_all", 0x5D6078, "8b4c241085c1", 0x5D607E, 0x5D6084),
    # The bypass skips `mov [esp+0x54],ebp`: nothing reachable from 0x4E3C0C
    # touches that slot (tools/probe_hook_liveness.py), unlike tech_level's.
    "prerequisites": (("tech_all", "unlock_build"), 0x4E38FC, "8b442424896c2454", 0x4E3904, 0x4E3C0C),
    "canbuild_limit": ("unlock_build", 0x4E3C5A, "8b038bcbff502c", 0x4E3C61, 0x4E38ED),
    "reached_limit": ("unlock_build", 0x4F4F30, "83ec085355", 0x4F4F35, None),
}


def site_code(name, base):
    _fid, _site, original_hex, resume, bypass = SITES[name]
    a = X86()
    # Preserve both the caller's EAX and flags. At tech_level the original
    # continuation still consumes the flags set before this hook.
    a.emit("9c 50")
    a.imm("a1", PLAYER_PTR)
    a.emit("85 c0")
    a.jump("0f 84", "original")
    if name == "tech_level":
        a.emit("39 c7")  # cmp edi, player
    elif name == "reached_limit":
        a.emit("39 c8")  # cmp eax, ecx (house)
    else:
        offset = 0x30 if name == "factory_owner" else 0x18
        a.emit(f"3b 44 24 {offset:02x}")  # original stack offset + saved eax/flags
    a.jump("0f 85", "original")
    if name == "tech_level":
        # The game's default TechLevel=255 marks dormant/civilian types;
        # TechLevel=-1 (0xFFFFFFFF) was already rejected before this site.
        a.emit("81 bb 5c 05 00 00 ff 00 00 00")
        a.jump("0f 84", "reject")
        # Stock Allied Airforce Command buildings are mutually exclusive by
        # country. Their matching owners are bypassed by tech_all, so retain
        # this one native identity restriction without reducing other tech.
        a.imm("81 7b 24", int.from_bytes(b"GAAI", "little"))
        a.jump("0f 85", "check_amradr")
        a.emit("66 81 7b 28 52 43")  # "RC"
        a.jump("0f 85", "check_amradr")
        a.emit("80 7b 2a 00")
        a.jump("0f 85", "check_amradr")
        a.emit("8b 47 34 85 c0")  # House.Type can be absent during teardown
        a.jump("0f 84", "reject")
        a.emit("83 b8 b8 00 00 00 00")  # American CountryIndex == 0
        a.jump("0f 84", "reject")  # GAAIRC forbidden for Americans
        a.jump("e9", "boost")
        a.label("check_amradr")
        a.imm("81 7b 24", int.from_bytes(b"AMRA", "little"))
        a.jump("0f 85", "boost")
        a.emit("66 81 7b 28 44 52")  # "DR"
        a.jump("0f 85", "boost")
        a.emit("80 7b 2a 00")
        a.jump("0f 85", "boost")
        a.emit("8b 47 34 85 c0")
        a.jump("0f 84", "reject")
        a.emit("83 b8 b8 00 00 00 00")
        a.jump("0f 85", "reject")  # AMRADR only for Americans
    if name == "reached_limit":
        a.emit("83 7c 24 0c 00")  # Type* is original [esp+4]
        a.jump("0f 84", "original")
        # Airport-bound AircraftTypes still obey real House.PadAircraft
        # capacity, even while ordinary build quantity limits are unlocked.
        a.emit("51 52 8b 44 24 14")  # save ECX/EDX, load Type*
        a.emit("8b 10 89 c1 ff 52 2c")  # Type.GetAbstractType()
        a.emit("83 f8 03")  # AircraftType kind
        a.jump("0f 85", "not_aircraft")
        a.emit("8b 44 24 14 80 b8 a3 0b 00 00 00")
        a.jump("0f 84", "not_aircraft")
        a.emit("5a 59")
        a.jump("e9", "original")
        a.label("not_aircraft")
        a.emit("5a 59")
    if name == "tech_level":
        a.label("boost")
    a.emit("58 9d")
    if name == "tech_level":
        a.emit("be ff ff ff 7f 89 6c 24 14")
    elif name == "reached_limit":
        a.emit("31 c0 c2 04 00")  # not at limit; thiscall cleans its argument
    if name != "reached_limit":
        at = base + len(a.code)
        a.code += relative_jump(at, bypass)
    if name == "tech_level":
        a.label("reject")
        a.emit("58 9d")
        a.code += relative_jump(base + len(a.code), 0x4E3740)
    a.label("original")
    a.emit("58 9d")
    a.emit(original_hex)
    code = a.finish()
    return code + relative_jump(base + len(code), resume)


def refresh_code():
    """Set House.RecheckTechTree on the current player inside the game tick."""
    a = X86()
    a.imm("a1", PLAYER_PTR)
    a.emit("85 c0")
    a.jump("0f 84", "empty")
    a.emit("c6 80 44 01 00 00 01 b8 01 00 00 00 c3")
    a.label("empty")
    a.emit("31 c0 c3")
    return a.finish()


REFRESH_OFFSET = 0x700


def build_block(base):
    placements = [(0x100 * (i + 1), site_code(name, base + 0x100 * (i + 1)))
                  for i, name in enumerate(SITES)]
    return assemble(0x1000, placements + [(REFRESH_OFFSET, refresh_code())], "科技和建造")


class BuildUnlockController:
    def __init__(self, proc):
        self.enabled = set()
        self.hooks = HookSites(proc, "科技或建造", {
            name: (address, bytes.fromhex(original), 0x100 * (i + 1))
            for i, (name, (_fid, address, original, _resume, _bypass)) in enumerate(SITES.items())},
            build_block)

    @property
    def base(self):
        return self.hooks.base

    @property
    def installed(self):
        return self.hooks.installed

    def set(self, fid, on):
        if fid not in {"tech_all", "unlock_build"}:
            raise ValueError(fid)
        desired = self.enabled | {fid} if on else self.enabled - {fid}
        # The prerequisite site is shared by both switches.
        wanted = {name for name, spec in SITES.items()
                  if set(spec[0] if isinstance(spec[0], tuple) else (spec[0],)) & desired}
        self.hooks.apply(wanted)
        self.enabled = desired

    def close(self):
        self.hooks.close()
        self.enabled.clear()

    def refresh_sidebar(self, executor):
        """Ask the native House tick to rebuild factory entries and prune sidebar."""
        if self.base is None:
            return 0
        return executor.call(self.base + REFRESH_OFFSET, timeout=5.0)
