"""Stable external paths for both source and portable executable runs."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys

VERSION = "0.3.0"


def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def game_dir():
    root = app_dir()
    candidates = (root / "游戏本体", root.parent / "游戏本体", root)
    return next((p for p in candidates if (p / "ra2.exe").is_file()), candidates[0])


def launch_game(exe):
    """Do not pass PyInstaller's DLL search directory to the external game."""
    frozen = getattr(sys, "frozen", False) and sys.platform == "win32"
    if frozen:
        set_directory = ctypes.windll.kernel32.SetDllDirectoryW
        set_directory.argtypes = [ctypes.c_wchar_p]
        set_directory.restype = ctypes.c_int
        if not set_directory(None):
            raise ctypes.WinError()
    try:
        env = os.environ.copy()
        if frozen:
            bundle = os.path.normcase(os.path.abspath(sys._MEIPASS))
            env["PATH"] = os.pathsep.join(
                p for p in env.get("PATH", "").split(os.pathsep)
                if not os.path.normcase(os.path.abspath(p)).startswith(bundle + os.sep)
                and os.path.normcase(os.path.abspath(p)) != bundle)
        return subprocess.Popen(str(exe), cwd=str(Path(exe).parent), env=env)
    finally:
        if frozen:
            set_directory(sys._MEIPASS)
