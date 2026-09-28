"""Campaign helpers: frozen mission timer, instant win and mission select.

Mission timer: ScenarioClass ([0xA3D290]) + 0x7C8 is a countdown
{+0 start frame (-1 = stopped), +8 frames left at start}. Trigger actions
23..27 start/stop/extend/shorten/set it (0x6AE652..); the HUD (0x6A5061)
draws it only while running and the per-frame logic 0x53D890 (single caller
0x540504) fires "timer expired" when time left reaches 0. The hook at that
function's entry keeps time left constant while the timer runs; when a
trigger changes it, the new value becomes the frozen one.

Instant win: trigger action 1 ("Winner is") calls HouseClass::Win(bool)
0x4E7EE0 (thiscall, ret 4) on the player; the trainer makes the same call on
the simulation thread.

Mission select: campaigns from BATTLE*.INI live in DynamicVectorClass
0xA35D64 (count 0xA35D70); each entry has its ID at +0x24 and the first
scenario char[0x200] at +0x9C, read when the campaign starts (0x5142BA).
Rewriting ALL1 / SOV1 makes the campaign buttons start the chosen mission.
"""
import struct

from .addresses import FRAME, PLAYER_PTR
from .executor import relative_jump
from .hooks import HookSites, assemble
from .operations import X86

SCENARIO_PTR = 0xA3D290
MISSION_TIMER = 0x7C8
FRAME_ENTRY = 0x53D890
FRAME_ORIGINAL = bytes.fromhex("83ec288b1518eba600")
HOUSE_WIN = 0x4E7EE0
CODE_OFFSET = 0x100
# block settings: +0 frozen on, +4 captured, +8 frozen frames left, +C last written

CAMPAIGN_ITEMS = 0xA35D64
CAMPAIGN_COUNT = 0xA35D70
CAMPAIGN_ID = 0x24
CAMPAIGN_SCENARIO = 0x9C
SCENARIO_SIZE = 0x200
# Official order from MISSION.INI / BATTLE.INI (ra2.mix -> local.mix).
MISSIONS = {
    "ALL1": ["ALL01T", "ALL02S", "ALL03U", "ALL04U", "ALL05S", "ALL06U",
             "ALL07T", "ALL08U", "ALL09T", "ALL10S", "ALL11T", "ALL12S"],
    "SOV1": ["SOV01T", "SOV02T", "SOV03U", "SOV04S", "SOV05U", "SOV06T",
             "SOV07S", "SOV08U", "SOV09U", "SOV10T", "SOV11S", "SOV12S"],
}


def freeze_code(base):
    """Entry of the per-frame logic; ECX is live and every register is restored."""
    on, captured, frozen, last = base, base + 4, base + 8, base + 0xC
    a = X86()
    a.emit("9c 50 51 52")
    a.emit("83 3d")
    a.code += struct.pack("<I", on) + b"\x00"
    a.jump("0f 84", "done")
    a.imm("8b 15", SCENARIO_PTR)  # EDX = Scenario
    a.emit("85 d2")
    a.jump("0f 84", "done")
    a.emit("8b 8a")
    a.code += struct.pack("<I", MISSION_TIMER)  # ECX = start frame
    a.emit("83 f9 ff")
    a.jump("0f 85", "running")
    a.emit("c7 05")
    a.code += struct.pack("<II", captured, 0)  # stopped: recapture when restarted
    a.jump("e9", "done")
    a.label("running")
    a.emit("83 3d")
    a.code += struct.pack("<I", captured) + b"\x00"
    a.jump("0f 84", "capture")
    a.emit("8b 82")
    a.code += struct.pack("<I", MISSION_TIMER + 8)
    a.imm("3b 05", last)
    a.jump("0f 84", "hold")  # unchanged since our last write
    a.label("capture")  # new timer, or a trigger extended/shortened/set it
    a.imm("a1", FRAME)
    a.emit("29 c8")  # elapsed = frame - start
    a.emit("f7 d8")
    a.emit("03 82")
    a.code += struct.pack("<I", MISSION_TIMER + 8)  # left = time - elapsed
    a.jump("0f 89", "left_ok")
    a.emit("31 c0")
    a.label("left_ok")
    a.imm("a3", frozen)
    a.emit("c7 05")
    a.code += struct.pack("<II", captured, 1)
    a.label("hold")
    a.imm("a1", FRAME)
    a.emit("29 c8")  # elapsed
    a.imm("03 05", frozen)  # time = frozen + elapsed, so left stays frozen
    a.emit("89 82")
    a.code += struct.pack("<I", MISSION_TIMER + 8)
    a.imm("a3", last)
    a.label("done")
    a.emit("5a 59 58 9d")
    a.code += FRAME_ORIGINAL
    code = a.finish()
    at = base + CODE_OFFSET
    return code + relative_jump(at + len(code), FRAME_ENTRY + len(FRAME_ORIGINAL))


def build_block(base):
    return assemble(0x1000, [(CODE_OFFSET, freeze_code(base))], "任务计时")


