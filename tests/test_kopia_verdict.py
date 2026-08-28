"""How a finished snapshot is graded — and why «warn» was the wrong answer.

On 2026-08-26 the Kopia backup of a ~1.8 TB source produced three snapshots of 3745 / 205 /
4068 files. Its source was a NAS mounted over sshfs whose SFTP session collapsed roughly once
a second under the read load, so kopia hit EIO on open/readdir and dropped whole directories.
Every one of those runs was graded «warn» and shown next to a green restore drill (the drill
samples ONE small file — it answers «is the repository readable», never «is the source in
there»). Meanwhile the repository on the destination grew to 452 GB that no snapshot
referenced. The verdict below is what turns that into a red run.
"""
import importlib.util
import os
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


STRICT = {"id": "b1", "rules": {"ignore_file_errors": False, "ignore_dir_errors": False}}
LENIENT = {"id": "b1", "rules": {"ignore_file_errors": True, "ignore_dir_errors": True}}
FOLDERS = ["/mnt/remote/Ugreen-NAS"]


def verdict(bk=STRICT, files=4068, size=1_225_642_701, failed=0, folders=None,
            retried=False, rc=0, history=()):
    with mock.patch.object(nas, "kp_history", return_value=list(history)):
        return nas._kp_verdict(bk, "b1", 2_000_000, files, size, failed,
                               FOLDERS if folders is None else folders, retried, rc)


class UnreadableEntryTests(unittest.TestCase):
    def test_the_real_run_of_2026_08_26_is_now_an_error(self):
        res, err = verdict(failed=246)
        self.assertEqual(res, "error")
        self.assertIn("246", err)
        self.assertIn("does NOT cover the whole source", err)

    def test_a_single_unreadable_file_is_still_an_error_by_default(self):
        # kopia itself calls it fatal when the rules do not say otherwise
        self.assertEqual(verdict(failed=1)[0], "error")

    def test_singular_wording(self):
        self.assertIn("1 entry", verdict(failed=1)[1])

    def test_opting_in_downgrades_it_to_a_warning(self):
        res, err = verdict(bk=LENIENT, failed=246)
        self.assertEqual(res, "warn")
        self.assertIn("rules say to ignore them", err)

    def test_half_an_opt_in_is_not_an_opt_in(self):
        # ignoring files but not folders still loses whole subtrees silently
        bk = {"id": "b1", "rules": {"ignore_file_errors": True, "ignore_dir_errors": False}}
        self.assertEqual(verdict(bk=bk, failed=3)[0], "error")

    def test_a_clean_run_is_ok(self):
        self.assertEqual(verdict(), ("ok", ""))


class EmptySnapshotTests(unittest.TestCase):
    def test_zero_files_is_an_error_not_a_success(self):
        # an unmounted source reads as an empty directory; retention would then age the
        # real snapshots out behind an empty one
        res, err = verdict(files=0, size=0)
        self.assertEqual(res, "error")
        self.assertIn("is the source mounted", err)

    def test_no_source_folders_at_all_is_left_to_the_caller(self):
        self.assertEqual(verdict(files=0, size=0, folders=[]), ("ok", ""))


class ShrinkTests(unittest.TestCase):
    @staticmethod
    def hist(files, size, result="ok", ts=1_000_000):
        return {"backup": "b1", "ts": ts, "result": result, "files": files, "bytes": size}

    def test_losing_most_of_the_source_is_an_error(self):
        res, err = verdict(files=205, size=650_000_000,
                           history=[self.hist(3745, 1_089_787_999)])
        self.assertEqual(res, "error")
        self.assertIn("205 files instead of 3745", err)

    def test_a_small_drop_is_fine(self):
        self.assertEqual(verdict(files=3600, size=1_000_000_000,
                                 history=[self.hist(3745, 1_089_787_999)]), ("ok", ""))

    def test_growth_is_fine(self):
        self.assertEqual(verdict(files=9000, size=9_000_000_000,
                                 history=[self.hist(3745, 1_089_787_999)]), ("ok", ""))

    def test_a_broken_run_never_becomes_the_baseline(self):
        """The heart of it: 3745 → 205 → 4068, each graded «warn», each unremarkable beside
        the one before. Comparing against the last run instead of the last GOOD run lets a
        backup normalise its own breakage."""
        history = [self.hist(3745, 1_089_787_999, result="ok", ts=1_000_000),
                   self.hist(205, 650_000_000, result="warn", ts=1_500_000)]
        res, err = verdict(files=300, size=700_000_000, history=history)
        self.assertEqual(res, "error")
        self.assertIn("instead of 3745", err)      # the good run, not the broken one

    def test_a_tiny_baseline_is_not_used(self):
        # a handful of files makes the ratio noise, not signal
        self.assertEqual(verdict(files=1, size=10, history=[self.hist(10, 1000)]), ("ok", ""))

    def test_a_brand_new_backup_has_no_baseline(self):
        self.assertEqual(verdict(files=3, size=10, history=[]), ("ok", ""))

    def test_another_backups_history_is_not_borrowed(self):
        other = dict(self.hist(9999, 9_000_000_000), backup="b2")
        self.assertEqual(verdict(files=4068, size=1_225_642_701, history=[other]), ("ok", ""))

    def test_bytes_alone_can_trip_it(self):
        # same file count, a fraction of the content: big files stopped being readable
        res, err = verdict(files=3700, size=100_000_000,
                           history=[self.hist(3745, 1_089_787_999)])
        self.assertEqual(res, "error")
        self.assertIn("instead of", err)

    def test_unreadable_entries_outrank_a_shrink(self):
        # both true → report the cause, not the symptom
        res, err = verdict(files=205, size=650_000_000, failed=34,
                           history=[self.hist(3745, 1_089_787_999)])
        self.assertEqual(res, "error")
        self.assertIn("could not be read", err)


