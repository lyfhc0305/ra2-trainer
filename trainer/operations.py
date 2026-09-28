"""Validated game-thread operations for RA2 1.006."""
import struct

from . import units
from .addresses import HOUSE_VT, INFANTRY_VT, UNIT_VT
from .executor import CommandPending, MainThreadExecutor


class X86:
    def __init__(self):
        self.code = bytearray()
        self.labels = {}
        self.refs = []

    def emit(self, hexbytes):
        self.code += bytes.fromhex(hexbytes)

    def imm(self, opcode, value):
        self.emit(opcode)
        self.code += struct.pack("<I", value & 0xFFFFFFFF)

    def jump(self, opcode, label):
        self.emit(opcode)
        self.refs.append((len(self.code), label))
        self.code += bytes(4)

    def label(self, name):
        self.labels[name] = len(self.code)

    def finish(self):
        for pos, label in self.refs:
            struct.pack_into("<i", self.code, pos, self.labels[label] - pos - 4)
        return bytes(self.code)


def spawn_code(typ, house, coords):
    """Create + place + delete-on-failure in one simulation tick."""
    a = X86()
    a.emit("56")  # preserve esi
    a.imm("68", house)
    a.imm("b9", typ)
    a.emit("8b 01 ff 90 8c 00 00 00 85 c0")  # Type::CreateObject(house)
    a.jump("0f 84", "fail")
    a.emit("89 c6 6a 00")
    a.imm("68", coords)
    a.emit("89 f1 8b 06 ff 90 d4 00 00 00 84 c0")  # Unlimbo(coords, dir)
    a.jump("0f 85", "success")
    a.emit("6a 01 89 f1 8b 06 ff 50 20")  # deleting destructor
    a.label("fail")
    a.emit("31 c0")
    a.jump("e9", "end")
    a.label("success")
    a.emit("89 f0")
    a.label("end")
    a.emit("5e c3")
    return a.finish()


CLONE_RECORD = 48  # coords xyz (12) + cell (4) + 8 neighbour cells (32)
CLONE_CODE = 0x400  # code at the block start, data after it
MAX_CLONES = 500
DIRECTIONS = ((0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1))


ZONE_UNKNOWN = 0xFFFFFFFE
MAX_MOVEMENT_ZONE = 15
GET_ZONE = 0x54F8F0  # MapClass zone of CellStruct* for a MovementZone (thiscall, ret 0xC)


