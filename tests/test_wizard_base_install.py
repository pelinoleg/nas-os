"""The base install must install the engines it advertises — and admit when it didn't.

Two defects, one root: the System stage of the wizard reported success unconditionally.

1. kopia was never in the base at all. It was install-on-demand behind a button in its
   own app, on the reasoning that "a box that never opens the app should not carry the
   binary" — while rclone, fetched the exact same way from a vendor's site, was always
   in the base. A backup engine you discover missing on the day you need it is the one
   failure a NAS must not have.
2. Everything in the base only warns and carries on (correct in itself: a dead download
   must not abort a 20-minute install), and stage_system_apply ended with a bare
   `echo "system prepared"`. The panel marks a stage done on exit code 0 and nothing
   else, so the stage went green whether docker installed or not.

The behavioural half of this file runs the real report_base_failures out of the wizard —
sourcing is side-effect free thanks to the NASW_NO_MAIN guard, and the function only
calls warn().
"""
import os
import re
import subprocess
import unittest

ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIZARD = os.path.join(ROOT, "nas-wizard.sh")
SETUP  = os.path.join(ROOT, "web", "setup.html")


def _sh(body):
    """Run bash with the wizard sourced (no install, no main). Returns (code, output)."""
    script = 'NASW_NO_MAIN=1\nsource %s\n%s\n' % (WIZARD, body)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _func_body(name):
    """The text of a shell function, from its `name() {` line to the closing `}`."""
    with open(WIZARD, encoding="utf-8") as f:
        src = f.read()
    m = re.search(r'^%s\(\)\s*\{\n(.*?)^\}' % re.escape(name), src, re.M | re.S)
    return m.group(1) if m else ""


class BaseInstallExitCode(unittest.TestCase):
    """report_base_failures decides whether the panel may paint the stage green."""

    def test_clean_install_returns_zero(self):
        code, _ = _sh('NASW_BASE_FAIL=(); NASW_SKIPPED=(); report_base_failures')
        self.assertEqual(code, 0, "a clean base install must still report success")

    def test_one_failure_fails_the_stage_and_names_it(self):
        code, out = _sh('NASW_BASE_FAIL=("kopia — no snapshot backups"); NASW_SKIPPED=();'
                        ' report_base_failures')
        self.assertEqual(code, 1, "a failed engine install still reported success")
        self.assertIn("kopia", out, "the failure was counted but not named")

    def test_missing_required_package_fails_the_stage(self):
        # report_skipped_packages prints this loudly and returns 0 — only the console saw it
        code, out = _sh('NASW_BASE_FAIL=();'
                        ' NASW_SKIPPED=("$(printf \'req\\tmergerfs\\tNAS stack\\tno pool\')");'
                        ' report_base_failures')
        self.assertEqual(code, 1, "a REQUIRED package that never installed left the stage green")
        self.assertIn("mergerfs", out)

    def test_optional_package_does_not_fail_the_stage(self):
        # convenience packages (editors, viewers) must not turn the whole install red
        code, _ = _sh('NASW_BASE_FAIL=();'
                      ' NASW_SKIPPED=("$(printf \'opt\\tncdu\\tutilities\\t\')");'
                      ' report_base_failures')
        self.assertEqual(code, 0, "a missing convenience package must not fail the install")

    def test_dry_run_records_no_failures(self):
        # --dry-run installs nothing by definition; a plan preview reporting failures
        # would make the preview useless and the stage permanently red
        code, _ = _sh('DRY_RUN=1; NASW_BASE_FAIL=(); NASW_SKIPPED=();'
                      ' base_fail "docker"; report_base_failures')
        self.assertEqual(code, 0, "--dry-run reported failures for things it never tried")


class EnginesAreInTheBase(unittest.TestCase):
    """What the box is set up with, once, and never asked about again."""

    ENGINES = ("install_rclone", "install_kopia", "install_syncthing")

    def test_all_three_engines_are_installed_by_the_base(self):
        body = _func_body("stage_system_apply")
        self.assertTrue(body, "stage_system_apply not found in the wizard")
        for fn in self.ENGINES:
            self.assertTrue(re.search(r'^\s*%s\b' % fn, body, re.M),
                            "%s is not part of the base install" % fn)

    def test_engine_and_docker_failures_are_reported(self):
        # installing them is half the job: each can fail on the network alone, and a
        # silent failure here is exactly what made the stage lie
        body = _func_body("stage_system_apply")
        for fn in self.ENGINES:
            self.assertTrue(re.search(r'%s\s*\|\|\s*base_fail' % fn, body),
                            "a failed %s would pass unnoticed" % fn)
        self.assertIn("base_fail", body.split("ensure_gh")[0],
                      "a missing docker would pass unnoticed")

    def test_the_terminal_wizard_installs_the_same_set(self):
        # a box set up from the TUI menu must not end up with fewer apps than one set
        # up from the browser — the terminal path had none of the three
        body = _func_body("stage_system")
        self.assertTrue(body, "stage_system not found in the wizard")
        for fn in self.ENGINES:
            self.assertTrue(re.search(r'^\s*%s\b' % fn, body, re.M),
                            "the terminal wizard skips %s" % fn)

    def test_the_stage_returns_the_verdict(self):
        # the whole point: the last word of the base install is an exit code, not an echo
        body = _func_body("stage_system_apply")
        self.assertTrue(re.search(r'report_base_failures\s*\|\|\s*return 1', body),
                        "stage_system_apply reports success unconditionally again")

    def test_kopia_button_survives_as_the_retry_path(self):
        # being in the base does not remove the app's «Install kopia» button — that is
        # how a box recovers when the download failed during setup
        with open(WIZARD, encoding="utf-8") as f:
            src = f.read()
        self.assertTrue(re.search(r'^\s*kopia\)\s+install_kopia\s*;;', src, re.M),
                        "api kopia went missing")
        self.assertTrue(re.search(r'^\s*kopia-update\)\s+install_kopia update\s*;;', src, re.M),
                        "api kopia-update went missing")


class WizardScreenTellsTheTruth(unittest.TestCase):
    """The screen listing what gets installed must list what gets installed.

    rclone and syncthing were in the base for months and named nowhere on it, which is
    how "the main apps didn't install" happens even when they did.
    """

    def test_system_stage_names_the_engines(self):
        with open(SETUP, encoding="utf-8") as f:
            html = f.read()
        for name in ("rclone", "kopia", "syncthing"):
            self.assertIn(name, html,
                          "the System stage never tells the owner %s is installed" % name)


if __name__ == "__main__":
    unittest.main()
