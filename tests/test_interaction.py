import os
import json
import tempfile
import unittest
from unittest.mock import patch, Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from trainer import units
from trainer.jobs import Jobs
from trainer.hotkeys import Hotkeys, parse_shortcut
from trainer.operations import GameOperations, clone_batch_code, clone_place_code, clone_record


class CursorMemory:
    def __init__(self, x, y, left, top, width, height):
        self.values = {
            0x839C68: 0x10000,
            0x10000: 0x7AFBEC,
            0x1001C: x, 0x10020: y,
            0x839630: left, 0x839634: top,
            0x839638: width, 0x83963C: height,
        }

    def read_u32(self, address):
        return self.values.get(address)

    read_i32 = read_u32


class InteractionTests(unittest.TestCase):
    def setUp(self):
        from trainer import app as app_module
        self._state_directory = tempfile.TemporaryDirectory()
        self._state_patch = patch.object(
            app_module, "STATE_FILE",
            os.path.join(self._state_directory.name, "state.json"))
        self._state_patch.start()

    def tearDown(self):
        self._state_patch.stop()
        self._state_directory.cleanup()

    def test_cursor_uses_viewport_width_and_rejects_sidebar(self):
        operation = GameOperations.__new__(GameOperations)
        operation.proc = CursorMemory(125, 225, 100, 200, 400, 300)
        self.assertEqual(operation.cursor_point(), (25, 25, 400, 300))
        operation.proc.values[0x1001C] = 501
        with self.assertRaises(ValueError):
            operation.cursor_point()

    def test_one_cached_scan_serves_multiple_unit_kinds(self):
        class Memory:
            def read_u32(self, address):
                return 0x1000 if address == units.PLAYER_PTR else None
        proc = Memory()
        objects = [(0x2000, 0x7ADDF8), (0x3000, 0x7A3540)]
        with patch("time.monotonic", side_effect=[0, 4, 4.1]), \
             patch.object(units, "player_technos", return_value=objects) as scan, \
             patch.object(units, "valid_techno", return_value=True):
            self.assertEqual(units.player_technos_cached(proc, kinds={0x7ADDF8}), [objects[0]])
            self.assertEqual(units.player_technos_cached(proc, kinds={0x7A3540}), [objects[1]])
            self.assertEqual(scan.call_count, 1)

    def test_unit_regen_reads_selection_not_all_units(self):
        from trainer import app as app_module
        from trainer import addresses as A
        window = app_module.MainWindow.__new__(app_module.MainWindow)
        window.repair_amount = 0
        window.proc = Mock()
        window.proc.read_i32.side_effect = lambda a: {0x2000 + 0x6C: 50, 0x9000 + 0xA0: 100}.get(a)
        window.proc.write_i32.return_value = True
        with patch.object(units, "player_technos_cached", side_effect=AssertionError), \
             patch.object(units, "selected_technos",
                          return_value=[(0x2000, 0x7ADDF8), (0x3000, 0x79CBBC)]), \
             patch.object(units, "player_house", return_value=0x1000), \
             patch.object(units, "type_of", return_value=0x9000), \
             patch.object(units, "valid_techno", return_value=True):
            self.assertEqual(window._apply_loop(A.LOOP_ACTIONS["u_regen"]), 1)
        window.proc.write_i32.assert_any_call(0x2000 + 0x6C, 100)

    def test_background_job_keeps_qt_event_loop_responsive(self):
        import time
        app = QApplication.instance() or QApplication([])
        jobs = Jobs(app)
        loop = QEventLoop()
        events = []
        heartbeat = QTimer()
        heartbeat.setInterval(20)
        heartbeat.timeout.connect(lambda: events.append(time.monotonic()))
        heartbeat.start()
        jobs.submit(lambda: time.sleep(0.3), lambda _result, _error: loop.quit())
        loop.exec()
        heartbeat.stop()
        jobs.close()
        self.assertGreaterEqual(len(events), 5)

    def test_periodic_refresh_entry_keeps_filter_events_responsive(self):
        import time
        from trainer import app as app_module
        app = QApplication.instance() or QApplication([])
        with patch.object(app_module.Process, "find_pids", return_value=[]):
            window = app_module.MainWindow()
        window.t_poll.stop()
        window.hotkey_timer.stop()
        window.proc = object()
        window.patches = Mock()
        window.patches.compatible.return_value = True
        window.enabled = {"u_regen": True}
        window._apply_loop = Mock(side_effect=lambda _spec: time.sleep(0.3))
        loop = QEventLoop()
        beats = []
        heartbeat = QTimer()
        heartbeat.setInterval(20)
        heartbeat.timeout.connect(lambda: (beats.append(1), window.apply_filter()))
        heartbeat.start()
        window._loop_finished = lambda _result, _error: loop.quit()
        try:
            window.reapply_all()
            loop.exec()
            self.assertGreaterEqual(len(beats), 5)
            window._apply_loop.assert_called_once()
        finally:
            heartbeat.stop()
            window.jobs.drain()
            window.proc = None
            window.close()

    def test_memory_scan_runs_in_background(self):
        import time
        from trainer import app as app_module
        QApplication.instance() or QApplication([])
        with patch.object(app_module.Process, "find_pids", return_value=[]):
            window = app_module.MainWindow()
        window.t_poll.stop()
        window.hotkey_timer.stop()
        window.proc = Mock()
        window.proc.scan_i32.side_effect = lambda _value: (time.sleep(0.3), [0x1234])[1]
        loop = QEventLoop()
        beats = []
        heartbeat = QTimer()
        heartbeat.setInterval(20)
        heartbeat.timeout.connect(lambda: beats.append(1))
        heartbeat.start()
        window._log_hits = lambda title, hits: (beats.append(title), loop.quit())
        try:
            started = time.monotonic()
            window.run_debug("dbg_scan", "10000")
            self.assertLess(time.monotonic() - started, 0.2)  # returned before the scan ended
            loop.exec()
            self.assertGreaterEqual(len(beats), 5)
            self.assertIn("扫描 10000 -> 1 处命中", beats)
        finally:
            heartbeat.stop()
            window.jobs.drain()
            window.proc = None
            window.close()

    def test_state_is_saved_atomically(self):
        from trainer import app as app_module
        QApplication.instance() or QApplication([])
        with patch.object(app_module.Process, "find_pids", return_value=[]), \
             patch.object(app_module.MainWindow, "_install_hotkeys"):
            window = app_module.MainWindow()
        try:
            window.enabled = {"reveal_map": True}
            self.assertTrue(window.save_state())
            self.assertFalse(os.path.exists(app_module.STATE_FILE + ".tmp"))
            with open(app_module.STATE_FILE, encoding="utf-8") as file:
                self.assertTrue(json.load(file)["reveal_map"])
        finally:
            window.close()

    def test_one_shot_garrison_keeps_qt_event_loop_responsive(self):
        import time
        from trainer import app as app_module
        app = QApplication.instance() or QApplication([])
        with patch.object(app_module.Process, "find_pids", return_value=[]):
            window = app_module.MainWindow()
        window.t_poll.stop()
        window.hotkey_timer.stop()
        window.proc = object()
        window.patches = Mock()
        window.patches.compatible.return_value = True
        window.auto_enter = Mock()
        window.auto_enter.snapshot_selected.return_value = (0x1000, [(0x2000, 7)])
        window.auto_enter.execute.side_effect = lambda _snapshot: (time.sleep(0.3), (1, 1))[1]
        loop = QEventLoop()
        beats = []
        heartbeat = QTimer()
        heartbeat.setInterval(20)
        heartbeat.timeout.connect(lambda: (beats.append(1), window.apply_filter()))
        heartbeat.start()
        window._action_finished = lambda _fid, _result, _error: loop.quit()
        try:
            window.run_action("v_auto_enter")
            loop.exec()
            self.assertGreaterEqual(len(beats), 5)
            window.auto_enter.execute.assert_called_once_with((0x1000, [(0x2000, 7)]))
        finally:
            heartbeat.stop()
            window.jobs.drain()
            window.proc = None
            window.close()

    def _window_with_state(self, state_file):
        from trainer import app as app_module
        QApplication.instance() or QApplication([])
        with patch.object(app_module, "STATE_FILE", state_file), \
             patch.object(app_module.Process, "find_pids", return_value=[]), \
             patch.object(app_module.MainWindow, "_install_hotkeys"):
            window = app_module.MainWindow()
        window.t_poll.stop()
        window.hotkey_timer.stop()
        return window

    def test_unsaved_or_old_auto_saved_state_starts_with_everything_off(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = os.path.join(directory, "state.json")
            # A file an older version wrote automatically: no user-save mark.
            with open(state_file, "w", encoding="utf-8") as file:
                json.dump({"money_no_decrease": True, "infinite_power": True,
                           "_repair_amount": 50, "_airport_factor": 4,
                           "_hotkeys": {"u_clone": "Ctrl+Alt+V"}}, file)
            window = self._window_with_state(state_file)
            try:
                self.assertFalse(any(window.enabled.values()))
                self.assertEqual(window.repair_amount, 0)
                self.assertEqual(window.airport_factor, 1)
                self.assertFalse(window.rows["money_no_decrease"].toggle.isChecked())
                self.assertEqual(window.hotkey_mapping, {"u_clone": "Ctrl+Alt+V"})
            finally:
                window.close()

    def test_only_the_save_button_persists_feature_state(self):
        from trainer import app as app_module
        with tempfile.TemporaryDirectory() as directory:
            state_file = os.path.join(directory, "state.json")
            window = self._window_with_state(state_file)
            try:
                with patch.object(app_module, "STATE_FILE", state_file):
                    window.proc = Mock()
                    window.patches = Mock()
                    window.patches.compatible.return_value = True
                    window.set_patch("money_no_decrease", True)
                    window.value_write("repair_amount", "30")
                    window.rows["credits"].input.setText("5000")
                    window.rows["credits"].input.editingFinished.emit()
                    self.assertFalse(os.path.exists(state_file))
                    window.side_action("保存当前功能状态")
                    with open(state_file, encoding="utf-8") as file:
                        saved = json.load(file)
                    self.assertTrue(saved["_saved_by_user"])
                    self.assertTrue(saved["money_no_decrease"])
                    self.assertEqual(saved["_repair_amount"], 30)
            finally:
                window.proc = window.patches = None
                window.close()
            restored = self._window_with_state(state_file)
            try:
                self.assertTrue(restored.enabled["money_no_decrease"])
                self.assertTrue(restored.rows["money_no_decrease"].toggle.isChecked())
                self.assertEqual(restored.repair_amount, 30)
                self.assertEqual(restored.rows["credits"].input.text(), "5000")
            finally:
                restored.close()

    def test_hotkey_save_keeps_the_saved_feature_state(self):
        from trainer import app as app_module
        with tempfile.TemporaryDirectory() as directory:
            state_file = os.path.join(directory, "state.json")
            with open(state_file, "w", encoding="utf-8") as file:
                json.dump({"_saved_by_user": True, "reveal_map": True}, file)
            window = self._window_with_state(state_file)
            try:
                window.enabled["reveal_map"] = False  # changed, but not saved
                window.hotkey_mapping = {"u_clone": "Ctrl+Alt+X"}
                with patch.object(app_module, "STATE_FILE", state_file):
                    self.assertTrue(window._save_hotkeys())
                with open(state_file, encoding="utf-8") as file:
                    saved = json.load(file)
                self.assertTrue(saved["reveal_map"])
                self.assertEqual(saved["_hotkeys"], {"u_clone": "Ctrl+Alt+X"})
            finally:
                window.close()

    def test_legacy_garrison_toggle_is_ignored_but_hotkey_survives(self):
        import json
        from trainer import app as app_module
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            state_file = os.path.join(directory, "state.json")
            with open(state_file, "w", encoding="utf-8") as file:
                json.dump({"_saved_by_user": True, "v_auto_enter": True, "tank_auto_repair": True,
                           "_hotkeys": {"v_auto_enter": "Ctrl+Shift+G"}}, file)
            with patch.object(app_module, "STATE_FILE", state_file), \
                 patch.object(app_module.Process, "find_pids", return_value=[]), \
                 patch.object(app_module.MainWindow, "_install_hotkeys"):
                window = app_module.MainWindow()
            try:
                self.assertNotIn("v_auto_enter", window.enabled)
                self.assertTrue(window.enabled["tank_auto_repair"])
                self.assertEqual(window.hotkey_mapping["v_auto_enter"], "Ctrl+Shift+G")
                self.assertEqual(window.rows["v_auto_enter"].feat["kind"], "action")
            finally:
                window.close()

    def test_airport_input_draft_does_not_change_committed_factor(self):
        from trainer import app as app_module
        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            state_file = os.path.join(directory, "state.json")
            with open(state_file, "w", encoding="utf-8") as file:
                json.dump({"_saved_by_user": True, "_airport_factor": 3,
                           "_values": {"airport_slots": "16"}}, file)
            with patch.object(app_module, "STATE_FILE", state_file), \
                 patch.object(app_module.Process, "find_pids", return_value=[]), \
                 patch.object(app_module.MainWindow, "_install_hotkeys"):
                window = app_module.MainWindow()
                try:
                    self.assertEqual(window.airport_factor, 3)
                    self.assertEqual(window.rows["airport_slots"].input.text(), "3")
                    window.rows["airport_slots"].input.setText("1")
                    window.save_state()
                    with open(state_file, encoding="utf-8") as file:
                        saved = json.load(file)
                    self.assertEqual(saved["_airport_factor"], 3)
                    self.assertNotIn("airport_slots", saved["_values"])
                    airport = Mock()
                    airport.factor = 3
                    window.airport_slots = airport
                    window.reapply_all()
                    airport.set_factor.assert_not_called()
                finally:
                    window.airport_slots = None
                    window.close()

    def test_sidebar_refresh_uses_existing_background_worker(self):
        import time
        from trainer import app as app_module
        app = QApplication.instance() or QApplication([])
        with patch.object(app_module.Process, "find_pids", return_value=[]):
            window = app_module.MainWindow()
        window.t_poll.stop()
        window.hotkey_timer.stop()
        window.proc = object()
        window.patches = Mock()
        window.patches.compatible.return_value = True
        window.operations = Mock()
        window.build_unlock = Mock()
        window.build_unlock.enabled = set()
        window.build_unlock.set.side_effect = lambda fid, on: window.build_unlock.enabled.add(fid)
        window.build_unlock.refresh_sidebar.side_effect = lambda _executor: time.sleep(0.3)
        window.enabled = {"tech_all": True}
        loop = QEventLoop()
        beats = []
        heartbeat = QTimer()
        heartbeat.setInterval(20)
        heartbeat.timeout.connect(lambda: beats.append(1))
        heartbeat.start()
        window._loop_finished = lambda _result, _error: loop.quit()
        try:
            window.reapply_all()
            loop.exec()
            self.assertGreaterEqual(len(beats), 5)
            window.build_unlock.refresh_sidebar.assert_called_once()
        finally:
            heartbeat.stop()
            window.jobs.drain()
            window.proc = None
            window.close()

    def test_hotkey_parse_conflict_rollback_and_unregister(self):
        app = QApplication.instance() or QApplication([])
        self.assertEqual(parse_shortcut("Ctrl+Alt+C")[:2], (0x4003, ord("C")))
        with self.assertRaises(ValueError):
            parse_shortcut("C")

        class FakeUser32:
            def __init__(self):
                self.registered = {}

            def RegisterHotKey(self, _window, ident, mods, key):
                if key == ord("D"):
                    return False
                self.registered[ident] = (mods, key)
                return True

            def UnregisterHotKey(self, _window, ident):
                self.registered.pop(ident, None)
                return True

        fake = FakeUser32()
        with patch("trainer.hotkeys.user32", fake):
            hotkeys = Hotkeys(lambda _fid: None)
            try:
                hotkeys.replace({"u_clone": "Ctrl+Alt+C"})
                with self.assertRaises(ValueError):
                    hotkeys.replace({"u_clone": "Ctrl+Alt+C", "u_hp": "Ctrl+Alt+D"})
                self.assertEqual(hotkeys.mapping, {"u_clone": "Ctrl+Alt+C"})
                self.assertEqual(len(fake.registered), 1)
                with self.assertRaises(ValueError):
                    hotkeys.replace({"u_clone": "Ctrl+Alt+C", "u_hp": "Ctrl+Alt+C"})
            finally:
                hotkeys.close()
            self.assertFalse(fake.registered)

    def test_empty_hotkeys_stay_empty_after_save_and_reload(self):
        from trainer import app as app_module
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            filename = os.path.join(directory, "state.json")
            with open(filename, "w", encoding="utf-8") as stream:
                json.dump({"_hotkeys": {}}, stream)
            with patch.object(app_module, "STATE_FILE", filename), \
                 patch.object(app_module.Process, "find_pids", return_value=[]):
                window = app_module.MainWindow()
                try:
                    self.assertEqual(window.hotkey_mapping, {})
                    self.assertTrue(window.save_state())
                    with open(filename, encoding="utf-8") as stream:
                        self.assertEqual(json.load(stream)["_hotkeys"], {})
                finally:
                    window.close()

    def test_invalid_clone_destination_never_spawns(self):
        operation = GameOperations.__new__(GameOperations)
        operation.proc = object()
        operation.world_at_screen = Mock(return_value=None)
        operation.spawn_batch = Mock()
        with patch.object(units, "selected_technos") as scan:
            with self.assertRaises(ValueError):
                operation.clone_selected((10, 10, 100, 100))
            scan.assert_not_called()
        operation.spawn_batch.assert_not_called()

    def test_clone_code_lets_the_game_decide_occupancy(self):
        from capstone import Cs, CS_ARCH_X86, CS_MODE_32

        code = clone_batch_code(0x87654321, 3, 0x1000400, 0x1000410, 9, 0x10005C0, 0x10005CC)
        instructions = list(Cs(CS_ARCH_X86, CS_MODE_32).disasm(code, 0x1000000))
        self.assertEqual(sum(i.size for i in instructions), len(code))
        starts = {i.address for i in instructions}
        for instruction in instructions:
            if instruction.mnemonic.startswith("j") or instruction.mnemonic == "call":
                if instruction.op_str.startswith("0x"):
                    self.assertIn(int(instruction.op_str, 16), starts)
        text = [(i.mnemonic, i.op_str) for i in instructions]
        # No home-made "cell must be empty" test any more: CanEnterCell decides.
        self.assertNotIn(("test", "dword ptr [ebx + 0x124], 0xff"), text)
        self.assertIn(("test", "dword ptr [ebx + 0x140], 0x100"), text)
        self.assertIn(("call", "dword ptr [eax + 0x194]"), text)
        self.assertIn(("call", "dword ptr [eax + 0xd4]"), text)
        self.assertIn(("call", "dword ptr [eax + 0x20]"), text)
        self.assertEqual(len(clone_record((2560, 5120, 0))), 48)
        self.assertLess(len(code), 0x400)

    def _clone_fixture(self, selected, anchor, placed=None):
        operation = GameOperations.__new__(GameOperations)
        operation.proc = object()
        operation.world_at_screen = Mock(return_value=anchor)
        operation.spawn_batch = Mock(side_effect=placed or (lambda types, _d, _h: [0x9000] * len(types)))
        patches = (
            patch.object(units, "selected_technos", return_value=selected),
            patch.object(units, "valid_techno", return_value=True),
            patch.object(units, "type_of", side_effect=lambda _, addr, __: addr + 0x1000),
            patch.object(units, "player_house", return_value=0x4000),
            patch.object(units, "clear_cache"),
        )
        return operation, patches

    def test_all_selected_units_go_out_in_one_batch(self):
        selected = [(0x1000 + i * 0x100, 0x7A3540) for i in range(40)]  # beyond the old cap of 20
        operation, patches = self._clone_fixture(selected, (10 * 256 + 5, 20 * 256 + 7, 0))
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            self.assertEqual(operation.clone_selected((100, 100, 300, 300)), (40, 40))
        operation.spawn_batch.assert_called_once()
        types, destinations, house = operation.spawn_batch.call_args.args
        self.assertEqual(len(types), 40)
        self.assertEqual(house, 0x4000)
        self.assertEqual(destinations[0], (10 * 256 + 5, 20 * 256 + 7, 0))
        self.assertEqual({(x >> 8, y >> 8) for x, y, _ in destinations[1:9]},
                         {(10 + dx, 20 + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)} - {(10, 20)})
        operation.world_at_screen.assert_called_once_with((100, 100))

    def test_destination_rings_grow_with_the_batch(self):
        small = GameOperations._clone_destinations((2560, 5120, 0), 1)
        large = GameOperations._clone_destinations((100 * 256, 100 * 256, 0), 300)
        self.assertEqual(len(small), 81)
        self.assertGreaterEqual(len(large), 600)
        edge = GameOperations._clone_destinations((128, 128, 0), 1)
        self.assertTrue(all(x >= 0 and y >= 0 for x, y, _ in edge))

    def test_partial_placement_is_reported(self):
        selected = [(0x1000, 0x7ADDF8), (0x2000, 0x7A3540)]
        operation, patches = self._clone_fixture(
            selected, (2560, 5120, 0), placed=lambda types, _d, _h: [0x9000, 0])
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            self.assertEqual(operation.clone_selected((100, 100, 300, 300)), (1, 2))

    def test_transfer_is_one_batch(self):
        operation = GameOperations.__new__(GameOperations)
        operation.proc = Mock()
        # UniqueID at +0x10, Owner at +0x1B4: the snapshot the game thread rechecks.
        operation.proc.read_u32.side_effect = lambda a: 0x4000 if a & 0xFFF == 0x1B4 else a
        operation.transfer_batch = Mock(return_value=30)
        selected = [(0x1000 + i * 0x1000, 0x7ADDF8) for i in range(30)]
        with patch.object(units, "selected_technos", return_value=selected), \
             patch.object(units, "valid_techno", return_value=True), \
             patch.object(units, "clear_cache"):
            self.assertEqual(operation.transfer_selected(0x5000), (30, 30))
        operation.transfer_batch.assert_called_once_with(
            [(a, vt, a + 0x10, 0x4000) for a, vt in selected], 0x5000)

    def test_too_many_selected_is_refused(self):
        from trainer.operations import MAX_CLONES
        selected = [(0x1000 + i, 0x7A3540) for i in range(MAX_CLONES + 1)]
        operation, patches = self._clone_fixture(selected, (2560, 5120, 0))
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaises(ValueError):
                operation.clone_selected((100, 100, 300, 300))
        operation.spawn_batch.assert_not_called()

if __name__ == "__main__":
    unittest.main()
