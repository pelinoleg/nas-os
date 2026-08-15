"""Branch-dependent behaviour, checked against a real mergerfs pool.

This is the one class of code in the panel that CANNOT be tested by faking anything: it
exists precisely because the kernel lies about a FUSE union. Every branch of a mergerfs
mount shares the mount's device number, so `os.stat(a).st_dev == os.stat(b).st_dev` is
true for two files on two different physical disks — and three checks in the panel used
exactly that to answer "same disk?", so all three always answered yes. Harmless while the
pool spread files with `mfs`; since the pool became path preserving (2026-08-14) a move
between two folders IS a move between two disks, and the wrong answer costs real copies,
failed renames and a free-space check that passes right before ENOSPC.

The fix reads mergerfs' own xattrs instead. A test that mocks those xattrs would only
prove the mock agrees with itself, so this module builds a throwaway two-branch pool with
the wizard's own create policy and asks the real questions of the real thing — including
the trap itself: the first assertion here is that st_dev DOES report one device for the
two branches, so the day mergerfs changes that, this test says so instead of quietly
becoming vacuous.

It is skipped, never failed, where a pool cannot be built: GitHub Actions has no mergerfs,
and on the box itself an unprivileged FUSE mount is refused by the kernel — so run it as
root to actually exercise it:

    sudo python3 -m unittest tests.test_pool_branches -v
"""
import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
SPEC = importlib.util.spec_from_file_location("nas_web", os.path.join(ROOT, "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


def _wizard_opts():
    """The pool options the box actually ships, minus the ones a throwaway pool cannot use.

    Sourced from the wizard rather than written out here on purpose: the create policy is
    what makes any of this true, and a copy of it in the test would keep passing after the
    real one changed. Only minfreespace is overridden — a 20 G floor would make every
    branch of a temporary pool ineligible for creation."""
    with open(os.path.join(ROOT, "nas-wizard.sh"), encoding="utf-8") as f:
        m = re.search(r'^MERGERFS_SVC_OPTS="([^"]*)"', f.read(), re.M)
    keep = []
    for o in (m.group(1) if m else "category.create=epmfs,statfs=full").split(","):
        k = o.split("=")[0]
        if k in ("minfreespace", "fsname", "branches-mount-timeout", "nofail", "defaults"):
            continue                      # box-specific, or not understood by every build
        keep.append(o)
    return ",".join(keep + ["minfreespace=1M"])


class PoolCase(unittest.TestCase):
    """Two branches, one pool. Files are placed by writing into a BRANCH directly, which
    is how a test pins a file to a chosen disk; everything under test then looks at it
    through the pool, which is how the panel sees it."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("mergerfs"):
            raise unittest.SkipTest("mergerfs is not installed")
        cls.d = tempfile.mkdtemp(prefix="nas-pool-")
        cls.br1 = os.path.join(cls.d, "disk1")
        cls.br2 = os.path.join(cls.d, "disk2")
        cls.pool = os.path.join(cls.d, "storage")
        for p in (cls.br1, cls.br2, cls.pool):
            os.makedirs(p)
        # Everything past this point unmounts before it raises: a skip or a stray
        # AttributeError must not leave a FUSE mount (and a root-owned /tmp directory)
        # behind on the box. This module leaked six of them once, in exactly that way.
        try:
            r = subprocess.run(["mergerfs", "-o", _wizard_opts(),
                                cls.br1 + ":" + cls.br2, cls.pool],
                               capture_output=True, text=True, timeout=30)
            why = "" if r.returncode == 0 else (r.stderr or r.stdout or "").strip()
            if not why and nas._pool_mount_of(cls.pool) != os.path.realpath(cls.pool):
                why = "the mountpoint is not a mergerfs mount afterwards"
        except Exception as e:                       # noqa: BLE001 — nothing may survive it
            cls._release()
            raise unittest.SkipTest("mergerfs could not be started: %s" % e)
        if why:
            cls._release()
            raise unittest.SkipTest(
                "no pool to test against (an unprivileged FUSE mount is refused on this "
                "box — run the suite as root): %s" % why)

    @classmethod
    def _release(cls):
        """Unmount and remove the throwaway pool. Safe to call at any point."""
        for cmd in (["fusermount", "-u", cls.pool], ["umount", cls.pool]):
            try:
                if subprocess.run(cmd, capture_output=True).returncode == 0:
                    break
            except (OSError, subprocess.SubprocessError):
                pass
        shutil.rmtree(cls.d, ignore_errors=True)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "d", None) and os.path.isdir(cls.d):
            cls._release()

    def setUp(self):
        for br in (self.br1, self.br2):
            for nm in os.listdir(br):
                p = os.path.join(br, nm)
                shutil.rmtree(p) if os.path.isdir(p) else os.unlink(p)

    # helpers ---------------------------------------------------------------
    def put(self, branch, rel, size=0):
        p = os.path.join(branch, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        return os.path.join(self.pool, rel)

    def mkdir(self, branch, rel):
        os.makedirs(os.path.join(branch, rel), exist_ok=True)
        return os.path.join(self.pool, rel)


class TheTrapItself(PoolCase):

    def test_st_dev_still_cannot_tell_the_branches_apart(self):
        # If this ever fails, the union stopped lying and the xattr detour could be
        # reconsidered. Until then it is the reason every check below exists.
        a = self.put(self.br1, "a.bin", 10)
        b = self.put(self.br2, "b.bin", 10)
        self.assertEqual(os.stat(a).st_dev, os.stat(b).st_dev,
                         "two branches now report different devices")


class BranchOfAPath(PoolCase):

    def test_a_file_reports_the_branch_it_lives_on(self):
        self.assertEqual(nas._pool_branch(self.put(self.br2, "movies/x.mkv", 5)),
                         os.path.realpath(self.br2))
        self.assertEqual(nas._pool_branch(self.put(self.br1, "docs/y.pdf", 5)),
                         os.path.realpath(self.br1))

    def test_a_path_outside_a_pool_has_no_branch(self):
        self.assertIsNone(nas._pool_branch(self.d))
        self.assertIsNone(nas._pool_branch("/etc/hostname"))

    def test_a_directory_on_both_branches_lists_both(self):
        self.mkdir(self.br1, "shared")
        self.mkdir(self.br2, "shared")
        got = sorted(nas._pool_paths(os.path.join(self.pool, "shared")))
        self.assertEqual(got, sorted([os.path.realpath(self.br1) + "/shared",
                                      os.path.realpath(self.br2) + "/shared"]))

    def test_the_pool_is_recognised_as_a_pool(self):
        self.mkdir(self.br1, "movies")
        self.assertTrue(nas._in_pool(os.path.join(self.pool, "movies")))
        self.assertTrue(nas._in_pool(os.path.join(self.pool, "does-not-exist-yet")))
        self.assertFalse(nas._in_pool(self.d), "a path beside the pool was taken for one")
        self.assertFalse(nas._in_pool("/etc"))


class SameDisk(PoolCase):

    def test_two_branches_are_not_the_same_disk(self):
        a = self.put(self.br1, "a.bin", 10)
        b = self.put(self.br2, "b.bin", 10)
        self.assertFalse(nas._same_disk(a, b),
                         "the pool answered 'same disk' for two different disks")

    def test_two_files_on_one_branch_are_the_same_disk(self):
        a = self.put(self.br2, "one/a.bin", 10)
        b = self.put(self.br2, "two/b.bin", 10)
        self.assertTrue(nas._same_disk(a, b))

    def test_outside_the_pool_the_ordinary_answer_still_works(self):
        a = os.path.join(self.d, "plain-a")
        b = os.path.join(self.d, "plain-b")
        for p in (a, b):
            open(p, "w").close()
        self.assertTrue(nas._same_disk(a, b))


class RenameStaysPut(PoolCase):
    """Whether a move inside the pool is an instant rename or a full copy. Answering it
    wrong is not cosmetic: the Immich promotion asks this before it stops anything."""

    def test_into_a_folder_on_the_same_branch(self):
        src = self.put(self.br2, "library/photo.jpg", 10)
        dst = self.mkdir(self.br2, "immich")
        self.assertTrue(nas._rename_stays_put(src, dst))

    def test_into_a_folder_that_lives_only_on_the_other_branch(self):
        src = self.put(self.br2, "library/photo.jpg", 10)
        dst = self.mkdir(self.br1, "immich")
        self.assertFalse(nas._rename_stays_put(src, dst),
                         "a cross-disk move was reported as a rename")

    def test_the_pool_root_counts_as_the_source_branch(self):
        # The common case the fix was written for: the pool ROOT resolves to the first
        # branch by search policy, so comparing the two paths' own branches called every
        # library outside branch one "a different disk".
        src = self.put(self.br2, "library/photo.jpg", 10)
        self.assertTrue(nas._rename_stays_put(src, self.pool),
                        "a move to the pool root was reported as cross-disk")

    def test_a_folder_present_on_both_branches_is_reachable(self):
        src = self.put(self.br2, "library/photo.jpg", 10)
        self.mkdir(self.br1, "immich")
        dst = self.mkdir(self.br2, "immich")
        self.assertTrue(nas._rename_stays_put(src, dst))


class NeedBytesForAMove(PoolCase):
    """The free-space check in front of a file-manager job. Inside a path-preserving pool
    a move writes only the files that change branch — asking the top directory gives one
    branch for a tree spread over several, and that single answer was wrong in BOTH
    directions: zero (skipping the check on the moves that copy every byte) and whole
    trees that were never going to move."""

    def spread(self):
        self.put(self.br1, "lib/here.bin", 1000)     # already on the destination branch
        self.put(self.br2, "lib/there.bin", 2000)    # will have to be copied over
        self.mkdir(self.br1, "target")
        return os.path.join(self.pool, "lib"), os.path.join(self.pool, "target")

    def test_only_the_files_that_change_branch_are_counted(self):
        lib, target = self.spread()
        self.assertEqual(nas._fsjob_need_bytes([lib], target, "move", "overwrite"), 2000)

    def test_files_named_one_by_one_are_judged_one_by_one(self):
        lib, target = self.spread()
        items = [os.path.join(lib, "here.bin"), os.path.join(lib, "there.bin")]
        self.assertEqual(nas._fsjob_need_bytes(items, target, "move", "overwrite"), 2000)

    def test_a_move_onto_the_other_branch_counts_the_other_half(self):
        self.spread()
        target = self.mkdir(self.br2, "target2")
        self.assertEqual(nas._fsjob_need_bytes([os.path.join(self.pool, "lib")],
                                               target, "move", "overwrite"), 1000)

    def test_a_copy_still_counts_everything(self):
        lib, target = self.spread()
        self.assertEqual(nas._fsjob_need_bytes([lib], target, "copy", "overwrite"), 3000)

    def test_what_skip_will_not_write_is_not_reserved(self):
        lib, target = self.spread()
        self.put(self.br1, "target/there.bin", 1)     # the crossing file is already there
        items = [os.path.join(lib, "here.bin"), os.path.join(lib, "there.bin")]
        self.assertEqual(nas._fsjob_need_bytes(items, target, "move", "skip"), 0)


class TheShippedPolicyIsPathPreserving(PoolCase):
    """test_pool_policy locks the option string; this checks that the string still buys
    what it was chosen for on the mergerfs build actually installed here."""

    def test_a_new_file_lands_on_the_branch_its_folder_lives_on(self):
        self.mkdir(self.br2, "movies")
        with open(os.path.join(self.pool, "movies", "new.mkv"), "wb") as f:
            f.write(b"x" * 100)
        self.assertTrue(os.path.exists(os.path.join(self.br2, "movies", "new.mkv")),
                        "the file did not land beside its folder")
        self.assertFalse(os.path.exists(os.path.join(self.br1, "movies")),
                         "the folder was cloned onto a second branch")


if __name__ == "__main__":
    unittest.main()