class CampaignController:
    def __init__(self, proc, executor):
        self.proc, self.executor = proc, executor
        self.hooks = HookSites(proc, "任务计时", {"frame": (FRAME_ENTRY, FRAME_ORIGINAL, CODE_OFFSET)},
                               build_block)
        # campaign entry address -> (id, original bytes, names this trainer may have written)
        self.scenario_originals = {}
        self.starts = {}

    # ---- mission timer ----
    @property
    def installed(self):
        return self.hooks.installed

    def set_freeze(self, on):
        if on:
            self.hooks.prepare()
            if self.proc.read_u32(self.hooks.base) != 1:
                if not self.proc.write(self.hooks.base, struct.pack("<II", 1, 0)):
                    raise OSError("任务计时设置写入失败")
            self.hooks.apply({"frame"})
        else:
            self.hooks.close()
            if self.hooks.base is not None:
                self.proc.write(self.hooks.base, bytes(16))

    def timer_left(self):
        """Frames left on the running mission timer, or None when none runs."""
        scenario = self.proc.read_u32(SCENARIO_PTR)
        if not scenario:
            return None
        raw = self.proc.read(scenario + MISSION_TIMER, 12)
        if not raw:
            return None
        start, _clock, time_left = struct.unpack("<iii", raw)
        if start == -1:
            return None
        frame = self.proc.read_i32(FRAME) or 0
        return max(0, time_left - (frame - start))

    # ---- instant win ----
    def win(self):
        house = self.proc.read_u32(PLAYER_PTR)
        if not house:
            raise RuntimeError("当前不在战局中")
        if self.proc.read(house + 0x13E, 3) != b"\x00\x00\x00":
            raise RuntimeError("本局已经结束")
        self.executor.call(HOUSE_WIN, this=house, args=(0,))
        return True

    # ---- mission select ----
    def _campaigns(self):
        items = self.proc.read_u32(CAMPAIGN_ITEMS)
        count = self.proc.read_i32(CAMPAIGN_COUNT)
        if not items or count is None or not 0 < count <= 256:
            raise RuntimeError("未找到游戏战役列表")
        blob = self.proc.read(items, count * 4)
        if blob is None:
            raise OSError("无法读取游戏战役列表")
        out = {}
        for (entry,) in struct.iter_unpack("<I", blob):
            ident = self.proc.read_cstr(entry + CAMPAIGN_ID, 25) if entry else None
            if ident in MISSIONS:
                out[ident] = entry
        return out

    def set_start(self, campaign, number):
        """Make the campaign button start mission `number` (1 = original)."""
        names = MISSIONS[campaign]
        if type(number) is not int or not 1 <= number <= len(names):
            raise ValueError(f"请输入1到{len(names)}之间的关卡号")
        entry = self._campaigns().get(campaign)
        if not entry:
            raise RuntimeError("未找到该阵营的战役")
        current = self.proc.read(entry + CAMPAIGN_SCENARIO, 32)
        if current is None:
            raise OSError("无法读取战役起始关卡")
        if entry not in self.scenario_originals:
            original = current.split(b"\0", 1)[0]
            if not original.upper().endswith(b".MAP"):
                raise RuntimeError("战役起始关卡格式不符，未修改")
            self.scenario_originals[entry] = (campaign, original, ())
        wanted = (names[number - 1] + ".MAP").encode()
        _campaign, original, written = self.scenario_originals[entry]
        if not self.proc.write(entry + CAMPAIGN_SCENARIO, wanted + b"\0"):
            # The outcome is unknown: accept either name when restoring.
            self.scenario_originals[entry] = (campaign, original, written + (wanted,))
            raise OSError("战役起始关卡写入失败")
        self.scenario_originals[entry] = (campaign, original, (wanted,))
        self.starts[campaign] = number
        return names[number - 1]

    def start_of(self, campaign):
        entry = self._campaigns().get(campaign)
        raw = self.proc.read(entry + CAMPAIGN_SCENARIO, 32) if entry else None
        if not raw:
            raise RuntimeError("未找到该阵营的战役")
        name = raw.split(b"\0", 1)[0].decode("latin1").upper()
        stem = name[:-4] if name.endswith(".MAP") else name
        names = MISSIONS[campaign]
        return names.index(stem) + 1 if stem in names else 1

    def restore_starts(self):
        """Restore each campaign independently; keep failed records and report the first."""
        error = None
        for entry, (campaign, original, written) in list(self.scenario_originals.items()):
            try:
                if self.proc.read_cstr(entry + CAMPAIGN_ID, 25) == campaign:
                    raw = self.proc.read(entry + CAMPAIGN_SCENARIO, 32)
                    if raw is None:
                        raise OSError("无法读取战役起始关卡，未还原")
                    current = raw.split(b"\0", 1)[0]
                    if current != original:
                        if current not in written:
                            raise RuntimeError("战役起始关卡被外部修改，未覆盖")
                        if (not self.proc.write(entry + CAMPAIGN_SCENARIO, original + b"\0")
                                or self.proc.read(entry + CAMPAIGN_SCENARIO, len(original) + 1)
                                != original + b"\0"):
                            raise OSError("战役起始关卡还原失败")
                del self.scenario_originals[entry]  # restored, already original, or entry gone
                self.starts.pop(campaign, None)
            except Exception as exc:
                error = error or exc
        if error:
            raise error
        self.starts.clear()

    def close(self):
        error = None
        try:
            self.set_freeze(False)
        except Exception as exc:
            error = exc
        try:
            self.restore_starts()
        except Exception as exc:
            error = error or exc
        if error:
            raise error
