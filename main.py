"""红警2修改器入口：python main.py 或 python main.py --check。"""
import os
import sys
from importlib import import_module

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def check_environment():
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 或更新版本。", flush=True)
        return 2
    missing = []
    for name in ("PySide6.QtWidgets", "capstone"):
        try:
            import_module(name)
        except Exception as exc:
            missing.append(f"{name}（{exc}）")
    if missing:
        print(f"缺少运行依赖：{', '.join(missing)}", flush=True)
        print("请在 ra2-trainer 目录执行：python -m pip install -r requirements.txt", flush=True)
        return 2
    print(f"环境检查通过：{sys.executable}", flush=True)
    return 0


def start():
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        from trainer.selftest import run
        return run(sys.argv[2])
    if len(sys.argv) == 2 and sys.argv[1] == "--check":
        return check_environment()
    if len(sys.argv) != 1:
        print("用法：main.py [--check]", flush=True)
        return 2

    from PySide6.QtCore import QLockFile, QStandardPaths
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication.instance() or QApplication(sys.argv)
    lock_path = os.path.join(
        QStandardPaths.writableLocation(QStandardPaths.TempLocation),
        "ra2-trainer-single-instance.lock")
    lock = QLockFile(lock_path)
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, "红警2修改器", "修改器已经在运行，请使用已打开的窗口。")
        return 0
    try:
        from trainer.app import main
        return main()
    finally:
        lock.unlock()

if __name__ == "__main__":
    sys.exit(start())
