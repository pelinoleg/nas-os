"""A watchdog over the monitor — the one alarm nothing on this box could raise.

Every alarm here is produced by `monitor_tick`, running as a daemon thread inside the panel.
So the monitor is the only component whose failure is silent BY CONSTRUCTION: when the loop
dies or wedges, the box stops complaining about anything, and a box that has stopped
complaining is exactly what a healthy box looks like. The readiness audit filed this as
"nothing watches the monitor — when it dies, the silence is indistinguishable from health".

The recorder (`nas-blackbox.service`) is the only other thing here with its own unit, its own
process and a tick of its own, so the watch lives there. What these tests pin down:

  * the monitor leaves a heartbeat every time round its loop, and the recorder reads it;
  * a verdict is only believed once it has HELD — a wall-clock jump (this box has booted 12 h
    in the future once already, RTC in local time) makes a fresh beat look ancient for one
    beat interval, and that must not ring;
  * the two failures are told apart, because they ask different things of the owner: the
    panel process is gone, or the process is up and its loop stopped;
  * recovery re-arms the watch, so a SECOND death is reported too;
  * and the trap underneath all of it: the recorder is a second process writing the panel's
    event file, whose in-memory copy is loaded once and never re-read. Writing twice from
    here without dropping that copy silently deletes everything the panel logged in between —
    the watchdog would report the outage by erasing the log of it.
"""
import importlib.util
import json
import os
import re
import tempfile
import time
import unittest

SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


class _Base(unittest.TestCase):
    """Every test runs against a temp beat file, a temp event file and a fake systemctl."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="monwatch-")
        self._saved = {k: getattr(nas, k) for k in
                       ("MON_BEAT", "EVENTS_FILE", "MONITOR_FILE", "NAS_CONFIG",
                        "_run", "push_notify", "_events")}
        nas.MON_BEAT = os.path.join(self.tmp, "monitor.beat")
        nas.NAS_CONFIG = self.tmp
        nas.EVENTS_FILE = os.path.join(self.tmp, "events.json")
        nas.MONITOR_FILE = os.path.join(self.tmp, "monitor.json")
        nas._events = None
        nas.push_notify = lambda *a, **k: False        # no channel is configured on this box
        nas._MON_LAST.clear()
        self.unit = "active"
        nas._run = lambda cmd, **k: {"ok": True, "code": 0, "log": self.unit}
        self.st = {}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(nas, k, v)
        nas._MON_LAST.clear()

    def beat(self, age=0):
        with open(nas.MON_BEAT, "w") as f:
            f.write("%d\n" % int(time.time() - age))
        t = time.time() - age
        os.utime(nas.MON_BEAT, (t, t))

    def events(self):
        with open(nas.EVENTS_FILE) as f:
            return json.load(f)["items"]


class Heartbeat(_Base):

    def test_beat_is_written(self):
        nas._mon_beat()
        self.assertTrue(os.path.exists(nas.MON_BEAT))
        self.assertLess(time.time() - os.path.getmtime(nas.MON_BEAT), 5)

    def test_monitor_loop_beats(self):
        """Source check: the writer is useless unless the loop actually calls it. Pins the
        call site, not behaviour — the loop itself never returns, so it cannot be run here."""
        with open(SPEC.origin, encoding="utf-8") as f:
            src = f.read()
        body = re.search(r"\ndef monitor_loop\(\):(.*?)\n(?=# ---|\ndef )", src, re.S)
        self.assertTrue(body, "monitor_loop not found")
        self.assertIn("_mon_beat()", body.group(1),
                      "monitor_loop does not leave a heartbeat — the watchdog would report a "
                      "healthy monitor as dead on every box")


class Verdicts(_Base):

    def test_fresh_beat_is_silent(self):
        self.beat(age=10)
        self.assertIsNone(nas._mon_watch(self.st, mono=1000))
        self.assertIsNone(nas._mon_watch(self.st, mono=1000 + 10 * nas.MON_CONFIRM))

    def test_stale_beat_with_a_live_panel(self):
        """The loop stopped inside a running process — systemd sees nothing wrong."""
        self.beat(age=nas.MON_STALE + 600)
        self.assertIsNone(nas._mon_watch(self.st, mono=1000), "fired before the verdict held")
        got = nas._mon_watch(self.st, mono=1000 + nas.MON_CONFIRM)
        self.assertEqual(got, "Monitoring has stopped")
        ev = self.events()[-1]
        self.assertEqual(ev["event"], "monitor_dead")
        self.assertEqual(ev["lvl"], "crit")
        self.assertIn("15 min", ev["msg"])          # (300 + 600) // 60

    def test_dead_panel_is_a_different_verdict(self):
        self.unit = "failed"
        self.beat(age=nas.MON_STALE + 60)
        self.assertIsNone(nas._mon_watch(self.st, mono=1000))
        self.assertEqual(nas._mon_watch(self.st, mono=1000 + nas.MON_CONFIRM),
                         "The panel is not running")
        self.assertIn("failed", self.events()[-1]["msg"])

    def test_no_beat_at_all(self):
        """Nothing ever wrote one: the loop never started. /run is tmpfs, so a missing beat
        can never be a leftover from an older boot."""
        self.assertIsNone(nas._mon_watch(self.st, mono=1000))
        self.assertEqual(nas._mon_watch(self.st, mono=1000 + nas.MON_CONFIRM),
                         "Monitoring has stopped")
        self.assertIn("never ticked", self.events()[-1]["msg"])

    def test_a_verdict_that_changes_restarts_the_window(self):
        """Panel down, then back up but not ticking: the second verdict must earn its own
        confirmation instead of inheriting the first one's age."""
        self.unit = "inactive"
        self.beat(age=nas.MON_STALE + 60)
        nas._mon_watch(self.st, mono=1000)
        self.unit = "active"
        self.assertIsNone(nas._mon_watch(self.st, mono=1000 + nas.MON_CONFIRM))


