import importlib.util
import os
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


class KopiaDrillTests(unittest.TestCase):
    def test_failed_restore_is_reported(self):
        manifests = [{"source": {"path": "/source"},
                      "rootEntry": {"obj": "k" + "a" * 32}}]
        entry = {"dir": False, "size": 3, "name": "file.txt", "oid": "b" * 32}
        lines = []
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(nas, "NAS_CONFIG", td), \
                mock.patch.object(nas, "kp_snap_ls", return_value={"ok": True, "entries": [entry]}), \
                mock.patch.object(nas.os.path, "isfile", return_value=True), \
                mock.patch.object(nas.os.path, "getsize", return_value=3), \
                mock.patch.object(nas, "_kp", return_value={"ok": False, "out": "", "err": "read failed"}):
            result = nas._kp_drill("abcdef", manifests, lines.append)
        self.assertGreater(result["attempted"], 0)
        self.assertEqual(result["checked"], 0)
        self.assertGreater(result["failed"], 0)
        self.assertTrue(any("read failed" in line for line in lines))


class ScheduleTests(unittest.TestCase):
    @staticmethod
    def mirror_profile():
        return {"id": "main", "name": "Mirror", "saved": 0,
                "schedule": {"enabled": True, "freq": "daily", "time": "03:00"}}

    def test_mirror_slot_is_not_marked_when_start_fails(self):
        cfg = self.mirror_profile()
        with mock.patch.object(nas, "nb_profiles", return_value=[cfg]), \
                mock.patch.object(nas, "_nb_sched_last_due", return_value=(1000, "slot")), \
                mock.patch.object(nas, "_nb_last_real_run", return_value=0), \
                mock.patch.object(nas.time, "time", return_value=1100), \
                mock.patch.object(nas, "nb_run_bg", return_value={"ok": False, "log": "launch failed"}), \
                mock.patch.object(nas, "_nb_sched_mark") as mark, \
                mock.patch.object(nas, "_nb_queue_drain"), \
                mock.patch.object(nas, "notify_event"):
            nas._nb_sched_tick()
        mark.assert_not_called()

    def test_mirror_slot_is_marked_after_successful_start(self):
        cfg = self.mirror_profile()
        with mock.patch.object(nas, "nb_profiles", return_value=[cfg]), \
                mock.patch.object(nas, "_nb_sched_last_due", return_value=(1000, "slot")), \
                mock.patch.object(nas, "_nb_last_real_run", return_value=0), \
                mock.patch.object(nas.time, "time", return_value=1100), \
                mock.patch.object(nas, "nb_run_bg", return_value={"ok": True}), \
                mock.patch.object(nas, "_nb_sched_mark") as mark, \
                mock.patch.object(nas, "_nb_queue_drain"):
            nas._nb_sched_tick()
        mark.assert_called_once_with("main", "slot")

    @staticmethod
    def kopia_config():
        return {"sources": [], "dests": [],
                "backups": [{"id": "abcdef", "name": "Snapshots", "enabled": True,
                             "schedule": {"mode": "daily", "time": "03:00"}}]}

    def test_kopia_slot_is_not_marked_when_start_fails(self):
        state = {"done": {}}
        with mock.patch.object(nas, "kopia_installed", return_value=True), \
                mock.patch.object(nas, "kp_load", return_value=self.kopia_config()), \
                mock.patch.object(nas, "_kp_state_load", return_value=state), \
                mock.patch.object(nas, "sched_last_due", return_value=(1000, "slot")), \
                mock.patch.object(nas, "_kp_last_run_ts", return_value=0), \
                mock.patch.object(nas.time, "time", return_value=1100), \
                mock.patch.object(nas, "kp_run_start", return_value={"ok": False, "log": "source missing"}), \
                mock.patch.object(nas, "_json_save"), \
                mock.patch.object(nas, "_kp_srv_tick"), \
                mock.patch.object(nas, "_kp_health"), \
                mock.patch.object(nas, "kp_update_info"), \
                mock.patch.object(nas, "notify_event"):
            nas._kopia_tick()
        self.assertNotIn("abcdef", state["done"])

    def test_kopia_busy_queue_is_not_recreated_each_tick(self):
        cfg = self.kopia_config()
        state = {"done": {}, "pending": {"abcdef": 1000}}
        with mock.patch.object(nas, "kopia_installed", return_value=True), \
                mock.patch.object(nas, "kp_load", return_value=cfg), \
                mock.patch.object(nas, "_kp_state_load", return_value=state), \
                mock.patch.object(nas.time, "time", return_value=1100), \
                mock.patch.object(nas, "kp_run_start", return_value={"ok": False, "log": "destination is busy"}) as start, \
                mock.patch.object(nas, "_json_save"), \
                mock.patch.object(nas, "_kp_srv_tick"), \
                mock.patch.object(nas, "_kp_health"), \
                mock.patch.object(nas, "kp_update_info"):
            nas._kopia_tick()
        start.assert_called_once_with("abcdef")
        self.assertEqual(state["pending"]["abcdef"], 1000)


if __name__ == "__main__":
    unittest.main()
