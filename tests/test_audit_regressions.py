"""Regression tests for the defects found in the 2026-08-14 audit.

Each case here is something that was already shipped and wrong, so each one is written
to fail against the code as it stood that morning:

  * a record from the FUTURE in the availability journal painted a box that had been up
    the whole time as eight hours of outage — and the first fix for it only deferred the
    problem until wall time caught up with the bogus stamp;
  * a tar member named ../../etc/... was written exactly there, as root, because
    shutil.unpack_archive inherits tarfile's fully_trusted default;
  * eight wrong passwords a minute from any device on the LAN locked the OWNER out too,
    because the brute-force counter was one number for the whole process;
  * the system disk was named "SSD" even when nothing could be read about it.
"""
import importlib.util
import io
import os
import tarfile
import tempfile
import time
import unittest


SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


def _journal(lines):
    """Write an avail.log and return its path."""
    fd, p = tempfile.mkstemp()
    with os.fdopen(fd, "w") as f:
        f.write("".join("%d %s\n" % l for l in lines))
    return p


class AvailFutureRecord(unittest.TestCase):
    """The journal is read as 'state until the next line', so a stamp beyond `now`
    stretches the PREVIOUS state to the present."""

    def setUp(self):
        self.now = int(time.time())
        self.paths = []

    def tearDown(self):
        for p in self.paths:
            os.unlink(p)

    def bars(self, lines, hours=24, slots=24):
        p = _journal(lines)
        self.paths.append(p)
        return nas.avail_bars(hours, slots, p)

    def test_future_record_does_not_become_an_outage(self):
        # the real 2026-08-13 journal: up, the crash, then "up" stamped 7.5 h ahead
        d = self.bars([(self.now - 17000, "up"), (self.now - 13200, "off"),
                       (self.now + 26900, "up")])
        self.assertNotIn(0, d["bars"][-4:],
                         "an unverifiable stretch must not be painted as an outage")
        self.assertEqual(d["bars"][-1], -1, "the tail is unknown, not measured")

    def test_small_clock_nudge_is_not_a_future_record(self):
        # NTP steps of a second or two, and the gap between the guard writing a line and
        # us reading it, are normal — they must not open a hole in the timeline
        d = self.bars([(self.now - 7200, "up"), (self.now + 20, "up")])
        self.assertEqual(d["pct"], 100.0)
        self.assertEqual(d["bars"][-1], 2)

    def test_good_records_after_a_future_one_close_the_gap(self):
        d = self.bars([(self.now - 17000, "up"), (self.now - 13200, "off"),
                       (self.now + 26900, "up"), (self.now - 3600, "off"),
                       (self.now - 1800, "up")])
        self.assertIn(0, d["bars"], "the later real outage must still be visible")
        self.assertIsNotNone(d["pct"])

    def test_backdated_record_after_a_future_one_stays_unknown(self):
        # a record that does not advance past the last trustworthy stamp cannot say when
        # the unverifiable stretch ended — publishing its state as fact produced a
        # confident two-hour outage over a period nothing is known about
        d = self.bars([(self.now - 7200, "up"), (self.now + 43200, "off"),
                       (self.now - 7200, "off")])
        self.assertIsNone(d["pct"], "nothing is known here, so there is no uptime figure")
        self.assertEqual(d["known_s"], 0)

    def test_unknown_time_is_never_counted_as_measured(self):
        d = self.bars([(self.now - 3600, "up"), (self.now + 99999, "up")])
        self.assertEqual(d["events"], [], "an unknown stretch is not a tooltip event")


