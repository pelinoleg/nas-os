"""Three places where the box reported good news it had not actually checked.

None of these is a crash. Each one is the box saying "fine" — which is worse, because the
owner acts on it: nothing to update, everything comes back after a reboot, the last power
cut was a clean shutdown.

  * a locked dpkg, broken lists or no network made `apt-get -s upgrade` fail with an empty
    package list, which the panel drew as a green "everything is up to date" — and cached
    that answer for five minutes, so the check that could not run reported the best
    possible outcome;
  * `systemctl is-enabled` answers "not-found" with exit code 0, so a critical unit that
    did not exist on the box at all passed the drill's enabled/disabled check. The drill
    scored a clean 100/100 while nas-stacks.service — the thing that brings the docker
    stacks back after a reboot — was simply absent;
  * the flight recorder called a boot clean whenever the clean-shutdown marker existed,
    and the marker is removed per shutdown, not per boot: one left behind by a manual
    `systemctl stop` weeks earlier whitewashed the power cut that followed it.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import time
import unittest

SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)

UPGRADE_OUT = ("Inst libssl3 [3.0.11-1] (3.0.14-1 Debian-Security:12/stable [amd64])\n"
               "Inst bash [5.2.15-2] (5.2.15-3 Debian:12/stable [amd64])\n"
               "Conf bash (5.2.15-3 Debian:12/stable [amd64])\n")


class AptUpdates(unittest.TestCase):

    def setUp(self):
        self._run = nas._run
        self.calls = []
        nas._APT_CACHE["t"], nas._APT_CACHE["d"] = 0.0, None

    def tearDown(self):
        nas._run = self._run
        nas._APT_CACHE["t"], nas._APT_CACHE["d"] = 0.0, None

    def apt(self, ok, log):
        def fake(cmd, timeout=40, env=None, cwd=None):
            self.calls.append(list(cmd))
            return {"ok": ok, "code": 0 if ok else 100, "log": log}
        nas._run = fake

    def test_a_failed_check_is_not_everything_is_up_to_date(self):
        self.apt(False, "E: Could not get lock /var/lib/dpkg/lock-frontend")
        r = nas.apt_updates()
        self.assertFalse(r["ok"], "a check that could not run reported success")
        self.assertIn("lock", r["log"], "the reason was not passed on to the panel")

    def test_a_failure_is_not_cached(self):
        self.apt(False, "E: Unable to fetch some archives")
        nas.apt_updates()
        nas.apt_updates()
        self.assertEqual(len(self.calls), 2,
                         "the failure was cached: the panel would repeat it for 5 minutes")

    def test_a_success_is_cached(self):
        self.apt(True, UPGRADE_OUT)
        first = nas.apt_updates()
        second = nas.apt_updates()
        self.assertEqual(len(self.calls), 1, "the expensive check ran twice")
        self.assertEqual(first, second)

    def test_packages_are_parsed_and_security_comes_first(self):
        self.apt(True, UPGRADE_OUT)
        r = nas.apt_updates()
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 2, "the Conf line was counted as a package")
        self.assertEqual(r["packages"][0]["name"], "libssl3")
        self.assertTrue(r["packages"][0]["security"])
        self.assertEqual(r["packages"][1]["cur"], "5.2.15-2")
        self.assertEqual(r["packages"][1]["new"], "5.2.15-3")

    def test_nothing_to_upgrade_is_still_a_real_answer(self):
        self.apt(True, "")
        r = nas.apt_updates()
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 0)


class DrillFindsAMissingUnit(unittest.TestCase):
    """The drill's verdict about a unit that does not exist on the box."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._run, self._file = nas._run, nas.DRILL_FILE
        nas.DRILL_FILE = os.path.join(self.d, "resil-drill.json")

    def tearDown(self):
        nas._run, nas.DRILL_FILE = self._run, self._file
        shutil.rmtree(self.d, ignore_errors=True)

    def systemd(self, missing=()):
        """Answer every command the drill asks; `missing` units are not-found."""
        def fake(cmd, timeout=40, env=None, cwd=None):
            out = ""
            if cmd[:2] == ["systemctl", "is-enabled"]:
                out = "not-found" if cmd[2] in missing else "enabled"
            return {"ok": True, "code": 0, "log": out}
        nas._run = fake

    def issues(self, res, area, needle):
        return [i for i in res["issues"] if i["area"] == area and needle in i["title"]]

    def test_a_unit_that_does_not_exist_is_a_blocker(self):
        self.systemd(missing={"nas-stacks.service"})
        res = nas.drill_run()
        found = self.issues(res, "services", "nas-stacks.service")
        self.assertTrue(found, "a unit absent from the box passed the readiness check")
        self.assertEqual(found[0]["sev"], "bad",
                         "nothing will bring the stacks back, and that is not a warning")
        self.assertIn("does not exist", found[0]["detail"])

    def test_a_missing_unit_costs_the_perfect_score(self):
        self.systemd(missing={"nas-stacks.service"})
        self.assertLess(nas.drill_run()["score"], 100,
                        "the drill scored 100 with a critical unit missing")

    def test_an_enabled_unit_is_not_reported(self):
        self.systemd()
        res = nas.drill_run()
        self.assertEqual(self.issues(res, "services", "nas-stacks.service"), [],
                         "an enabled unit was reported as a problem")

    def test_the_verdict_is_written_where_the_panel_reads_it(self):
        self.systemd(missing={"docker.service"})
        nas.drill_run()
        with open(nas.DRILL_FILE) as f:
            self.assertTrue(json.load(f)["issues"])


