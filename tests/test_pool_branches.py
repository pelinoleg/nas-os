"""Branch-dependent behaviour, checked against a real mergerfs pool.

This is the one class of code in the panel that CANNOT be tested by faking anything: it
exists precisely because the kernel lies about a FUSE union. Every branch of a mergerfs
mount shares the mount's device number, so `os.stat(a).st_dev == os.stat(b).st_dev` is
true for two files on two different physical disks — and three checks in the panel used
exactly that to answer "same disk?", so all three always answered yes. Under any policy
that can put two folders on two branches — the pool has run under `epmfs` (2026-08-14) and
under `mfs` (2026-08-27) — a move between them IS a move between two disks, and the wrong
answer costs real copies, failed renames and a free-space check that passes right before
ENOSPC.

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
from unittest import mock

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
    for o in (m.group(1) if m else "category.create=mfs,statfs=full").split(","):
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


class MakeTheParentBesideTheSource(PoolCase):
    """The other half of the Immich promotion guard, and the half that is invisible: the
    destination's PARENT usually does not exist yet, and creating it through the pool lets
    the create policy pick its branch by free space. The guard then asks about the nearest
    existing ancestor, answers "same disk", and the rename — which runs after the standby
    has already been stopped — returns EXDEV anyway. Measured on the box: a library on
    disk3 moved to a new folder whose parent had been created on disk2."""

    def test_a_new_parent_lands_on_the_sources_branch(self):
        src = self.put(self.br2, "library/photo.jpg", 10)
        parent = os.path.join(self.pool, "new-home")
        nas._pool_makedirs_beside(parent, src)
        self.assertTrue(os.path.isdir(os.path.join(self.br2, "new-home")),
                        "the parent was not created beside the library")
        self.assertFalse(os.path.isdir(os.path.join(self.br1, "new-home")),
                         "the parent landed on another disk — the move will hit EXDEV")

    def test_and_the_guard_then_agrees_the_move_will_stay_put(self):
        # the two halves are only worth anything together
        src = self.put(self.br2, "library/photo.jpg", 10)
        parent = os.path.join(self.pool, "new-home")
        nas._pool_makedirs_beside(parent, src)
        self.assertTrue(nas._rename_stays_put(src, parent))

    def test_a_deep_parent_is_created_whole(self):
        src = self.put(self.br2, "library/photo.jpg", 10)
        parent = os.path.join(self.pool, "a", "b", "c")
        nas._pool_makedirs_beside(parent, src)
        self.assertTrue(os.path.isdir(os.path.join(self.br2, "a/b/c")))

    def test_outside_a_pool_it_is_an_ordinary_makedirs(self):
        plain = os.path.join(self.d, "plain", "deep")
        nas._pool_makedirs_beside(plain, self.d)
        self.assertTrue(os.path.isdir(plain))

    def test_an_existing_directory_is_not_an_error(self):
        src = self.put(self.br2, "library/photo.jpg", 10)
        parent = self.mkdir(self.br1, "already-there")
        nas._pool_makedirs_beside(parent, src)     # must not raise


class HonestFreeSpace(PoolCase):
    """What the panel is allowed to call "free".

    statvfs on the pool answers with the SUM of the branches. Under the spreading policy a
    new file really can go to any of them, so the sum is nearly the truth — except for
    minfreespace: a branch below it is not a candidate, and mergerfs still counts what is
    left on it. `free_new` is the sum with that reserve taken off every branch, which is
    the space a write can actually reach."""

    def setUp(self):
        super().setUp()
        self._storage = nas.STORAGE
        nas.STORAGE = self.pool

    def tearDown(self):
        nas.STORAGE = self._storage

    def test_the_reserve_is_read_from_the_pool_itself(self):
        # not parsed out of the unit file: the mount is what decides, and a box whose unit
        # was edited without a restart would otherwise be measured against the wrong number
        self.assertEqual(nas.pool_info()["minfree"], 1024 * 1024,
                         "minfreespace was not read back from the running pool")

    def test_free_new_adds_the_branches_up_and_takes_the_reserve_off(self):
        # This fixture's branches share one filesystem, so both report the same room and
        # the pool (which counts such a filesystem once) reports one branch worth. That is
        # what makes the shape visible: free_new must exceed a single branch — it is a sum
        # — and must stay under the raw doubling, because minfreespace comes off each one.
        di = nas.pool_info()
        one = os.statvfs(self.br1).f_bavail * os.statvfs(self.br1).f_frsize
        self.assertGreater(di["free_new"], one,
                           "free_new is still the biggest branch, not the reachable sum")
        self.assertLess(di["free_new"], 2 * one,
                        "the minfreespace reserve was promised as usable space")

    def test_the_tile_shows_what_new_files_can_use(self):
        tile = nas._glance_tile("pool", True)
        self.assertIn("free", tile["note"])
        self.assertEqual(tile["raw"]["free"], nas.pool_info()["free_new"])
        self.assertEqual(tile["raw"]["free_total"], nas.pool_info()["free"],
                         "the total was dropped instead of being kept beside the truth")

    def test_the_fullest_branch_is_still_carried(self):
        # It no longer raises an alarm by itself (see TheFullestBranchIsNotAnAlarm), but the
        # health report names it: it is the branch that stops taking new files first.
        di = nas.pool_info()
        self.assertGreaterEqual(di["pct_worst"], di["pct"])

    def test_outside_a_pool_nothing_is_invented(self):
        nas.STORAGE = self.d          # a plain directory, not a mount
        self.assertIsNone(nas.pool_info())


class TheShippedPolicySpreads(PoolCase):
    """test_pool_policy locks the option string; this checks that the string still buys
    what it was chosen for on the mergerfs build actually installed here."""

    def test_the_build_understands_and_applies_the_options(self):
        # an option a build silently ignores is worse than one that fails to mount: the
        # unit would look right for months. Ask the running pool what it is actually doing.
        ctl = os.path.join(self.pool, ".mergerfs")
        self.assertEqual(os.getxattr(ctl, "user.mergerfs.category.create").decode(), "mfs")
        self.assertEqual(os.getxattr(ctl, "user.mergerfs.moveonenospc").decode(), "mfs")


class TheShippedPolicyGoesWhereTheRoomIs(unittest.TestCase):
    """The reason the policy was changed on 2026-08-27, proven rather than assumed.

    A folder bigger than any single branch has to be able to spill, or the copy that fills
    it fails with ENOSPC on a pool that still has room — which is exactly how the Ugreen
    mirror died at 33 %. The other fixtures put both branches on one filesystem, so they
    cannot tell "most free space" apart from "any branch"; this one gives each branch its
    own tmpfs and fills the branch the folder lives on."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("mergerfs"):
            raise unittest.SkipTest("mergerfs is not installed")
        if os.geteuid() != 0:
            raise unittest.SkipTest("needs root: tmpfs branches and a FUSE mount")
        cls.d = tempfile.mkdtemp(prefix="nas-spill-")
        cls.br1 = os.path.join(cls.d, "disk1")
        cls.br2 = os.path.join(cls.d, "disk2")
        cls.pool = os.path.join(cls.d, "storage")
        cls.mounted = []
        for p in (cls.br1, cls.br2, cls.pool):
            os.makedirs(p)
        # everything past this point unmounts before it raises: a skip or a stray error must
        # not leave a tmpfs and a FUSE mount behind on the box
        try:
            for b in (cls.br1, cls.br2):
                r = subprocess.run(["mount", "-t", "tmpfs", "-o", "size=64M", "tmpfs", b],
                                   capture_output=True, text=True, timeout=30)
                if r.returncode != 0:
                    raise unittest.SkipTest("tmpfs branch: " + (r.stderr or r.stdout).strip())
                cls.mounted.append(b)
            r = subprocess.run(["mergerfs", "-o", _wizard_opts(),
                                cls.br1 + ":" + cls.br2, cls.pool],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                raise unittest.SkipTest("pool: " + (r.stderr or r.stdout).strip())
            cls.mounted.append(cls.pool)
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        for m in reversed(getattr(cls, "mounted", [])):
            subprocess.run(["umount", "-l", m], capture_output=True, timeout=30)
        cls.mounted = []
        shutil.rmtree(getattr(cls, "d", "") or ".", ignore_errors=True)

    def test_a_new_file_goes_to_the_branch_with_room_not_to_its_folder(self):
        os.makedirs(os.path.join(self.br1, "movies"), exist_ok=True)
        with open(os.path.join(self.br1, "filler"), "wb") as f:      # br1: ~4 MB left of 64
            f.write(b"x" * 60 * 1024 * 1024)
        self.assertGreater(os.statvfs(self.br2).f_bavail * os.statvfs(self.br2).f_frsize,
                           os.statvfs(self.br1).f_bavail * os.statvfs(self.br1).f_frsize)
        with open(os.path.join(self.pool, "movies", "new.mkv"), "wb") as f:
            f.write(b"y" * 8 * 1024 * 1024)                          # would not fit on br1
        self.assertTrue(os.path.exists(os.path.join(self.br2, "movies", "new.mkv")),
                        "the file did not follow the free space onto the other branch — "
                        "a folder can then never outgrow the branch it started on")
        self.assertEqual(os.path.getsize(os.path.join(self.pool, "movies", "new.mkv")),
                         8 * 1024 * 1024, "the file was truncated, not relocated")


class HonestFreeSpaceArithmetic(unittest.TestCase):
    """The aggregation itself, against a pool that really does report the sum.

    Runs everywhere: the branches are ordinary directories and the union figure is
    supplied, which is exactly the shape mergerfs produces on a box with three separate
    filesystems (2172 GiB reported, 849 reachable)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.brs = [os.path.join(self.d, "disk1"), os.path.join(self.d, "disk2")]
        for b in self.brs:
            os.makedirs(b)
        self._storage, self._info, self._dirs = nas.STORAGE, nas.disk_info, nas._pool_branch_dirs
        nas.STORAGE = os.path.join(self.d, "storage")
        nas._pool_branch_dirs = lambda mp: list(self.brs)
        one = os.statvfs(self.brs[0])
        self.avail = one.f_bavail * one.f_frsize
        # what the union answers: both branches added together, as a real pool does
        nas.disk_info = lambda p: {"path": p, "total": self.avail * 2, "used": 0,
                                   "free": self.avail * 2, "pct": 0.0}

    def tearDown(self):
        nas.STORAGE, nas.disk_info, nas._pool_branch_dirs = self._storage, self._info, self._dirs
        shutil.rmtree(self.d, ignore_errors=True)

    def info(self):
        with mock.patch.object(nas.os.path, "ismount", return_value=True):
            return nas.pool_info()

    def test_the_sum_is_kept_and_the_reserve_taken_off(self):
        di = self.info()
        self.assertEqual(di["free"], self.avail * 2, "the honest total was thrown away")
        self.assertEqual(di["free_new"], self.avail * 2,
                         "the reachable space is the branches added up")

    def test_the_unreachable_reserve_is_not_promised(self):
        # a branch under minfreespace takes no new files at all, so nothing left on it is
        # reachable — on the box that is 3 x 20 GiB the panel would otherwise promise
        with mock.patch.object(nas, "_pool_minfree", return_value=self.avail // 4):
            di = self.info()
        self.assertEqual(di["free_new"], 2 * (self.avail - self.avail // 4))
        with mock.patch.object(nas, "_pool_minfree", return_value=self.avail * 2):
            self.assertEqual(self.info()["free_new"], 0,
                             "a pool no write can reach still reported room")

    def test_the_tile_stops_promising_the_unreachable(self):
        with mock.patch.object(nas.os.path, "ismount", return_value=True):
            tile = nas._glance_tile("pool", True)
        self.assertEqual(tile["raw"]["free"], self.avail * 2)
        self.assertEqual(tile["raw"]["free_total"], self.avail * 2)

    def test_an_unreadable_branch_does_not_zero_the_answer(self):
        nas._pool_branch_dirs = lambda mp: ["/does/not/exist"] + self.brs
        self.assertEqual(self.info()["free_new"], self.avail * 2)

    def test_the_fullest_branch_alone_does_not_colour_the_tile(self):
        # The mirror keeps disk1 at 98 % while the pool sits at 40 %. Under the spreading
        # policy that is not a failure — new files go to another branch — and a tile that
        # went red on it would sit red for as long as the mirror exists, which is how an
        # owner learns to ignore the panel.
        with mock.patch.object(nas, "pool_info", return_value={
                "path": nas.STORAGE, "total": 100, "used": 40, "free": 60, "pct": 40.0,
                "free_new": 60, "pct_worst": 98.0, "minfree": 0}):
            tile = nas._glance_tile("pool", True)
        self.assertEqual(tile["state"], "ok",
                         "one full branch turned the pool tile red")

    def test_with_no_branch_list_the_union_figure_stands(self):
        # mergerfs too old for the xattr: better the old number than a zero that would
        # read as "the pool is full"
        nas._pool_branch_dirs = lambda mp: []
        di = self.info()
        self.assertEqual(di["free_new"], di["free"])


if __name__ == "__main__":
    unittest.main()
