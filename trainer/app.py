"""红警2 修改器 —— PySide6 主界面。

用法：python main.py
"""
import json
import math
import os
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QLineEdit,
    QPlainTextEdit, QScrollArea, QVBoxLayout, QHBoxLayout, QFrame,
    QInputDialog, QKeySequenceEdit, QMessageBox,
)

from .mem import Process
from . import addresses as A
from . import units
from .features import FEATURES, SECTIONS
from .patches import PatchManager
from .operations import GameOperations
from .typeedit import TypeEditor, TYPE_FEATURES
from .power import PowerController
from .production import ProductionController, FEATURES as PRODUCTION_FEATURES
from .protection import ProtectionController
from .build_unlock import BuildUnlockController
from .anti_stealth import AntiStealthController
from .all_target import AllTargetController
from .auto_enter import AutoEnterController
from .tank_repair import TankRepairController
from .chrono_landing import ChronoLandingController
from .water_walk import WaterWalkController
from .airport_slots import AirportSlotsController
from .techno_ai import TechnoAIController
from .psychic import PsychicScanController
from .campaign import CampaignController
from .economy import EconomyController
from .weapons import WeaponController
from .psionic import PsionicShieldController
from .jobs import Jobs
from .hotkeys import Hotkeys, HotkeyDialog, foreground_pid
from . import runtime

APP_NAME = "RA2修改器"
APP_VER = "v" + runtime.VERSION
GAME_DIR = str(runtime.game_dir())
STATE_FILE = str(runtime.app_dir() / ".state.json")
BUSY_MESSAGE = "后台操作进行中，请稍后重试"
SAVED_MARK = "_saved_by_user"  # only files from the save button restore feature state

# Per-connection controllers in creation order: (attribute, factory(window)).
CONTROLLERS = (
    ("patches", lambda w: PatchManager(w.proc)),
    ("operations", lambda w: GameOperations(w.proc)),
    ("type_editor", lambda w: TypeEditor(w.proc)),
    ("power", lambda w: PowerController(w.proc, w.operations.executor)),
    ("production", lambda w: ProductionController(w.proc)),
    ("protection", lambda w: ProtectionController(w.proc)),
    ("build_unlock", lambda w: BuildUnlockController(w.proc)),
    ("anti_stealth", lambda w: AntiStealthController(w.proc)),
    ("all_target", lambda w: AllTargetController(w.proc)),
    ("auto_enter", lambda w: AutoEnterController(w.proc, w.operations)),
    ("tank_repair", lambda w: TankRepairController(w.proc)),
    ("chrono_landing", lambda w: ChronoLandingController(w.proc)),
    ("water_walk", lambda w: WaterWalkController(w.proc)),
    ("airport_slots", lambda w: AirportSlotsController(w.proc, w.operations.executor)),
    ("techno_ai", lambda w: TechnoAIController(w.proc, w.repair_amount)),
    ("psychic", lambda w: PsychicScanController(w.proc)),
    ("campaign", lambda w: CampaignController(w.proc, w.operations.executor)),
    ("economy", lambda w: EconomyController(w.proc)),
    ("weapons", lambda w: WeaponController(w.proc)),
    ("psionic", lambda w: PsionicShieldController(w.proc)),
)

# Restore order on exit. build_unlock goes first (it may refresh the sidebar
# through the executor); the executor itself closes with operations.
CLOSE_ORDER = (
    ("科技/建造", "build_unlock"), ("三星与建筑修理", "techno_ai"), ("心灵探测", "psychic"),
    ("战役", "campaign"), ("经济", "economy"), ("武器", "weapons"), ("反心灵控制", "psionic"),
    ("反隐身", "anti_stealth"), ("全目标攻击", "all_target"),
    ("自动进驻", "auto_enter"), ("坦克自动维修", "tank_repair"),
    ("时空兵快速落地", "chrono_landing"), ("水面行走", "water_walk"),
    ("机场机位", "airport_slots"), ("建造", "production"), ("无敌", "protection"),
    ("无限电力", "power"), ("单位操作", "operations"), ("补丁", "patches"),
)


def _set_fid(controller, fid, on):
    controller.set(fid, on)


def _set_on(controller, _fid, on):
    controller.set(on)


def _enable(controller, _fid, on):
    controller.enable() if on else controller.disable()


def _freeze(controller, _fid, on):
    controller.set_freeze(on)


# Code-hook switches: fid -> (controller attribute, apply, log suffix, error label).
HOOK_SWITCHES = {
    "tech_all": ("build_unlock", _set_fid, "（仅己方）", "科技或建造"),
    "unlock_build": ("build_unlock", _set_fid, "（仅己方）", "科技或建造"),
    "anti_stealth": ("anti_stealth", _set_on, "（仅己方）", "反隐身"),
    "psy_scan": ("psychic", _set_on, "（全图显示非盟军单位的行动路线）", "心灵探测"),
    "mission_timer_freeze": ("campaign", _freeze, "（保持当前剩余时间）", "冻结任务倒计时"),
    "ore_infinite": ("economy", _set_fid, "（仅己方矿车）", "矿石无限"),
    "repair_free": ("economy", _set_fid, "（己方建筑与维修厂）", "修理免费"),
    "sell_full": ("economy", _set_fid, "（己方）", "出售全额退款"),
    "anti_mind_control": ("psionic", _set_on, "（己方单位和建筑不会被尤里控制）", "反尤里控制"),
    "u_all_target": ("all_target", _set_on, "（仅己方，遵守武器射程）", "全目标攻击"),
    "tank_auto_repair": ("tank_repair", _enable,
                         "（己方可维修地面战车，原版维修厂速度）", "坦克自动维修"),
    "chrono_quick_land": ("chrono_landing", _enable, "（仅己方传送后的落地等待）", "时空兵快速落地"),
    "water_walk": ("water_walk", _enable, "（己方；关后在水上或海滩的单位可返岸）", "水面行走"),
    "invincible": ("protection", _enable, "（己方单位和建筑）", "防护"),
    "u_vet3": ("techno_ai", _set_fid, "（己方载具/步兵/飞机，每帧生效）", "我军升三星"),
    "auto_repair": ("techno_ai", _set_fid, "（己方建筑，每帧检查）", "自动修理建筑"),
    "infinite_power": ("power", _enable, "（已同步电力和小地图状态）", "电力"),
    **{fid: ("production", _set_fid, "（仅己方）", "建造") for fid in PRODUCTION_FEATURES},
}
# Multipliers kept by hook controllers: fid -> (controller, setter, current attr, label).
HOOK_VALUES = {
    "ore_income": ("economy", "set_income", "income", "采矿收入倍率"),
    "rof_mult": ("weapons", "set_rof", "rof", "己方射速倍率"),
    "range_mult": ("weapons", "set_range", "range", "己方射程倍率"),
}
CAMPAIGN_VALUES = {"campaign_all": ("ALL1", "盟军"), "campaign_sov": ("SOV1", "苏军")}
# Built-in one-click profiles; user profiles are stored under "_profiles".
BUILTIN_PROFILES = {
    "全部关闭": {"enabled": {}, "values": {}},
    "战役轻松档": {
        "enabled": {"money_no_decrease": True, "infinite_power": True, "auto_repair": True,
                    "tank_auto_repair": True, "repair_free": True, "mission_timer_freeze": True},
        "values": {"ore_income": 2.0},
    },
    "遭遇战纯享档": {
        "enabled": {"fast_build": True, "unlimited_queue": True, "infinite_power": True,
                    "auto_repair": True, "u_vet3": True, "sell_full": True, "ore_infinite": True},
        "values": {"ore_income": 2.0, "rof_mult": 1.5},
    },
}
# Switches whose hook may stay live even when the call failed afterwards.
REPORTS_STATE = {"invincible", "infinite_power"}


def write_fields(proc, targets, fields, value, house):
    """Write value into each target's fields; return how many were fully written.

    Every target is rechecked (vtable, HP and owner) right before the write;
    an unknown house (None/0) writes nothing."""
    n = 0
    for a, vt in targets:
        if not units.valid_techno(proc, a, vt, house):
            continue
        ok = True
        for off, kind in fields:
            ok = proc.write_value(a + off, float(value) if kind.startswith("f") else int(value),
                                  kind) and ok
        n += ok
    return n


