"""The config repository must not turn 0600 files into world-readable git objects.

2026-08-14: $NAS_CONFIG is world-readable on purpose, the panel writes its secrets there
with 0600, and `git add -A` copied them into .git/objects — created 0444, and kept forever.
The display token, rotated that same morning *because* it had been world-readable, was
recovered from those objects by an unprivileged user; the allow-list committed next to it
still granted `poweroff`.

The guard is a shell function, so the test runs the real function text out of the wizard
against a throwaway repository and checks the outcome: repo private, secrets untracked.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

WIZARD = os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-wizard.sh")
SECRETS = ("glance.json", "credentials.json", "disaster-card.md")


def _guard_source():
    """The nas_config_git_guard function as written in the wizard."""
    src = open(WIZARD, encoding="utf-8").read()
    m = re.search(r"^nas_config_git_guard\(\) \{.*?^\}", src, re.S | re.M)
    return m.group(0) if m else ""


@unittest.skipIf(shutil.which("git") is None, "git not installed")
class ConfigGitKeepsSecretsOut(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", self.d], check=True)
        os.chmod(os.path.join(self.d, ".git"), 0o755)      # the state found on the box
        for n in SECRETS + ("desktop.json",):
            with open(os.path.join(self.d, n), "w") as f:
                f.write('{"token": "s3cr3t"}\n')
        subprocess.run(["git", "-C", self.d, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.d, "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "-m", "before"], check=True)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _run_guard(self):
        script = "%s\nDRY_RUN=0\nNAS_CONFIG=%s\ninfo(){ :; }\nnas_config_git_guard\n" % (
            _guard_source(), self.d)
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_guard_exists(self):
        self.assertTrue(_guard_source(), "nas_config_git_guard vanished from the wizard")

    def test_repo_becomes_owner_only(self):
        self._run_guard()
        mode = os.stat(os.path.join(self.d, ".git")).st_mode & 0o777
        self.assertEqual(mode & 0o077, 0, "group/other can still read .git (mode %o)" % mode)

    def test_already_committed_secrets_are_untracked(self):
        self._run_guard()
        tracked = subprocess.run(["git", "-C", self.d, "ls-files"],
                                 capture_output=True, text=True).stdout.split()
        for n in SECRETS:
            self.assertNotIn(n, tracked, "%s is still tracked" % n)
        self.assertIn("desktop.json", tracked, "the guard untracked a non-secret file")

    def test_secrets_stay_on_disk(self):
        # untracking must not delete the owner's actual settings
        self._run_guard()
        for n in SECRETS:
            self.assertTrue(os.path.exists(os.path.join(self.d, n)), "%s was deleted" % n)

    def test_a_later_add_does_not_pick_them_up_again(self):
        self._run_guard()
        subprocess.run(["git", "-C", self.d, "add", "-A"], check=True)
        tracked = subprocess.run(["git", "-C", self.d, "ls-files"],
                                 capture_output=True, text=True).stdout.split()
        for n in SECRETS:
            self.assertNotIn(n, tracked, "%s came back on the next add -A" % n)


if __name__ == "__main__":
    unittest.main()
