"""SMB («Servers» over CIFS) — the transport that replaced sshfs for NAS sources.

Why this exists: on 2026-08-27 the Kopia backup of the Ugreen NAS was reading its source
over sshfs. That SFTP session collapsed roughly once a second under sustained reading —
400 files gave 101 read errors, and reading strictly one file at a time failed too, so it
was not the backup's parallelism. Single-stream ssh to the same box was flawless, and the
identical read tests over CIFS failed zero times. sshfs's `reconnect` hid every drop by
re-connecting, which invalidates open handles, so the backup saw EIO, lost whole
directories and stored 4068 files of a ~1.8 TB source.

The two properties worth pinning down here are the ones that make the switch safe:
  * a share name can never escape the mountpoint (it becomes a directory name), and
  * a connection is «mounted» only when EVERY share is up — half a connection reads as
    empty folders, which is exactly how a backup silently loses a share's worth of data.
"""
import importlib.util
import os
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


SMB = {"id": "ug", "kind": "smb", "host": "192.168.1.95", "user": "oleg",
       "shares": ["Cloud", "PMedia", "personal_folder"]}
SSHFS = {"id": "box", "host": "10.0.0.2", "user": "root", "path": ""}


class ShareNameTests(unittest.TestCase):
    def test_separators_and_dot_dot_are_dropped(self):
        r = {"shares": ["Cloud", "../../etc", "a/b", "a\\b", ".", "..", "", "  ", "ok"]}
        self.assertEqual(nas._remote_shares(r), ["Cloud", "ok"])

    def test_nt_illegal_characters_are_dropped(self):
        r = {"shares": ['a:b', 'a*b', 'a?b', 'a"b', "a<b", "a>b", "a|b", "keep"]}
        self.assertEqual(nas._remote_shares(r), ["keep"])

    def test_duplicates_collapse_and_order_is_kept(self):
        self.assertEqual(nas._remote_shares({"shares": ["b", "a", "b"]}), ["b", "a"])

    def test_a_share_can_never_leave_the_mountpoint(self):
        mp = nas._remote_mp("ug")
        for t in nas._remote_targets(dict(SMB, shares=["../escape", "Cloud"])):
            self.assertTrue(os.path.normpath(t).startswith(mp + os.sep), t)


class TargetTests(unittest.TestCase):
    def test_sshfs_is_one_mount_at_the_mountpoint(self):
        self.assertEqual(nas._remote_targets(SSHFS), [nas._remote_mp("box")])

    def test_smb_is_one_mount_per_share_under_the_mountpoint(self):
        mp = nas._remote_mp("ug")
        self.assertEqual(nas._remote_targets(SMB),
                         [os.path.join(mp, s) for s in SMB["shares"]])

    def test_layout_matches_what_sftp_showed(self):
        # the whole point of mounting shares as subfolders: a backup pointed at the
        # mountpoint keeps the same paths when the connection switches sshfs -> SMB
        self.assertIn(os.path.join(nas._remote_mp("ug"), "Cloud"), nas._remote_targets(SMB))


class MountedTests(unittest.TestCase):
    @staticmethod
    def _with(mounted):
        return mock.patch.object(nas, "_mount_points", return_value=set(mounted))

    def test_all_shares_up_is_mounted(self):
        tg = nas._remote_targets(SMB)
        with self._with(tg), mock.patch.object(nas.os, "statvfs", return_value=None):
            self.assertTrue(nas._remote_mounted("ug", SMB))

    def test_one_missing_share_is_NOT_mounted(self):
        tg = nas._remote_targets(SMB)
        with self._with(tg[:-1]), mock.patch.object(nas.os, "statvfs", return_value=None):
            self.assertFalse(nas._remote_mounted("ug", SMB))

    def test_connection_with_no_shares_is_never_mounted(self):
        with self._with([nas._remote_mp("ug")]):
            self.assertFalse(nas._remote_mounted("ug", dict(SMB, shares=[])))


