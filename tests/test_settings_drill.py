"""Settings-backup restore drill: verdicts must be earned, not assumed.

Two properties pinned here: a deliberately broken archive FAILS (a drill whose
zero can't become non-zero proves nothing), and absence of a tool or a file is a
SKIP, never a failure — the July rule: no data about a check is not a failed
check."""
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


def make_archive(d, files):
    path = os.path.join(d, "nas-settings-20990101-000000.tar.gz")
    with tarfile.open(path, "w:gz") as t:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return path


class DrillTests(unittest.TestCase):
    def _drill(self, d):
        with mock.patch.object(nas, "settings_backup_dir", return_value=d), \
                mock.patch.object(nas, "SB_DRILL_FILE", os.path.join(d, "drill.json")), \
                mock.patch.object(nas, "log_event", lambda *a, **k: None):
            return nas.settings_backup_drill()

    def _by(self, res, name):
        return next(c for c in res["checks"] if c["name"] == name)

    def test_healthy_minimal_archive_passes(self):
        with tempfile.TemporaryDirectory() as d:
            n = len(json.dumps({}))  # noqa - readability only
            make_archive(d, {
                "manifest.json": json.dumps({"version": 1, "files": ["a", "b"]}).encode(),
                "etc/nas-os/webauth.json": json.dumps({"salt": "ab", "hash": "cd"}).encode(),
                "var/lib/samba/private/passdb.tdb": b"TDB file\n" + b"\0" * 64,
            })
            r = self._drill(d)
        self.assertTrue(r["ok"], r)
        self.assertTrue(self._by(r, "webauth")["ok"])
        self.assertTrue(self._by(r, "passdb.tdb")["ok"])

    def test_broken_archive_fails_with_named_causes(self):
        with tempfile.TemporaryDirectory() as d:
            make_archive(d, {
                "manifest.json": json.dumps({"version": 1, "files": ["only-one"]}).encode(),
                "etc/nas-os/webauth.json": b"{broken",
                "var/lib/samba/private/passdb.tdb": b"NOT A TDB",
                "nas-config/desktop.json": b"{also broken",
            })
            r = self._drill(d)
        self.assertFalse(r["ok"])
        self.assertFalse(self._by(r, "passdb.tdb")["ok"])
        self.assertFalse(self._by(r, "json state")["ok"])
        # present-but-unparsable is a FAILURE with an honest note, not "not in archive"
        wa = self._by(r, "webauth")
        self.assertIs(wa["ok"], False)
        self.assertIn("unparsable", wa["note"])

    def test_absent_things_are_skips_not_failures(self):
        with tempfile.TemporaryDirectory() as d:
            make_archive(d, {
                "manifest.json": json.dumps({"version": 1, "files": []}).encode(),
            })
            r = self._drill(d)
        for name in ("passdb.tdb", "webauth", "ssh key", "rclone.conf"):
            self.assertIsNone(self._by(r, name)["ok"], name)
        self.assertTrue(r["ok"], "an empty-but-valid archive must not read as broken")

    def test_hostile_member_paths_are_refused(self):
        # extraction is filter="data": an absolute path or .. escape kills the run
        # as an archive-level failure instead of writing outside the scratch dir
        with tempfile.TemporaryDirectory() as d:
            make_archive(d, {"../escape.txt": b"x"})
            r = self._drill(d)
        self.assertFalse(r["ok"])
        self.assertFalse(r["checks"][0]["ok"])

    def test_no_archives_is_an_error_not_a_pass(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._drill(d)
        self.assertFalse(r["ok"])
        self.assertIn("no settings archives", r.get("log", ""))


if __name__ == "__main__":
    unittest.main()


class ResurrectionScanTests(unittest.TestCase):
    """The setup wizard's dead-box scan: the pure directory part."""

    def _arc(self, d, name, host="oldbox"):
        os.makedirs(os.path.dirname(os.path.join(d, name)), exist_ok=True)
        with tarfile.open(os.path.join(d, name), "w:gz") as t:
            data = json.dumps({"version": 1, "ts": 1700000000, "host": host,
                               "files": ["a", "b"]}).encode()
            ti = tarfile.TarInfo("manifest.json")
            ti.size = len(data)
            t.addfile(ti, __import__("io").BytesIO(data))

    def test_finds_archives_at_root_and_one_level_deep(self):
        with tempfile.TemporaryDirectory() as root:
            self._arc(root, "nas-settings-backup/nas-settings-20260101-000000.tar.gz")
            self._arc(root, "backups/nas-settings-backup/nas-settings-20260202-000000.tar.gz")
            got = nas._scan_dir_for_archives(root)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["host"], "oldbox")
        self.assertEqual(got[0]["files"], 2)

    def test_foreign_files_are_ignored(self):
        # the name contract is _BK_NAME_RE — deliberately loose (nas-settings-*.tar.gz),
        # the same rule the list/rotation code lives by; anything else is not ours
        with tempfile.TemporaryDirectory() as root:
            d = os.path.join(root, "nas-settings-backup")
            os.makedirs(d)
            open(os.path.join(d, "random.tar.gz"), "w").close()
            open(os.path.join(d, "backup.tgz"), "w").close()
            self.assertEqual(nas._scan_dir_for_archives(root), [])

    def test_unreadable_archive_is_listed_without_manifest_fields(self):
        # a truncated/corrupt archive still shows up (the user should SEE it and
        # judge), just without host/date — the restore itself would fail loudly
        with tempfile.TemporaryDirectory() as root:
            d = os.path.join(root, "nas-settings-backup")
            os.makedirs(d)
            open(os.path.join(d, "nas-settings-broken.tar.gz"), "w").close()
            got = nas._scan_dir_for_archives(root)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["host"], "")
        self.assertEqual(got[0]["ts"], 0)

    def test_restore_ref_validation_refuses_escapes(self):
        for bad in ({"rel": "../etc/passwd.tar.gz"}, {"rel": "/abs.tar.gz"},
                    {"rel": "x.txt"}, {"dev": "dev; rm -rf /", "rel": "a.tar.gz"}):
            r = nas.setup_restore_backup(bad)
            self.assertFalse(r["ok"], bad)
