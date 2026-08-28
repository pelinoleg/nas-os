"""The pool's create policy is a decision, not a default — this test locks it.

2026-08-14 the owner chose path preservation (`epmfs`) for /mnt/storage: with no SnapRAID
parity a dead branch takes its files either way, and the only question is which ones.

2026-08-27 the owner reversed it to `mfs`, on evidence rather than taste. The Ugreen mirror
put a 1.77 TB folder on a pool whose largest branch is 916 GiB; under a path-preserving
policy the only candidate branch for a file in that folder is the branch the folder already
lives on, so once that branch fell below `minfreespace` every create returned ENOSPC while
the pool still reported 1.3 TB free. That backup could never have finished — it died at
33 % with 16310 ENOSPC errors. What makes the trade acceptable is what this pool holds:
~99 % of it is a mirror of another NAS, the one kind of data a dead disk cannot destroy.

Nothing in the code reads these strings back, so an edit could quietly flip the policy again
and no one would notice until a disk died or a backup stalled. Hence a test on the literal.
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

    def test_create_policy_spreads_across_branches(self):
        # A folder larger than the biggest branch has to be able to spill, or the copy that
        # fills it fails with ENOSPC on a pool that still has room. See the module docstring.
        for name in ("MERGERFS_OPTS", "MERGERFS_SVC_OPTS"):
            opts = _opts(name)
            self.assertIn("category.create=mfs", opts,
                          "%s lost the spreading create policy" % name)
            self.assertNotIn("category.create=epmfs", opts,
                             "%s went back to path preservation — a folder can then never "
                             "grow past one branch" % name)

    def test_a_full_branch_relocates_instead_of_failing(self):
        # The create policy picks a branch when the file is created; nothing stops THAT branch
        # from crossing minfreespace while the file is still being written. moveonenospc moves
        # it instead of leaving it half-written. It was banned under epmfs (it cloned the
        # folder onto a second branch and ended path preservation); with mfs there is no such
        # invariant left to protect.
        for name in ("MERGERFS_OPTS", "MERGERFS_SVC_OPTS"):
            self.assertIn("moveonenospc=mfs", _opts(name),
                          "%s: a branch filling mid-write would abort the file" % name)

    def test_free_space_is_reported_per_branch(self):
        # Without this, statvfs() inside a freshly created folder — which still lives on one
        # disk — reports the whole pool, and every space check in the panel promises room
        # that folder does not have.
        for name in ("MERGERFS_OPTS", "MERGERFS_SVC_OPTS"):
            self.assertIn("statfs=full", _opts(name),
                          "%s: space checks would see the sum of all branches" % name)

    def test_no_option_that_clones_the_destination_folder(self):
        # Pointless under mfs (renames no longer need it) and it still clones the destination
        # path onto the source branch. Verified on a throwaway pool, not assumed.
        for name in ("MERGERFS_OPTS", "MERGERFS_SVC_OPTS"):
            self.assertNotIn("ignorepponrename", _opts(name),
                             "%s: a move would clone the destination folder onto a second disk" % name)

    def test_branch_wait_survived(self):
        # The service opts also carry the fix for the boot race that could build the pool on
        # empty mountpoints; it lives in the same string and is easy to drop by accident.
        self.assertIn("branches-mount-timeout=30", _opts("MERGERFS_SVC_OPTS"))


if __name__ == "__main__":
    unittest.main()
