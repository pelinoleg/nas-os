"""What a file-manager job will actually write, and how much room it really has.

Both questions were answered wrong once the pool became path preserving:

  * "will it fit" counted only the items whose st_dev differed from the target, and every
    branch of a FUSE union shares one st_dev — so a move inside the pool computed zero
    bytes and skipped the check on exactly the moves that now copy every byte;
  * the first repair asked the question per ITEM, which is still wrong for a tree spread
    over several branches (everything created while the pool spread files is such a tree):
    it undercounted to zero again in one direction and overcounted whole trees in the other;
  * a job that skips existing entries writes nothing, but the size was computed before the
    conflict policy was consulted, so "move and keep what is there" started refusing for
    lack of room it did not need;
  * free space at the POOL ROOT is the sum of every branch, while nothing created there can
    span more than one — the root promised terabytes to a folder that gets one disk.

The branch-aware half needs a real mergerfs mount and is verified by hand on a throwaway
pool (see the commit); what runs here is everything that holds without one.
"""
import os
import shutil
import tempfile
import threading
import unittest
import importlib.util

SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


def _job():
    return {"cancel": threading.Event(), "total_bytes": 0, "done_bytes": 0}


class NeedBytes(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.src = os.path.join(self.d, "src")
        self.dst = os.path.join(self.d, "dst")
        os.makedirs(os.path.join(self.src, "tree"))
        os.makedirs(self.dst)
        with open(os.path.join(self.src, "tree", "f.bin"), "wb") as f:
            f.write(b"x" * 4096)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_copy_counts_the_whole_tree(self):
        j = _job()
        j["total_bytes"] = nas._fsjob_tree_size([os.path.join(self.src, "tree")], j)
        need = nas._fsjob_need_bytes([os.path.join(self.src, "tree")], self.dst,
                                     "copy", "replace", j)
        self.assertEqual(need, 4096)

    def test_move_on_one_filesystem_needs_nothing(self):
        # src and dst are the same tmpfs/ext4 here: a move is a rename, not a copy
        j = _job()
        need = nas._fsjob_need_bytes([os.path.join(self.src, "tree")], self.dst,
                                     "move", "replace", j)
        self.assertEqual(need, 0)

    def test_skip_policy_excludes_what_will_not_be_written(self):
        os.makedirs(os.path.join(self.dst, "tree"))          # the conflict
        j = _job()
        j["total_bytes"] = 4096
        for op in ("copy", "move"):
            self.assertEqual(
                nas._fsjob_need_bytes([os.path.join(self.src, "tree")], self.dst, op, "skip", j),
                0, "%s+skip asked for room it will not use" % op)

    def test_replace_policy_still_counts_a_conflict(self):
        os.makedirs(os.path.join(self.dst, "tree"))
        j = _job()
        j["total_bytes"] = nas._fsjob_tree_size([os.path.join(self.src, "tree")], j)
        self.assertEqual(
            nas._fsjob_need_bytes([os.path.join(self.src, "tree")], self.dst, "copy", "replace", j),
            4096)


class FreeAt(unittest.TestCase):

    def test_outside_a_pool_it_is_plain_free_space(self):
        d = tempfile.mkdtemp()
        try:
            self.assertEqual(nas._fsjob_free_at(d), shutil.disk_usage(d).free)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_no_pool_means_no_pool_mount(self):
        d = tempfile.mkdtemp()
        try:
            self.assertIsNone(nas._pool_mount_of(d))
            self.assertFalse(nas._in_pool(d))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class SameDisk(unittest.TestCase):

    def test_two_paths_on_one_filesystem(self):
        d = tempfile.mkdtemp()
        try:
            a, b = os.path.join(d, "a"), os.path.join(d, "b")
            for f in (a, b):
                with open(f, "w"):
                    pass
            self.assertTrue(nas._same_disk(a, b))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_missing_path_is_not_the_same_disk(self):
        d = tempfile.mkdtemp()
        try:
            self.assertFalse(nas._same_disk(d, os.path.join(d, "nope")))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