class SafeUntar(unittest.TestCase):
    """The file manager extracts as root; an archive is input, even one of ours."""

    def _archive(self, members):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.tar")
        with tarfile.open(p, "w") as tf:
            for name in members:
                data = b"x"
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        return d, p

    def test_parent_traversal_is_refused(self):
        work, arc = self._archive(["../escaped.txt", "ok.txt"])
        dest = os.path.join(work, "dest")
        os.makedirs(dest)
        with self.assertRaises(Exception):
            nas._safe_untar(arc, dest)
        self.assertFalse(os.path.exists(os.path.join(work, "escaped.txt")),
                         "a member must never land outside the destination")

    def test_absolute_path_cannot_escape(self):
        # tar itself strips the leading slash when WRITING, so this member arrives as a
        # relative path and simply lands inside dest. The invariant under test is
        # containment, not the exception: an archive that names /tmp/... must not be
        # able to write /tmp/...
        work, arc = self._archive(["/tmp/nas-os-test-escape.txt"])
        dest = os.path.join(work, "dest")
        os.makedirs(dest)
        try:
            nas._safe_untar(arc, dest)
        except Exception:
            pass
        self.assertFalse(os.path.exists("/tmp/nas-os-test-escape.txt"),
                         "nothing may be written outside the destination")

    def test_ordinary_archive_still_extracts(self):
        work, arc = self._archive(["ok.txt", "sub/also-ok.txt"])
        dest = os.path.join(work, "dest")
        os.makedirs(dest)
        nas._safe_untar(arc, dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, "ok.txt")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "sub", "also-ok.txt")))


