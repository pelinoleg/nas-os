"""The Unmount button must not be able to take the box apart.

The panel draws an Unmount button next to every disk without partitions, which is exactly
what a pool branch is. One click ran `umount /mnt/disk2`: the pool stays "healthy" while
that disk's files vanish from it, and every new write lands on the now-empty mountpoint —
on the SYSTEM disk. /boot/efi was reachable the same way.

The fix added a guard, and the guard is the whole value of the button's safety: nothing
else in the panel stops that click. It is checked here against the paths that are worth
protecting AND against the paths that must stay unmountable — a guard that refuses a USB
stick has replaced one bug with another.

The path is resolved before it is judged, so `/mnt/disk1/`, `/mnt/x/../disk1` and a
symlink to a branch are the same request as `/mnt/disk1` and must be refused as such.
"""
import importlib.util
import os
import shutil
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


class UnmountGuard(unittest.TestCase):
    """disk_mount(..., unmount=True) — what it refuses and what it must still allow."""

    def setUp(self):
        # umount/mount are never actually run: the test records what would have been.
        self.calls = []
        self._run = nas._run

        def fake_run(cmd, timeout=40, env=None, cwd=None):
            self.calls.append(list(cmd))
            return {"ok": True, "code": 0, "log": ""}

        nas._run = fake_run

    def tearDown(self):
        nas._run = self._run

    def refuse(self, target, why):
        r = nas.disk_mount(target, unmount=True)
        self.assertFalse(r["ok"], "%s was unmounted: %s" % (target, why))
        self.assertEqual(self.calls, [],
                         "%s reached umount despite being refused" % target)

    def test_a_pool_branch_is_refused(self):
        for t in ("/mnt/disk1", "/mnt/disk2", "/mnt/disk10", "/mnt/parity1"):
            self.calls = []
            self.refuse(t, "the pool loses a disk and keeps reporting healthy")

    def test_the_pool_itself_is_refused(self):
        self.refuse(nas.STORAGE, "every share on the box points into it")

    def test_system_mountpoints_are_refused(self):
        for t in nas._SYS_MPS:
            self.calls = []
            self.refuse(t, "the box does not survive it")

    def test_a_trailing_slash_is_the_same_request(self):
        self.refuse("/mnt/disk1/", "a trailing slash walked around the guard")

    def test_a_dotdot_detour_is_the_same_request(self):
        self.refuse("/mnt/parity1/../disk1", "a .. detour walked around the guard")

    def test_a_symlink_to_a_branch_is_refused(self):
        d = tempfile.mkdtemp()
        try:
            link = os.path.join(d, "shortcut")
            os.symlink("/mnt/disk1", link)
            self.refuse(link, "a symlink walked around the guard")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_removable_disk_is_still_unmountable(self):
        # The point of the button. A guard that also refuses this has replaced the bug.
        r = nas.disk_mount("/media/nas/usb-stick", unmount=True)
        self.assertTrue(r["ok"], "an ordinary removable disk can no longer be unmounted")
        self.assertEqual(self.calls, [["umount", "/media/nas/usb-stick"]])
        self.assertEqual(r["log"], "unmounted", "a silent umount reported nothing to the user")

    def test_mounting_a_branch_back_is_allowed(self):
        # The guard is about taking things apart; putting a branch back is the repair.
        r = nas.disk_mount("/mnt/disk1")
        self.assertTrue(r["ok"], "a branch can no longer be mounted back")
        self.assertEqual(self.calls, [["mount", "/mnt/disk1"]])

    def test_a_shell_shaped_path_never_reaches_a_command(self):
        for t in ("/mnt/disk1; reboot", "/mnt/$(id)", "mnt/disk1", "", None):
            self.calls = []
            r = nas.disk_mount(t, unmount=True)
            self.assertFalse(r["ok"], "%r was accepted as a path" % (t,))
            self.assertEqual(self.calls, [], "%r reached a command" % (t,))


if __name__ == "__main__":
    unittest.main()
