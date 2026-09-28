import struct
import unittest
from unittest.mock import Mock, patch

from test_core import ROOT, PE, Cs, CS_ARCH_X86, CS_MODE_32, disasm_checked, game_pe
from trainer import auto_enter


class AutoEnterTests(unittest.TestCase):
    def test_enter_stub_matches_native_abi_and_instruction_boundaries(self):
        pe = game_pe(self)
        self.assertEqual(pe.read(0x6CC2A0, 10), bytes.fromhex("81ec880000005356578b"))
        self.assertEqual(pe.read(0x7A3540 + 0x320, 4), struct.pack("<I", 0x6CC2A0))
        self.assertEqual(pe.read(0x452EB0, 10), bytes.fromhex("568bf1578b8e18040000"))
        code = auto_enter.enter_batch_code(0x12370000, 5, 0x1000400, 0x1000800)
        ins = disasm_checked(self, code, 0x1000000)
        text = "\n".join(f"{i.mnemonic} {i.op_str}".strip() for i in ins)
        self.assertLess(len(code), auto_enter.ENTER_CODE)
        # A garrison click ignores the soldier's and building's current mission.
        self.assertNotIn("0x98]", text)
        self.assertNotIn("0xa0]", text)
        self.assertIn("[edx + 0x564]", text)
        self.assertIn("cmp eax, 0x12370000", text)  # player unchanged since the click
        self.assertIn("add eax, dword ptr [ebx]", text)  # shared reservation counter
        self.assertIn("inc dword ptr [ebx]", text)
        self.assertIn("push 0\npush dword ptr [esi + 8]\npush 0\npush 8", text)
        self.assertIn("call dword ptr [eax + 0x320]", text)
        self.assertIn("call eax\ntest al, al", text)

    def test_deselection_does_not_release_pending_capacity(self):
        p = Mock()
        me, neutral = 0x100000, 0x200000
        first, second, building = 0x300000, 0x310000, 0x400000
        inf_type, build_type = 0x500000, 0x600000
        mission = {first: 5, second: 5}
        ints = {building + 0x1220: 1, building + 0x564: 0,
                first + 0xA0: -1, second + 0xA0: -1}
        def read_u32(a):
            if a in (first, second):
                return auto_enter.INFANTRY_VT
            if a in (first + 0x10, second + 0x10):
                return 10 if a == first + 0x10 else 11
            if a == building + 0x10:
                return 20
            if a == auto_enter.FRAME:
                return 100
            if a in (first + 0x1B4, second + 0x1B4):
                return me
            if a == building + 0x1B4:
                return neutral
            if a == neutral + 0x34:
                return 0x700000
            return 0
        def read_i32(a):
            if a in (first + 0x98, second + 0x98):
                return mission[a - 0x98]
            if a == build_type + 0x1220:
                return 1
            return ints.get(a, 0)
        def read_u8(a):
            if a in (first + 0x7D, second + 0x7D):
                return 1
            if a in (inf_type + 0xC08, build_type + 0x121E, 0x700000 + auto_enter.CIVILIAN_FLAG):
                return 1
            return 0
        p.read_u32.side_effect = read_u32
        p.read_i32.side_effect = read_i32
        p.read_u8.side_effect = read_u8
        p.read.side_effect = lambda a, n: struct.pack("<iii", 10000, 10000, 0) if n == 12 else None
        operations = Mock()
        c = auto_enter.AutoEnterController(p, operations)
        c._validated = True
        c._order_batch = Mock(side_effect=lambda plans, _res, _house: [1] * len(plans))
        with patch.object(auto_enter.units, "player_house", return_value=me), \
             patch.object(auto_enter.units, "iter_technos", return_value=[
                 (first, auto_enter.INFANTRY_VT), (second, auto_enter.INFANTRY_VT),
                 (building, auto_enter.BUILDING_VT)]), \
             patch.object(auto_enter.units, "valid_techno", return_value=True), \
             patch.object(auto_enter.units, "type_of", side_effect=lambda _p, a, _vt:
                          build_type if a == building else inf_type):
            self.assertEqual(c.execute((me, [(first, 10)])), (1, 1))
            mission[first] = auto_enter.ENTER_MISSION
            self.assertEqual(c.execute((me, [(second, 11)])), (0, 1))
            self.assertEqual(c.execute((me, [(first, 10)])), (0, 1))
            self.assertEqual(c._order_batch.call_count, 1)

    def test_selected_snapshot_is_captured_before_background_work(self):
        p = Mock()
        me, own, enemy = 0x100000, 0x200000, 0x300000
        p.read_i32.return_value = 2
        p.read_u32.side_effect = lambda a: {
            auto_enter.SELECTED_DATA: 0x400000,
            own: auto_enter.INFANTRY_VT, enemy: auto_enter.INFANTRY_VT,
            own + 0x1B4: me, enemy + 0x1B4: 0xDEAD,
            own + 0x10: 17, enemy + 0x10: 18,
        }.get(a)
        p.read.return_value = struct.pack("<II", own, enemy)
        c = auto_enter.AutoEnterController(p, Mock())
        with patch.object(auto_enter.units, "player_house", return_value=me):
            self.assertEqual(c.snapshot_selected(), (me, [(own, 17)]))

    def test_single_execution_is_not_limited_to_eight_soldiers(self):
        p, operations = Mock(), Mock()
        me, neutral, building = 0x100000, 0x200000, 0x800000
        soldiers = [0x300000 + i * 0x1000 for i in range(12)]
        snapshot = (me, [(obj, 100 + i) for i, obj in enumerate(soldiers)])
        p.read_u32.side_effect = lambda a: (
            auto_enter.FRAME and 500 if a == auto_enter.FRAME else
            auto_enter.INFANTRY_VT if a in soldiers else
            next((100 + i for i, obj in enumerate(soldiers) if a == obj + 0x10),
                 200 if a == building + 0x10 else
                 me if any(a == obj + 0x1B4 for obj in soldiers) else
                 neutral if a == building + 0x1B4 else
                 0x700000 if a == neutral + 0x34 else 0))
        p.read_i32.side_effect = lambda a: (
            12 if a == 0xA00000 + 0x1220 else
            0 if a == building + 0x564 else
            -1 if any(a == obj + 0xA0 for obj in soldiers) else
            5 if any(a == obj + 0x98 for obj in soldiers) else 0)
        p.read_u8.side_effect = lambda a: (
            1 if a in (0xB00000 + 0xC08, 0xA00000 + 0x121E, 0x700000 + auto_enter.CIVILIAN_FLAG) or
            any(a == obj + 0x7D for obj in soldiers) else 0)
        p.read.return_value = struct.pack("<iii", 10000, 10000, 0)
        c = auto_enter.AutoEnterController(p, operations)
        c._validated = True
        c._order_batch = Mock(side_effect=lambda plans, _res, _house: [1] * len(plans))
        with patch.object(auto_enter.units, "player_house", return_value=me), \
             patch.object(auto_enter.units, "iter_technos", return_value=[
                 (building, auto_enter.BUILDING_VT)]), \
             patch.object(auto_enter.units, "valid_techno", return_value=True), \
             patch.object(auto_enter.units, "type_of", side_effect=lambda _p, obj, _vt:
                          0xA00000 if obj == building else 0xB00000):
            self.assertEqual(c.execute(snapshot), (12, 12))
        # All twelve go out in one game-frame command.
        self.assertEqual(c._order_batch.call_count, 1)
        self.assertEqual(len(c._order_batch.call_args[0][0]), 12)


