"""Regression tests for the hardware assumptions this panel is allowed to make.

They were written when the project still ran on a Raspberry Pi and had to be taught
that a PC is not one. Raspberry Pi support is gone (2026-08-13, x86 only), but the
three things pinned here are exactly the ones that broke during that move, and each
is a claim the panel makes about hardware it did not verify:

  * the CPU temperature must come from a sensor asked for BY NAME — the thermal_zone0
    fallback usually reads the chassis, not the CPU;
  * a skipped package must carry its LEVEL and its CONSEQUENCE all the way to the
    panel, and records written by an older install must still parse;
  * the memory cgroup must be judged by the kernel, never by a bootloader file.
"""
import importlib.util
import os
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


class CpuTempSensorTests(unittest.TestCase):
    """thermal_zone0 is the SoC on a Pi and the ACPI chassis zone on a PC."""

    def _hwmon(self, td, entries):
        for name, value in entries.items():
            d = os.path.join(td, name)
            os.makedirs(d)
            with open(os.path.join(d, "name"), "w") as f:
                f.write(value[0] + "\n")
            if value[1] is not None:
                with open(os.path.join(d, "temp1_input"), "w") as f:
                    f.write(str(value[1]) + "\n")
        return td

    def _resolve(self, td, zone0=True):
        """Point every /sys/class/hwmon lookup at a fake tree.

        _read must be redirected too, not just listdir/exists — the first cut of this
        test patched only the latter two, so the name lookup still hit the real box,
        found its genuine cpu_thermal and "passed" for the wrong reason. The zone0
        flag exists for the same reason, caught by CI's first-ever run: the fallback
        check went to the REAL filesystem, so the test passed on a Pi (which has
        thermal_zone0) and failed on a GitHub runner (which does not). A test that
        peeks at its host is not a test of the code."""
        nas._TEMP_PATH["v"] = None          # the path is cached per boot
        real_listdir, real_exists = os.listdir, os.path.exists

        def fake(p):
            return p.replace("/sys/class/hwmon", td, 1) \
                if p.startswith("/sys/class/hwmon") else p

        def listdir(p):
            return real_listdir(fake(p))

        def exists(p):
            if p == "/sys/class/thermal/thermal_zone0/temp":
                return zone0
            return real_exists(fake(p))

        def read(p, default=""):
            try:
                with open(fake(p)) as f:
                    return f.read()
            except OSError:
                return default

        with mock.patch.object(nas.os, "listdir", listdir), \
                mock.patch.object(nas.os.path, "exists", exists), \
                mock.patch.object(nas, "_read", read):
            return nas._cpu_temp_path()

    def test_named_cpu_sensor_wins_over_an_unnamed_one(self):
        with tempfile.TemporaryDirectory() as td:
            # hwmon0 first alphabetically, and NOT a CPU sensor: a numeric scan would
            # take it. This is exactly the x86 layout (nvme/acpitz before coretemp).
            self._hwmon(td, {"hwmon0": ("acpitz", 27000),
                             "hwmon1": ("coretemp", 46000)})
            got = self._resolve(td)
        self.assertTrue(got.endswith("hwmon1/temp1_input"), got)

    def test_sensor_without_a_reading_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            self._hwmon(td, {"hwmon0": ("k10temp", None),
                             "hwmon1": ("k10temp", 51000)})
            got = self._resolve(td)
        self.assertTrue(got.endswith("hwmon1/temp1_input"), got)

    def test_falls_back_to_thermal_zone0_when_no_sensor_is_named(self):
        with tempfile.TemporaryDirectory() as td:
            self._hwmon(td, {"hwmon0": ("nvme", 40000)})
            got = self._resolve(td, zone0=True)
        self.assertEqual(got, "/sys/class/thermal/thermal_zone0/temp")

    def test_no_sensor_at_all_yields_no_path(self):
        # a VM with neither a named hwmon nor thermal_zone0 — temp_c must answer
        # None (not measured), not crash and not invent a reading
        with tempfile.TemporaryDirectory() as td:
            self._hwmon(td, {"hwmon0": ("nvme", 40000)})
            got = self._resolve(td, zone0=False)
        self.assertEqual(got, "")

    def tearDown(self):
        nas._TEMP_PATH["v"] = None


