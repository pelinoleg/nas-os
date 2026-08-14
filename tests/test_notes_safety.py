"""Notes: what the tree may read, and who wins when two devices save at once.

From the 2026-08-14 app audit, both measured before the fix:

  * the tree listing opened every .md it found and put the first 2 KB in the preview —
    including a symlink. A link named leak.md pointing at /etc/shadow put the shadow file
    into the notes list. The panel runs as root and the notes folder is owned by the user
    and exported over SMB, so anyone who could write there could read anything on the box.
    note_get and search already refused symlinks; only the listing did not.
  * the optimistic lock compared mtimes with ">" at one-second resolution, so two saves in
    one second both won. Worse on this box: mergerfs caches attributes for a second, so a
    stat taken right after another writer's save returns the mtime from BEFORE it — the
    lock let the second writer through and the first one's text was gone with no conflict
    dialog. The lock now compares a hash of the content, which the pool does not cache.
"""
import errno
import importlib.util
import os
import shutil
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


class NotesBase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._root = nas.notes_root
        nas.notes_root = lambda: self.d

    def tearDown(self):
        nas.notes_root = self._root
        shutil.rmtree(self.d, ignore_errors=True)


class TreeReadsOnlyNotes(NotesBase):

    def test_a_symlinked_note_is_not_read(self):
        with open(os.path.join(self.d, "real.md"), "w") as f:
            f.write("# Real\n\nbody")
        secret = os.path.join(self.d, "..secret")
        with open(secret, "w") as f:
            f.write("root:$y$very$secret:20678:0:99999")
        os.symlink(secret, os.path.join(self.d, "leak.md"))
        t = nas.notes_tree()
        self.assertEqual([n["path"] for n in t["notes"]], ["real.md"])
        self.assertFalse(any("$y$" in (n.get("prev") or "") for n in t["notes"]),
                         "the tree preview leaked a file it followed a symlink to")


class OptimisticLock(NotesBase):

    def test_a_stale_revision_is_a_conflict(self):
        nas.note_save("a.md", "A", [], "start")
        base = nas.note_get("a.md")["rev"]
        self.assertTrue(base)
        self.assertTrue(nas.note_save("a.md", "A", [], "from B", base_rev=base).get("ok"))
        r = nas.note_save("a.md", "A", [], "from A", base_rev=base)
        self.assertTrue(r.get("conflict"), "the second writer overwrote the first silently")
        with open(os.path.join(self.d, "a.md")) as f:
            self.assertIn("from B", f.read())

    def test_a_fresh_revision_saves(self):
        nas.note_save("a.md", "A", [], "start")
        rev = nas.note_get("a.md")["rev"]
        self.assertTrue(nas.note_save("a.md", "A", [], "next", base_rev=rev).get("ok"))

    def test_a_client_without_a_revision_still_works(self):
        nas.note_save("a.md", "A", [], "start")
        g = nas.note_get("a.md")
        self.assertTrue(nas.note_save("a.md", "A", [], "next", base_mtime=g["mtime"]).get("ok"))

    def test_force_overrides_the_lock(self):
        nas.note_save("a.md", "A", [], "start")
        r = nas.note_save("a.md", "A", [], "forced", base_rev="stale", force=True)
        self.assertTrue(r.get("ok"))


class CrossDeviceMoves(NotesBase):
    """os.rename between two pool branches returns EXDEV, and for FOLDERS that is the
    normal case since the pool became path preserving: moving or deleting a notes folder
    raised OSError(18) straight through the API as a 500."""

    def _exdev_once(self):
        real = os.rename
        state = {"first": True}

        def fake(a, b):
            if state["first"]:
                state["first"] = False
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(a, b)
        return fake

    def test_move_survives_exdev(self):
        os.makedirs(os.path.join(self.d, "Folder"))
        with open(os.path.join(self.d, "Folder", "n.md"), "w") as f:
            f.write("# n\n\nbody")
        with mock.patch("os.rename", self._exdev_once()):
            nas.note_move("Folder", "Moved")
        self.assertTrue(os.path.exists(os.path.join(self.d, "Moved", "n.md")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "Folder")))

    def test_delete_survives_exdev(self):
        os.makedirs(os.path.join(self.d, "Folder"))
        with open(os.path.join(self.d, "Folder", "n.md"), "w") as f:
            f.write("# n\n\nbody")
        with mock.patch("os.rename", self._exdev_once()):
            nas.note_delete("Folder")
        self.assertFalse(os.path.exists(os.path.join(self.d, "Folder")))


if __name__ == "__main__":
    unittest.main()
