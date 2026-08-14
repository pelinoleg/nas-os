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

    def test_enospc_rescue_is_on(self):
        # Path preservation means a folder's branch can fill while the pool has room. This
        # rescues the write case (mergerfs default is false); a create still fails, which is
        # the accepted price.
        for name in ("MERGERFS_OPTS", "MERGERFS_SVC_OPTS"):
            self.assertIn("moveonenospc=true", _opts(name),
                          "%s: a full branch would fail writes with no attempt to move" % name)

    def test_branch_wait_survived(self):
        # The service opts also carry the fix for the boot race that could build the pool on
        # empty mountpoints; it lives in the same string and is easy to drop by accident.
        self.assertIn("branches-mount-timeout=30", _opts("MERGERFS_SVC_OPTS"))


if __name__ == "__main__":
    unittest.main()