class SaveTests(unittest.TestCase):
    def _save(self, body, existing=None):
        saved = []
        with mock.patch.object(nas, "_remotes_load", return_value=list(existing or [])), \
                mock.patch.object(nas, "_remotes_save", side_effect=lambda l: saved.append(l)), \
                mock.patch.object(nas, "remote_umount", return_value={"ok": True}):
            res = nas.remotes_save(body)
        return res, (saved[-1] if saved else [])

    def test_smb_without_shares_is_refused(self):
        res, _ = self._save({"host": "192.168.1.95", "user": "oleg", "kind": "smb"})
        self.assertFalse(res["ok"])

    def test_smb_defaults_to_port_445(self):
        res, lst = self._save({"host": "192.168.1.95", "user": "oleg",
                               "kind": "smb", "shares": ["Cloud"]})
        self.assertTrue(res["ok"])
        self.assertEqual(lst[0]["port"], 445)
        self.assertEqual(lst[0]["kind"], "smb")

    def test_sftp_still_defaults_to_port_22(self):
        res, lst = self._save({"host": "10.0.0.2", "user": "root"})
        self.assertTrue(res["ok"])
        self.assertEqual(lst[0]["port"], 22)
        self.assertEqual(lst[0]["kind"], "sshfs")

    def test_switching_protocol_unmounts_the_old_layout(self):
        # one mount vs one-per-share: leaving the old shape mounted would strand it
        old = [{"id": "ug", "name": "Ugreen", "kind": "sshfs", "host": "192.168.1.95",
                "user": "oleg", "path": ""}]
        calls = []
        with mock.patch.object(nas, "_remotes_load", return_value=list(old)), \
                mock.patch.object(nas, "_remotes_save"), \
                mock.patch.object(nas, "remote_umount", side_effect=lambda i: calls.append(i)):
            nas.remotes_save({"id": "ug", "host": "192.168.1.95", "user": "oleg",
                              "name": "Ugreen", "kind": "smb", "shares": ["Cloud"]})
        self.assertEqual(calls, ["ug"])


class MountPasswordTests(unittest.TestCase):
    def test_password_goes_through_the_environment_not_argv(self):
        seen = []

        def fake_run(cmd, timeout=40, env=None, cwd=None):
            seen.append((cmd, env))
            return {"ok": True, "code": 0, "log": ""}

        r = dict(SMB, shares=["Cloud"], **{"pass": "s3cret"})
        with mock.patch.object(nas, "_run", side_effect=fake_run), \
                mock.patch.object(nas.os, "makedirs"), \
                mock.patch.object(nas, "_mount_points", return_value=set()), \
                mock.patch.object(nas.os.path, "exists", return_value=True):
            res = nas._remote_mount_smb(r)
        self.assertTrue(res["ok"])
        cmd, env = seen[0]
        self.assertNotIn("s3cret", " ".join(cmd))       # /proc/<pid>/cmdline is world-readable
        self.assertEqual(env.get("PASSWD"), "s3cret")

    def test_a_failing_share_leaves_nothing_half_mounted(self):
        # all-or-nothing: a half-mounted connection is the failure this whole design avoids
        calls = []

        def fake_run(cmd, timeout=40, env=None, cwd=None):
            calls.append(cmd)
            if cmd[0] == "umount":
                return {"ok": True, "code": 0, "log": ""}
            ok = "//192.168.1.95/Cloud" in cmd          # the second share always fails
            return {"ok": ok, "code": 0 if ok else 32, "log": "" if ok else "mount error(13)"}

        with mock.patch.object(nas, "_run", side_effect=fake_run), \
                mock.patch.object(nas.os, "makedirs"), \
                mock.patch.object(nas, "_mount_points", return_value=set()), \
                mock.patch.object(nas.os.path, "exists", return_value=True):
            res = nas._remote_mount_smb(dict(SMB, shares=["Cloud", "PMedia"]))
        self.assertFalse(res["ok"])
        self.assertIn("PMedia", res["log"])
        unmounted = [c[-1] for c in calls if c[0] == "umount"]
        self.assertIn(os.path.join(nas._remote_mp("ug"), "Cloud"), unmounted)


class RealpathTests(unittest.TestCase):
    def test_smb_resolves_locally_without_shelling_into_the_server(self):
        mp = nas._remote_mp("ug")
        with mock.patch.object(nas, "_remotes_load", return_value=[SMB]), \
                mock.patch.object(nas, "_run",
                                  side_effect=AssertionError("must not run anything")):
            res = nas.remote_realpath("ug", os.path.join(mp, "Cloud", "sub"))
        self.assertTrue(res["ok"])
        self.assertEqual(res["path"], "//192.168.1.95/Cloud/sub")

    def test_path_outside_the_mount_is_refused(self):
        with mock.patch.object(nas, "_remotes_load", return_value=[SMB]):
            self.assertFalse(nas.remote_realpath("ug", "/etc/shadow")["ok"])


if __name__ == "__main__":
    unittest.main()
