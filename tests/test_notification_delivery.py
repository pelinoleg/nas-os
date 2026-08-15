"""The last mile: an alarm that is detected, logged — and then delivered to nobody.

This box has had zero delivery channels for its whole life: /etc/nas-wizard/notify.conf
does not exist, so push_notify() has never once run to completion here. Everything the two
audits fixed — overheating, a pool branch gone, a brute-force attempt — reaches the panel
log and stops there. The code was written, reviewed, and never executed.

Which is exactly why it deserves a test: the first real run of this path will be a genuine
alarm, and the failure modes are all silent ones. A request that Pushover rejects for a
missing retry/expire, a priority outside the allowed range, a token written world-readable,
a network error taking down the monitor tick — none of them announce themselves.

The gate is here too. Whether an event rings a phone is decided in two places (the event's
own switch and the global "what may ring") and the panel draws a per-event "rings"/"held
back" marker from the server's own list — so the list is a promise the UI repeats, and a
name that drifts out of it silently downgrades an urgent alarm.
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


class KeysOnDisk(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._conf = nas.NOTIFY_CONF
        nas.NOTIFY_CONF = os.path.join(self.d, "wizard", "notify.conf")

    def tearDown(self):
        nas.NOTIFY_CONF = self._conf
        shutil.rmtree(self.d, ignore_errors=True)

    def test_the_token_is_written_owner_only(self):
        nas.save_notify("uKEY", "aTOKEN")
        self.assertEqual(os.stat(nas.NOTIFY_CONF).st_mode & 0o777, 0o600,
                         "the Pushover token is readable by every local process")

    def test_the_mode_does_not_come_from_a_chmod_afterwards(self):
        # 0644 for the length of the write is long enough for another local process to
        # read the token, and it stays 0644 forever if the write dies before the chmod.
        # What matters is the mode the file is CREATED with.
        with mock.patch.object(nas.os, "chmod"):
            nas.save_notify("uKEY", "aTOKEN")
        self.assertEqual(os.stat(nas.NOTIFY_CONF).st_mode & 0o777, 0o600,
                         "the token was world-readable until a later chmod fixed it")

    def test_no_temp_file_is_left_holding_the_token(self):
        nas.save_notify("uKEY", "aTOKEN")
        self.assertEqual(sorted(os.listdir(os.path.dirname(nas.NOTIFY_CONF))),
                         ["notify.conf"])

    def test_the_keys_read_back(self):
        nas.save_notify("uKEY", "aTOKEN")
        n = nas.load_notify()
        self.assertEqual((n["user"], n["token"]), ("uKEY", "aTOKEN"))
        self.assertTrue(n["configured"])

    def test_without_both_keys_nothing_is_configured(self):
        nas.save_notify("uKEY", "")
        self.assertFalse(nas.load_notify()["configured"])

    def test_other_settings_in_the_file_survive(self):
        # notify.conf is the wizard's file too — a save here must not eat its lines
        os.makedirs(os.path.dirname(nas.NOTIFY_CONF), exist_ok=True)
        with open(nas.NOTIFY_CONF, "w") as f:
            f.write('NETGUARD_PING="1.1.1.1"\nPUSHOVER_USER="old"\n')
        nas.save_notify("new", "tok")
        with open(nas.NOTIFY_CONF) as f:
            body = f.read()
        self.assertIn('NETGUARD_PING="1.1.1.1"', body)
        self.assertIn('PUSHOVER_USER="new"', body)
        self.assertNotIn('"old"', body)

    def test_a_failed_write_keeps_the_previous_keys(self):
        nas.save_notify("uKEY", "aTOKEN")
        with mock.patch.object(nas.os, "replace", side_effect=OSError("disk full")):
            r = nas.save_notify("broken", "broken")
        self.assertFalse(r["ok"])
        self.assertEqual(nas.load_notify()["user"], "uKEY",
                         "a failed save destroyed the working keys")
        self.assertEqual(sorted(os.listdir(os.path.dirname(nas.NOTIFY_CONF))),
                         ["notify.conf"], "a leftover file with the token stayed behind")


class TheRequestPushoverActuallyGets(unittest.TestCase):
    """push_notify has never completed on this box. What it would send."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._conf = nas.NOTIFY_CONF
        nas.NOTIFY_CONF = os.path.join(self.d, "notify.conf")
        nas.save_notify("uKEY", "aTOKEN")
        self.sent = []

    def tearDown(self):
        nas.NOTIFY_CONF = self._conf
        shutil.rmtree(self.d, ignore_errors=True)

    def send(self, *a, **kw):
        def fake_urlopen(url, data=None, timeout=None):
            self.sent.append((url, dict(p.split("=", 1) for p in
                                        data.decode().split("&"))))
            return mock.MagicMock()
        with mock.patch.object(nas.urllib.request, "urlopen", fake_urlopen):
            return nas.push_notify(*a, **kw)

    def test_a_normal_alarm_is_delivered(self):
        self.assertTrue(self.send("NAS: disk failed SMART", "/dev/sda — replace the disk", 1))
        url, body = self.sent[0]
        self.assertEqual(url, "https://api.pushover.net/1/messages.json")
        self.assertEqual(body["token"], "aTOKEN")
        self.assertEqual(body["user"], "uKEY")
        self.assertEqual(body["priority"], "1")

    def test_an_emergency_carries_retry_and_expire(self):
        # Pushover REJECTS priority 2 without them — the one level that is meant to keep
        # ringing until acknowledged would be the one that never arrives
        self.send("NAS: read-only filesystem", "data at risk", 2)
        _url, body = self.sent[0]
        self.assertIn("retry", body)
        self.assertIn("expire", body)

    def test_a_priority_out_of_range_is_clamped_not_refused(self):
        for given, expect in ((7, "2"), (-9, "-2"), ("loud", "0"), (None, "0")):
            self.sent = []
            self.send("t", "m", given)
            self.assertEqual(self.sent[0][1]["priority"], expect,
                             "priority %r was passed through as-is" % (given,))

    def test_without_keys_nothing_is_sent_and_nothing_raises(self):
        nas.save_notify("", "")
        self.assertFalse(self.send("t", "m", 1))
        self.assertEqual(self.sent, [], "a request went out with empty credentials")

    def test_a_network_failure_is_reported_not_raised(self):
        # push_notify is called from inside monitor_tick: an exception here would take
        # down every check that runs after it
        with mock.patch.object(nas.urllib.request, "urlopen", side_effect=OSError("no route")):
            self.assertFalse(nas.push_notify("t", "m", 1))


