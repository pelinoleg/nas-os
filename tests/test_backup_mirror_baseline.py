"""A mirror that cannot finish, and a panel that does not say so.

2026-08-27/28, the Ugreen mirror on this box, three separate failures that add up to
"the backup ran every night and there was no backup":

  * the destination filled at 33 % and rsync went on failing every single write for two
    hours — 16 310 identical ENOSPC errors — while the panel said "Backup is running…";
  * the folder that never finished had no deletion-guard baseline, so every following run
    refused it outright, and a SCHEDULED run cannot tick "the source is intact" — the
    refusal was permanent, and reported as a warning;
  * the speed limit is a KB/s field, so "50" (meaning 50 MB/s) capped the run at 50 KB/s.

These tests hold the three fixes in place. They drive the real nb_run loop with a fake
rsync process: the point is the decisions around the transfer, not the transfer.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


class _FakeRsync:
    """Just enough of Popen for the run loop: lines, then an exit code."""

    def __init__(self, lines, code=0):
        self._lines = list(lines)
        self.returncode = code
        self.killed = False
        self.stdout = self

    def readline(self):
        return self._lines.pop(0) if self._lines else ""

    def close(self):
        pass

    def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class MirrorRunCase(unittest.TestCase):
    """One pull profile, one folder, a real destination on disk."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.dest = os.path.join(self.td, "Mirror", "home")
        os.makedirs(self.dest)
        self.state = os.path.join(self.td, "state")
        os.makedirs(self.state)
        self.log = []
        self.argv = []

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def copy(self, files=3):
        """Put a copy in the destination — the thing the deletion guard protects."""
        for i in range(files):
            with open(os.path.join(self.dest, "f%d" % i), "w") as f:
                f.write("x")

    def cfg(self):
        return {"id": "main", "name": "T", "direction": "pull", "transport": "rsync",
                "host": "nas.local", "user": "u", "password": "p", "ssh_port": 22,
                "auth": "password", "dest_mode": "per", "dest_base": os.path.dirname(self.dest),
                "jobs": [{"src": "home", "dest": self.dest, "enabled": True}],
                "excludes": [], "delete_mode": "archive", "deleted_dir": "_deleted/{date}",
                "max_delete_pct": 20, "change_guard_pct": 50, "bwlimit": 0,
                "verify": False, "drill_auto": False,
                "schedule": {"enabled": False, "freq": "daily", "time": "03:00", "dow": "Sun"}}

    def run_mirror(self, proc, probe=7, **kw):
        """Drive nb_run with a fake rsync. `probe` — what listing the source returns."""
        def popen(args, **_kw):
            self.argv.append(list(args))
            return proc
        with mock.patch.object(nas, "NAS_CONFIG", self.state), \
                mock.patch.object(nas, "nb_test", return_value={"ok": True}), \
                mock.patch.object(nas, "_nb_valid_dest", return_value=True), \
                mock.patch.object(nas, "_dest_disk_absent", return_value=False), \
                mock.patch.object(nas, "nb_dest_fs", return_value="ext4"), \
                mock.patch.object(nas, "_nb_src_probe", return_value=probe), \
                mock.patch.object(nas, "_du_bytes", return_value=1), \
                mock.patch.object(nas, "_nb_owner_access"), \
                mock.patch.object(nas, "_nb_prune", return_value=0), \
                mock.patch.object(nas, "nb_drill_start"), \
                mock.patch.object(nas.subprocess, "Popen", popen):
            return nas.nb_run(self.cfg(), False, self.log.append, **kw)


class NoBaselineIsProvedNotAssumed(MirrorRunCase):
    """A first run against a populated destination used to be refused forever."""

    OK = ["Number of files: 1,000\n", "Number of regular files transferred: 900\n",
          "Total transferred file size: 9,000\n"]

    def test_a_non_empty_source_lets_the_first_run_through(self):
        self.copy(5)
        r = self.run_mirror(_FakeRsync(self.OK), probe=7)
        self.assertTrue(r["jobs"][0]["ok"], "a first run was refused although the source has content")
        self.assertNotEqual(r["jobs"][0].get("block"), "nobaseline")

    def test_the_guard_is_armed_from_the_copy_itself(self):
        self.copy(5)
        self.run_mirror(_FakeRsync(self.OK), probe=7)
        rsync = [a for a in self.argv if "--list-only" not in a][0]
        # 20 % of the 5 files already in the copy — a cap, where before there was none
        self.assertIn("--max-delete=1", rsync,
                      "the first run deleted without any cap at all")

    def test_an_empty_source_is_still_refused(self):
        self.copy(5)
        r = self.run_mirror(_FakeRsync(self.OK), probe=0)
        j = r["jobs"][0]
        self.assertFalse(j["ok"])
        self.assertEqual(j["block"], "empty")
        self.assertEqual(len(self.argv), 0, "rsync ran against a source that lists nothing")

    def test_a_source_that_cannot_be_listed_is_still_refused(self):
        self.copy(5)
        r = self.run_mirror(_FakeRsync(self.OK), probe=None)
        j = r["jobs"][0]
        self.assertFalse(j["ok"])
        self.assertEqual(j["block"], "nobaseline")
        self.assertEqual(len(self.argv), 0, "rsync ran against a source that could not be read")

    def test_an_empty_destination_needs_no_probe_at_all(self):
        r = self.run_mirror(_FakeRsync(self.OK), probe=0)   # nothing to lose → nothing to guard
        self.assertTrue(r["jobs"][0]["ok"])

    def test_the_mass_change_guard_does_not_fire_on_the_first_fill(self):
        # the change guard compares against "what the copy held last time". A baseline taken
        # from a half-filled copy would read the REST of the first fill as a mass rewrite and
        # refuse exactly the run meant to finish it.
        self.copy(300)
        r = self.run_mirror(_FakeRsync(self.OK), probe=7)
        self.assertTrue(r["jobs"][0]["ok"])
        self.assertFalse(any("--dry-run" in a for a in self.argv),
                         "the mass-change probe ran off a baseline that is not evidence")


