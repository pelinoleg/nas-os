"""Where the Source picker starts.

Kopia takes any path, so the picker opened at the filesystem root and asked the owner to
find his own data in it. 2026-08-28, setting up his first backup: "I didn't understand how
to pick the data — it should say plainly, local paths or SMB, and you choose inside that."

The picker now opens on named places: the disks of this NAS, one entry per CONNECTED share
of each server, and the whole filesystem as a single labelled escape hatch. Two things must
hold or the feature is worse than the root listing it replaced: a place must carry the
absolute path it stands for (the tree appends names to it), and "everything on this box"
must expand into the filesystem — not into the list of places again.
"""

import importlib.util
import os
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


class PlacesCase(unittest.TestCase):
    REMOTES = [{"id": "ug", "name": "Ugreen NAS", "kind": "smb", "host": "192.168.1.95",
                "shares": ["Cloud", "PMedia"]}]

    def places(self, mounts=(), remotes=None, isdir=True):
        mps = set(mounts)
        with mock.patch.object(nas, "_mount_points", return_value=mps), \
                mock.patch.object(nas, "_remotes_load",
                                  return_value=list(self.REMOTES if remotes is None else remotes)), \
                mock.patch.object(nas.os.path, "isdir", return_value=isdir), \
                mock.patch.object(nas.os.path, "ismount", side_effect=lambda p: p in mps):
            return nas._kp_places()


class TheChoiceIsNamed(PlacesCase):
    def test_every_place_carries_the_path_it_stands_for(self):
        for pl in self.places(mounts={nas.STORAGE}):
            self.assertTrue(pl["path"].startswith("/"), pl)
            self.assertTrue(pl["dir"])
            self.assertTrue(pl["name"], "a place with no label is the root listing again")

    def test_a_connected_share_is_its_own_place_named_after_the_server(self):
        mp = os.path.join(nas.REMOTE_MNT, "ug")
        pl = self.places(mounts={os.path.join(mp, "Cloud"), os.path.join(mp, "PMedia")})
        remote = [p for p in pl if p["place"] == "remote"]
        self.assertEqual([p["path"] for p in remote],
                         [os.path.join(mp, "Cloud"), os.path.join(mp, "PMedia")])
        self.assertIn("Ugreen NAS", remote[0]["name"])
        self.assertIn("SMB", remote[0]["name"])

    def test_a_share_that_is_not_mounted_is_not_offered(self):
        # an unmounted share is an empty directory — offering it invites a backup of nothing
        pl = self.places(mounts={os.path.join(nas.REMOTE_MNT, "ug", "Cloud")})
        self.assertEqual([p["path"] for p in pl if p["place"] == "remote"],
                         [os.path.join(nas.REMOTE_MNT, "ug", "Cloud")])

    def test_the_pool_is_offered_only_when_it_is_mounted(self):
        self.assertTrue(any(p["path"] == nas.STORAGE for p in self.places(mounts={nas.STORAGE})))
        self.assertFalse(any(p["path"] == nas.STORAGE for p in self.places(mounts=set())))

    def test_the_whole_filesystem_stays_reachable_and_labelled(self):
        last = self.places()[-1]
        self.assertEqual(last["path"], "/")
        self.assertIn("advanced", last["name"])

    def test_nothing_is_listed_twice(self):
        paths = [p["path"] for p in self.places(mounts={nas.STORAGE, "/media/nas/t7"})]
        self.assertEqual(len(paths), len(set(paths)))


class TheRootDoesNotExpandIntoItself(unittest.TestCase):
    def test_no_path_gives_the_places(self):
        with mock.patch.object(nas, "_kp_places", return_value=[{"name": "x", "path": "/x"}]):
            r = nas.kp_browse("")
        self.assertTrue(r["places"])
        self.assertEqual(r["entries"][0]["path"], "/x")

    def test_a_slash_gives_the_filesystem(self):
        """The "everything" place is a path like any other: expanding it must list / —
        if "" and "/" were still the same request it would expand into itself forever."""
        with mock.patch.object(nas, "_kp_places", side_effect=AssertionError("expanded into itself")):
            r = nas.kp_browse("/")
        self.assertTrue(r["ok"])
        self.assertFalse(r.get("places"))
        self.assertTrue(any(e["name"] in ("mnt", "etc", "var", "usr") for e in r["entries"]),
                        "the filesystem root listed nothing recognisable")

    def test_traversal_is_still_refused(self):
        self.assertFalse(nas.kp_browse("/mnt/../etc")["ok"])


if __name__ == "__main__":
    unittest.main()
