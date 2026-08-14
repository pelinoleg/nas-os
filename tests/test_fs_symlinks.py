"""Deleting a shortcut must delete the shortcut.

Found 2026-08-14 by an app-by-app audit and reproduced before the fix: _fs_guard resolved
the whole path with realpath, so every mutating operation was handed the symlink's TARGET.

    fs_trash("/mnt/storage/shortcut.txt")  ->  ok
    target gone: True      shortcut still on disk: True

A shortcut to a folder took the entire folder into the trash; a broken shortcut could not
be deleted at all ("already deleted or moved"), because realpath led nowhere.

The fix resolves the parent and leaves the leaf alone for operations that act on the object
itself — delete, trash, move, rename — while writers keep following the link, since
open(path, "w") follows it too and the target is what must be judged there.
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


class GuardKeepsTheLeaf(unittest.TestCase):

    def setUp(self):
        # /tmp/<name> is depth 2 (the guard rejects depth < 2) and outside _FS_PROTECTED,
        # which lists /var among others — a tempdir under /var/tmp would be refused as a
        # system path and the test would pass for the wrong reason.
        self.d = tempfile.mkdtemp(dir="/tmp")
        os.makedirs(os.path.join(self.d, "real"))
        self.target = os.path.join(self.d, "real", "valuable.txt")
        with open(self.target, "w") as f:
            f.write("owner data")
        self.link = os.path.join(self.d, "shortcut.txt")
        os.symlink(self.target, self.link)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_a_shortcut_resolves_to_itself(self):
        rp, err = nas._fs_guard(self.link, follow=False)
        self.assertIsNone(err)
        self.assertEqual(rp, self.link, "the guard handed back the target, not the shortcut")

    def test_a_writer_still_resolves_the_target(self):
        # open(path,"w") follows the link, so the TARGET is what the guard must judge
        rp, err = nas._fs_guard(self.link)
        self.assertIsNone(err)
        self.assertEqual(rp, os.path.realpath(self.target))

    def test_deleting_a_shortcut_keeps_the_target(self):
        r = nas.fs_delete(self.link)
        self.assertTrue(r.get("ok"), r)
        self.assertTrue(os.path.exists(self.target), "the file the shortcut pointed at was deleted")
        self.assertFalse(os.path.lexists(self.link))

    def test_a_broken_shortcut_can_be_deleted(self):
        dangling = os.path.join(self.d, "broken")
        os.symlink(os.path.join(self.d, "nothing"), dangling)
        self.assertTrue(nas.fs_delete(dangling).get("ok"))
        self.assertFalse(os.path.lexists(dangling))

    def test_renaming_a_shortcut_moves_the_shortcut(self):
        r = nas.fs_rename(self.link, "renamed.lnk")
        self.assertTrue(r.get("ok"), r)
        self.assertTrue(os.path.islink(os.path.join(self.d, "renamed.lnk")))
        self.assertTrue(os.path.exists(self.target))

    def test_protected_trees_are_still_refused(self):
        for bad in ("/etc", "/mnt", "/", ""):
            _, err = nas._fs_guard(bad, follow=False)
            self.assertTrue(err, "%r slipped through the guard" % bad)

    def test_a_shortcut_into_a_system_tree_is_harmless_now(self):
        # the link may point anywhere: what gets unlinked is the link, so the guard need not
        # refuse it — but it must not hand back the system path either
        evil = os.path.join(self.d, "to-etc")
        os.symlink("/etc", evil)
        rp, err = nas._fs_guard(evil, follow=False)
        self.assertIsNone(err)
        self.assertEqual(rp, evil)
        self.assertTrue(nas.fs_delete(evil).get("ok"))
        self.assertTrue(os.path.isdir("/etc"), "/etc must still exist")


if __name__ == "__main__":
    unittest.main()
