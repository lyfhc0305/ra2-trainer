"""Player-only amphibious movement for stock RA2 1.006.

All adjustments are local to the moving Foot object. Shared Type and Rules
data remain unchanged. A detached trainer leaves DRAIN mode behind, allowing
already-waterborne units to return over beaches to native land.
"""
import struct

from .addresses import BUILDING_VT, INFANTRY_VT, PLAYER_PTR, UNIT_VT
from .executor import relative_jump
from .hooks import HookSites
from .operations import X86


MAGIC = b"RA2WATER7\0"
CLONING_MAGIC = b"RA2WATER6\0"
DESTINATION_MAGIC = b"RA2WATER5\0"
RALLY_MAGIC = b"RA2WATER4\0"
ATTACK_MAGIC = b"RA2WATER3\0"
PREVIOUS_MAGIC = b"RA2WATER2\0"
LEGACY_MAGIC = b"RA2WATER1\0"  # first ten sites only; upgraded in place
MODE_OFFSET = 0x20
HELPER_OFFSET = 0x100
SITE_OFFSET = 0x400
SITE_STRIDE = 0x200
BLOCK_SIZE = 0x3000
MODE_OFF, MODE_ON, MODE_DRAIN = 0, 1, 2
MATRIX = 0x850EA0
CELL_ARRAY_PTR = 0x832618

# Full instructions from the local RA2 1.006 game.exe.
SITES = {
    "path_zone": (0x42A220, "8b7c24488b74243c", "zone_edi_esi"),
    "path_region": (0x42AAD4, "57508d442418", "zone_eax_edi"),
    "foot_region": (0x4C2EFF, "8b9800050000", "zone_ebx_esi"),
    "clicked_zone": (0x4C653C, "8bb800050000", "zone_edi_esi"),
    "destination_zone": (0x4CCB27, "8ba800050000", "zone_ebp_esi"),
    "special_nearby": (0x4C6741, "8b80a4050000", "speed_eax_esi"),
    "move_nearby": (0x4C6861, "8b80a4050000", "speed_eax_esi"),
    "infantry_enter": (0x50390C, "d9048da00e8500", "factor_ecx_ebp"),
    "infantry_level": (0x503974, "d90495a00e8500", "factor_edx_ebp"),
    "unit_enter": (0x703928, "d9048da00e8500", "factor_ecx_ebp"),
    # Attacking: TechnoClass::NearbyLocation 0x6CF130 finds the firing position
    # with the object's SpeedType/MovementZone; target evaluation compares the
    # shooter's zone (0x6C6530 -> 0x6C5570) and candidate cells (0x6C6130).
    "near_speed": (0x6CF141, "8b98a4050000", "speed_ebx_esi"),
    "near_zone": (0x6CF1DB, "8bb800050000", "zone_edi_esi"),
    "shooter_zone": (0x6C65EE, "8b8000050000", "type_eax_edi"),
    "target_zone": (0x6C571F, "8b8000050000", "type_eax_edi"),
    "candidate_zone": (0x6C6170, "8b8000050000", "type_eax_esi"),
    # Foot's firing-position search is shared by infantry and vehicles and
    # keeps the shooter in EBP (ESI is a candidate/direction index here).
    "approach_zone": (0x4C524E, "8b8000050000", "type_eax_ebp"),
    "approach_cell_speed": (0x4C547B, "8b88a4050000", "speed_ecx_ebp"),
    "approach_ring_speed": (0x4C55C6, "8b80a4050000", "speed_eax_ebp"),
    "approach_near_speed": (0x4C585A, "8b80a4050000", "speed_eax_ebp"),
    # Techno::IsReachable, called by Building::GetAction for rally points.
    "rally_zone": (0x6D32F0, "8bb800050000", "rally_zone"),
    "rally_destination": (0x4401A9, "8d9588000000", "rally_destination"),
    # Foot nearby-cell fallback when the requested destination is occupied.
    "occupied_zone": (0x4C3198, "8b8000050000", "type_eax_ebp"),
    "occupied_speed": (0x4C3238, "8b80a4050000", "speed_eax_ebp"),
    "alternate_zone": (0x4C33F5, "8b8000050000", "type_eax_ebp"),
    "alternate_speed": (0x4C3495, "8b80a4050000", "speed_eax_ebp"),
}
LEGACY_SITE_COUNT = 10
PREVIOUS_SITE_COUNT = 15
ATTACK_SITE_COUNT = 19
RALLY_SITE_COUNT = 20