class LoginThrottlePerAddress(unittest.TestCase):
    """One counter for the whole process was a free permanent lockout: the gate is
    checked before the password is, so someone else's failures blocked the owner."""

    def setUp(self):
        nas._login_fail.clear()

    def tearDown(self):
        nas._login_fail.clear()

    def test_one_address_does_not_block_another(self):
        for _ in range(8):
            nas._login_miss("192.168.1.99")
        self.assertTrue(nas._login_gate("192.168.1.99"))
        self.assertFalse(nas._login_gate("192.168.1.230"),
                         "the owner's address must be unaffected by someone else's guessing")

    def test_window_expires(self):
        for _ in range(8):
            nas._login_miss("10.0.0.1")
        self.assertTrue(nas._login_gate("10.0.0.1"))
        nas._login_fail["10.0.0.1"]["t"] = time.time() - 61
        self.assertFalse(nas._login_gate("10.0.0.1"), "the window is a minute, not forever")

    def test_table_is_bounded(self):
        for i in range(nas._LOGIN_FAIL_MAX + 40):
            nas._login_miss("10.1.%d.%d" % (i // 256, i % 256))
        self.assertLessEqual(len(nas._login_fail), nas._LOGIN_FAIL_MAX,
                             "a scan from many addresses must not grow the table forever")


class SystemDiskKind(unittest.TestCase):
    """A status line that misnames the disk teaches its reader to distrust the rest."""

    def setUp(self):
        nas._SYSDISK_KIND.update(t=0.0, v=None)

    def tearDown(self):
        nas._SYSDISK_KIND.update(t=0.0, v=None)

    def test_unknown_device_is_not_guessed_to_be_an_ssd(self):
        orig = nas._sys_disk
        nas._sys_disk = lambda: "?"
        try:
            self.assertEqual(nas._sysdisk_kind(), "system disk")
        finally:
            nas._sys_disk = orig

    def test_nvme_is_named(self):
        orig = nas._sys_disk
        nas._sys_disk = lambda: "nvme1n1"
        try:
            self.assertEqual(nas._sysdisk_kind(), "NVMe SSD")
        finally:
            nas._sys_disk = orig


class DiskTemperatureVerdict(unittest.TestCase):
    """One formula, one answer: the tile, the per-disk list and the screen light used to
    parse the threshold separately."""

    def test_danger_is_the_threshold_plus_ten(self):
        self.assertEqual(nas._disktemp_state(59.9, 60), "ok")
        self.assertEqual(nas._disktemp_state(60.0, 60), "warn")
        self.assertEqual(nas._disktemp_state(69.9, 60), "warn")
        self.assertEqual(nas._disktemp_state(70.0, 60), "danger")

    def test_tile_state_matches_the_hottest_disk_in_the_list(self):
        temps = [("cold", 41.0), ("hot", 71.0)]
        w = 60
        per_disk = [nas._disktemp_state(c, w) for _, c in temps]
        tile = nas._disktemp_state(max(c for _, c in temps), w)
        self.assertEqual(tile, "danger")
        self.assertEqual(per_disk, ["ok", "danger"],
                         "a cold disk must not inherit the hot one's colour")


class SecondAuditFixes(unittest.TestCase):
    """The second audit found defects in the FIRST audit's fixes. These pin the repairs.

    Every case fails against the code as it stood after the first round.
    """

    def setUp(self):
        nas._login_fail.clear()
        nas._login_all.update(n=0, t=0.0)

    def tearDown(self):
        nas._login_fail.clear()
        nas._login_all.update(n=0, t=0.0)

    def test_brute_force_alarm_survives_address_rotation(self):
        # a machine with SLAAC has a /64 of addresses; per-address counters alone turned
        # 600 guesses into 600 counters of one and the alarm never fired
        total = 0
        for i in range(600):
            total = nas._login_miss("2001:db8::%x" % i)
        self.assertGreaterEqual(total, 5, "the alarm counts every failure, whatever the address")

    def test_flooding_cannot_free_a_blocked_address(self):
        for _ in range(8):
            nas._login_miss("192.168.1.66")
        self.assertTrue(nas._login_gate("192.168.1.66"))
        for i in range(400):
            nas._login_miss("10.9.%d.%d" % (i // 256, i % 256))
        self.assertTrue(nas._login_gate("192.168.1.66"),
                        "evicting a blocked entry is how an attacker buys their way back in")
        self.assertLessEqual(len(nas._login_fail), nas._LOGIN_FAIL_MAX)

    def test_owner_is_not_locked_out_by_someone_else(self):
        for _ in range(20):
            nas._login_miss("192.168.1.66")
        self.assertFalse(nas._login_gate("192.168.1.230"))

    def test_concurrent_saves_never_publish_broken_json(self):
        # One process, many threads: a PID-only temp name meant they all opened the SAME
        # temp file, so one truncated what another was still writing and os.replace
        # published the mixture. Asserted on the OUTCOME, not on the temp name — thread
        # ids are reused once a thread exits, so comparing them proves nothing.
        import json as _json
        import threading as _t
        work = tempfile.mkdtemp()
        target = os.path.join(work, "state.json")
        payload = {"k": ["v"] * 4000}          # big enough that a write is interruptible
        start = _t.Barrier(8)
        errors = []

        def writer():
            start.wait()
            for _ in range(12):
                try:
                    nas._json_save(target, payload)
                except OSError as e:
                    errors.append(e)

        ts = [_t.Thread(target=writer) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]

        with open(target) as f:
            _json.load(f)                      # raises if the file was published torn
        self.assertEqual(errors, [], "a losing thread must not fail on a vanished temp")
        self.assertEqual([f for f in os.listdir(work) if ".tmp." in f], [],
                         "no temp file may be left behind")

    def test_cancelled_copy_leaves_the_existing_target_intact(self):
        import threading as _t
        work = tempfile.mkdtemp()
        src = os.path.join(work, "src.bin")
        dst = os.path.join(work, "dst.bin")
        with open(src, "wb") as f:
            f.write(b"N" * (4 * 1024 * 1024))
        with open(dst, "w") as f:
            f.write("OLD VERSION")
        job = {"cancel": _t.Event(), "done_bytes": 0}
        job["cancel"].set()
        with self.assertRaises(Exception):
            nas._fsjob_copy_file(src, dst, job)
        with open(dst) as f:
            self.assertEqual(f.read(), "OLD VERSION",
                             "Cancel is offered as a safe way back — it must be one")
        self.assertEqual([f for f in os.listdir(work) if "nasos-part" in f], [],
                         "no partial file may be left behind")

    def test_same_named_items_from_different_folders_are_refused(self):
        work = tempfile.mkdtemp()
        a, b, d = (os.path.join(work, x) for x in ("a", "b", "d"))
        for x in (a, b, d):
            os.makedirs(x)
        for x in (a, b):
            with open(os.path.join(x, "report.txt"), "w") as f:
                f.write(x)
        r = nas.fs_job_start("move", [os.path.join(a, "report.txt"),
                                      os.path.join(b, "report.txt")], d)
        self.assertFalse(r.get("ok"), "they would silently overwrite each other")
        self.assertTrue(os.path.exists(os.path.join(a, "report.txt")))
        self.assertTrue(os.path.exists(os.path.join(b, "report.txt")))

    def test_job_destination_is_guarded(self):
        work = tempfile.mkdtemp()
        src = os.path.join(work, "x.txt")
        with open(src, "w") as f:
            f.write("x")
        r = nas.fs_job_start("copy", [src], "/etc")
        self.assertFalse(r.get("ok"), "the panel runs as root; /etc is not a destination")


if __name__ == "__main__":
    unittest.main()