def clone_place_code(house, anchor_cell, zones):
    """try_place(TechnoType* type, record* rec) -> new object or 0 (cdecl, caller cleans).

    Create, then let the game decide: the cell must be on the playable map,
    not a bridge, the type's own CanEnterCell (vtable +0x194) must accept it
    (vehicles do not share a cell; infantry use free sub-spots) and one
    neighbour must be enterable so the new unit is not trapped. The cell must
    also lie in the same movement zone (the game's reachability areas, as
    compared by target evaluation 0x6C5735) as the cursor cell, so no clone
    lands in a cliff pocket, a patch walled in by trees/rocks or across water;
    when the cursor cell is not enterable for the type (occupied, building,
    cliff...), the first (nearest) successful cell sets the zone. Unlimbo picks the infantry spot itself.
    A failed placement is destroyed at once.
    """
    a = X86()
    a.emit("56 53 55 57")  # esi object, ebx cell, ebp level, edi direction
    a.emit("8b 4c 24 14")  # ECX = type
    a.imm("68", house)
    a.emit("8b 01 ff 90 8c 00 00 00 85 c0")  # Type::CreateObject(house)
    a.jump("0f 84", "fail_empty")
    a.emit("89 c6")

    a.emit("8b 44 24 18 83 c0 0c 50")  # &rec->cell
    a.imm("b9", 0x8324E0)
    a.imm("b8", 0x548070)  # Map.GetCell(CellStruct*)
    a.emit("ff d0")
    a.imm("3d", 0xA6F9F8)
    a.jump("0f 84", "destroy")
    a.emit("85 c0")
    a.jump("0f 84", "destroy")
    a.emit("89 c3")

    a.emit("6a 01 53")
    a.imm("b9", 0x8324E0)
    a.imm("b8", 0x55A740)  # playable area
    a.emit("ff d0 84 c0")
    a.jump("0f 84", "destroy")
    a.emit("f7 83 40 01 00 00 00 01 00 00")  # bridge: conservatively skipped
    a.jump("0f 85", "destroy")
    a.emit("0f be ab 1b 01 00 00")  # signed cell level

    a.emit("8b 44 24 18 50 89 d9")  # terrain height at rec->coords
    a.imm("b8", 0x4706C0)
    a.emit("ff d0 8b 4c 24 18 89 41 08")

    a.emit("6a 00 6a 00 6a ff 6a ff 53 89 f1 8b 06")
    a.emit("ff 90 94 01 00 00 85 c0")  # CanEnterCell(cell) == OK
    a.jump("0f 85", "destroy")

    # Same reachable zone as the cursor cell (per MovementZone, cached in `zones`).
    a.emit("8b 4c 24 14 8b 89 00 05 00 00")  # ECX = type->MovementZone
    a.emit(f"83 f9 {MAX_MOVEMENT_ZONE:02x}")
    a.jump("0f 87", "destroy")
    a.emit("8d 3c 8d")
    a.code += struct.pack("<I", zones)  # EDI = &zones[mz]
    a.emit("6a 00 51 8b 44 24 20 83 c0 0c 50")  # GetZone(&rec->cell, mz, false)
    a.imm("b9", 0x8324E0)
    a.imm("b8", GET_ZONE)
    a.emit("ff d0")
    a.imm("81 3f", ZONE_UNKNOWN)
    a.jump("0f 85", "have_zone")
    a.emit("50")  # keep this cell's zone
    a.imm("68", anchor_cell)
    a.imm("b9", 0x8324E0)
    a.imm("b8", 0x548070)
    a.emit("ff d0")
    a.imm("3d", 0xA6F9F8)
    a.jump("0f 84", "own_zone")
    a.emit("85 c0")
    a.jump("0f 84", "own_zone")
    a.emit("6a 00 6a 00 6a ff 6a ff 50 89 f1 8b 06")
    a.emit("ff 90 94 01 00 00 85 c0")  # cursor cell itself enterable (OK)?
    a.jump("0f 85", "own_zone")
    a.emit("6a 00 8b 44 24 1c ff b0 00 05 00 00")
    a.imm("68", anchor_cell)
    a.imm("b9", 0x8324E0)
    a.imm("b8", GET_ZONE)
    a.emit("ff d0 89 07")
    a.jump("e9", "zone_set")
    a.label("own_zone")
    a.emit("8b 04 24 89 07")
    a.label("zone_set")
    a.emit("58")
    a.label("have_zone")
    a.emit("3b 07")
    a.jump("0f 85", "destroy")

    a.emit("31 ff")
    a.label("next_exit")
    a.emit("8b 44 24 18 8d 44 b8 10 50")  # &rec->neighbours[edi/2]
    a.imm("b9", 0x8324E0)
    a.imm("b8", 0x548070)
    a.emit("ff d0")
    a.imm("3d", 0xA6F9F8)
    a.jump("0f 84", "skip_exit")
    a.emit("85 c0")
    a.jump("0f 84", "skip_exit")
    a.emit("50 6a 01 50")
    a.imm("b9", 0x8324E0)
    a.imm("b8", 0x55A740)
    a.emit("ff d0 5a 84 c0")
    a.jump("0f 84", "skip_exit")
    a.emit("f7 82 40 01 00 00 00 01 00 00")
    a.jump("0f 85", "skip_exit")
    a.emit("6a 00 53 55 57 52 89 f1 8b 06")
    a.emit("ff 90 94 01 00 00 85 c0")
    a.jump("0f 84", "place")
    a.label("skip_exit")
    a.emit("83 c7 02 83 ff 08")
    a.jump("0f 8c", "next_exit")
    a.jump("e9", "destroy")

    a.label("place")
    a.emit("6a 00 ff 74 24 1c")  # Unlimbo(&rec->coords, 0)
    a.emit("89 f1 8b 06 ff 90 d4 00 00 00 84 c0")
    a.jump("0f 85", "success")
    a.label("destroy")
    a.emit("6a 01 89 f1 8b 06 ff 50 20")
    a.label("fail_empty")
    a.emit("31 c0")
    a.jump("e9", "end")
    a.label("success")
    a.emit("89 f0")
    a.label("end")
    a.emit("5f 5d 5b 5e c3")
    return a.finish()