def qualifier_code(base, mode_address):
    """ECX=Foot*, EAX=amphibious zone or -1; clobbers ECX/EDX."""
    a = X86()
    a.emit("53 56 57 89 ce")  # EBX/ESI/EDI; ESI=Foot
    a.emit("85 f6")
    a.jump("0f 84", "reject")
    a.imm("81 3e", INFANTRY_VT)
    a.jump("0f 84", "infantry")
    a.imm("81 3e", UNIT_VT)
    a.jump("0f 85", "reject")
    a.emit("8b be ac 05 00 00")  # Unit.Type
    a.jump("e9", "class_ok")
    a.label("infantry")
    a.emit("8b be a8 05 00 00")  # Infantry.Type
    a.label("class_ok")
    a.imm("a1", PLAYER_PTR)
    a.emit("85 c0")
    a.jump("0f 84", "reject")
    a.emit("39 86 b4 01 00 00")  # current player owns this instance
    a.jump("0f 85", "reject")
    a.emit("83 7e 6c 00")  # Health > 0
    a.jump("0f 8e", "reject")
    a.emit("80 7e 7d 01")  # active
    a.jump("0f 85", "reject")
    a.emit("80 7e 76 00")  # not limbo
    a.jump("0f 85", "reject")
    a.emit("85 ff")
    a.jump("0f 84", "reject")
    a.emit("80 bf da 0a 00 00 00")  # Type.Naval
    a.jump("0f 85", "reject")
    a.emit("8b 9f a4 05 00 00 83 fb 02")  # SpeedType 0..2 only
    a.jump("0f 87", "reject")
    a.emit("83 3c 9d e8 0e 85 00 00")  # Water[2*9+speed] originally zero
    a.jump("0f 85", "reject")
    a.imm("80 3d", mode_address)
    a.emit("01")
    a.jump("0f 84", "zone")
    a.imm("80 3d", mode_address)
    a.emit("02")
    a.jump("0f 85", "reject")
    # DRAIN: only a unit still in Water or Beach retains amphibious routing.
    a.emit("80 7e 79 00")  # not on a bridge
    a.jump("0f 85", "reject")
    a.emit("8b 86 88 00 00 00 c1 f8 08 3d ff 01 00 00")
    a.jump("0f 87", "reject")  # also catches negative after SAR
    a.emit("8b 96 8c 00 00 00 c1 fa 08 81 fa ff 01 00 00")
    a.jump("0f 87", "reject")
    a.emit("c1 e2 09 01 c2 c1 e2 02")
    a.imm("8b 1d", CELL_ARRAY_PTR)
    a.emit("85 db")
    a.jump("0f 84", "reject")
    a.emit("8b 04 13 85 c0")
    a.jump("0f 84", "reject")
    a.emit("3d f8 f9 a6 00")  # Map.GetCell sentinel
    a.jump("0f 84", "reject")
    a.emit("8b 80 ec 00 00 00 83 f8 02")
    a.jump("0f 84", "zone")
    a.emit("83 f8 06")
    a.jump("0f 85", "reject")
    a.label("zone")
    a.emit("8b 87 00 05 00 00")  # original MovementZone
    a.emit("83 f8 00")
    a.jump("0f 84", "amph5")
    a.emit("83 f8 07")
    a.jump("0f 84", "amph5")
    a.emit("83 f8 01")
    a.jump("0f 84", "amph4")
    a.emit("83 f8 02")
    a.jump("0f 84", "amph3")
    a.emit("83 f8 08")
    a.jump("0f 84", "amph3")
    a.jump("e9", "reject")
    a.label("amph5")
    a.emit("b8 05 00 00 00")
    a.jump("e9", "done")
    a.label("amph4")
    a.emit("b8 04 00 00 00")
    a.jump("e9", "done")
    a.label("amph3")
    a.emit("b8 03 00 00 00")
    a.jump("e9", "done")
    a.label("reject")
    a.emit("b8 ff ff ff ff")
    a.label("done")
    a.emit("5f 5e 5b c3")
    return a.finish()


