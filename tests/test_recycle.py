"""SMB recycle-bin sweep: the code that DELETES user data gets tests first.

The sweep runs nightly as root over every share's .recycle. The cases below pin
the three properties that make it safe: age is honoured (fresh files survive),
days=0 means never, and a symlink pointing outside the share must not let the
walk delete anything beyond the bin.
"""
import importlib.util
import os
import time
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


def _mk(path, age_days=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("x")
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))


class RecycleSweepTests(unittest.TestCase):
    def _sweep(self, share, days=30):
        with mock.patch.object(nas, "smb_get_shares",
                               return_value=[{"name": "t", "path": share}]), \
                mock.patch.object(nas, "smb_recycle_days", return_value=days):
            return nas.smb_recycle_sweep()

    def test_old_files_go_fresh_files_stay(self):
        with tempfile.TemporaryDirectory() as sh:
            _mk(os.path.join(sh, ".recycle", "sub", "old.txt"), age_days=40)
            _mk(os.path.join(sh, ".recycle", "fresh.txt"), age_days=1)
            r = self._sweep(sh, days=30)
        self.assertEqual(r["removed"], 1)

    def test_emptied_subdirs_are_pruned(self):
        with tempfile.TemporaryDirectory() as sh:
            _mk(os.path.join(sh, ".recycle", "a", "b", "old.txt"), age_days=40)
            self._sweep(sh, days=30)
            self.assertFalse(os.path.exists(os.path.join(sh, ".recycle", "a")))
            # the bin root itself stays: Samba recreates it anyway, and its absence
            # would flip-flop in directory listings
            self.assertTrue(os.path.isdir(os.path.join(sh, ".recycle")))

    def test_days_zero_keeps_everything(self):
        with tempfile.TemporaryDirectory() as sh:
            _mk(os.path.join(sh, ".recycle", "ancient.txt"), age_days=3650)
            r = self._sweep(sh, days=0)
            self.assertEqual(r.get("removed", 0), 0)
            self.assertTrue(os.path.exists(os.path.join(sh, ".recycle", "ancient.txt")))

    def test_symlink_target_outside_the_share_survives(self):
        # an old symlink IN the bin may be unlinked — but the file it points to,
        # outside the share, must never be touched
        with tempfile.TemporaryDirectory() as sh, tempfile.TemporaryDirectory() as outside:
            victim = os.path.join(outside, "precious.txt")
            _mk(victim, age_days=40)
            os.makedirs(os.path.join(sh, ".recycle"))
            ln = os.path.join(sh, ".recycle", "link")
            os.symlink(victim, ln)
            old = time.time() - 40 * 86400
            os.utime(ln, (old, old), follow_symlinks=False)
            self._sweep(sh, days=30)
            self.assertTrue(os.path.exists(victim), "sweep followed a symlink out of the bin")
            self.assertFalse(os.path.lexists(ln))

    def test_bin_that_is_a_symlink_is_skipped_entirely(self):
        # .recycle itself pointing elsewhere = someone is playing games; realpath
        # containment must refuse the whole bin
        with tempfile.TemporaryDirectory() as sh, tempfile.TemporaryDirectory() as outside:
            _mk(os.path.join(outside, "old.txt"), age_days=40)
            os.symlink(outside, os.path.join(sh, ".recycle"))
            r = self._sweep(sh, days=30)
            self.assertEqual(r.get("removed", 0), 0)
            self.assertTrue(os.path.exists(os.path.join(outside, "old.txt")))


if __name__ == "__main__":
    unittest.main()
