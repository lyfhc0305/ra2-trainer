"""技术类（单位/建筑）对象操作。

vtable 与字段偏移均由 tools/objects.py 在 RA2 1.006 实机验证：
    TechnoClass + 0x1B4 = Owner (HouseClass*)       271/274 命中
    TechnoClass + 0x120 = ArmorMultiplier   (double, 默认 1.0)  270/271
    TechnoClass + 0x128 = FirepowerMultiplier (double, 默认 1.0) 270/271
    TechnoClass + 0x06C = Health   /  +0x070 = EstimatedHealth  (int 成对)
"""
import struct

from . import objscan as O
from .addresses import (PLAYER_PTR, SELECTED_COUNT, SELECTED_DATA, SUPER_VT, TECHNO_ARRAY,
                        TECHNO_COUNT, TECHNO_FIELD)

# 游戏里同时存在的技术类对象远少于此；超出说明读到的不是这个向量。
MAX_TECHNOS = 0x10000

# valid_techno(house=ANY_OWNER) skips the owner check on purpose. Any other
# falsy house (None = read failed, 0 = no player) means "owner unknown" and
# never matches, so a failed player read can't widen "own units" to everyone.
ANY_OWNER = object()


def player_house(proc):
    return proc.read_u32(PLAYER_PTR)


def require_player_house(proc):
    house = player_house(proc)
    if not house:
        raise RuntimeError("无法识别玩家势力（未进入战局或读取失败），已停止对己方单位的操作")
    return house


def techno_pointers(proc):
    """TechnoClass::Array 中的全部对象指针（游戏自己维护，一次读完，不扫描堆）。"""
    count = proc.read_i32(TECHNO_COUNT)
    if count is None or not 0 <= count <= MAX_TECHNOS:
        raise RuntimeError("游戏单位列表数量异常")
    if not count:
        return []
    data = proc.read_u32(TECHNO_ARRAY)
    if not data:
        raise RuntimeError("无法读取游戏单位列表")
    blob = proc.read(data, count * 4)
    if blob is None:
        raise OSError("无法读取游戏单位列表")
    return [a for (a,) in struct.iter_unpack("<I", blob) if a]


def iter_technos(proc):
    """产出所有技术类对象 (addr, vtable)。"""
    for a in techno_pointers(proc):
        vt = proc.read_u32(a)
        if vt in O.TECHNO_VT:
            yield a, vt


def house_technos(proc, house, kinds=None):
    """返回某势力拥有的技术类对象列表 [(addr, vtable), ...]。"""
    owner_off = TECHNO_FIELD["Owner"]
    out = []
    for a in techno_pointers(proc):
        head = proc.read(a, owner_off + 4)  # 一次读出 vtable 和 Owner
        if head is None:
            continue
        vt = struct.unpack_from("<I", head, 0)[0]
        if vt not in O.TECHNO_VT or (kinds and vt not in kinds):
            continue
        if struct.unpack_from("<I", head, owner_off)[0] == house:
            out.append((a, vt))
    return out


def player_technos(proc, kinds=None):
    h = player_house(proc)
    if not h:
        return []
    return house_technos(proc, h, kinds)


def clear_cache(proc):
    proc._unit_cache = None


def valid_techno(proc, addr, vt, house=ANY_OWNER):
    """Recheck identity before writing cached pointers; this reduces stale writes."""
    if proc.read_u32(addr) != vt or vt not in O.TECHNO_VT:
        return False
    if house is not ANY_OWNER and (not house
                                   or proc.read_u32(addr + TECHNO_FIELD["Owner"]) != house):
        return False
    hp = proc.read_i32(addr + TECHNO_FIELD["Health"])
    return hp is not None and hp > 0


def selection(proc):
    """Object pointers in the game's current-selection vector, in order, deduplicated."""
    count = proc.read_i32(SELECTED_COUNT)
    data = proc.read_u32(SELECTED_DATA)
    if count is None or count < 0 or count > 2048:
        raise RuntimeError("游戏选中列表数量异常")
    if not count:
        return []
    if not data:
        raise RuntimeError("无法读取游戏选中列表")
    blob = proc.read(data, count * 4)
    if blob is None:
        raise OSError("无法获取当前选中的单位")
    return list(dict.fromkeys(a for (a,) in struct.iter_unpack("<I", blob)))


def selected_technos(proc, own=False):
    """Selected technos; reads only the selection, never scans the heap.

    own=True with an unknown player returns nothing rather than every selection."""
    house = player_house(proc) if own else ANY_OWNER
    if own and not house:
        return []
    try:
        objects = selection(proc)
    except (RuntimeError, OSError):
        return []
    out = []
    for a in objects:
        vt = proc.read_u32(a)
        if valid_techno(proc, a, vt, house) and proc.read_u8(a + 0x77) == 1:
            out.append((a, vt))
    return out


def player_technos_cached(proc, ttl=3.0, kinds=None):
    """带缓存的己方技术类对象列表（读取游戏单位列表，周期性刷新即可）。"""
    import time
    h = player_house(proc)
    if not h:
        clear_cache(proc)
        return []
    now = time.monotonic()
    cache = getattr(proc, "_unit_cache", None)
    if cache is None or h != cache["house"] or now - cache["t"] > ttl:
        objs = player_technos(proc)
        cache = dict(t=time.monotonic(), house=h, objs=objs)
        proc._unit_cache = cache
    return [(a, vt) for a, vt in cache["objs"]
            if (not kinds or vt in kinds) and valid_techno(proc, a, vt, h)]


def type_of(proc, addr, vt):
    """取某个对象的 TechnoTypeClass*（各类别的 Type 偏移不同）。"""
    from .addresses import CLASS_TYPE_OFF
    off = CLASS_TYPE_OFF.get(vt)
    if off is None:
        return None
    t = proc.read_u32(addr + off)
    return t if t and t > 0x10000 else None


def enemy_houses(proc):
    """Legacy helper: returns other houses, which may include allies and neutrals."""
    from .addresses import HOUSE_ARRAY
    items = proc.read_u32(HOUSE_ARRAY)
    cnt = proc.read_i32(HOUSE_ARRAY + 0xC)
    if not items or not cnt or not 0 < cnt <= 32:
        return []
    blob = proc.read(items, cnt * 4)
    if not blob:
        return []
    me = player_house(proc)
    out = []
    for i in range(cnt):
        h = int.from_bytes(blob[i * 4:i * 4 + 4], "little")
        if h and h != me:
            out.append(h)
    return out


def player_supers(proc):
    house = player_house(proc)
    if not house:
        return []
    items = proc.read_u32(house + 0x1A0)
    count = proc.read_i32(house + 0x1AC)
    if not items or count is None or not 0 <= count <= 64:
        return []
    out = []
    for i in range(count):
        a = proc.read_u32(items + 4 * i)
        if a and proc.read_u32(a) == SUPER_VT and proc.read_u32(a + 0x28) == house:
            out.append(a)
    return out
