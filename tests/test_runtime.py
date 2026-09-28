import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trainer import runtime


class RuntimeTests(unittest.TestCase):
    def test_frozen_paths_use_executable_not_extraction_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            exe = root / "RA2Trainer.exe"
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(exe)):
                self.assertEqual(runtime.app_dir(), root)
                (root / "游戏本体").mkdir()
                (root / "游戏本体" / "ra2.exe").touch()
                self.assertEqual(runtime.game_dir(), root / "游戏本体")

    def test_game_can_be_beside_exe_or_in_parent_game_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            portable = root / "release"
            portable.mkdir()
            with patch.object(runtime, "app_dir", return_value=portable):
                (portable / "ra2.exe").touch()
                self.assertEqual(runtime.game_dir(), portable)
                (root / "游戏本体").mkdir()
                (root / "游戏本体" / "ra2.exe").touch()
                self.assertEqual(runtime.game_dir(), root / "游戏本体")

    def test_external_launch_restores_dll_directory_even_on_failure(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "_MEIPASS", "C:\\bundle", create=True), \
             patch.object(sys, "platform", "win32"), \
             patch.object(runtime.ctypes, "windll", create=True) as dll, \
             patch.object(runtime.subprocess, "Popen", side_effect=OSError("test")):
            with self.assertRaises(OSError):
                runtime.launch_game(Path("C:/game/ra2.exe"))
            self.assertEqual(dll.kernel32.SetDllDirectoryW.call_args_list[0].args, (None,))
            self.assertEqual(dll.kernel32.SetDllDirectoryW.call_args_list[-1].args, ("C:\\bundle",))
