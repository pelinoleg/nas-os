"""Files on this box that hold secrets, and the three ways they leaked.

Every one of these was fixed on 2026-08-14/15 by hand and left without a test, which is
the same as untested: nothing here is visible in the panel, so a regression shows up as a
0644 file nobody looks at, or as tokens that were overwritten weeks ago.

  * rclone.conf is where every cloud token on the box lives. The temp file was created by
    plain open() — 0644 under the umask — and an empty or mangled paste was accepted and
    wiped every token with no copy kept.
  * a docker stack's .env holds database passwords and API keys. It was written 0644 into
    a 0755 directory, and saving — unlike deleting — followed a symlink out of /opt/stacks
    and wrote the passwords wherever it pointed.
  * glance.json carries the display token, checked BEFORE the session gate, and its action
    list authorises what an unauthenticated display may do. reboot and poweroff came back
    into that list on their own once already, so they now take a second explicit flag.

The list of remotes is here too: it is the check every other rclone call is gated on, so a
remote the panel cannot name is a remote the owner cannot use.

Most of these fail against the code as it stood before their fix; the two file-mode cases
on rclone.conf are locks rather than proofs — that one was born correct in the commit that
introduced the writer, and the mode is exactly the kind of detail a later rewrite drops.
"""
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

CONF = "[gdrive]\ntype = drive\ntoken = {\"access_token\":\"secret\"}\n"


