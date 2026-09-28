"""Type properties located through the RA2 INI-reading code, not YR offsets.

Type objects are shared by all houses. Edits therefore apply to every instance
of the same type. Original values are retained for explicit/exit restoration.
"""
import math

from . import units

TYPE_FEATURES = {
    "u_speed": (0x5A0, "i32", 0.1, 10.0, "multiplier"),
    "u_sight": (0x530, "i32", 0, 30, "value"),
    "u_cost": (0x550, "i32", 0, 1000000, "value"),
    "u_unselectable": (0x200, "u8", 0, 1, "inverse"),
    "u_invisible": (0xADC, "u8", 0, 1, "value"),
    "u_detect": (0xAA9, "u8", 0, 1, "value"),
    "u_rad_immune": (0xAFE, "u8", 0, 1, "value"),
    "turn_speed": (0x638, "i32", 1, 127, "value"),
}


ID_OFFSET = 0x24  # AbstractTypeClass::ID, e.g. "HTNK"


class TypeEditor:
    def __init__(self, proc):
        self.proc = proc
        self.originals = {}

    def _identity(self, typ):
        # The vtable is shared by every type of a class; the ID separates them
        # when a new match reuses the same address for another type.
        return self.proc.read_u32(typ), self.proc.read_cstr(typ + ID_OFFSET, 25)

    def targets(self):
        units.require_player_house(self.proc)
        targets = {units.type_of(self.proc, a, vt)
                   for a, vt in units.selected_technos(self.proc, own=True)}
        targets.discard(None)
        if not targets:
            raise ValueError("请先选择己方单位；无法再选中时，请使用“还原类型修改”")
        return sorted(targets)

    def write(self, fid, text):
        off, kind, lo, hi, mode = TYPE_FEATURES[fid]
        value = float(text) if mode == "multiplier" else int(text, 0)
        if not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"请输入 {lo} 到 {hi} 之间的数值")
        targets = self.targets()
        for typ in targets:
            key = (typ, off, kind)
            old = self.proc.read_value(typ + off, kind)
            if old is None:
                raise OSError("类型属性读取失败")
            identity = self._identity(typ)
            if identity[0] not in units.O.TYPE_VT or not identity[1]:
                raise ValueError("类型对象已失效")
            saved = self.originals.get(key)
            if saved and saved[2] != identity:
                saved = None  # same address, different type since a reload
            initial, _last, identity = saved or (old, old, identity)
            if mode == "multiplier":
                # A type that cannot move (speed 0) stays immobile.
                changed = min(255, max(1, round(initial * value))) if initial else 0
            else:
                changed = 1 - value if mode == "inverse" else value
            self.originals[key] = (initial, changed, identity)
            if not self.proc.write_value(typ + off, changed, kind):
                raise OSError("类型属性写入失败")
            if self.proc.read_value(typ + off, kind) != changed:
                raise OSError("类型属性读回校验失败")
        return len(targets)

    def read(self, fid):
        off, kind, _lo, _hi, mode = TYPE_FEATURES[fid]
        typ = self.targets()[0]
        value = self.proc.read_value(typ + off, kind)
        if value is None:
            raise OSError("类型属性读取失败")
        if mode == "inverse":
            return 1 - value
        if mode == "multiplier":
            saved = self.originals.get((typ, off, kind))
            baseline = saved[0] if saved and saved[2] == self._identity(typ) else value
            return round(value / baseline, 3) if baseline else 0.0
        return value

    def restore(self):
        count = 0
        for key, (initial, last, identity) in list(self.originals.items()):
            typ, off, kind = key
            if self._identity(typ) == identity:
                current = self.proc.read_value(typ + off, kind)
                if current == last:
                    if not self.proc.write_value(typ + off, initial, kind):
                        raise OSError("类型属性还原失败")
                    count += 1
                elif current != initial:
                    raise RuntimeError("类型属性被外部修改，未覆盖")
            del self.originals[key]
        return count
