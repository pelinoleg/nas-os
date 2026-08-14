"""The pool's create policy is a decision, not a default — this test locks it.

2026-08-14 the owner chose path preservation for /mnt/storage: with no SnapRAID parity a
dead branch takes its files either way, and the only question is which ones. Under `mfs`
a folder's files are spread over every branch, so losing one disk punches a hole in EVERY
folder; under `epmfs` a folder lives on one branch and whole folders are lost — something
that can be named and re-fetched.

Nothing in the code reads this string back, so a future edit could quietly restore `mfs`
and no one would notice until a disk died. Hence a test on the literal.
"""
import os
import re
import unittest

WIZARD = os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-wizard.sh")


def _opts(name):
    """The value of a MERGERFS_*OPTS assignment in the wizard."""
    with open(WIZARD, encoding="utf-8") as f:
        m = re.search(r'^%s="([^"]*)"' % re.escape(name), f.read(), re.M)
    return m.group(1) if m else ""


class PoolCreatePolicy(unittest.TestCase):

    def test_both_option_sets_exist(self):
        # fstab-style opts and the systemd service opts are separate strings; changing one
        # and not the other is exactly how the two would drift apart.
        self.assertTrue(_opts("MERGERFS_OPTS"), "MERGERFS_OPTS not found in the wizard")
        self.assertTrue(_opts("MERGERFS_SVC_OPTS"), "MERGERFS_SVC_OPTS not found in the wizard")

    def test_create_policy_is_path_preserving(self):
        for name in ("MERGERFS_OPTS", "MERGERFS_SVC_OPTS"):
            opts = _opts(name)
            self.assertIn("category.create=epmfs", opts,
                          "%s lost the path-preserving create policy" % name)
            self.assertNotIn("category.create=mfs", opts,
                             "%s fell back to the spreading policy" % name)

    def test_free_space_is_reported_per_branch(self):
        # Without this, statvfs() inside a folder pinned to one disk reports the whole pool
        # and every space check in the panel promises room that folder does not have.
        for name in ("MERGERFS_OPTS", "MERGERFS_SVC_OPTS"):
            self.assertIn("statfs=full", _opts(name),
                          "%s: space checks would see the sum of all branches" % name)

    def test_no_option_that_silently_clones_folders(self):
        # Both of these end path preservation the moment it matters — one when a branch
        # fills, the other on any cross-folder move — by cloning the folder onto a second
        # branch. Verified on a throwaway pool, not assumed.
        for name in ("MERGERFS_OPTS", "MERGERFS_SVC_OPTS"):
            opts = _opts(name)
            self.assertNotIn("moveonenospc", opts,
                             "%s: a full branch would scatter the folder instead of failing" % name)
            self.assertNotIn("ignorepponrename", opts,
                             "%s: a move would clone the destination folder onto a second disk" % name)

    def test_branch_wait_survived(self):
        # The service opts also carry the fix for the boot race that could build the pool on
        # empty mountpoints; it lives in the same string and is easy to drop by accident.
        self.assertIn("branches-mount-timeout=30", _opts("MERGERFS_SVC_OPTS"))


if __name__ == "__main__":
    unittest.main()