def clone_batch_code(house, count, types, records, record_count, results, zones):
    """Place every clone in one game frame, nearest free cell first.

    After a success the search for the next unit resumes at the same cell, so
    infantry can share it while vehicles move on to the next one.
    """
    a = X86()
    a.emit("56 53 55 57 31 db 31 ed")  # EBX = unit index, EBP = first cell to try
    a.label("unit")
    a.imm("81 fb", count)
    a.jump("0f 8d", "done")
    a.emit("89 ef")  # EDI = cell index
    a.label("cell")
    a.imm("81 ff", record_count)
    a.jump("0f 8d", "next_unit")  # no room left for this one
    a.emit("6b c7")
    a.code += bytes([CLONE_RECORD])
    a.imm("05", records)
    a.emit("50 ff 34 9d")
    a.code += struct.pack("<I", types)
    a.jump("e8", "try_place")
    a.emit("83 c4 08 85 c0")
    a.jump("0f 84", "next_cell")
    a.emit("89 04 9d")
    a.code += struct.pack("<I", results)
    a.emit("89 fd")
    a.jump("e9", "next_unit")
    a.label("next_cell")
    a.emit("47")
    a.jump("e9", "cell")
    a.label("next_unit")
    a.emit("43")
    a.jump("e9", "unit")
    a.label("done")
    a.emit("5f 5d 5b 5e c3")
    a.label("try_place")
    a.code += clone_place_code(house, records + 12, zones)
    return a.finish()


def clone_record(xyz):
    cx, cy = xyz[0] >> 8, xyz[1] >> 8
    out = struct.pack("<iiihh", xyz[0], xyz[1], xyz[2], cx, cy)
    out += b"".join(struct.pack("<hh", cx + dx, cy + dy) for dx, dy in DIRECTIONS)
    return out