class FakeGame:
    """One player soldier and a set of buildings: {addr: (owner, x_cell, y_cell)}."""
    ME, CIV_TYPE, ENEMY_TYPE = 0x100000, 0x700000, 0x710000
    SOLDIER, INF_TYPE, BUILD_TYPE = 0x300000, 0x500000, 0x600000

    def __init__(self, buildings, soldier_mission=2):
        self.buildings = buildings
        self.mission = soldier_mission
        p = self.p = Mock()
        p.read_u32.side_effect = self.u32
        p.read_i32.side_effect = self.i32
        p.read_u8.side_effect = self.u8
        p.read.side_effect = self.read

    def u32(self, a):
        s = self.SOLDIER
        if a == s:
            return auto_enter.INFANTRY_VT
        if a == s + 0x10:
            return 7
        if a == s + 0x1B4:
            return self.ME
        if a == auto_enter.FRAME:
            return 100
        for b, (owner, _x, _y) in self.buildings.items():
            if a == b + 0x10:
                return b >> 12
            if a == b + 0x1B4:
                return owner
        return {0x200000 + 0x34: self.CIV_TYPE, 0x210000 + 0x34: self.CIV_TYPE,
                0x220000 + 0x34: self.ENEMY_TYPE}.get(a, 0)

    def i32(self, a):
        if a == self.SOLDIER + 0x98:
            return self.mission
        if a == self.BUILD_TYPE + 0x1220:
            return 5
        return 0

    def u8(self, a):
        if a in (self.SOLDIER + 0x7D, self.INF_TYPE + 0xC08, self.BUILD_TYPE + 0x121E,
                 self.CIV_TYPE + auto_enter.CIVILIAN_FLAG):
            return 1
        return 0

    def read(self, a, n):
        if n != 12:
            return None
        if a == self.SOLDIER + 0x88:
            return struct.pack("<iii", 10 << 8, 10 << 8, 0)
        for b, (_owner, x, y) in self.buildings.items():
            if a == b + 0x88:
                return struct.pack("<iii", x << 8, y << 8, 0)
        return None

    def run(self, answer=1):
        c = auto_enter.AutoEnterController(self.p, Mock())
        c._validated = True
        c._order_batch = Mock(side_effect=lambda plans, _res, _house: [answer] * len(plans))
        operations = c._order_batch
        technos = [(b, auto_enter.BUILDING_VT) for b in self.buildings]
        with patch.object(auto_enter.units, "player_house", return_value=self.ME), \
             patch.object(auto_enter.units, "iter_technos", return_value=technos), \
             patch.object(auto_enter.units, "valid_techno", return_value=True), \
             patch.object(auto_enter.units, "type_of", side_effect=lambda _p, a, _vt:
                          self.BUILD_TYPE if a in self.buildings else self.INF_TYPE):
            result = c.execute((self.ME, [(self.SOLDIER, 7)]))
        return result, operations, c