class RcloneConfWrite(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._conf = nas.RCLONE_CONF
        nas.RCLONE_CONF = os.path.join(self.d, "etc", "rclone.conf")

    def tearDown(self):
        nas.RCLONE_CONF = self._conf
        shutil.rmtree(self.d, ignore_errors=True)

    def mode(self, p):
        return os.stat(p).st_mode & 0o777

    def test_written_owner_only(self):
        self.assertTrue(nas.rclone_conf_write(CONF))
        self.assertEqual(self.mode(nas.RCLONE_CONF), 0o600,
                         "every cloud token on the box is readable by other local users")

    def test_no_temp_file_survives_the_write(self):
        nas.rclone_conf_write(CONF)
        left = [f for f in os.listdir(os.path.dirname(nas.RCLONE_CONF)) if f != "rclone.conf"]
        self.assertEqual(left, [], "a leftover file with the tokens stayed behind")

    def test_a_paste_that_is_not_a_config_is_refused(self):
        nas.rclone_conf_write(CONF)
        self.assertFalse(nas.rclone_conf_write("oops, wrong window"),
                         "a stray paste was accepted as a config")
        with open(nas.RCLONE_CONF) as f:
            self.assertIn("access_token", f.read(), "the tokens were wiped by a bad paste")

    def test_one_step_back_is_always_available(self):
        nas.rclone_conf_write(CONF)
        self.assertTrue(nas.rclone_conf_write(""))          # an empty paste is a wipe
        prev = nas.RCLONE_CONF + ".prev"
        self.assertTrue(os.path.exists(prev), "the previous config was not kept")
        with open(prev) as f:
            self.assertIn("access_token", f.read())
        self.assertEqual(self.mode(prev), 0o600,
                         "the copy holding the old tokens is world-readable")

    def test_a_missing_trailing_newline_is_added(self):
        # rclone silently ignores a last section that does not end with a newline
        nas.rclone_conf_write("[s3]\ntype = s3")
        with open(nas.RCLONE_CONF) as f:
            self.assertTrue(f.read().endswith("\n"))


class RcloneRemoteNames(unittest.TestCase):
    """`listremotes` output is one remote per line, and a remote may be named
    "Backup Drive". Splitting on whitespace made the real one unreachable — every call
    checks `remote in rclone_remotes()` — and invented a phantom that passed that check
    and failed inside rclone with "didn't find section in config file"."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._conf, self._inst = nas.RCLONE_CONF, nas.rclone_installed
        nas.RCLONE_CONF = os.path.join(self.d, "rclone.conf")
        with open(nas.RCLONE_CONF, "w") as f:
            f.write(CONF)
        nas.rclone_installed = lambda: True

    def tearDown(self):
        nas.RCLONE_CONF, nas.rclone_installed = self._conf, self._inst
        shutil.rmtree(self.d, ignore_errors=True)

    def remotes(self, stdout):
        with mock.patch.object(nas.subprocess, "run",
                               return_value=mock.Mock(stdout=stdout, returncode=0)):
            return nas.rclone_remotes()

    def test_a_name_with_a_space_stays_one_remote(self):
        self.assertEqual(self.remotes("Backup Drive:\ngdrive:\n"), ["Backup Drive", "gdrive"])

    def test_blank_lines_and_noise_are_dropped(self):
        self.assertEqual(self.remotes("\ngdrive:\n\nnot a remote\n"), ["gdrive"])


class StackSecrets(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = tempfile.mkdtemp()          # somewhere a symlink could point
        self._dir = nas.STACKS_DIR
        nas.STACKS_DIR = os.path.join(self.d, "stacks")
        os.makedirs(nas.STACKS_DIR)

    def tearDown(self):
        nas.STACKS_DIR = self._dir
        shutil.rmtree(self.d, ignore_errors=True)
        shutil.rmtree(self.out, ignore_errors=True)

    def test_env_is_written_owner_only(self):
        nas.stack_save("immich", "services: {}\n", "DB_PASSWORD=hunter2\n")
        env = os.path.join(nas.STACKS_DIR, "immich", ".env")
        self.assertEqual(os.stat(env).st_mode & 0o777, 0o600,
                         "database passwords are readable by every local user")

    def test_a_rewrite_keeps_the_previous_env(self):
        nas.stack_save("immich", "a\n", "DB_PASSWORD=hunter2\n")
        nas.stack_save("immich", "b\n", "DB_PASSWORD=new\n")
        with open(os.path.join(nas.STACKS_DIR, "immich", ".env.bak")) as f:
            self.assertIn("hunter2", f.read())

    def test_saving_cannot_follow_a_symlink_out_of_the_stacks_directory(self):
        os.symlink(self.out, os.path.join(nas.STACKS_DIR, "escape"))
        r = nas.stack_save("escape", "services: {}\n", "DB_PASSWORD=hunter2\n")
        self.assertFalse(r["ok"], "the compose file and its passwords were written outside")
        self.assertEqual(os.listdir(self.out), [],
                         "passwords landed outside the stacks directory")

    def test_a_name_that_is_not_a_stack_name_is_refused(self):
        for name in ("../etc", "a/b", "", None, ".hidden"):
            self.assertFalse(nas.stack_save(name, "x", "y")["ok"],
                             "%r was accepted as a stack name" % (name,))


class GlancePowerActions(unittest.TestCase):
    """The display feed is authorised by a token, not by a session, so its action list is
    the whole permission model for anything holding that token."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._file = nas.GLANCE_FILE
        nas.GLANCE_FILE = os.path.join(self.d, "glance.json")

    def tearDown(self):
        nas.GLANCE_FILE = self._file
        shutil.rmtree(self.d, ignore_errors=True)

    def test_saving_the_whole_catalogue_does_not_arm_the_power_switches(self):
        cur = nas.save_glance({"actions": list(nas.GLANCE_ACTIONS)})
        self.assertNotIn("reboot", cur["actions"], "a bulk save re-armed reboot")
        self.assertNotIn("poweroff", cur["actions"], "a bulk save re-armed poweroff")
        self.assertIn("touch", cur["actions"], "the harmless actions were dropped too")

    def test_the_power_switches_can_still_be_granted_deliberately(self):
        cur = nas.save_glance({"actions": ["reboot"], "allow_power": True})
        self.assertEqual(cur["actions"], ["reboot"])

    def test_an_action_outside_the_catalogue_is_never_stored(self):
        cur = nas.save_glance({"actions": ["touch", "rm -rf"], "allow_power": True})
        self.assertEqual(cur["actions"], ["touch"])

    def test_the_file_with_the_display_token_is_owner_only(self):
        nas.save_glance({"token_action": "new"})
        self.assertEqual(os.stat(nas.GLANCE_FILE).st_mode & 0o777, 0o600,
                         "the token is checked before the session gate and readable by all")

    def test_a_save_does_not_drop_keys_it_knows_nothing_about(self):
        nas._json_save(nas.GLANCE_FILE, {"screens": [{"tiles": ["cpu"]}]}, indent=2, mode=0o600)
        nas.save_glance({"enabled": True})
        self.assertIn("screens", nas._json_load_strict(nas.GLANCE_FILE, {}),
                      "ticking a box deleted a layout the user had built")


if __name__ == "__main__":
    unittest.main()