class BlackBoxRollover(unittest.TestCase):
    """What the recorder concludes about the boot that just ended."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._var = nas.BB_VAR
        nas.BB_VAR = self.d

    def tearDown(self):
        nas.BB_VAR = self._var
        shutil.rmtree(self.d, ignore_errors=True)

    def ring(self, boot_id="old-boot", ago=60):
        p = os.path.join(self.d, "current.json")
        with open(p, "w") as f:
            json.dump({"boot_id": boot_id, "updated": int(time.time()) - ago,
                       "samples": [{"cpu": 3, "mem": 40}]}, f)
        return p

    def marker(self, ago):
        p = os.path.join(self.d, "clean-shutdown")
        open(p, "w").close()
        os.utime(p, (time.time() - ago, time.time() - ago))
        return p

    def boots(self):
        with open(os.path.join(self.d, "boots.jsonl")) as f:
            return [json.loads(l) for l in f if l.strip()]

    def test_a_power_cut_is_archived_as_a_dirty_boot(self):
        self.ring()
        nas._bb_rollover("new-boot")
        self.assertFalse(os.path.exists(os.path.join(self.d, "current.json")),
                         "the previous boot's ring was left in place")
        flights = [f for f in os.listdir(self.d) if f.startswith("flight-")]
        self.assertEqual(len(flights), 1, "the pre-crash flight was not archived")
        rec = self.boots()
        self.assertEqual(len(rec), 1)
        self.assertFalse(rec[0]["clean"], "a power cut was recorded as a clean shutdown")

    def test_a_marker_from_this_shutdown_means_clean(self):
        self.ring(ago=60)
        self.marker(ago=30)
        nas._bb_rollover("new-boot")
        self.assertTrue(self.boots()[0]["clean"])

    def test_an_old_marker_cannot_whitewash_a_later_crash(self):
        self.ring(ago=60)                 # the box died a minute ago
        self.marker(ago=86400)            # a `systemctl stop` a day earlier
        nas._bb_rollover("new-boot")
        self.assertFalse(self.boots()[0]["clean"],
                         "a stale marker turned a power cut into a clean shutdown")

    def test_the_marker_never_survives_into_the_next_boot(self):
        self.ring()
        self.marker(ago=10)
        nas._bb_rollover("new-boot")
        self.assertFalse(os.path.exists(os.path.join(self.d, "clean-shutdown")),
                         "the marker stayed behind to whitewash the next crash")

    def test_the_current_boots_own_ring_is_left_alone(self):
        p = self.ring(boot_id="same-boot")
        nas._bb_rollover("same-boot")
        self.assertTrue(os.path.exists(p), "the running boot's ring was archived from under it")
        self.assertEqual([f for f in os.listdir(self.d) if f.startswith("flight-")], [])

    def test_only_the_last_flights_are_kept(self):
        for i in range(nas.BB_FLIGHTS + 3):
            open(os.path.join(self.d, "flight-2026010%d-00000%d.json" % (i // 9, i % 9)), "w").close()
        self.ring()
        nas._bb_rollover("new-boot")
        left = [f for f in os.listdir(self.d) if f.startswith("flight-")]
        self.assertEqual(len(left), nas.BB_FLIGHTS,
                         "the archive grows without limit (or was trimmed too far)")

    def test_a_missing_ring_is_not_an_error(self):
        nas._bb_rollover("new-boot")      # first ever start: nothing to roll over
        self.assertEqual(os.listdir(self.d), [])


class BlackBoxActuallyRecords(unittest.TestCase):
    """The recorder's whole purpose is the copy on DISK: /run is tmpfs and dies with the
    box, so a recorder writing only there keeps nothing about the power cut it is meant to
    explain. That is what it did — a NameError on the disk write, swallowed by the loop's
    bare except, for a day: service running, /run ring full, /var/lib empty."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._var, self._run_dir = nas.BB_VAR, nas.BB_RUN
        self._sample, self._dmesg = nas._bb_sample, nas._bb_dmesg_tail
        nas.BB_VAR = os.path.join(self.d, "var")
        nas.BB_RUN = os.path.join(self.d, "run")
        os.makedirs(nas.BB_VAR)
        os.makedirs(nas.BB_RUN)
        nas._bb_sample = lambda prev: ({"t": 1, "cpu": 5}, prev)
        nas._bb_dmesg_tail = lambda n=25: ["dmesg line"]
        self.st = {"boot_id": "b1", "ring": [], "dmesg": [], "cpu": None, "n": 0}

    def tearDown(self):
        nas.BB_VAR, nas.BB_RUN = self._var, self._run_dir
        nas._bb_sample, nas._bb_dmesg_tail = self._sample, self._dmesg
        shutil.rmtree(self.d, ignore_errors=True)

    def disk(self):
        return os.path.join(nas.BB_VAR, "current.json")

    def tmpfs(self):
        return os.path.join(nas.BB_RUN, "current.json")

    def test_the_ring_reaches_the_disk(self):
        for _ in range(3):
            nas._bb_tick(self.st)
        self.assertTrue(os.path.exists(self.disk()),
                        "the flight recorder is not recording anywhere that survives a power cut")
        with open(self.disk()) as f:
            self.assertEqual(len(json.load(f)["samples"]), 3)

    def test_every_tick_reaches_the_tmpfs_ring(self):
        nas._bb_tick(self.st)
        self.assertTrue(os.path.exists(self.tmpfs()), "the live ring stopped being written")

    def test_the_disk_copy_is_not_written_on_every_sample(self):
        # the SD-wear reason the copy is every third sample and not every one
        nas._bb_tick(self.st)
        self.assertFalse(os.path.exists(self.disk()))

    def test_the_ring_does_not_grow_forever(self):
        for _ in range(nas.BB_KEEP + 5):
            nas._bb_tick(self.st)
        self.assertEqual(len(self.st["ring"]), nas.BB_KEEP)

    def test_the_boot_id_travels_with_the_payload(self):
        # without it the next boot cannot tell whose ring it found, and rolls over nothing
        for _ in range(3):
            nas._bb_tick(self.st)
        with open(self.disk()) as f:
            self.assertEqual(json.load(f)["boot_id"], "b1")

    def test_a_failure_in_the_loop_is_reported_once(self):
        seen = []
        _ev, _seen = nas.log_event, nas._BB_ERR["seen"]
        nas.log_event = lambda *a, **k: seen.append(a)
        nas._BB_ERR["seen"] = False
        try:
            nas._bb_note_error(NameError("curf"))
            nas._bb_note_error(NameError("curf"))
        finally:
            nas.log_event, nas._BB_ERR["seen"] = _ev, _seen
        self.assertEqual(len(seen), 1,
                         "a recorder that is quietly not recording is worse than none")


if __name__ == "__main__":
    unittest.main()
