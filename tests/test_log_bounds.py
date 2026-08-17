"""Every log on this box is bounded except the one the box writes itself.

The journal is capped (`install_journal_caps`: 512M, one month), container logs are capped
(`install_docker_logcaps`), and everything Debian ships arrives with its own logrotate
snippet. `/var/log/nas-wizard.log` had none: it grew forever, on the system NVMe, which is
the disk this project already watches for wear. It came out of the 200-point readiness
audit as one of the "мелочи" — the kind that is only small until the day it is not.

What is pinned here:

  * the snippet exists and covers the wizard's log;
  * it is installed by BOTH setup paths (browser and terminal) — an install path that
    quietly ends up with a different box is how this class of gap appears in the first place;
  * `create` matches the mode the wizard gives the file itself (0640 root root, see the
    `touch`/`chmod` at the top of stage_prepare) — logrotate's default would leave the new
    file world-readable, and this log carries command output;
  * no `copytruncate`. Every write is a plain `>>` from a short-lived shell, so nothing
    holds the file across a rotation; copytruncate would buy nothing and cost a full copy
    of the file on a disk whose write budget is the reason this project counts GB/day.
"""
import os
import re
import unittest

ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIZARD = os.path.join(ROOT, "nas-wizard.sh")


def _func_body(name):
    with open(WIZARD, encoding="utf-8") as f:
        src = f.read()
    m = re.search(r'^%s\(\)\s*\{\n(.*?)^\}' % re.escape(name), src, re.M | re.S)
    return m.group(1) if m else ""


class WizardLogRotates(unittest.TestCase):

    def setUp(self):
        self.body = _func_body("install_logrotate")
        self.assertTrue(self.body, "install_logrotate is gone — the wizard's log grows forever")

    def test_it_writes_a_snippet_for_the_wizard_log(self):
        self.assertIn("/etc/logrotate.d/nas-os", self.body)
        self.assertIn("/var/log/nas-wizard.log", self.body)

    def test_it_bounds_the_history(self):
        self.assertTrue(re.search(r'^\s*rotate\s+\d+', self.body, re.M),
                        "no `rotate` — logrotate would keep every generation forever")
        self.assertTrue(re.search(r'^\s*(daily|weekly|monthly)\b', self.body, re.M),
                        "no rotation interval")
        self.assertIn("compress", self.body)
        self.assertIn("missingok", self.body,
                      "a box that has never run the wizard would error every night")

    def test_the_rotated_file_keeps_the_wizard_permissions(self):
        self.assertTrue(re.search(r'^\s*create\s+0640\s+root\s+root\s*$', self.body, re.M),
                        "without an explicit `create` the new log is world-readable — and "
                        "this one holds the output of every command the wizard ran")

    def test_no_copytruncate(self):
        self.assertNotIn("copytruncate", self.body,
                         "nothing holds this file open across a rotation (every write is a "
                         "`>>` from a short-lived shell); copytruncate only buys a full copy "
                         "of the file on the system disk")

    def test_both_setup_paths_install_it(self):
        for stage, how in (("stage_system_apply", "the browser wizard"),
                           ("stage_system", "the terminal wizard")):
            body = _func_body(stage)
            self.assertTrue(body, "%s not found" % stage)
            self.assertTrue(re.search(r'^\s*install_logrotate\b', body, re.M),
                            "%s sets up a box whose wizard log never rotates" % how)

    def test_it_can_be_re_run_from_the_panel(self):
        # every other installer of this shape has an api action; without one the only way to
        # repair the snippet on a live box is to hand-edit /etc
        with open(WIZARD, encoding="utf-8") as f:
            src = f.read()
        self.assertTrue(re.search(r'^\s*logrotate\)\s+install_logrotate\s*;;', src, re.M),
                        "no `nas-wizard.sh api logrotate`")


if __name__ == "__main__":
    unittest.main()