class GameOperations:
    def __init__(self, proc):
        self.proc = proc
        self.executor = MainThreadExecutor(proc)

    def run(self, code, data=b""):
        e = self.executor
        e.install()
        if self.proc.read_u32(e.base) not in (0, 3):
            raise RuntimeError("上一条命令仍在执行")
        if len(code) > 0x300 or len(data) > 0x700:
            raise ValueError("命令超过缓冲区容量")
        if not self.proc.patch(e.base + 0x600, code):
            raise OSError("执行代码写入失败")
        if data and not self.proc.write(e.base + 0x900, data):
            raise OSError("执行参数写入失败")
        return e.call(e.base + 0x600)

    def spawn_at(self, typ, house, xyz):
        self.executor.install()
        data = struct.pack("<iii", *xyz)
        return self.run(spawn_code(typ, house, self.executor.base + 0x900), data)

    def _validate_clone(self):
        if getattr(self, "_clone_validated", False):
            return
        signatures = ((0x548070, "8b542404560fbf4202"),
                      (0x55A740, "8b442404538a5c240c55"),
                      (0x4706C0, "558bec83e4f883ec0c"),
                      (0x703040, "83ec68538b5c247055"),
                      (0x503190, "83ec2453558be9bb00010000"),
                      (0x504FD0, "83ec1853568b"),
                      (GET_ZONE, "518a4424105355568b74"))
        if any(self.proc.read(addr, len(bytes.fromhex(sig))) != bytes.fromhex(sig)
               for addr, sig in signatures):
            raise RuntimeError("复制安全检查代码与游戏版本不匹配")
        if (self.proc.read_u32(UNIT_VT + 0x194) != 0x703040 or
                self.proc.read_u32(INFANTRY_VT + 0x194) != 0x503190):
            raise RuntimeError("单位通行检查虚表与游戏版本不匹配")
        self._clone_validated = True

    def run_block(self, size, build, result_offset, result_size, timeout=10.0):
        """Run one batched command on the simulation thread from its own block.

        build(base) returns code (at base) followed by its data; the bytes at
        base+result_offset are returned. A command that may have been submitted
        but was not seen to finish (timeout, unreadable state) keeps its block:
        the game may still execute it later.
        """
        p, e = self.proc, self.executor
        e.install()
        if p.read_u32(e.base) not in (0, 3):
            raise RuntimeError("上一条游戏主线程命令仍在执行")
        block = p.alloc((size + 0xFFF) & ~0xFFF)
        if not block:
            raise OSError("无法分配批量命令区")
        try:
            data = build(block)
            if len(data) > size or not p.patch(block, data):
                raise OSError("批量命令写入失败")
            e.call(block, timeout=timeout)
        except CommandPending:
            raise  # the game may still run it: keep the block
        except Exception:
            p.free(block)
            raise
        raw = p.read(block + result_offset, result_size)
        p.free(block)
        if raw is None:
            raise OSError("无法读取批量命令结果")
        return raw

    def spawn_batch(self, types, destinations, house):
        """Place len(types) clones in one simulation-thread call; returns the new objects."""
        self._validate_clone()
        n, m = len(types), len(destinations)
        types_at = CLONE_CODE
        records_at = types_at + 4 * n
        results_at = records_at + CLONE_RECORD * m
        zones_at = results_at + 4 * n

        def build(block):
            code = clone_batch_code(house, n, block + types_at, block + records_at, m,
                                    block + results_at, block + zones_at)
            if len(code) > CLONE_CODE:
                raise ValueError("复制命令过长")
            return (code.ljust(CLONE_CODE, b"\xcc") + struct.pack(f"<{n}I", *types)
                    + b"".join(clone_record(xyz) for xyz in destinations) + bytes(4 * n)
                    + struct.pack("<I", ZONE_UNKNOWN) * (MAX_MOVEMENT_ZONE + 1))

        raw = self.run_block(zones_at + 4 * (MAX_MOVEMENT_ZONE + 1), build, results_at, 4 * n)
        return list(struct.unpack(f"<{n}I", raw))

    def cursor_point(self):
        """Game-maintained cursor position, relative to the tactical viewport."""
        p = self.proc
        mouse = p.read_u32(0x839C68)
        if not mouse or p.read_u32(mouse) != 0x7AFBEC:
            raise ValueError("游戏鼠标尚未初始化")
        x, y = p.read_i32(mouse + 0x1C), p.read_i32(mouse + 0x20)
        left, top = p.read_i32(0x839630), p.read_i32(0x839634)
        width, height = p.read_i32(0x839638), p.read_i32(0x83963C)
        if any(v is None for v in (x, y, left, top, width, height)):
            raise ValueError("无法读取游戏鼠标位置")
        if width <= 0 or height <= 0 or not (left <= x < left + width and top <= y < top + height):
            raise ValueError("请将游戏鼠标移到地图上后再复制")
        return x - left, y - top, width, height

    def world_at_screen(self, point):
        """Use the game's terrain-aware click conversion on the simulation thread."""
        p = self.proc
        if p.read(0x669650, 7) != bytes.fromhex("8b44241083ec18"):
            raise RuntimeError("鼠标坐标转换代码不匹配")
        self.executor.install()
        if p.read_u32(self.executor.base) not in (0, 3):
            raise RuntimeError("上一条游戏主线程命令仍在执行")
        base = self.executor.base + 0x900
        data = struct.pack("<ii", *point) + bytes(32)
        if not p.write(base, data):
            raise OSError("鼠标坐标参数写入失败")
        result = self.executor.call(0x669650, this=0x8324E0,
                                    args=(base, base + 8, base + 16,
                                          base + 28, base + 32, base + 36))
        if result is None or not result & 0xFF:
            return None
        raw = p.read(base + 16, 12)
        if raw is None:
            raise OSError("无法读取游戏地图坐标")
        xyz = struct.unpack("<iii", raw)
        return xyz if all(-0x1000000 < v < 0x1000000 for v in xyz) else None

    def clone_selected(self, point=None):
        p = self.proc
        cursor_x, cursor_y, width, height = point or self.cursor_point()
        # Freeze the exact map point before reading the selection.
        anchor = self.world_at_screen((cursor_x, cursor_y))
        if anchor is None:
            raise ValueError("鼠标位置无法转换为可放置的地图坐标")
        units.require_player_house(p)
        selected = units.selected_technos(p, own=True)
        if not selected:
            raise ValueError("请先在游戏中选中己方载具或步兵")
        if any(v not in (UNIT_VT, INFANTRY_VT) for _, v in selected):
            raise ValueError("复制目前支持地面载具和步兵，请取消选择建筑和飞机")
        if len(selected) > MAX_CLONES:
            raise ValueError(f"每次最多复制{MAX_CLONES}个单位")
        house = units.player_house(p)
        types = []
        for source, vt in selected:
            if not units.valid_techno(p, source, vt, house):
                continue
            typ = units.type_of(p, source, vt)
            if typ:
                types.append(typ)
        if not types:
            return 0, len(selected)
        destinations = self._clone_destinations(anchor, len(types))
        results = self.spawn_batch(types, destinations, house)
        units.clear_cache(p)
        return sum(1 for obj in results if obj), len(selected)

    @staticmethod
    def _clone_destinations(anchor, count=1):
        """Real map cells around the anchor, nearest first; enough rings for `count`."""
        cx, cy = anchor[0] >> 8, anchor[1] >> 8
        radius = 4
        while (2 * radius + 1) ** 2 < 2 * count and radius < 15:
            radius += 1
        out = [anchor]
        for r in range(1, radius + 1):
            ring = [(dx, dy) for dy in range(-r, r + 1)
                    for dx in range(-r, r + 1) if max(abs(dx), abs(dy)) == r]
            for dx, dy in sorted(ring, key=lambda d: (d[0] ** 2 + d[1] ** 2, d[1], d[0])):
                x, y = cx + dx, cy + dy
                if 0 <= x < 512 and 0 <= y < 512:
                    out.append((x * 256 + 128, y * 256 + 128, anchor[2]))
        return out

    def transfer_batch(self, objects, house):
        """Change the owner of every (object, vtable) in one game frame; returns successes."""
        if self.proc.read_u32(house) != HOUSE_VT:
            raise RuntimeError("目标势力无效")
        n = len(objects)
        records_at = 0x100

        def build(block):
            a = X86()
            a.emit("56 53 31 db 31 f6")  # ESI = index, EBX = successes
            a.label("next")
            a.imm("81 fe", n)
            a.jump("0f 8d", "done")
            a.emit("8b 0c f5")
            a.code += struct.pack("<I", block + records_at)  # object
            a.emit("8b 04 f5")
            a.code += struct.pack("<I", block + records_at + 4)  # expected vtable
            a.emit("85 c9")
            a.jump("0f 84", "skip")
            a.emit("39 01")
            a.jump("0f 85", "skip")  # object gone or reused since the click
            a.emit("6a 01")
            a.imm("68", house)
            a.emit("8b 01 ff 90 78 03 00 00 84 c0")  # SetOwningHouse(house, 1)
            a.jump("0f 84", "skip")
            a.emit("43")
            a.label("skip")
            a.emit("46")
            a.jump("e9", "next")
            a.label("done")
            a.emit("89 d8 5b 5e c3")
            code = a.finish()
            data = b"".join(struct.pack("<II", obj, vt) for obj, vt in objects)
            return code.ljust(records_at, b"\xcc") + data + bytes(4)

        size = records_at + 8 * n + 4
        # The count is returned in EAX; also verified by reading nothing extra.
        p, e = self.proc, self.executor
        e.install()
        if p.read_u32(e.base) not in (0, 3):
            raise RuntimeError("上一条游戏主线程命令仍在执行")
        block = p.alloc((size + 0xFFF) & ~0xFFF)
        if not block:
            raise OSError("无法分配批量命令区")
        try:
            if not p.patch(block, build(block)):
                raise OSError("批量命令写入失败")
            count = e.call(block, timeout=10.0)
        except CommandPending:
            raise  # the game may still run it: keep the block
        except Exception:
            p.free(block)
            raise
        p.free(block)
        return count or 0

    def transfer_selected(self, house):
        selected = units.selected_technos(self.proc)
        if not selected:
            raise ValueError("请先选中要转移控制权的单位或建筑")
        valid = [(a, vt) for a, vt in selected if units.valid_techno(self.proc, a, vt)]
        count = self.transfer_batch(valid, house) if valid else 0
        units.clear_cache(self.proc)
        return count, len(selected)

    def close(self):
        self.executor.close()