class GarrisonRuleTests(unittest.TestCase):
    def test_moving_soldier_enters_any_civilian_house_building(self):
        # Owner is a civilian house not named "Neutral" (e.g. Special); soldier is moving.
        game = FakeGame({0x800000: (0x210000, 22, 18)}, soldier_mission=2)
        (sent, total), operations, _c = game.run()
        self.assertEqual((sent, total), (1, 1))
        plans = operations.call_args[0][0]
        self.assertEqual(plans[0][2][0][0], 0x800000)

    def test_enemy_building_is_never_chosen(self):
        game = FakeGame({0x800000: (0x220000, 12, 12)})
        (sent, _total), operations, c = game.run()
        self.assertEqual(sent, 0)
        operations.assert_not_called()
        self.assertIn("没有有空位", c.last_note)

    def test_nearest_in_range_and_note_for_far_buildings(self):
        near_far = FakeGame({0x800000: (0x200000, 60, 60), 0x900000: (0x200000, 35, 12)})
        (sent, _), operations, _c = near_far.run()
        self.assertEqual(sent, 1)
        self.assertEqual(operations.call_args[0][0][0][2], [(0x900000, 0x900)])  # far one out of range
        far = FakeGame({0x800000: (0x200000, 60, 60)})
        (sent, _), operations, c = far.run()
        self.assertEqual(sent, 0)
        self.assertIn("50 格外", c.last_note)

    def test_native_refusal_is_reported(self):
        game = FakeGame({0x800000: (0x200000, 12, 12)})
        (sent, total), _operations, c = game.run(answer=0)
        self.assertEqual((sent, total), (0, 1))
        self.assertIn("被游戏拒绝", c.last_note)

if __name__ == "__main__":
    unittest.main()
