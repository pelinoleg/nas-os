"""Two ways to set this box up, and they did not build the same box.

NAS-OS has two setup paths: the browser wizard (`stage_system_apply`, what nearly every box
actually runs) and the terminal menu (`stage_system` + its stages). They drifted, and the
drift is invisible from either side — each path works, so nothing ever reports that the box
you ended up with is missing pieces the other one installs from boot one:

  * the **black box** was browser-only. It is the one component that outlives the panel, so
    a terminal-installed box had no crash forensics — and, once the watchdog landed, no
    watch over its own monitor either (see test_monitor_watchdog.py);
  * **netguard, the memory guard, the notify helper** were reachable only through Stage 8's
    "smartd" checkbox — reliability the browser path calls core, sitting behind an optional
    tick in a stage about disk health. The memory guard is the answer to the hour this box
    spent thrashing itself into swap;
  * **UAS-off / USB timeout** were browser-only: a USB bridge that lies about UAS resets the
    whole device, and a backup running on it goes down with it;
  * **avahi** was enabled by the terminal path without scoping it to an interface — the
    exact bug the browser path fixes, where `<host>.local` answers with a docker bridge
    address and the name does not work in the house;
  * **journald** was configured by BOTH, differently: two filenames, two limits, and only
    the terminal one sorted late enough (99-) to survive a distribution drop-in setting
    `Storage=volatile`. The browser path — the default — wrote 50-nas.conf, the weaker of
    the two, and only it was ever reviewed.

This file pins the invariant rather than the list: whatever a box is given from boot one, it
is given on both paths.
"""
import os
import re
import unittest

ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIZARD = os.path.join(ROOT, "nas-wizard.sh")

with open(WIZARD, encoding="utf-8") as _f:
    SRC = _f.read()


def _body(name):
    m = re.search(r'^%s\(\)\s*\{\n(.*?)^\}' % re.escape(name), SRC, re.M | re.S)
    return m.group(1) if m else ""


def _calls(name):
    """Installer calls made directly by a shell function."""
    return set(re.findall(r'^\s*(install_[a-z_]+)\b', _body(name), re.M))


class BothPathsBuildTheSameBox(unittest.TestCase):

    # what a box gets from boot one, whichever wizard set it up
    FROM_BOOT_ONE = {
        "install_blackbox",      # the only watcher that outlives the panel
        "install_netguard",      # one active link, Wi-Fi failover, availability log
        "install_memory_guard",  # slice limits — the answer to the swap-thrash incident
        "install_notify_helper", # without it smartd/netguard alerts reach nobody
        "install_logrotate",     # the wizard's own log
        "install_uas_off",
        "install_usb_timeout",
        "install_motd",
    }

    def test_the_browser_path_still_has_them(self):
        missing = self.FROM_BOOT_ONE - _calls("stage_system_apply")
        self.assertFalse(missing, "the browser wizard stopped installing: %s" % sorted(missing))

    def test_the_terminal_path_has_them_too(self):
        missing = self.FROM_BOOT_ONE - _calls("stage_system")
        self.assertFalse(missing,
                         "a box set up from the terminal comes out without: %s" % sorted(missing))

    def test_no_new_drift_in_the_reliability_set(self):
        """The browser path is where new reliability work lands. Anything added there and
        not here is the next divergence — caught while it is one line, not a year later."""
        browser = _calls("stage_system_apply") & self.FROM_BOOT_ONE
        self.assertTrue(browser <= _calls("stage_system"),
                        "the two setup paths have drifted apart again")


class OneJournaldImplementation(unittest.TestCase):

    def test_the_terminal_handle_delegates(self):
        body = _body("sec_journald")
        self.assertTrue(body, "sec_journald is gone — the terminal menu addresses it by name")
        self.assertIn("install_journal_caps", body,
                      "journald is configured twice again, with two sets of numbers")
        self.assertNotIn("write_file", body, "sec_journald grew its own copy back")

    def test_the_drop_in_sorts_last(self):
        body = _body("install_journal_caps")
        self.assertIn("/etc/systemd/journald.conf.d/99-nas.conf", body,
                      "a drop-in below 99- can be overruled by one the distribution ships "
                      "(Storage=volatile would put the journal back in /run)")
        self.assertNotIn("write_file /etc/systemd/journald.conf.d/50-nas.conf", body)

    def test_it_clears_the_files_it_used_to_write(self):
        body = _body("install_journal_caps")
        for old in ("00-nas.conf", "50-nas.conf"):
            self.assertIn(old, body,
                          "%s is left behind on every box upgraded from an older wizard, "
                          "and two drop-ins disagreeing is how the limit silently changes"
                          % old)

    def test_it_still_keeps_the_journal_on_disk(self):
        body = _body("install_journal_caps")
        self.assertIn("Storage=persistent", body)
        self.assertTrue(re.search(r'^SystemMaxUse=\d+M', body, re.M), "the journal is unbounded")
        self.assertIn("log2ram", body,
                      "the guard against /var/log on tmpfs was lost in the merge")


class AvahiIsAlwaysScoped(unittest.TestCase):

    def test_enabling_avahi_scopes_it(self):
        body = _body("shares_avahi")
        self.assertTrue(body, "shares_avahi not found")
        self.assertIn("install_mdns_scope", body,
                      "avahi answers on every interface it can see, docker bridges "
                      "included — <host>.local then resolves to 172.18.0.1")

    def test_the_browser_path_scopes_it_too(self):
        self.assertIn("install_mdns_scope", _calls("stage_system_apply"))


if __name__ == "__main__":
    unittest.main()