def jobs_busy(window):
    if getattr(window, "jobs", None) and window.jobs.busy:
        window.log(BUSY_MESSAGE)
        return True
    return False

C = dict(
    bg="#141018", panel="#1d161f", panel2="#241a24", line="#3a2b39",
    txt="#eadfe7", dim="#9b8b96", accent="#ff4d6d", accent2="#7de2d1",
    on="#ff4d6d", off="#3a2b39", ok="#5ad18b", warn="#ffb454",
)

QSS = f"""
QMainWindow, QWidget#root {{ background:{C['bg']}; color:{C['txt']}; }}
QLabel {{ background:transparent; }}
QLabel#title {{ font-size:26px; font-weight:800; color:{C['accent']}; }}
QLabel#badge {{ background:{C['accent']}; color:#1a1016; border-radius:8px; padding:2px 8px; font-size:11px; }}
QLabel#sub {{ color:{C['dim']}; font-size:12px; }}
QLabel#sec {{ color:{C['accent']}; font-size:15px; font-weight:700; padding:6px 4px; }}
QLabel#name {{ font-size:14px; color:{C['txt']}; }}
QLabel#name:disabled {{ color:#6d5f68; }}
QLabel#info {{ color:{C['dim']}; font-size:11px; }}
QFrame#card {{ background:{C['panel2']}; border:1px solid {C['line']}; border-radius:10px; }}
QFrame#side {{ background:{C['panel']}; border-right:1px solid {C['line']}; }}
QFrame#topbar {{ background:{C['panel']}; border-bottom:1px solid {C['line']}; }}
QPushButton {{ background:#2b2030; color:{C['txt']}; border:1px solid {C['line']};
  border-radius:7px; padding:6px 14px; font-size:13px; }}
QPushButton:hover {{ border-color:{C['accent']}; color:{C['accent']}; }}
QPushButton:disabled {{ color:#6d5f68; background:#221a26; border-color:#2e2431; }}
QPushButton#chip {{ border-radius:14px; padding:5px 12px; font-size:12px; }}
QPushButton#chip[active="true"] {{ background:{C['accent']}; color:#1a1016; border-color:{C['accent']}; font-weight:700; }}
QPushButton#chipOff {{ border-radius:14px; padding:5px 12px; font-size:12px; background:#2b2030; }}
QPushButton#pill {{ border-radius:6px; padding:5px 16px; font-size:13px; min-width:56px; }}
QPushButton#pillOn {{ background:{C['on']}; color:#1a1016; border:1px solid {C['on']}; font-weight:700; }}
QPushButton#pillOff {{ background:#33263a; color:{C['dim']}; border:1px solid {C['line']}; }}
QPushButton#pillOn:disabled {{ background:#33263a; color:#6d5f68; border:1px solid {C['line']}; }}
QLineEdit {{ background:#2b2030; border:1px solid {C['line']}; border-radius:7px;
  padding:6px 10px; color:{C['txt']}; font-size:13px; }}
QLineEdit:focus {{ border-color:{C['accent']}; }}
QPlainTextEdit {{ background:#181320; border:1px solid {C['line']}; border-radius:8px;
  color:{C['dim']}; font-family:Consolas,monospace; font-size:11px; padding:6px; }}
QScrollArea {{ border:none; background:transparent; }}
QScrollArea > QWidget > QWidget {{ background:transparent; }}
QToolButton {{ background:transparent; border:none; color:{C['dim']}; }}
"""