def site_code(name, base, helper_address, one_address, *, cloning=True):
    address, original_hex, kind = SITES[name]
    original = bytes.fromhex(original_hex)
    a = X86()
    if kind == "rally_destination":
        a.code += original
        a.emit("9c 60")
        a.imm("80 3d", one_address - 8)
        a.emit("01")
        a.jump("0f 85", "rally_restore")
        a.imm("81 7d 00", BUILDING_VT)
        a.jump("0f 85", "rally_restore")
        a.imm("a1", PLAYER_PTR)
        a.emit("85 c0")
        a.jump("0f 84", "rally_restore")
        a.emit("39 85 b4 01 00 00")
        a.jump("0f 85", "rally_restore")
        a.emit("83 7d 6c 00")
        a.jump("0f 8e", "rally_restore")
        a.emit("80 7d 7d 01")
        a.jump("0f 85", "rally_restore")
        a.emit("80 7d 76 00")
        a.jump("0f 85", "rally_restore")
        a.emit("8b 85 18 04 00 00 85 c0")
        a.jump("0f 84", "rally_restore")
        a.emit("80 b8 da 0a 00 00 00")
        a.jump("0f 85", "rally_restore")
        a.emit("83 b8 00 05 00 00 00")
        a.jump("0f 85", "rally_restore")
        if cloning:
            # BuildingType::Cloning; stock NACLON has Factory=None (0).
            a.emit("80 b8 4b 13 00 00 00")
            a.jump("0f 85", "rally_factory")
        a.emit("8b 80 44 0c 00 00 83 f8 10")
        a.jump("0f 84", "rally_factory")
        a.emit("83 f8 28")
        a.jump("0f 85", "rally_restore")
        a.label("rally_factory")
        # SetRallyPoint chooses a nearby valid cell before queuing its event.
        # ESI is MovementZone; native [ESP+0x20] is SpeedType.
        a.emit("c7 44 24 04 05 00 00 00 c7 44 24 44 06 00 00 00")
        a.label("rally_restore")
        a.emit("61 9d")
    elif kind == "rally_zone":
        a.code += original
        a.emit("9c 60")
        # Original return address: 0x20 bytes of native frame + PUSHFD/PUSHAD.
        # Restrict this override to the two building rally cursor callers.
        a.emit("8b 44 24 44 3d a1 3c 44 00")
        a.jump("0f 84", "rally_caller")
        a.emit("3d 2e 3b 44 00")
        a.jump("0f 85", "rally_restore")
        a.label("rally_caller")
        mode_address = one_address - 8
        a.imm("80 3d", mode_address)
        a.emit("01")  # DRAIN must not authorize new cross-water production orders.
        a.jump("0f 85", "rally_restore")
        a.imm("81 3e", BUILDING_VT)
        a.jump("0f 85", "rally_restore")
        a.imm("a1", PLAYER_PTR)
        a.emit("85 c0")
        a.jump("0f 84", "rally_restore")
        a.emit("39 86 b4 01 00 00")
        a.jump("0f 85", "rally_restore")
        a.emit("83 7e 6c 00")
        a.jump("0f 8e", "rally_restore")
        a.emit("80 7e 7d 01")
        a.jump("0f 85", "rally_restore")
        a.emit("80 7e 76 00")
        a.jump("0f 85", "rally_restore")
        a.emit("8b 86 18 04 00 00 85 c0")
        a.jump("0f 84", "rally_restore")
        a.emit("80 b8 da 0a 00 00 00")
        a.jump("0f 85", "rally_restore")
        if cloning:
            # BuildingType::Cloning; stock NACLON has Factory=None (0).
            a.emit("80 b8 4b 13 00 00 00")
            a.jump("0f 85", "rally_factory")
        a.emit("8b 80 44 0c 00 00 83 f8 10")
        a.jump("0f 84", "rally_factory")
        a.emit("83 f8 28")
        a.jump("0f 85", "rally_restore")
        a.label("rally_factory")
        a.emit("85 ff")  # Stock land factories use MovementZone::Normal.
        a.jump("0f 85", "rally_restore")
        a.emit("c7 04 24 05 00 00 00")  # EDI = Amphibious, preserve native reachability check.
        a.label("rally_restore")
        a.emit("61 9d")
    elif kind.startswith("factor_"):
        a.emit("9c 60")
        saved_index = 0x18 if "ecx" in kind else 0x14
        saved_owner = 0x08  # EBP in PUSHAD frame
        a.emit(f"8b 44 24 {saved_index:02x}")
        a.emit("83 f8 12")
        a.jump("0f 82", "original")
        a.emit("83 f8 14")
        a.jump("0f 86", "land_ok")
        a.emit("83 f8 36")
        a.jump("0f 82", "original")
        a.emit("83 f8 38")
        a.jump("0f 87", "original")
        a.label("land_ok")
        a.emit("83 3c 85 a0 0e 85 00 00")  # actual factor zero
        a.jump("0f 85", "original")
        a.emit(f"8b 4c 24 {saved_owner:02x}")
        a.imm("e8", helper_address - (base + len(a.code) + 5))
        a.emit("83 f8 ff")
        a.jump("0f 84", "original")
        a.emit("61 9d")
        a.imm("d9 05", one_address)  # exactly one x87 stack value
        a.jump("e9", "resume")
        a.label("original")
        a.emit("61 9d")
        a.code += original
        a.label("resume")
    else:
        if kind != "zone_eax_edi":
            a.code += original
        a.emit("9c 60")
        source_slot = {"edi": 0, "esi": 4, "ebp": 8}[kind.rsplit("_", 1)[1]]
        a.emit(f"8b 4c 24 {source_slot:02x}")
        a.imm("e8", helper_address - (base + len(a.code) + 5))
        a.emit("83 f8 ff")
        a.jump("0f 84", "restore")
        output_slot = {"zone_edi_esi": 0, "zone_eax_edi": 0x1C,
                       "zone_ebx_esi": 0x10, "zone_ebp_esi": 0x08,
                       "speed_eax_esi": 0x1C, "speed_ebx_esi": 0x10,
                       "type_eax_edi": 0x1C, "type_eax_esi": 0x1C,
                       "type_eax_ebp": 0x1C, "speed_ecx_ebp": 0x18,
                       "speed_eax_ebp": 0x1C}[kind]
        if kind.startswith("speed_"):
            a.emit("b8 06 00 00 00")
        a.emit(f"89 44 24 {output_slot:02x}")
        a.label("restore")
        a.emit("61 9d")
        if kind == "zone_eax_edi":
            a.code += original
    code = a.finish()
    return code + relative_jump(base + len(code), address + len(original))