class ClockJumps(_Base):

    def test_a_clock_jump_does_not_ring(self):
        """The beat carries wall-clock time. An NTP correction (or this box's RTC arriving
        12 h ahead) ages every beat at once; the next beat, ≤60 s later, undoes it. The watch
        must sit through that, which is exactly what the confirmation window is for."""
        self.beat(age=12 * 3600)
        self.assertIsNone(nas._mon_watch(self.st, mono=1000))
        self.beat(age=5)                                     # the monitor beats again
        self.assertIsNone(nas._mon_watch(self.st, mono=1000 + 60))
        self.assertIsNone(nas._mon_watch(self.st, mono=1000 + 10 * nas.MON_CONFIRM))
        self.assertFalse(os.path.exists(nas.EVENTS_FILE), "rang on a clock jump")

    def test_a_beat_from_the_future_is_not_death(self):
        self.beat(age=-3600)
        self.assertIsNone(nas._mon_watch(self.st, mono=1000))
        self.assertIsNone(nas._mon_watch(self.st, mono=1000 + 10 * nas.MON_CONFIRM))

    def test_recovery_re_arms_the_watch(self):
        """A monitor that dies, is restarted and dies again must ring the second time too."""
        self.beat(age=nas.MON_STALE + 60)
        nas._mon_watch(self.st, mono=1000)
        self.assertTrue(nas._mon_watch(self.st, mono=1000 + nas.MON_CONFIRM))
        self.beat(age=5)
        self.assertIsNone(nas._mon_watch(self.st, mono=2000))       # recovered
        nas._MON_LAST.clear()                                       # ...and MON_REPEAT elapses
        self.beat(age=nas.MON_STALE + 60)
        self.assertIsNone(nas._mon_watch(self.st, mono=3000))
        self.assertEqual(nas._mon_watch(self.st, mono=3000 + nas.MON_CONFIRM),
                         "Monitoring has stopped")


class SecondWriterOnTheEventFile(_Base):
    """The recorder writes the panel's event file from a DIFFERENT process."""

    def test_it_does_not_erase_what_the_panel_logged(self):
        self.beat(age=nas.MON_STALE + 60)
        nas._mon_watch(self.st, mono=1000)
        self.assertTrue(nas._mon_watch(self.st, mono=1000 + nas.MON_CONFIRM))

        # meanwhile the panel — the other process, with its own copy in memory — logs a disk
        # failure straight into the file
        with open(nas.EVENTS_FILE) as f:
            ev = json.load(f)
        ev["seq"] += 1
        ev["items"].append({"id": ev["seq"], "t": int(time.time()), "event": "smart",
                            "title": "Disk is failing", "msg": "", "lvl": "crit",
                            "cond": 1, "kind": "disk", "desk": True})
        with open(nas.EVENTS_FILE, "w") as f:
            json.dump(ev, f)

        # and the recorder writes a second time
        self.beat(age=5)
        nas._mon_watch(self.st, mono=2000)
        nas._MON_LAST.clear()
        self.beat(age=nas.MON_STALE + 60)
        nas._mon_watch(self.st, mono=3000)
        self.assertTrue(nas._mon_watch(self.st, mono=3000 + nas.MON_CONFIRM))

        titles = [i.get("title") for i in self.events()]
        self.assertIn("Disk is failing", titles,
                      "the recorder's second write restored its own stale snapshot of the "
                      "event file and deleted what the panel had logged in between")

    def test_the_recorder_error_path_drops_its_copy_too(self):
        """_bb_note_error is the other write from this process and shares the trap."""
        with open(SPEC.origin, encoding="utf-8") as f:
            src = f.read()
        body = re.search(r"\ndef _bb_note_error\(e\):(.*?)\n(?=\ndef )", src, re.S)
        self.assertTrue(body, "_bb_note_error not found")
        self.assertIn("_bb_events_reset", body.group(1))


class Catalogued(_Base):
    """An event the owner cannot see or switch is not wired up."""

    def test_event_is_in_the_monitor_catalog(self):
        ev = nas._def_monitor()["events"].get("monitor_dead")
        self.assertTrue(ev, "monitor_dead missing from the monitor defaults")
        self.assertTrue(ev.get("on"))
        self.assertEqual(ev.get("priority"), 2)

    def test_it_is_a_condition_and_rings_immediately(self):
        self.assertIn("monitor_dead", nas._EVENT_COND,
                      "not a condition — a monitor that stays dead would file one record "
                      "per repeat and flood the feed")
        self.assertTrue(nas._push_allowed("monitor_dead", 2, {"push_mode": "important"}))

    def test_it_has_a_row_in_the_notifications_window(self):
        html = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web", "desktop.html")
        with open(html, encoding="utf-8") as f:
            self.assertIn('k:"monitor_dead"', f.read(),
                          "no row in the notifications catalog — the owner cannot see or "
                          "switch off an alarm that isn't listed")


if __name__ == "__main__":
    unittest.main()