class WhatMayRingThePhone(unittest.TestCase):

    def mode(self, m):
        return {"push_mode": m, "events": {}}

    def test_off_means_nothing_rings(self):
        for name in ("readonly", "smart", "diskfull"):
            self.assertFalse(nas._push_allowed(name, 2, self.mode("off")))

    def test_all_means_everything_switched_on_rings(self):
        self.assertTrue(nas._push_allowed("write_load", 0, self.mode("all")))

    def test_the_recommended_mode_is_the_narrow_list_plus_emergencies(self):
        self.assertTrue(nas._push_allowed("readonly", 0, self.mode("important")))
        self.assertFalse(nas._push_allowed("write_load", 0, self.mode("important")))
        self.assertTrue(nas._push_allowed("write_load", 2, self.mode("important")),
                        "an explicitly critical event was held back")

    def test_a_dying_disk_rings_however_it_is_dying(self):
        # An ATA disk usually fails the overall SMART health check; an NVMe usually does
        # not — it reports media errors, an exhausted spare reserve and rated life used up
        # while still answering "passed". Both mean the same thing to the owner, so both
        # ring. (Owner's decision, 2026-08-15.)
        for name in ("smart", "smart_wear"):
            self.assertTrue(nas._push_allowed(name, 1, self.mode("important")),
                            "%s waits for the evening digest" % name)

    def test_a_backup_that_did_not_happen_rings(self):
        # The one failure whose silence is indistinguishable from success. All of it was
        # held back for the evening digest — which itself could not be delivered.
        for name in ("nb_missed", "nb_stale", "nb_verify", "kp_err", "kp_stale"):
            self.assertTrue(nas._push_allowed(name, 1, self.mode("important")),
                            "%s waits for a digest nobody gets" % name)

    def test_a_failed_service_rings(self):
        self.assertTrue(nas._push_allowed("svcfail", 1, self.mode("important")))

    def test_the_digest_itself_can_be_delivered(self):
        # 23 switched-on events are deliberately held back "for the evening digest", so a
        # digest that cannot ring turns that promise into silence.
        cat = nas._def_monitor()["events"]["daily_summary"]
        self.assertTrue(cat["on"], "the digest is switched off in the catalogue")
        self.assertTrue(nas._push_allowed("daily_summary", cat["priority"], self.mode("important")))
        self.assertLess(cat["priority"], 0, "the digest should arrive quietly, not as an alarm")

    def test_the_narrow_list_only_names_events_that_exist(self):
        # the panel draws its "rings" marker from this list; a name that no longer matches
        # an event silently downgrades that alarm to the evening digest
        catalog = nas._def_monitor()["events"]
        for name in nas._PUSH_NOW:
            self.assertIn(name, catalog, "%s rings nothing — no such event" % name)

    def test_the_events_that_may_ring_are_switched_on_in_the_catalog(self):
        # an urgent event whose own switch is off by default cannot ring either
        catalog = nas._def_monitor()["events"]
        for name in nas._PUSH_NOW:
            self.assertTrue(catalog[name].get("on"),
                            "%s is on the urgent list but switched off by default" % name)