class AFullDestinationStopsTheRun(MirrorRunCase):
    """ENOSPC is the end of the job, not one more error in the log."""

    NOSPC = ["rsync: [receiver] mkstemp \"/mnt/storage/Mirror/home/.a\" failed: "
             "No space left on device (28)\n",
             "rsync: [receiver] mkstemp \"/mnt/storage/Mirror/home/.b\" failed: "
             "No space left on device (28)\n"]

    def test_the_job_stops_at_the_first_failed_write(self):
        self.copy(5)
        proc = _FakeRsync(self.NOSPC, code=23)
        r = self.run_mirror(proc, probe=7)
        self.assertTrue(proc.killed, "rsync was left running with nowhere to write")
        self.assertTrue(r["jobs"][0].get("nospc"))
        self.assertFalse(r["jobs"][0]["ok"])

    def test_it_is_never_reported_as_stopped_by_hand(self):
        self.copy(5)
        r = self.run_mirror(_FakeRsync(self.NOSPC, code=23), probe=7)
        self.assertFalse(r["jobs"][0].get("stopped"),
                         "a full destination was painted as «you pressed Stop»")
        self.assertIn("No space left", r["jobs"][0].get("err") or "")

    def test_the_reason_is_in_the_log_in_words(self):
        self.copy(5)
        self.run_mirror(_FakeRsync(self.NOSPC, code=23), probe=7)
        self.assertTrue(any("OUT OF SPACE" in l for l in self.log))


class ErrorsAreVisibleWhileTheRunLasts(MirrorRunCase):
    """The status file is all the panel has during a run."""

    def test_the_first_error_reaches_the_status_immediately(self):
        self.copy(5)
        lines = ["rsync: [receiver] chgrp \"x\" failed: Operation not permitted (1)\n"] * 3
        seen = []
        self.run_mirror(_FakeRsync(lines, code=23), probe=7, on_stat=seen.append)
        self.assertTrue(seen, "a folder failing on every file still looked like a folder in progress")
        self.assertEqual(seen[0]["errn"], 1)
        self.assertEqual(seen[0]["src"], "home")

    def test_the_driver_writes_it_where_the_panel_reads_it(self):
        with mock.patch.object(nas, "NAS_CONFIG", self.state):
            nas._nb_run_state_write("main", {"running": True, "cur": "home"})
            st = nas._nb_run_state_read("main")
            st["cur_errn"], st["cur_err"] = 50, "rsync: … No space left on device (28)"
            nas._nb_run_state_write("main", st)
            with open(nas.nb_run_state("main")) as f:
                back = json.load(f)
        self.assertEqual(back["cur_errn"], 50)


class TheSpeedLimitTakesTheUnitYouMean(unittest.TestCase):
    """The field is KB/s; nobody thinks about a backup in kilobytes."""

    def test_a_bare_number_is_still_kilobytes(self):
        self.assertEqual(nas._nb_bw_kb("5000"), 5000)
        self.assertEqual(nas._nb_bw_kb(5000), 5000)

    def test_megabytes_are_understood(self):
        for v in ("50M", "50 MB", "50 MB/s", "50mb/s", "50 m"):
            self.assertEqual(nas._nb_bw_kb(v), 51200, v)

    def test_gigabytes_and_fractions(self):
        self.assertEqual(nas._nb_bw_kb("1.5G"), 1572864)
        self.assertEqual(nas._nb_bw_kb("1,5 GB/s"), 1572864)

    def test_nothing_means_no_limit_and_nonsense_means_no_change(self):
        self.assertEqual(nas._nb_bw_kb("0"), 0)
        self.assertEqual(nas._nb_bw_kb(""), 0)
        self.assertIsNone(nas._nb_bw_kb("as fast as possible"))
        self.assertIsNone(nas._nb_bw_kb("50 Mbit"))     # bits, not bytes — do not guess

    def test_saving_the_profile_resolves_the_unit(self):
        """The value only exists in KB/s once nb_save has been through it."""
        cur = dict(nas._nb_defaults(), id="main", name="T", bwlimit=0)
        with mock.patch.object(nas, "nb_load", return_value=cur), \
                mock.patch.object(nas, "nb_profiles", return_value=[cur]), \
                mock.patch.object(nas, "_nb_write_profiles"):
            out = nas.nb_save({"bwlimit": "50 MB/s"}, "main")
        self.assertEqual(out["bwlimit"], 51200)

    def test_a_value_it_cannot_read_leaves_the_old_one_alone(self):
        cur = dict(nas._nb_defaults(), id="main", name="T", bwlimit=51200)
        with mock.patch.object(nas, "nb_load", return_value=cur), \
                mock.patch.object(nas, "nb_profiles", return_value=[cur]), \
                mock.patch.object(nas, "_nb_write_profiles"):
            out = nas.nb_save({"bwlimit": "quickly"}, "main")
        self.assertEqual(out["bwlimit"], 51200)


if __name__ == "__main__":
    unittest.main()
