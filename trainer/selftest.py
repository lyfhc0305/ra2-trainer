"""Packaged GUI smoke check, without attaching to a game or loading user state."""
import json
from pathlib import Path
import traceback


def run(report_path):
    report = {"ok": False}
    try:
        from PySide6.QtCore import QTimer, Qt
        from PySide6.QtWidgets import QApplication
        import capstone
        from .app import MainWindow, QSS
        from .features import FEATURES
        from .runtime import VERSION, app_dir

        class SmokeWindow(MainWindow):
            def tick(self):
                pass

            def _restore_state(self):
                pass

            def _install_hotkeys(self):
                pass

        app = QApplication([])
        app.setStyleSheet(QSS)
        window = SmokeWindow()
        window.t_poll.stop()
        window.hotkey_timer.stop()
        window.setAttribute(Qt.WA_DontShowOnScreen)
        window.show()
        app.processEvents()
        assert len(window.rows) == len(FEATURES)
        assert list(capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32).disasm(b'\x90', 0))[0].mnemonic == 'nop'
        report.update(version=VERSION, feature_count=len(window.rows), app_dir=str(app_dir()),
                      qt_platform=app.platformName())
        QTimer.singleShot(300, window.close)
        assert app.exec() == 0
        report["ok"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