class TheLogIsKeptWhateverHappens(unittest.TestCase):
    """Delivery is a method; the log is the record. It must not depend on the channel."""

    def setUp(self):
        self.logged, self.pushed = [], []
        self._log, self._push = nas.log_event, nas.push_notify
        self._mon = nas.load_monitor
        nas.log_event = lambda *a, **k: self.logged.append(a)
        nas.push_notify = lambda *a, **k: self.pushed.append(a)
        nas._MON_LAST.clear()

    def tearDown(self):
        nas.log_event, nas.push_notify = self._log, self._push
        nas.load_monitor = self._mon
        nas._MON_LAST.clear()

    def monitor(self, enabled, on=True):
        nas.load_monitor = lambda: {"enabled": enabled, "push_mode": "important",
                                    "events": {"readonly": {"on": on, "priority": 1}}}

    def test_an_event_is_logged_even_with_no_channel_at_all(self):
        self.monitor(enabled=False)
        nas.notify_event("readonly", "ro:/mnt/disk1", "NAS: read-only", "data at risk")
        self.assertEqual(len(self.logged), 1, "the alarm was not even written down")
        self.assertEqual(self.pushed, [])

    def test_with_the_channel_on_it_is_both_logged_and_delivered(self):
        self.monitor(enabled=True)
        nas.notify_event("readonly", "ro:/mnt/disk1", "NAS: read-only", "data at risk")
        self.assertEqual(len(self.logged), 1)
        self.assertEqual(len(self.pushed), 1)

    def test_the_events_own_switch_still_decides(self):
        self.monitor(enabled=True, on=False)
        nas.notify_event("readonly", "ro:/mnt/disk1", "NAS: read-only", "data at risk")
        self.assertEqual(len(self.logged), 1)
        self.assertEqual(self.pushed, [], "an event switched off was delivered anyway")

    def test_the_same_alarm_does_not_repeat_inside_its_cooldown(self):
        self.monitor(enabled=True)
        for _ in range(3):
            nas.notify_event("readonly", "ro:/mnt/disk1", "NAS: read-only", "data at risk")
        self.assertEqual(len(self.logged), 1, "a flapping condition would spam the phone")

    def test_a_different_source_of_the_same_kind_is_its_own_alarm(self):
        self.monitor(enabled=True)
        nas.notify_event("readonly", "ro:/mnt/disk1", "NAS: read-only", "disk1")
        nas.notify_event("readonly", "ro:/mnt/disk2", "NAS: read-only", "disk2")
        self.assertEqual(len(self.logged), 2, "the second disk was swallowed by the first")


class TheDigestDoesNotRepeatItself(unittest.TestCase):
    """The slot already sent is remembered across restarts.

    It used to live only in the process, while the tick owes a slot that went by while the
    panel was down (up to six hours) — so every restart re-sent it. The log holds six
    copies of the 08-14 summary inside 55 minutes."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._state, self._last = nas.SUMMARY_STATE, nas._LAST_SUMMARY
        nas.SUMMARY_STATE = os.path.join(self.d, "summary-state.json")
        nas._LAST_SUMMARY = ""

    def tearDown(self):
        nas.SUMMARY_STATE, nas._LAST_SUMMARY = self._state, self._last
        shutil.rmtree(self.d, ignore_errors=True)

    def test_the_slot_survives_a_restart(self):
        nas._summary_mark("2026-08-15T20:00")
        nas._LAST_SUMMARY = ""                      # the panel restarts
        self.assertEqual(nas._summary_last(), "2026-08-15T20:00",
                         "the digest would be sent again after every restart")

    def test_nothing_sent_yet_is_not_a_slot(self):
        self.assertEqual(nas._summary_last(), "")

    def test_a_corrupt_state_file_does_not_break_the_tick(self):
        with open(nas.SUMMARY_STATE, "w") as f:
            f.write("{not json")
        self.assertEqual(nas._summary_last(), "")


if __name__ == "__main__":
    unittest.main()