class RetryTests(unittest.TestCase):
    def test_a_retried_but_complete_snapshot_is_a_warning(self):
        res, err = verdict(retried=True)
        self.assertEqual(res, "warn")
        self.assertIn("second attempt", err)

    def test_a_non_zero_exit_with_a_manifest_is_a_warning(self):
        self.assertEqual(verdict(rc=1)[0], "warn")


class HalfMountedSourceTests(unittest.TestCase):
    """An SMB connection mounts one share per subfolder. Lose a share and the source folder
    is still non-empty, so the driver's empty-folder check sees nothing wrong — and the
    snapshot comes out short by everything that share held."""

    REM = [{"id": "Ugreen-NAS", "name": "Ugreen NAS", "kind": "smb",
            "host": "192.168.1.95", "shares": ["Cloud", "PMedia"]}]

    def test_a_half_mounted_connection_refuses_the_run(self):
        with mock.patch.object(nas, "_remotes_load", return_value=self.REM), \
                mock.patch.object(nas, "_remote_mounted", return_value=False):
            msg = nas._kp_remote_incomplete(["/mnt/remote/Ugreen-NAS"])
        self.assertIn("not fully mounted", msg)
        self.assertIn("Ugreen NAS", msg)

    def test_a_subfolder_of_the_connection_counts_too(self):
        with mock.patch.object(nas, "_remotes_load", return_value=self.REM), \
                mock.patch.object(nas, "_remote_mounted", return_value=False):
            self.assertTrue(nas._kp_remote_incomplete(["/mnt/remote/Ugreen-NAS/Cloud"]))

    def test_a_parent_of_the_connection_counts_too(self):
        with mock.patch.object(nas, "_remotes_load", return_value=self.REM), \
                mock.patch.object(nas, "_remote_mounted", return_value=False):
            self.assertTrue(nas._kp_remote_incomplete(["/mnt/remote"]))

    def test_a_fully_mounted_connection_is_fine(self):
        with mock.patch.object(nas, "_remotes_load", return_value=self.REM), \
                mock.patch.object(nas, "_remote_mounted", return_value=True):
            self.assertEqual(nas._kp_remote_incomplete(["/mnt/remote/Ugreen-NAS"]), "")

    def test_a_local_source_is_never_blocked_by_a_dead_connection(self):
        with mock.patch.object(nas, "_remotes_load", return_value=self.REM), \
                mock.patch.object(nas, "_remote_mounted", return_value=False):
            self.assertEqual(nas._kp_remote_incomplete(["/mnt/storage/Photos"]), "")

    def test_a_similar_prefix_is_not_the_same_folder(self):
        # /mnt/remote/Ugreen-NAS-old must not be taken for /mnt/remote/Ugreen-NAS
        with mock.patch.object(nas, "_remotes_load", return_value=self.REM), \
                mock.patch.object(nas, "_remote_mounted", return_value=False):
            self.assertEqual(nas._kp_remote_incomplete(["/mnt/remote/Ugreen-NAS-old"]), "")

    def test_an_unreadable_remotes_file_does_not_block_backups(self):
        with mock.patch.object(nas, "_remotes_load", side_effect=OSError("gone")):
            self.assertEqual(nas._kp_remote_incomplete(["/mnt/remote/Ugreen-NAS"]), "")


if __name__ == "__main__":
    unittest.main()