# ---------------------------------------------------------------- widgets
class PillToggle(QWidget):
    """关闭/打开 双态按钮组（复刻原修改器样式）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.b_off = QPushButton("关闭")
        self.b_on = QPushButton("打开")
        for b in (self.b_off, self.b_on):
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(30)
            lay.addWidget(b)
        self.b_off.clicked.connect(lambda: self.set_checked(False, emit=True))
        self.b_on.clicked.connect(lambda: self.set_checked(True, emit=True))
        self._cb = None
        self.set_checked(False, emit=False)

    def set_checked(self, v, emit=False):
        self.b_off.setObjectName("pillOff" if v else "pillOn")
        self.b_on.setObjectName("pillOn" if v else "pillOff")
        for b in (self.b_off, self.b_on):
            b.style().unpolish(b)
            b.style().polish(b)
        self._v = v
        if emit and self._cb:
            self._cb(v)

    def isChecked(self):
        return self._v

    def onToggle(self, cb):
        self._cb = cb


class FeatureRow(QFrame):
    def __init__(self, feat, app, parent=None):
        super().__init__(parent)
        self.feat, self.app = feat, app
        self.setObjectName("card")
        self.setEnabled(False)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(14)

        # 名称区
        box = QVBoxLayout()
        box.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        name = QLabel(feat["name"])
        name.setObjectName("name")
        top.addWidget(name)
        if feat.get("info"):
            info = QLabel("ⓘ")
            info.setObjectName("info")
            info.setToolTip(feat["info"])
            top.addWidget(info)
        if feat["status"] != "ok":
            tag = QLabel("待定位")
            tag.setObjectName("info")
            tag.setStyleSheet(f"color:{C['warn']};border:1px solid {C['line']};border-radius:6px;padding:1px 6px;")
            top.addWidget(tag)
        top.addStretch(1)
        box.addLayout(top)
        if feat.get("info"):
            d = QLabel(feat["info"])
            d.setObjectName("info")
            box.addWidget(d)
        lay.addLayout(box, 1)

        # 控件区
        self.ctrl = QWidget()
        cl = QHBoxLayout(self.ctrl)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)
        self.input = None
        self.btn = None

        if feat["kind"] == "patch":
            self.toggle = PillToggle()
            self.toggle.onToggle(self._on_patch)
            cl.addWidget(self.toggle)
        elif feat["kind"] == "value":
            self.input = QLineEdit()
            self.input.setFixedWidth(110)
            self.input.setPlaceholderText("数值")
            self.btn = QPushButton("设置")
            self.btn.clicked.connect(self._on_value)
            cl.addWidget(self.input)
            cl.addWidget(self.btn)
            if feat["status"] == "ok":
                rd = QPushButton("读取")
                rd.clicked.connect(self._on_read)
                cl.insertWidget(1, rd)
        elif feat["kind"] == "action":
            self.btn = QPushButton("执行")
            self.btn.clicked.connect(self._on_action)
            cl.addWidget(self.btn)
        elif feat["kind"] == "debug":
            self.input = QLineEdit()
            self.input.setFixedWidth(150)
            self.input.setPlaceholderText("输入")
            self.btn = QPushButton("执行")
            self.btn.clicked.connect(self._on_debug)
            cl.addWidget(self.input)
            cl.addWidget(self.btn)

        lay.addWidget(self.ctrl, 0)
        self.setEnabled(feat["status"] == "ok")

    # ---- callbacks ----
    def _on_patch(self, on):
        self.app.set_patch(self.feat["id"], on)

    def _on_value(self):
        self.app.value_write(self.feat["id"], self.input.text())

    def _on_read(self):
        self.app.value_read(self.feat["id"], self.input)

    def _on_action(self):
        self.app.run_action(self.feat["id"])

    def _on_debug(self):
        self.app.run_debug(self.feat["id"], self.input.text())

    def set_toggle(self, v):
        if hasattr(self, "toggle"):
            self.toggle.set_checked(v, emit=False)


# ---------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VER} - 红警2（原版）")
        self.resize(1180, 780)
        self.proc = None
        self._detach()
        self.airport_factor = 1  # last committed setting, not the input draft
        self.repair_amount = 0
        self.hook_values = {}  # HOOK_VALUES fid -> multiplier other than 1
        self.pid = None
        self.enabled = {}          # fid -> bool
        self.rows = {}             # fid -> FeatureRow
        self.active_sections = set()
        self.keyword = ""
        self._tick_n = 0
        self._airport_failed_target = None
        self._airport_failed_player = None
        self._attach_failure = None
        self._pill_txt = self._pill2_txt = None
        self.jobs = Jobs(self)
        self.hotkeys = Hotkeys(self._hotkey_triggered,
                               lambda exc: self.log(f"快捷键执行失败：{exc}"))
        self.hotkey_mapping = {"u_clone": "Ctrl+Alt+C"}
        self._closing = False
        self._hotkey_dialog = False

        self._build()
        self._restore_state()
        self._update_hotkey_hint()
        self._install_hotkeys()
        self.hotkey_timer = QTimer(self)
        self.hotkey_timer.timeout.connect(self._sync_hotkey_focus)
        self.hotkey_timer.start(250)

        self.t_poll = QTimer(self)
        self.t_poll.timeout.connect(self.tick)
        self.t_poll.start(800)
        self.tick()

    # ---------------- UI ----------------
    def _build(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        h.addWidget(self._build_side())
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        rv.addWidget(self._build_topbar())
        rv.addWidget(self._build_filters())
        rv.addWidget(self._build_list())
        h.addWidget(right, 1)

    def _build_side(self):
        side = QFrame()
        side.setObjectName("side")
        side.setFixedWidth(250)
        v = QVBoxLayout(side)
        v.setContentsMargins(16, 16, 16, 12)
        v.setSpacing(10)

        t = QLabel(APP_NAME)
        t.setObjectName("title")
        v.addWidget(t)
        sub = QLabel("红警2 修改器控制面板")
        sub.setObjectName("sub")
        v.addWidget(sub)

        card = QFrame()
        card.setObjectName("card")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(14, 12, 14, 12)
        cv.setSpacing(3)
        n = QLabel("红色警戒2（原版）")
        n.setObjectName("name")
        n.setStyleSheet("font-weight:700;")
        cv.addWidget(n)
        e = QLabel("game.exe  ·  1.006")
        e.setObjectName("info")
        cv.addWidget(e)
        cnt = QLabel(f"{len(FEATURES)} 个功能")
        cnt.setObjectName("info")
        cv.addWidget(cnt)
        self.lbl_ver = QLabel("版本检测：--")
        self.lbl_ver.setObjectName("info")
        cv.addWidget(self.lbl_ver)
        v.addWidget(card)

        btn_launch = QPushButton("启动游戏")
        btn_launch.clicked.connect(self.launch_game)
        v.addWidget(btn_launch)
        btn_attach = QPushButton("附加进程")
        btn_attach.clicked.connect(lambda: self.tick(force=True))
        v.addWidget(btn_attach)

        for txt in ("环境检测", "保存当前功能状态", "功能方案", "快捷键设置", "关于"):
            b = QPushButton(txt)
            b.clicked.connect(lambda _, s=txt: self.side_action(s))
            v.addWidget(b)

        v.addSpacing(4)
        log_lab = QLabel("运行日志")
        log_lab.setObjectName("sub")
        v.addWidget(log_lab)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(300)
        v.addWidget(self.log_view, 1)
        return side

    def _build_topbar(self):
        bar = QFrame()
        bar.setObjectName("topbar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 10, 16, 10)
        h.setSpacing(10)
        self.pill_proc = QLabel("● 等待 game.exe")
        self.pill_proc.setStyleSheet(self._pill_style(C['dim']))
        h.addWidget(self.pill_proc)
        self.pill_patch = QLabel("● 补丁状态 --")
        self.pill_patch.setStyleSheet(self._pill_style(C['dim']))
        h.addWidget(self.pill_patch)
        h.addStretch(1)
        self.hotkey_hint = QLabel()
        self.hotkey_hint.setObjectName("info")
        h.addWidget(self.hotkey_hint)
        hotkey_button = QPushButton("快捷键设置")
        hotkey_button.clicked.connect(lambda: self.side_action("快捷键设置"))
        h.addWidget(hotkey_button)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索功能... Enter 直达")
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(self._on_search)
        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.timeout.connect(self.apply_filter)
        h.addWidget(self.search)
        return bar

    def _build_filters(self):
        w = QWidget()
        w.setContentsMargins(16, 8, 16, 4)
        wrap = QVBoxLayout(w)
        wrap.setContentsMargins(16, 8, 16, 4)
        wrap.setSpacing(8)
        lab = QLabel("分区筛选：")
        lab.setObjectName("sub")
        wrap.addWidget(lab)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.chips = {}
        for s in SECTIONS:
            b = QPushButton(s)
            b.setObjectName("chip")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _, sec=s: self._toggle_section(sec))
            self.chips[s] = b
            row.addWidget(b)
        row.addStretch(1)
        allb = QPushButton("显示全部")
        allb.setObjectName("chip")
        allb.clicked.connect(lambda: (self.active_sections.clear(), self._sync_chips(), self.apply_filter()))
        row.addWidget(allb)
        wrap.addLayout(row)
        return w

    def _build_list(self):
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        inner = QWidget()
        self.list_lay = QVBoxLayout(inner)
        self.list_lay.setContentsMargins(16, 8, 16, 24)
        self.list_lay.setSpacing(8)
        self.section_labels = {}
        self.list_inner = inner
        cur = None
        for f in FEATURES:
            if f["section"] != cur:
                cur = f["section"]
                lab = QLabel(cur)
                lab.setObjectName("sec")
                self.list_lay.addWidget(lab)
                self.section_labels.setdefault(cur, []).append(lab)
            row = FeatureRow(f, self)
            if f["id"] == "airport_slots":
                row.input.setText("1")
            self.rows[f["id"]] = row
            self.list_lay.addWidget(row)
        self.list_lay.addStretch(1)
        sa.setWidget(inner)
        return sa

    # ---------------- filters ----------------
    def _toggle_section(self, sec):
        if sec in self.active_sections:
            self.active_sections.discard(sec)
        else:
            self.active_sections.add(sec)
        self._sync_chips()
        self.apply_filter()

    def _sync_chips(self):
        for s, b in self.chips.items():
            b.setProperty("active", "true" if s in self.active_sections else "false")
            b.style().unpolish(b)
            b.style().polish(b)

    def _on_search(self, text):
        self.keyword = text.strip().lower()
        self.filter_timer.start(100)

    def apply_filter(self):
        self.list_inner.setUpdatesEnabled(False)
        try:
            counts = {sec: 0 for sec in self.section_labels}
            for row in self.rows.values():
                f = row.feat
                visible = (not self.active_sections or f["section"] in self.active_sections)
                visible = visible and (not self.keyword or self.keyword in f["name"].lower())
                counts[f["section"]] += int(visible)
                if row.isHidden() == visible:
                    row.setVisible(visible)
            for sec, labels in self.section_labels.items():
                for label in labels:
                    visible = counts[sec] > 0
                    if label.isHidden() == visible:
                        label.setVisible(visible)
        finally:
            self.list_inner.setUpdatesEnabled(True)

    # ---------------- process ----------------
    def tick(self, force=False):
        if self._closing or self.jobs.busy:
            return
        if self.proc is None:
            pids = Process.find_pids(["game.exe"])
            if pids:
                self.pid, name = pids[0]
                try:
                    self._attach()
                    self.log(f"已附加 {name} (pid={self.pid})")
                    for attr in ("water_walk", "airport_slots"):
                        error = getattr(getattr(self, attr), "adopt_error", None)
                        if isinstance(error, str):
                            self.log(error)
                    self.check_signatures()
                    self._adopt_saved_patches()
                    self.reapply_all()
                except Exception as e:
                    message = f"附加失败: {e}"
                    if (self.pid, message) != self._attach_failure:
                        self._attach_failure = (self.pid, message)
                        self.log(message + "（修改器会继续重试，相同原因不再重复提示）")
                    if self.proc:
                        self.proc.close()
                    self.proc = None
                    self._detach()
            self._update_pill()
            return
        # 已附加：减负——每 5 个周期(约4s)才做一次存活检查
        self._tick_n += 1
        if force or self._tick_n % 5 == 0:
            alive = any(p == self.pid for p, _ in Process.find_pids(["game.exe"]))
            if not alive:
                self.log("游戏进程已退出")
                self.proc.close()
                self.proc = None
                self._detach()
                self._update_pill()
                return
        self.reapply_all()

    def _attach(self):
        self.proc = Process(self.pid)
        for attr, factory in CONTROLLERS:
            setattr(self, attr, factory(self))
        self._airport_failed_target = None
        self._airport_failed_player = None
        self._attach_failure = None

    def _detach(self):
        for attr, _factory in CONTROLLERS:
            setattr(self, attr, None)
        self._airport_failed_target = None

    def _adopt_saved_patches(self):
        """Own bytes a previous, abnormally closed session left for saved-on switches."""
        if not self.patches.compatible():
            return
        for fid, on in self.enabled.items():
            if on and fid in A.PATCHES:
                try:
                    self.patches.enable(fid, adopt=True)
                except Exception as exc:
                    self.log(f"{A.PATCHES[fid]['name']}: {exc}")

    def _pill_style(self, color):
        return (f"color:{color};background:#241a24;border:1px solid {C['line']};"
                f"border-radius:14px;padding:6px 14px;font-size:12px;")

    def _update_pill(self):
        if self.proc:
            txt = f"● 已连接 game.exe (pid={self.pid})"
            color = C['accent2']
        else:
            txt = "● 等待 game.exe"
            color = C['dim']
        if self._pill_txt != txt:
            self._pill_txt = txt
            self.pill_proc.setText(txt)
            self.pill_proc.setStyleSheet(self._pill_style(color))
        on = [f for f, v in self.enabled.items() if v]
        if on:
            t2, col2 = f"● {len(on)} 个功能已开启", C['accent']
        else:
            t2, col2 = "● 补丁状态：无", C['dim']
        if self._pill2_txt != t2:
            self._pill2_txt = t2
            self.pill_patch.setText(t2)
            self.pill_patch.setStyleSheet(self._pill_style(col2))

    # ---------------- patches ----------------
    def can_write(self):
        if not self.proc:
            self.log("未附加游戏进程，无法修改")
            return False
        if not self.patches or not self.patches.compatible():
            self.log("版本或补丁特征不匹配，已停止修改")
            return False
        return True

    def _switch_failed(self, fid, controller, fallback):
        """Show what is actually installed after a failed switch."""
        state = controller.installed if fid in REPORTS_STATE else fallback
        self.enabled[fid] = state
        self.rows[fid].set_toggle(state)

    def _refresh_sidebar_later(self):
        self.jobs.submit(
            lambda: self.build_unlock.refresh_sidebar(self.operations.executor),
            lambda _result, error: self.log(
                f"生产侧栏刷新未完成：{error}" if error else "已请求游戏刷新生产侧栏"))

    def _flush_power_later(self):
        power = self.power
        if power and power.pending_refresh:
            self.jobs.submit(power.flush_refresh, lambda _r, error: error and self.log(str(error)))

    def set_patch(self, fid, on, background_refresh=True):
        """background_refresh=False leaves the sidebar/power refresh to the next
        reapply_all instead of submitting a job (a busy job would make every
        later switch of the same batch bail out)."""
        if jobs_busy(self):
            self.rows[fid].set_toggle(self.enabled.get(fid, False))
            return
        if not self.can_write():
            self.rows[fid].set_toggle(False)
            return
        switch = HOOK_SWITCHES.get(fid)
        if switch:
            attr, apply, note, label = switch
            controller = getattr(self, attr)
            try:
                apply(controller, fid, on)
                self.enabled[fid] = on
                self.log(f"[{'开' if on else '关'}] {self.rows[fid].feat['name']}{note}")
                if attr == "build_unlock" and background_refresh:
                    self._refresh_sidebar_later()
                elif attr == "power" and background_refresh:
                    self._flush_power_later()
            except Exception as exc:
                self._switch_failed(fid, controller, self.enabled.get(fid, False))
                self.log(f"{label}操作未完成：{exc}")
            self._update_pill()
            return
        loop = A.LOOP_ACTIONS.get(fid)
        if loop:
            if on:
                self.enabled[fid] = True
                self.jobs.submit(
                    lambda: self._apply_loop(loop, force_scan=True),
                    lambda count, error: self._loop_initial_finished(fid, loop, count, error))
                self.log(f"[开] {loop['name']}（正在刷新单位）")
            else:
                self.log(f"[关] {loop['name']}（已停止刷新，已产生的效果保留）")
                self.enabled[fid] = False
            self._update_pill()
            return
        spec = A.PATCHES.get(fid)
        if not spec:
            return
        try:
            self.patches.enable(fid) if on else self.patches.disable(fid)
        except Exception as exc:
            self.log(f"{spec['name']}: {exc}")
            self.rows[fid].set_toggle(self.enabled.get(fid, False))
            return
        self.enabled[fid] = on
        self.log(f"[{'开' if on else '关'}] {spec['name']}")
        self._update_pill()

    def _apply_loop(self, spec, force_scan=False):
        """周期性地把某个字段写进己方单位（用于「我军升三星」这类持续功能）。"""
        if spec.get("op") == "charge_supers":
            n = 0
            for a in units.player_supers(self.proc):
                if self.proc.read_u8(a + 0x55) == 1 and self.proc.read_u8(a + 0x58) == 0:
                    n += self.proc.write_i32(a + 0x34, 0)
            return n
        if spec.get("selected"):
            # Only the game's selection list: a few reads, not every owned object.
            targets = [(a, vt) for a, vt in units.selected_technos(self.proc, own=True)
                       if vt in spec["kinds"]]
        else:
            ttl = 0.001 if force_scan else 3.0
            targets = units.player_technos_cached(self.proc, ttl=ttl, kinds=spec["kinds"])
        if spec.get("op") == "heal":
            n = 0
            house = units.player_house(self.proc)
            for a, vt in targets:
                typ = units.type_of(self.proc, a, vt)
                hp = self.proc.read_i32(typ + 0xA0) if typ else None
                current = self.proc.read_i32(a + 0x6C)
                if hp and current and 0 < current < hp <= 1000000:
                    amount = self.repair_amount if vt == A.BUILDING_VT else 0
                    healed = min(hp, current + amount) if amount else hp
                    if not units.valid_techno(self.proc, a, vt, house):
                        continue
                    n += (self.proc.write_i32(a + 0x6C, healed)
                          and self.proc.write_i32(a + 0x70, healed))
            return n
        return write_fields(self.proc, targets, spec["fields"], spec["value"],
                            units.player_house(self.proc))

    def reapply_all(self, sidebar_refresh=False):
        """周期性校验已开启补丁的字节，必要时重写；同时刷新「持续生效」功能。"""
        changed = False
        build_refresh = sidebar_refresh
        loops = []
        if not self.patches or not self.patches.compatible():
            return
        for fid, on in list(self.enabled.items()):
            if not on:
                continue
            switch = HOOK_SWITCHES.get(fid)
            if switch:
                attr, apply, _note, label = switch
                controller = getattr(self, attr)
                try:
                    newly = attr == "build_unlock" and fid not in controller.enabled
                    apply(controller, fid, True)
                    build_refresh |= newly
                except Exception as exc:
                    self.log(f"{label}操作未完成：{exc}")
                    self._switch_failed(fid, controller, False)
                continue
            loop = A.LOOP_ACTIONS.get(fid)
            if loop:
                loops.append((fid, loop))
                continue
            spec = A.PATCHES.get(fid)
            if not spec:
                continue
            try:
                if any(self.proc.read(va, len(onb) // 2) != bytes.fromhex(onb)
                       for va, _, onb in spec["sites"]):
                    self.patches.enable(fid)
                    changed = True
            except Exception as exc:
                self.log(str(exc))
                self.enabled[fid] = False
                self.rows[fid].set_toggle(False)
        if changed:
            self.log("检测到补丁丢失，已重新写入")
        for fid, value in list(self.hook_values.items()):
            attr, setter, _current, label = HOOK_VALUES[fid]
            try:
                getattr(getattr(self, attr), setter)(value)
            except Exception as exc:
                self.log(f"{label}恢复未完成：{exc}")
                del self.hook_values[fid]
        airport_target = None
        player = (self.proc.read_u32(A.PLAYER_PTR) or 0) if self.airport_slots else 0
        if self.airport_slots and player:
            # At the main menu there is no player yet: wait, and retry a failed
            # target once a new match (a new player house) has started.
            desired = self.airport_factor
            if (desired != self.airport_slots.factor
                    and (desired != self._airport_failed_target
                         or player != self._airport_failed_player)):
                airport_target = desired
        power_refresh = bool(self.power and self.power.pending_refresh)
        if loops or build_refresh or airport_target is not None or power_refresh:
            def work():
                errors = []
                if power_refresh:
                    try:
                        self.power.flush_refresh()
                    except Exception as exc:
                        errors.append(("_power", str(exc)))
                if airport_target is not None:
                    try:
                        self.airport_slots.set_factor(airport_target)
                    except Exception as exc:
                        errors.append(("_airport_slots", str(exc)))
                if build_refresh:
                    try:
                        self.build_unlock.refresh_sidebar(self.operations.executor)
                    except Exception as exc:
                        errors.append(("_build_refresh", str(exc)))
                for fid, spec in loops:
                    try:
                        self._apply_loop(spec)
                    except Exception as exc:
                        errors.append((fid, str(exc)))
                return errors
            def finished(result, error):
                self._loop_finished(result, error)
                if airport_target is not None:
                    if self.airport_slots and self.airport_slots.factor == airport_target:
                        self._airport_failed_target = None
                        self.log(f"机场机位已同步为 {airport_target} 倍")
                    else:
                        self._airport_failed_target = airport_target
                        self._airport_failed_player = player
            self.jobs.submit(work, finished)

    def _loop_finished(self, result, error):
        if error:
            self.log(f"持续功能刷新失败：{error}")
        elif result:
            for fid, message in result:
                if fid == "_build_refresh":
                    self.log(f"生产侧栏刷新未完成：{message}")
                elif fid == "_airport_slots":
                    self.log(f"机场机位同步未完成：{message}")
                elif fid == "_power":
                    self.log(message)
                else:
                    self.log(f"{self.rows[fid].feat['name']}刷新失败：{message}")

    def _loop_initial_finished(self, fid, spec, count, error):
        if error:
            self.log(f"{spec['name']}开启失败：{error}")
            self.enabled[fid] = False
            self.rows[fid].set_toggle(False)
            self._update_pill()
        else:
            self.log(f"{spec['name']}已作用 {count} 个单位")

    def _va_states(self):
        """{va: (原始字节, 生效字节)}"""
        m = {}
        for spec in A.PATCHES.values():
            for va, off, on in spec["sites"]:
                m[va] = (off, on)
        return m

    def check_signatures(self):
        if not self.proc:
            return
        states = self._va_states()
        bad = []
        patched = []
        for k, (va, exp) in A.VERSION_SIGS.items():
            cur = self.proc.read(va, len(exp) // 2)
            if cur is None:
                bad.append(k)
                continue
            hexcur = cur.hex()
            if hexcur == exp:
                continue
            if va in states and hexcur == states[va][1]:
                patched.append(k)
            else:
                bad.append(k)
        if bad:
            txt = f"版本检测：{len(bad)} 项特征码不符（非 RA2 1.006 或已被改动）"
        else:
            txt = "版本检测：RA2 1.006 特征码全部命中"
        if patched:
            txt += f"（{len(patched)} 项补丁已生效）"
        self.lbl_ver.setText(txt)
        self.log(txt)

    # ---------------- value / action / debug ----------------
    def _value_spec(self, fid):
        spec = A.POINTER_FEATURES.get(fid)
        return spec if spec and spec.get("chain") else None

    def _value_addr(self, fid):
        spec = self._value_spec(fid)
        if not spec or not self.proc:
            return None
        return self.proc.resolve_chain(spec["chain"])

    def _submit(self, work, done, failed):
        """Run work() on the memory thread, then done(result) on the UI thread.

        Heap scans take long enough to freeze the window, so every one of them
        goes through here.
        """
        def finished(result, error):
            if error:
                self.log(f"{failed}：{error}")
            else:
                done(result)
        if not self.jobs.submit(work, finished):
            self.log(BUSY_MESSAGE)
            return False
        return True

    def _log_hits(self, title, hits):
        self.log(title)
        for h in hits[:25]:
            self.log(f"    0x{h:08X}")

    def _unit_write(self, spec, text):
        """把数值写进玩家所有技术类单位（单位编辑类功能）。"""
        first_kind = spec["fields"][0][1]
        try:
            raw = float(text) if first_kind.startswith("f") else int(text, 0)
            if not math.isfinite(raw) or not 0 < raw <= 1000000:
                raise ValueError()
        except ValueError:
            self.log("请输入大于0且不超过1000000的有限数值")
            return
        proc = self.proc

        def work():
            targets = units.player_technos(proc)
            house = units.player_house(proc)
            return write_fields(proc, targets, spec["fields"], raw, house), len(targets)

        def done(result):
            n, total = result
            if not total:
                self.log(f"{spec['name']}: 没有找到己方单位（请先进入战局）")
            else:
                self.log(f"[完成] {spec['name']} = {text} → 已写入 {n}/{total} 个单位")
        self._submit(work, done, f"{spec['name']}未完成")

    def value_write(self, fid, text):
        if jobs_busy(self) or not self.can_write():
            return
        if fid == "repair_amount":
            try:
                value = int(text, 0)
                if not 0 <= value <= 1000000:
                    raise ValueError()
            except ValueError:
                self.log("请输入0到1000000之间的整数；0表示恢复满血")
                return
            if self.techno_ai:
                try:
                    self.techno_ai.set_amount(value)
                except Exception as exc:
                    self.log(f"自动修理：{exc}")
                    return
            self.repair_amount = value
            self.log(f"自动修理：每次恢复 {value} 点血量" if value else "自动修理：每次恢复满血")
            return
        if fid in HOOK_VALUES:
            attr, setter, _current, label = HOOK_VALUES[fid]
            try:
                value = float(text)
                getattr(getattr(self, attr), setter)(value)
            except ValueError as exc:
                self.log(f"{label}：{exc}" if str(exc) and "could not convert" not in str(exc)
                         else f"{label}：请输入数字")
                return
            except Exception as exc:
                self.log(f"{label}设置未完成：{exc}")
                return
            if value == 1.0:
                self.hook_values.pop(fid, None)
                self.log(f"[完成] {label}已恢复原版（1倍）")
            else:
                self.hook_values[fid] = value
                self.log(f"[完成] {label} = {value:g}（仅己方）")
            return
        if fid in CAMPAIGN_VALUES:
            campaign, side = CAMPAIGN_VALUES[fid]
            try:
                number = int(text, 0)
                name = self.campaign.set_start(campaign, number)
            except ValueError as exc:
                self.log(f"{side}战役起始关：{exc}" if "invalid literal" not in str(exc)
                         else f"{side}战役起始关：请输入1到12之间的关卡号")
                return
            except Exception as exc:
                self.log(f"{side}战役起始关设置未完成：{exc}")
                return
            self.log(f"[完成] {side}战役将从第 {number} 关（{name}）开始；在主菜单点“{side}战役”即可")
            return
        if fid == "airport_slots":
            try:
                value = int(text, 0)
                if not 1 <= value <= 16:
                    raise ValueError()
            except ValueError:
                self.log("请输入1到16之间的整数；1表示原版4个机位")
                return
            self._airport_failed_target = None
            controller = self.airport_slots
            if not self.jobs.submit(
                    lambda: controller.set_factor(value),
                    lambda result, error: self._airport_value_finished(value, result, error)):
                self.log(BUSY_MESSAGE)
            return
        if fid in TYPE_FEATURES:
            try:
                n = self.type_editor.write(fid, text)
                self.log(f"[完成] 已修改 {n} 种类型；所有势力的同类型对象都会受影响")
            except (ValueError, OSError, RuntimeError) as exc:
                self.log(str(exc))
            return
        uspec = A.UNIT_FEATURES.get(fid)
        if uspec:
            self._unit_write(uspec, text)
            return
        spec = self._value_spec(fid)
        if not spec:
            self.log(f"{fid}: 未定义指针链（待逆向）")
            return
        try:
            v = int(text, 0)
            if not 0 <= v <= 2000000000:
                raise ValueError()
        except ValueError:
            self.log("请输入0到2000000000之间的整数")
            return
        addr = self._value_addr(fid)
        if addr is None:
            self.log(f"{spec['name']}: 指针链解析失败（未进入战局或进程已退出）")
            return
        kind = spec.get("type", "i32")
        ok = self.proc.write_value(addr, v, kind)
        cur = self.proc.read_value(addr, kind)
        self.log(f"[{'成功' if ok else '失败'}] {spec['name']} = {v} "
                 f"（0x{addr:08X}，读回 {cur}）")

    def _airport_value_finished(self, target, result, error):
        if error:
            self._airport_failed_target = target
            self._airport_failed_player = self.proc.read_u32(A.PLAYER_PTR) if self.proc else None
            self.log(f"机场机位设置未完成：{error}")
            return
        self._airport_failed_target = None
        self.airport_factor = target
        self.log(f"[完成] 空军指挥部机位 {result} 倍（每座 {4 * result} 个联系槽）")

    def value_read(self, fid, widget):
        if jobs_busy(self):
            return
        if not self.proc:
            self.log("未附加游戏进程")
            return
        if fid == "repair_amount":
            widget.setText(str(self.repair_amount))
            return
        if fid in HOOK_VALUES:
            attr, _setter, current, _label = HOOK_VALUES[fid]
            widget.setText(f"{getattr(getattr(self, attr), current):g}")
            return
        if fid in CAMPAIGN_VALUES:
            try:
                widget.setText(str(self.campaign.start_of(CAMPAIGN_VALUES[fid][0])))
            except Exception as exc:
                self.log(str(exc))
            return
        if fid == "airport_slots":
            current = self.airport_slots.factor if self.airport_slots else 1
            widget.setText(str(current))
            self.airport_factor = current
            return
        if fid in TYPE_FEATURES:
            try:
                widget.setText(str(self.type_editor.read(fid)))
            except (ValueError, OSError) as exc:
                self.log(str(exc))
            return
        uspec = A.UNIT_FEATURES.get(fid)
        if uspec:
            proc = self.proc
            off, kind = uspec["fields"][0]

            def work():
                targets = units.player_technos(proc)
                value = proc.read_value(targets[0][0] + off, kind) if targets else None
                return value, len(targets)

            def done(result):
                v, total = result
                if not total:
                    self.log(f"{uspec['name']}: 没有找到己方单位")
                    return
                widget.setText(str(v))
                self.log(f"读取 {uspec['name']} = {v}（共 {total} 个单位）")
            self._submit(work, done, f"读取{uspec['name']}未完成")
            return
        spec = self._value_spec(fid)
        if not spec:
            self.log(f"{fid}: 未定义指针链（待逆向）")
            return
        addr = self._value_addr(fid)
        if addr is None:
            self.log(f"{spec['name']}: 指针链解析失败（未进入战局或进程已退出）")
            return
        kind = spec.get("type", "i32")
        v = self.proc.read_value(addr, kind)
        if v is None:
            self.log(f"{spec['name']}: 读取失败（0x{addr:08X}）")
            return
        widget.setText(str(v))
        self.log(f"读取 {spec['name']} = {v}（0x{addr:08X}）")

    def run_action(self, fid):
        if jobs_busy(self):
            return
        cursor = None
        if fid == "u_clone" and self.proc:
            try:
                cursor = self.operations.cursor_point()
            except Exception as exc:
                self.log(f"复制失败：{exc}")
                return
        if not self.can_write():
            return
        if fid == "mission_win":
            answer = QMessageBox.question(self, "任务直接胜利", "立即以胜利结束当前战局？",
                                          QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            controller = self.campaign
            if not self.proc or not controller:
                self.log("游戏进程已退出，未执行")
                return
            if not self.jobs.submit(controller.win, lambda _result, error: self.log(
                    f"任务胜利未完成：{error}" if error else "[完成] 已宣布胜利")):
                self.log(BUSY_MESSAGE)
            return
        if fid == "v_auto_enter":
            try:
                snapshot = self.auto_enter.snapshot_selected()
                if not snapshot[1]:
                    self.log("没有选中己方可进驻步兵")
                    return
                controller = self.auto_enter
                if self.jobs.submit(
                        lambda: controller.execute(snapshot),
                        lambda result, error: self._action_finished(fid, result, error)):
                    self.log(f"已接受进驻请求，正在检查当时选中的 {len(snapshot[1])} 名己方步兵")
                else:
                    self.log("上一项操作尚未完成")
            except Exception as exc:
                self.log(f"进驻请求未完成：{exc}")
            return
        if fid == "restore_types":
            try:
                self.log(f"已还原 {self.type_editor.restore()} 项类型属性")
            except (RuntimeError, OSError) as exc:
                self.log(str(exc))
            return
        if fid in {"u_clone", "u_seize", "u_transfer"}:
            try:
                operation, attached = self.operations, self.proc
                if fid == "u_clone":
                    if not self.jobs.submit(
                            lambda: operation.clone_selected(cursor),
                            lambda result, error: self._action_finished(fid, result, error)):
                        self.log("上一项操作尚未完成")
                    else:
                        self.log("已接受复制请求，正在放置到游戏鼠标附近")
                    return
                house = units.player_house(self.proc)
                if fid == "u_transfer":
                    houses = [house] + units.enemy_houses(self.proc)
                    labels = []
                    for i, h in enumerate(houses):
                        typ = self.proc.read_u32(h + 0x34)
                        name = self.proc.read_cstr(typ + 0x24, 32) if typ else "未知"
                        labels.append(f"{i + 1}. {name}" + ("（己方）" if h == house else ""))
                    label, ok = QInputDialog.getItem(self, "转移单位控制权", "目标势力", labels, 0, False)
                    if not ok:
                        return
                    house = houses[labels.index(label)]
                if self.proc is not attached or self.operations is not operation:
                    self.log("游戏连接已变化，请重新执行")
                    return
                if not self.jobs.submit(
                        lambda: operation.transfer_selected(house),
                        lambda result, error: self._action_finished(fid, result, error)):
                    self.log("上一项操作尚未完成")
            except Exception as exc:
                self.log(f"执行未完成：{exc}")
            return
        spec = A.ACTION_FEATURES.get(fid)
        if not spec:
            self.log(f"{fid}: 尚未定位地址（action 待逆向完成）")
            return
        op = spec.get("op")
        if op == "set_unit_field":
            proc = self.proc

            def work():
                targets = units.player_technos(proc)
                house = units.player_house(proc)
                return (write_fields(proc, targets, spec["fields"], spec["value"], house),
                        len(targets))

            def done(result):
                n, total = result
                if not total:
                    self.log(f"{spec['name']}: 没有找到己方单位（请先进入战局）")
                else:
                    self.log(f"[完成] {spec['name']} → 已作用 {n}/{total} 个单位")
            self._submit(work, done, f"{spec['name']}未完成")
            return
        self.log(f"{fid}: 未实现的执行动作 {op}")

    def _action_finished(self, fid, result, error):
        if error:
            self.log(f"执行未完成：{error}")
        else:
            done, total = result
            if fid == "v_auto_enter":
                if not total:
                    self.log("[完成] 当时选中的步兵中没有可进驻的己方驻防步兵")
                else:
                    note = getattr(self.auto_enter, "last_note", "") if self.auto_enter else ""
                    self.log(f"[完成] 已下达进驻命令 {done}/{total} 名有效选中步兵"
                             + (f"；{note}" if note else ""))
            else:
                self.log(f"[完成] {fid}: {done}/{total}")

    def run_debug(self, fid, text):
        if jobs_busy(self):
            return
        if not self.proc:
            self.log("未附加游戏进程")
            return
        proc = self.proc
        if fid == "dbg_sig":
            self.check_signatures()
            for k, (va, exp) in A.VERSION_SIGS.items():
                cur = proc.read(va, len(exp) // 2)
                self.log(f"  {k} @0x{va:X}: {cur.hex() if cur else '读取失败'} (原始 {exp})")
        elif fid == "dbg_scan":
            try:
                val = int(text, 0)
            except ValueError:
                self.log("请输入整数，例如 10000")
                return
            if self._submit(lambda: proc.scan_i32(val),
                            lambda hits: self._log_hits(f"扫描 {val} -> {len(hits)} 处命中", hits),
                            "扫描未完成"):
                self.log(f"正在扫描 {val}，完成后显示结果")
        elif fid == "dbg_ptr":
            try:
                addr = int(text, 0)
            except ValueError:
                self.log("请输入地址，例如 0x12345678")
                return
            if self._submit(lambda: proc.find_pointers_to(addr),
                            lambda hits: self._log_hits(f"指向 0x{addr:X} 的指针 -> {len(hits)} 处", hits),
                            "指针查找未完成"):
                self.log(f"正在查找指向 0x{addr:X} 的指针，完成后显示结果")

    # ---------------- misc ----------------
    def launch_game(self):
        # 必须经启动器 ra2.exe：直接运行 game.exe 会立刻退出
        exe = os.path.join(GAME_DIR, "ra2.exe")
        if not os.path.exists(exe):
            self.log(f"找不到启动器 {exe}")
            return
        try:
            runtime.launch_game(exe)
            self.log("已启动 ra2.exe；进入单机战局后自动附加")
        except Exception as e:
            self.log(f"启动失败: {e}")

    def side_action(self, name):
        if name == "环境检测":
            import platform
            self.log(f"Python {platform.python_version()}  PySide6 {self._pyside_ver()}")
            self.log(f"游戏目录: {GAME_DIR}")
            self.log(f"进程状态: {'已附加' if self.proc else '未附加'}")
            self.check_signatures() if self.proc else self.log("请先启动并进入游戏")
        elif name == "保存当前功能状态":
            if self.save_state():
                self.log("功能状态已保存")
        elif name == "功能方案":
            self.profile_menu()
        elif name == "快捷键设置":
            self._hotkey_dialog = True
            self.hotkeys.suspend()
            try:
                editable = [f for f in FEATURES if f["status"] == "ok" and
                            f["kind"] in ("patch", "value", "action")]
                dialog = HotkeyDialog(self, editable, self.hotkey_mapping)
                if dialog.exec():
                    self.hotkey_mapping = dict(self.hotkeys.mapping)
                    self._save_hotkeys()
                    self._update_hotkey_hint()
                    self.log(f"快捷键已保存（{len(self.hotkey_mapping)} 项）")
            finally:
                self._hotkey_dialog = False
                try:
                    self._sync_hotkey_focus()
                except ValueError as exc:
                    self.log(f"快捷键注册失败：{exc}")
        else:
            self.log(f"{APP_NAME} {APP_VER} · 目标游戏 红色警戒2 1.006 (game.exe)")

    @staticmethod
    def _pyside_ver():
        try:
            import PySide6
            return PySide6.__version__
        except Exception:
            return "?"

    def _write_state(self, data):
        temp = STATE_FILE + ".tmp"
        try:
            # Write a complete file first so a crash never leaves half a JSON behind.
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp, STATE_FILE)
            return True
        except Exception as exc:
            self.log(f"保存设置失败：{exc}")
            return False

    def _read_state(self):
        if not os.path.exists(STATE_FILE):
            return {}
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("设置文件格式不正确")
        return data

    def _update_state(self, change):
        """Read-modify-write the settings file; never replace a file that could not be read.

        A corrupt file is moved aside (kept, not deleted) before starting a new
        one; an unreadable file (locked, no permission) blocks the save."""
        try:
            data = self._read_state()
        except (ValueError, UnicodeDecodeError) as exc:  # JSONDecodeError is a ValueError
            broken = STATE_FILE + ".broken"
            try:
                os.replace(STATE_FILE, broken)
            except OSError as move_error:
                self.log(f"设置文件已损坏且无法备份，未保存：{move_error}")
                return False
            self.log(f"设置文件已损坏，原文件另存为 {os.path.basename(broken)}，已新建：{exc}")
            data = {}
        except OSError as exc:
            self.log(f"读取设置文件失败，未保存（避免覆盖已有设置）：{exc}")
            return False
        change(data)
        return self._write_state(data)

    def save_state(self):
        """Only the “保存当前功能状态” button calls this: nothing else persists switches."""
        data = {k: bool(v) for k, v in self.enabled.items()}
        data[SAVED_MARK] = True
        data["_repair_amount"] = self.repair_amount
        data["_airport_factor"] = self.airport_factor
        data["_hook_values"] = dict(self.hook_values)
        data["_hotkeys"] = self.hotkey_mapping
        data["_values"] = {fid: row.input.text() for fid, row in self.rows.items()
                           if fid != "airport_slots" and row.input is not None
                           and row.feat["kind"] == "value"}

        def change(saved):
            profiles = saved.get("_profiles")
            saved.clear()
            saved.update(data)
            if isinstance(profiles, dict):
                saved["_profiles"] = profiles  # saved profiles are not part of the switch state
        return self._update_state(change)

    def _save_hotkeys(self):
        """Store hotkeys alone; the saved feature state in the file stays as it was."""
        return self._update_state(lambda data: data.__setitem__("_hotkeys", self.hotkey_mapping))

    def _restore_state(self):
        try:
            data = self._read_state()
        except Exception as exc:
            self.log(f"读取已保存的设置失败，已使用默认设置：{exc}")
            return
        editable = {feat["id"] for feat in FEATURES if feat["status"] == "ok"
                    and feat["kind"] in ("patch", "value", "action")}
        hotkeys = data.get("_hotkeys", {"u_clone": "Ctrl+Alt+C"})
        self.hotkey_mapping = {k: v for k, v in hotkeys.items()
                               if k in editable and isinstance(v, str)} if isinstance(hotkeys, dict) else {}
        if data.get(SAVED_MARK) is not True:
            # First run, or a file written automatically by an older version:
            # every feature starts switched off.
            self.log("所有功能默认关闭；点击“保存当前功能状态”后，下次打开会恢复")
            return
        amount = data.get("_repair_amount", 0)
        self.repair_amount = amount if type(amount) is int and 0 <= amount <= 1000000 else 0
        factor = data.get("_airport_factor", 1)
        self.airport_factor = factor if type(factor) is int and 1 <= factor <= 16 else 1
        self.rows["airport_slots"].input.setText(str(self.airport_factor))
        self.hook_values = self._valid_hook_values(data.get("_hook_values", {}))
        known = {feat["id"] for feat in FEATURES if feat["kind"] == "patch" and feat["status"] == "ok"}
        self.enabled = {k: bool(v) for k, v in data.items() if k in known}
        values = data.get("_values", {})
        if isinstance(values, dict):
            for fid, value in values.items():
                if (fid != "airport_slots" and fid in self.rows
                        and self.rows[fid].input is not None and isinstance(value, str)):
                    self.rows[fid].input.setText(value)
        for fid, on in self.enabled.items():
            if fid in self.rows:
                self.rows[fid].set_toggle(on)
        count = sum(self.enabled.values())
        self.log(f"已恢复保存的功能状态（{count} 项开启）" if count else "已恢复保存的功能状态（全部关闭）")

    # ---------------- profiles ----------------
    def _user_profiles(self):
        try:
            profiles = self._read_state().get("_profiles", {})
        except Exception:
            return {}
        return {k: v for k, v in profiles.items()
                if isinstance(k, str) and isinstance(v, dict)} if isinstance(profiles, dict) else {}

    def current_profile(self):
        return {"enabled": {fid: True for fid, on in self.enabled.items() if on},
                "values": dict(self.hook_values)}

    def profile_menu(self):
        user = self._user_profiles()
        choices = [f"应用：{name}（内置）" for name in BUILTIN_PROFILES]
        choices += [f"应用：{name}" for name in user]
        choices += ["保存当前开关和倍率为新方案…"]
        if user:
            choices += ["删除一个自定义方案…"]
        choice, ok = QInputDialog.getItem(self, "功能方案", "选择操作", choices, 0, False)
        if not ok:
            return
        if choice.startswith("保存"):
            name, ok = QInputDialog.getText(self, "保存功能方案", "方案名称")
            name = name.strip()
            if ok and name:
                if name in BUILTIN_PROFILES:
                    self.log("不能覆盖内置方案，请换一个名称")
                else:
                    self.save_profile(name)
        elif choice.startswith("删除"):
            name, ok = QInputDialog.getItem(self, "删除功能方案", "方案", list(user), 0, False)
            if ok:
                self.delete_profile(name)
        else:
            name = choice[3:].removesuffix("（内置）")
            profile = BUILTIN_PROFILES.get(name) if choice.endswith("（内置）") else user.get(name)
            if profile is not None:
                self.apply_profile(name, profile)

    def save_profile(self, name):
        profile = self.current_profile()

        def change(data):
            profiles = data.get("_profiles") if isinstance(data.get("_profiles"), dict) else {}
            profiles[name] = profile
            data["_profiles"] = profiles
        if self._update_state(change):
            self.log(f"已保存功能方案“{name}”（{len(profile['enabled'])} 个开关）")

    def delete_profile(self, name):
        try:
            data = self._read_state()
        except Exception:
            return
        profiles = data.get("_profiles")
        if isinstance(profiles, dict) and name in profiles:
            del profiles[name]
            if self._write_state(data):
                self.log(f"已删除功能方案“{name}”")

    def apply_profile(self, name, profile):
        """Switch exactly to the profile: off first (synchronous), then on in one reapply."""
        if jobs_busy(self):
            return False
        switches = {feat["id"] for feat in FEATURES if feat["kind"] == "patch" and feat["status"] == "ok"}
        wanted = profile.get("enabled", {}) if isinstance(profile.get("enabled"), dict) else {}
        wanted = {fid for fid, on in wanted.items() if on and fid in switches}
        attached = bool(self.proc) and self.can_write()
        sidebar = False
        for fid in sorted(switches):
            if self.enabled.get(fid) and fid not in wanted:
                if attached:
                    # No per-switch background job here (sidebar or power
                    # refresh): it would make every later switch in this loop
                    # bail out as "busy". reapply_all below flushes both.
                    self.set_patch(fid, False, background_refresh=False)
                    sidebar |= HOOK_SWITCHES.get(fid, ("",))[0] == "build_unlock"
                else:
                    self.enabled[fid] = False
                    self.rows[fid].set_toggle(False)
        for fid in sorted(wanted):
            self.enabled[fid] = True
            self.rows[fid].set_toggle(True)
        values = self._valid_hook_values(profile.get("values", {}))
        for fid in HOOK_VALUES:
            value = values.get(fid, 1.0)
            self.rows[fid].input.setText(f"{value:g}")
            if attached:
                attr, setter, _current, label = HOOK_VALUES[fid]
                try:
                    getattr(getattr(self, attr), setter)(value)
                except Exception as exc:
                    self.log(f"{label}：{exc}")
                    continue
            if value == 1.0:
                self.hook_values.pop(fid, None)
            else:
                self.hook_values[fid] = value
        if attached:
            self.reapply_all(sidebar_refresh=sidebar)
        self._update_pill()
        self.log(f"已应用功能方案“{name}”：{len(wanted)} 个开关开启"
                 + ("" if attached else "（进入游戏后生效）"))
        return True

    @staticmethod
    def _valid_hook_values(values):
        limits = {"ore_income": (0.1, 100), "rof_mult": (1, 10), "range_mult": (0.5, 5)}
        out = {}
        if isinstance(values, dict):
            for fid, value in values.items():
                if (fid in limits and type(value) in (int, float)
                        and limits[fid][0] <= value <= limits[fid][1] and value != 1):
                    out[fid] = float(value)
        return out

    def _install_hotkeys(self):
        try:
            self.hotkeys.replace(self.hotkey_mapping)
        except ValueError as exc:
            self.log(f"快捷键注册失败：{exc}")
            active = {}
            for fid, shortcut in self.hotkey_mapping.items():
                try:
                    self.hotkeys.replace({**active, fid: shortcut})
                    active[fid] = shortcut
                except ValueError as item_error:
                    self.log(f"{self.rows[fid].feat['name']}快捷键暂不可用：{item_error}")

    def _update_hotkey_hint(self):
        shortcut = self.hotkey_mapping.get("u_clone")
        self.hotkey_hint.setText(f"复制：{shortcut}" if shortcut else "复制：未设置快捷键")

    def _hotkey_triggered(self, fid):
        if foreground_pid() not in (os.getpid(), self.pid) or self._closing:
            return
        if foreground_pid() == os.getpid() and isinstance(QApplication.focusWidget(),
                                                          (QLineEdit, QKeySequenceEdit)):
            return
        row = self.rows.get(fid)
        if not row or row.feat["status"] != "ok":
            return
        kind = row.feat["kind"]
        if kind == "patch":
            on = not self.enabled.get(fid, False)
            self.set_patch(fid, on)
            row.set_toggle(self.enabled.get(fid, False))
        elif kind == "value":
            self.value_write(fid, row.input.text())
        elif kind == "action":
            self.run_action(fid)

    def _sync_hotkey_focus(self):
        if self._hotkey_dialog or self._closing:
            return
        active = foreground_pid() in (os.getpid(), self.pid)
        if active and self.hotkeys.paused:
            try:
                self.hotkeys.resume()
            except ValueError as exc:
                self.log(f"快捷键暂不可用：{exc}")
        elif not active and not self.hotkeys.paused:
            self.hotkeys.suspend()

    def log(self, msg):
        # The widget drops the oldest lines itself (setMaximumBlockCount).
        self.log_view.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {msg}")
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def closeEvent(self, ev):
        self._closing = True
        self.t_poll.stop()
        self.hotkey_timer.stop()
        self.jobs.drain()
        if self.proc and not any(p == self.pid for p, _ in Process.find_pids(["game.exe"])):
            self.proc.close()  # the game already exited: nothing left to restore
            self.proc = None
            self._detach()
        if self.proc:
            failures = []

            def attempt(label, function):
                # One failed restore must not keep the rest of the game patched.
                try:
                    function()
                except Exception as exc:
                    failures.append(f"{label}：{exc}")

            had_build_unlock = bool(self.build_unlock and self.build_unlock.enabled)
            for label, attr in CLOSE_ORDER:
                controller = getattr(self, attr)
                if controller:
                    attempt(label, controller.close)
                if attr == "build_unlock" and had_build_unlock and self.operations \
                        and not controller.enabled:
                    try:
                        controller.refresh_sidebar(self.operations.executor)
                    except Exception as exc:
                        self.log(f"退出时生产侧栏刷新未完成：{exc}")
            if self.type_editor:
                attempt("类型属性", self.type_editor.restore)
            if failures:
                for failure in failures:
                    self.log(f"恢复失败：{failure}")
                answer = QMessageBox.question(
                    self, "部分修改未能恢复",
                    "以下修改未能恢复：\n\n" + "\n".join(failures) +
                    "\n\n游戏暂停时请先继续游戏再重试。\n"
                    "仍要退出吗？未恢复的修改会留在本局游戏中，下次打开修改器时会尝试接管。",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes:
                    self._closing = False
                    self.t_poll.start()
                    self.hotkey_timer.start()
                    ev.ignore()
                    return
            self.proc.close()
        self.jobs.close()
        self.hotkeys.close()
        super().closeEvent(ev)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(QSS)
    app.setFont(QFont("Microsoft YaHei UI", 9))
    w = MainWindow()
    w.show()
    return app.exec()