class SkippedPackageTests(unittest.TestCase):
    """A count answers nothing — the level and the consequence must survive."""

    def test_tsv_record_keeps_level_and_consequence(self):
        self.assertEqual(
            nas._parse_skipped_line("req\tmergerfs\tNAS stack\tno pool"),
            ("req", "mergerfs", "no pool"))

    def test_legacy_record_still_parses(self):
        # boxes installed before the TSV format wrote "stage: pkg"
        self.assertEqual(nas._parse_skipped_line("utilities: ncdu"),
                         ("opt", "ncdu", ""))

    def test_pi_only_skip_is_tagged_in_both_formats(self):
        self.assertEqual(nas._parse_skipped_line("Pi packages: rpi-eeprom")[0], "pi")
        self.assertEqual(
            nas._parse_skipped_line("pi\traspi-config\tPi packages\t")[0], "pi")

    def test_pi_skips_never_reach_the_panel_and_installed_ones_drop_out(self):
        rows = ["req\tstill-missing\tNAS stack\tno parity",
                "pi\traspi-config\tPi packages\t",
                "opt\talready-here\tutilities\t"]
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "skipped-packages")
            with open(path, "w") as f:
                f.write("\n".join(rows) + "\n")
            # dpkg says the "opt" one is present now: the notice must retire itself
            def dpkg(cmd, **kw):
                ok = cmd[2] == "already-here"
                return mock.Mock(returncode=0 if ok else 1)
            with mock.patch.object(nas, "SKIPPED_PKGS_FILE", path), \
                    mock.patch.object(nas.subprocess, "run", dpkg):
                nas._MISS_PKGS.update(t=0.0)
                out = nas.missing_base_packages()
        self.assertEqual([m["pkg"] for m in out], ["still-missing"])
        self.assertEqual(out[0]["level"], "req")
        self.assertEqual(out[0]["why"], "no parity")

    def tearDown(self):
        nas._MISS_PKGS.update(t=0.0, v=[])


class MemoryCgroupTests(unittest.TestCase):
    """cmdline.txt does not exist off a Pi — ask the kernel instead."""

    def _read(self, controllers=None, procgroups=None):
        def rd(path, default=""):
            if path == "/sys/fs/cgroup/cgroup.controllers":
                return controllers if controllers is not None else default
            if path == "/proc/cgroups":
                return procgroups if procgroups is not None else default
            return default
        return rd

    def test_cgroup_v2_controller_list_is_enough(self):
        with mock.patch.object(nas, "_read", self._read(controllers="cpuset cpu memory pids")):
            self.assertTrue(nas._memory_cgroup_on())

    def test_cgroup_v1_enabled_column_is_read(self):
        table = ("#subsys_name\thierarchy\tnum_cgroups\tenabled\n"
                 "cpuset\t0\t73\t1\nmemory\t0\t73\t1\n")
        with mock.patch.object(nas, "_read", self._read(procgroups=table)):
            self.assertTrue(nas._memory_cgroup_on())

    def test_disabled_memory_controller_is_reported_off(self):
        # the real state of the Pi 4 this was written on: memory is absent from both
        table = ("#subsys_name\thierarchy\tnum_cgroups\tenabled\n"
                 "cpuset\t0\t73\t1\npids\t0\t73\t1\n")
        with mock.patch.object(nas, "_read",
                               self._read(controllers="cpuset cpu io pids",
                                          procgroups=table)):
            self.assertFalse(nas._memory_cgroup_on())


if __name__ == "__main__":
    unittest.main()
