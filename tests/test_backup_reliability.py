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




class AlreadyRunningTests(unittest.TestCase):
    """A run that is ALREADY going satisfies its own schedule slot. Treating that as a failed
    start raised «could not start» and re-attempted every minute until the run finished."""

    def test_mirror_slot_is_marked_when_the_run_is_already_going(self):
        marks, notes = [], []
        profile = {"id": "p1", "name": "P", "saved": 0,
                   "schedule": {"enabled": True, "freq": "daily", "time": "15:00"}}
        with mock.patch.object(nas, "nb_profiles", lambda: [profile]), \
             mock.patch.object(nas, "nb_run_bg",
                               lambda pid, dry=False, allow_delete=False:
                               {"ok": False, "log": "already running"}), \
             mock.patch.object(nas, "_nb_last_real_run", lambda pid: 0), \
             mock.patch.object(nas, "_nb_sched_mark",
                               lambda pid, label: marks.append((pid, label))), \
             mock.patch.object(nas, "_json_load_strict", lambda path, default: default), \
             mock.patch.object(nas, "notify_event",
                               lambda *a, **k: notes.append(a[0]) or True), \
             mock.patch.object(nas, "_nb_queue_drain", lambda: None):
            nas._nb_sched_tick()
        self.assertEqual(len(marks), 1, "an already-running backup must consume its slot")
        self.assertEqual(notes, [], "and must not report a failure to start")


class DrillSampleTests(unittest.TestCase):
    """No file small enough to spot-check is a limit of the sampler, not a bad backup."""

    def test_empty_sample_is_not_a_failure(self):
        logged = []
        manifests = [{"source": {"path": "/source"}, "rootEntry": {"obj": "k" + "a" * 32}}]
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(nas, "NAS_CONFIG", td), \
             mock.patch.object(nas, "_kp_drill_pick", lambda *a, **k: None):
            out = nas._kp_drill("dest", manifests, logged.append)
        self.assertEqual(out["attempted"], 0, "nothing was samplable")
        self.assertEqual(out["failed"], 0, "and nothing failed — the two are not the same")
        self.assertEqual(out["rot"], 0)


if __name__ == "__main__":
    unittest.main()


class MassChangeGuardTests(unittest.TestCase):
    """Ransomware rewrites, it does not delete — the change guard covers what
    --max-delete never sees. The e2e (baseline -> refuse -> override) was proven
    live; these pin the arithmetic and the stats parsing it depends on."""

    def test_limit_scales_with_the_baseline(self):
        self.assertEqual(nas._nb_change_limit(1000, {"change_guard_pct": 50}), 500)
        self.assertEqual(nas._nb_change_limit(10000, {"change_guard_pct": 30}), 3000)

    def test_small_baselines_use_the_floor(self):
        # 50% of 60 files is 30 — but rewriting 31 files is normal life, not malware
        self.assertEqual(nas._nb_change_limit(60, {"change_guard_pct": 50}),
                         nas.NB_CHANGE_MIN)

    def test_stats_transfer_count_parses_with_thousands_separators(self):
        st = nas._nb_parse_stats([
            "Number of files: 12,345 (reg: 12,000, dir: 345)",
            "Number of regular files transferred: 6,789",
            "Number of deleted files: 42",
        ])
        self.assertEqual(st["xfer"], 6789)
        self.assertEqual(st["deleted"], 42)