def site_offset(index):
    # Preserve resident return addresses; pack four small stubs in the last slot.
    return SITE_OFFSET + min(index, 21) * SITE_STRIDE + max(0, index - 21) * 0x80


def block_bytes(base, *, cloning=True):
    blob = bytearray(BLOCK_SIZE)
    blob[:len(MAGIC)] = MAGIC
    blob[MODE_OFFSET] = MODE_OFF
    struct.pack_into("<f", blob, 0x28, 1.0)
    helper = qualifier_code(base + HELPER_OFFSET, base + MODE_OFFSET)
    if len(helper) > SITE_OFFSET - HELPER_OFFSET:
        raise ValueError("水面行走资格代码过长")
    blob[HELPER_OFFSET:HELPER_OFFSET + len(helper)] = helper
    for i, name in enumerate(SITES):
        off = site_offset(i)
        code = site_code(name, base + off, base + HELPER_OFFSET, base + 0x28, cloning=cloning)
        if len(code) > (SITE_STRIDE if i < 21 else 0x80) or off + len(code) > BLOCK_SIZE:
            raise ValueError(f"水面行走补丁过长：{name}")
        blob[off:off + len(code)] = code
    return bytes(blob)


class WaterWalkController:
    def __init__(self, proc):
        self.proc = proc
        # Resident by design (DRAIN lets waterborne units return): adopt, never release.
        self.hooks = HookSites(proc, "水面行走", {
            name: (address, bytes.fromhex(raw), site_offset(i))
            for i, (name, (address, raw, _kind)) in enumerate(SITES.items())},
            block_bytes, size=BLOCK_SIZE, release=False)
        # A resident block this version cannot adopt only disables this
        # feature for the rest of the game session; it must not fail attach.
        self.adopt_error = None
        try:
            self._adopt()
        except Exception as exc:
            self.adopt_error = f"水面行走本局不可用（重启游戏后恢复）：{exc}"

    @property
    def base(self):
        return self.hooks.base

    @property
    def installed(self):
        return self.hooks.installed

    def _expected(self, name, base):
        return self.hooks.replacement(name, base)

    def _adopt(self):
        base = self.hooks.locate(MAGIC)
        old_count = None
        old_magic = None
        if base is None:
            for magic, count in ((CLONING_MAGIC, 21),
                                 (DESTINATION_MAGIC, 21),
                                 (RALLY_MAGIC, RALLY_SITE_COUNT),
                                 (ATTACK_MAGIC, ATTACK_SITE_COUNT),
                                 (PREVIOUS_MAGIC, PREVIOUS_SITE_COUNT),
                                 (LEGACY_MAGIC, LEGACY_SITE_COUNT)):
                base = self.hooks.locate(magic)
                if base is not None:
                    old_magic, old_count = magic, count
                    break
        if base is None:
            return
        block = self.proc.read(base, BLOCK_SIZE)
        expected = bytearray(block_bytes(base, cloning=old_magic in (None, CLONING_MAGIC)))
        if old_count:
            expected[:len(old_magic)] = old_magic
            end = SITE_OFFSET + old_count * SITE_STRIDE
            expected[end:] = bytes(BLOCK_SIZE - end)
        if not block or len(block) != BLOCK_SIZE or (
                block[:MODE_OFFSET] != expected[:MODE_OFFSET] or
                block[MODE_OFFSET + 1:] != expected[MODE_OFFSET + 1:]):
            raise RuntimeError("水面行走控制块版本不匹配，未接管")
        if block[MODE_OFFSET] not in (MODE_ON, MODE_DRAIN):
            raise RuntimeError("水面行走控制块状态异常")
        names = list(SITES)[:old_count] if old_count else None
        self.hooks.adopt(base, names)
        if old_count:
            replacement = bytearray(block_bytes(base))
            replacement[MODE_OFFSET] = block[MODE_OFFSET]
            # The two rally stubs are call-free. Earlier helper/calling stubs
            # keep their exact bytes and offsets, including return addresses.
            # Suspend and verify EIPs before replacing both stubs and the header
            # together; a failed write restores the complete previous block.
            self.proc.patch_quiescent(base, block, bytes(replacement))
        # Attach starts from a safe disabled state. Saved true is reapplied by
        # MainWindow; a stale ON left by an abnormal previous exit cannot lie
        # behind an off-looking toggle.
        if block[MODE_OFFSET] == MODE_ON:
            self.disable()

    @property
    def enabled(self):
        return self.base is not None and self.proc.read(self.base + MODE_OFFSET, 1) == bytes([MODE_ON])

    def _prepare(self):
        self.hooks.prepare()

    def enable(self):
        if self.adopt_error:
            raise RuntimeError(self.adopt_error)

        def switch_on():
            # Called on every periodic reapply; rewrite only when the mode changed.
            if self.proc.read(self.base + MODE_OFFSET, 1) == bytes([MODE_ON]):
                return
            if not self.proc.patch(self.base + MODE_OFFSET, bytes([MODE_ON])):
                raise OSError("无法启用水面行走控制块")
        self.hooks.apply(SITES, after=switch_on)

    def disable(self):
        if self.base is None:
            return
        # The autonomous hook now applies only to units still on water/beach.
        # It may remain resident if this trainer exits before those units land.
        if self.installed:
            if not self.proc.patch(self.base + MODE_OFFSET, bytes([MODE_DRAIN])):
                raise OSError("无法关闭水面行走控制块")

    close = disable
