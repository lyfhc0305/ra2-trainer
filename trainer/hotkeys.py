"""Windows registered hotkeys: no keyboard hook or key recording."""
import ctypes
from ctypes import wintypes as W

from PySide6.QtCore import QAbstractNativeEventFilter, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QLabel,
    QTableWidget, QTableWidgetItem, QKeySequenceEdit, QDialogButtonBox, QMessageBox,
    QLineEdit)

from .winapi import load

user32 = load("user32")
user32.RegisterHotKey.argtypes = [W.HWND, ctypes.c_int, W.UINT, W.UINT]
user32.RegisterHotKey.restype = W.BOOL
user32.UnregisterHotKey.argtypes = [W.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = W.BOOL
user32.GetForegroundWindow.restype = W.HWND
user32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
user32.GetWindowThreadProcessId.restype = W.DWORD


def foreground_pid():
    pid = W.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    return pid.value


def parse_shortcut(text):
    seq = QKeySequence.fromString(text, QKeySequence.PortableText)
    if seq.count() != 1:
        raise ValueError("快捷键必须是单个组合键")
    combination = seq[0]
    key, flags = combination.key().value, combination.keyboardModifiers()
    modifiers = 0x4000  # MOD_NOREPEAT
    for qt, win in ((Qt.ControlModifier, 2), (Qt.AltModifier, 1), (Qt.ShiftModifier, 4)):
        if flags & qt:
            modifiers |= win
    if flags & Qt.MetaModifier:
        raise ValueError("不支持Windows键组合")
    if ord('A') <= key <= ord('Z') or ord('0') <= key <= ord('9'):
        vk = key
    elif Qt.Key_F1.value <= key <= Qt.Key_F24.value:
        vk = 0x70 + key - Qt.Key_F1.value
    else:
        table = {Qt.Key_Space.value: 0x20, Qt.Key_Insert.value: 0x2D,
                 Qt.Key_Delete.value: 0x2E, Qt.Key_Home.value: 0x24,
                 Qt.Key_End.value: 0x23, Qt.Key_PageUp.value: 0x21,
                 Qt.Key_PageDown.value: 0x22}
        vk = table.get(key)
        if vk is None:
            raise ValueError("请选择字母、数字、F1～F24或Insert/Delete/Home/End/PageUp/PageDown")
    if modifiers == 0x4000 and not 0x70 <= vk <= 0x87:
        raise ValueError("字母、数字和导航键请搭配Ctrl、Alt或Shift，避免误操作")
    return modifiers, vk, seq.toString(QKeySequence.PortableText)


class Hotkeys(QAbstractNativeEventFilter):
    def __init__(self, callback, on_error=None):
        super().__init__()
        self.callback = callback
        self.on_error = on_error
        self.registered = {}
        self.mapping = {}
        self.paused = False
        QApplication.instance().installNativeEventFilter(self)

    def replace(self, mapping):
        parsed, seen = {}, set()
        for fid, text in mapping.items():
            if not text:
                continue
            mods, vk, normalized = parse_shortcut(text)
            if (mods, vk) in seen:
                raise ValueError(f"快捷键重复：{normalized}")
            seen.add((mods, vk))
            parsed[fid] = (mods, vk, normalized)
        old = dict(self.mapping)
        self.unregister()
        try:
            for i, (fid, (mods, vk, text)) in enumerate(parsed.items(), 0x3100):
                if not user32.RegisterHotKey(None, i, mods, vk):
                    raise ValueError(f"快捷键 {text} 已被占用或被系统保留，请换一个")
                self.registered[i] = fid
            self.mapping = {fid: value[2] for fid, value in parsed.items()}
        except Exception:
            self.unregister()
            # The old bindings were usable immediately before this transaction.
            for i, (fid, text) in enumerate(old.items(), 0x3100):
                mods, vk, _ = parse_shortcut(text)
                if user32.RegisterHotKey(None, i, mods, vk):
                    self.registered[i] = fid
            self.mapping = {fid: old[fid] for fid in self.registered.values()}
            raise

    def suspend(self):
        """Release registrations while QKeySequenceEdit records a new chord."""
        self.paused = True
        self.unregister()

    def resume(self):
        self.paused = False
        self.replace(self.mapping)

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            msg = W.MSG.from_address(int(message))
            if msg.message == 0x312 and msg.wParam in self.registered:
                if not self.paused:
                    try:
                        self.callback(self.registered[msg.wParam])
                    except Exception as exc:
                        if self.on_error:
                            self.on_error(exc)
                return True, 0
        return False, 0

    def unregister(self):
        for i in self.registered:
            user32.UnregisterHotKey(None, i)
        self.registered.clear()

    def close(self):
        self.unregister()
        QApplication.instance().removeNativeEventFilter(self)


class HotkeyDialog(QDialog):
    def __init__(self, parent, features, mapping):
        super().__init__(parent)
        self.setWindowTitle("自定义快捷键")
        self.resize(630, 620)
        self.setStyleSheet("QDialog,QTableWidget,QKeySequenceEdit {background:#241a24;color:#eadfe7;}"
                           "QDialog QLabel {color:#eadfe7;}")
        layout = QVBoxLayout(self)
        note = QLabel("在右侧按下组合键；清空即可取消。开关类切换开关，数值类应用输入框中的值。\n"
                      "仅游戏或修改器在前台时执行；编辑快捷键期间暂停触发。")
        note.setWordWrap(True)
        layout.addWidget(note)
        table = QTableWidget(len(features), 2)
        table.setHorizontalHeaderLabels(["修改项目", "快捷键"])
        table.horizontalHeader().setStretchLastSection(True)
        table.setColumnWidth(0, 300)
        self.editors = {}
        for i, f in enumerate(features):
            item = QTableWidgetItem(f['name'])
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            table.setItem(i, 0, item)
            edit = QKeySequenceEdit(QKeySequence(mapping.get(f['id'], '')))
            edit.setMaximumSequenceLength(1)
            edit.setClearButtonEnabled(True)
            line_edit = edit.findChild(QLineEdit)
            if line_edit:
                line_edit.setPlaceholderText("按下快捷键")
            table.setCellWidget(i, 1, edit)
            self.editors[f['id']] = edit
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply(self):
        values = {fid: e.keySequence().toString(QKeySequence.PortableText)
                  for fid, e in self.editors.items() if not e.keySequence().isEmpty()}
        try:
            self.parent().hotkeys.replace(values)
        except ValueError as exc:
            self.parent().hotkeys.suspend()
            QMessageBox.warning(self, "快捷键未保存", str(exc))
            return
        self.accept()
