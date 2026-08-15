"""The disk-wear alarm on a box whose disks are all NVMe.

Until 2026-08-14 the alarm read three ATA attributes and nothing else. NVMe drives have
none of them: they report Percentage Used, Available Spare, Media Errors and a critical
warning flag instead — which smartctl was printing all along, in the very same JSON the
scan already parsed. So on an all-NVMe box the alarm was not "quiet", it was incapable of
firing, and the panel's own SMART page kept showing the numbers it ignored.

An alarm that cannot fire looks exactly like a healthy box, which is why this one is
tested from both ends: the scan must carry the NVMe numbers out of smartctl's JSON, and
the conditions must turn them into a reason. The thresholds are the drive's own — the
firmware's declared spare minimum, any media error, any critical flag — plus 80 % of rated
write life, which is a plan-a-replacement warning rather than a verdict.

The false-positive side is tested too: a healthy NVMe must produce silence, or the owner
learns to ignore the one alarm that matters.
"""
import importlib.util
import os
import unittest

SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


# a real-shaped smartctl -j payload for an NVMe drive
def nvme_json(**log):
    base = {"percentage_used": 3, "available_spare": 100, "available_spare_threshold": 10,
            "media_errors": 0, "critical_warning": 0, "temperature": 41,
            "power_on_hours": 900}
    base.update(log)
    return {"smart_status": {"passed": True},
            "nvme_smart_health_information_log": base,
            "temperature": {"current": base["temperature"]},
            "power_on_time": {"hours": base["power_on_hours"]}}


class ScanCarriesTheNvmeNumbers(unittest.TestCase):
    """_smart_scan is the only reader of smartctl in the monitor loop: a field it drops
    is a field no alarm can ever see."""

    def setUp(self):
        self._devs, self._json = nas._phys_devs, nas._smartctl_json
        nas._phys_devs = lambda: ["/dev/nvme0n1"]

    def tearDown(self):
        nas._phys_devs, nas._smartctl_json = self._devs, self._json

    def scan(self, j):
        nas._smartctl_json = lambda extra, dev, timeout=12: j
        return nas._smart_scan()["/dev/nvme0n1"]

    def test_wear_spare_errors_and_flag_survive_the_scan(self):
        d = self.scan(nvme_json(percentage_used=84, available_spare=7,
                                available_spare_threshold=10, media_errors=2,
                                critical_warning=4))
        self.assertEqual(d["wear"], 84)
        self.assertEqual(d["spare"], 7)
        self.assertEqual(d["spare_min"], 10, "the firmware's own minimum was dropped")
        self.assertEqual(d["media_errors"], 2)
        self.assertEqual(d["critical_warning"], 4)

    def test_temperature_still_comes_through(self):
        self.assertEqual(self.scan(nvme_json(temperature=41))["temp"], 41)

    def test_an_ata_disk_is_unaffected(self):
        j = {"smart_status": {"passed": True},
             "ata_smart_attributes": {"table": [
                 {"name": "Reallocated_Sector_Ct", "raw": {"value": 8}},
                 {"name": "Current_Pending_Sector", "raw": {"value": 0}},
                 {"name": "Temperature_Celsius", "raw": {"value": 38}}]}}
        d = self.scan(j)
        self.assertEqual((d["realloc"], d["pending"], d["temp"]), (8, 0, 38))
        self.assertIsNone(d["wear"], "an ATA disk grew an NVMe wear number out of nowhere")


class WearConditions(unittest.TestCase):

    def test_a_healthy_nvme_is_silent(self):
        self.assertEqual(nas._wear_alarms({
            "passed": True, "wear": 3, "spare": 100, "spare_min": 10,
            "media_errors": 0, "critical_warning": 0}), [])

    def test_spare_at_the_firmware_minimum_fires(self):
        bad = nas._wear_alarms({"spare": 10, "spare_min": 10})
        self.assertTrue(bad, "the spare-block reserve reached the firmware's own floor")
        self.assertIn("10%", bad[0])

    def test_spare_above_the_minimum_is_silent(self):
        self.assertEqual(nas._wear_alarms({"spare": 11, "spare_min": 10}), [])

    def test_a_drive_declaring_no_minimum_does_not_fire(self):
        # spare_min 0 against spare 0 is "unknown vs unknown", not a dying disk
        self.assertEqual(nas._wear_alarms({"spare": 0, "spare_min": 0}), [])

    def test_a_single_media_error_fires(self):
        self.assertTrue(nas._wear_alarms({"media_errors": 1}))
        self.assertEqual(nas._wear_alarms({"media_errors": 0}), [])

    def test_any_critical_warning_flag_fires(self):
        bad = nas._wear_alarms({"critical_warning": 4})
        self.assertTrue(bad)
        self.assertIn("0x4", bad[0], "the flag was reported without its value")
        self.assertEqual(nas._wear_alarms({"critical_warning": 0}), [])

    def test_eighty_percent_of_rated_life_fires_and_seventy_nine_does_not(self):
        self.assertTrue(nas._wear_alarms({"wear": 80}))
        self.assertEqual(nas._wear_alarms({"wear": 79}), [])

    def test_ata_sectors_still_fire_at_the_configured_threshold(self):
        self.assertTrue(nas._wear_alarms({"realloc": 1}))
        self.assertEqual(nas._wear_alarms({"realloc": 1}, sector_thr=5), [])
        self.assertTrue(nas._wear_alarms({"pending": 5}, sector_thr=5))

    def test_missing_and_non_numeric_fields_are_not_an_alarm(self):
        # a disk in standby, an old smartctl, a firmware answering "-" — none of these
        # is a failing disk, and a crash here kills the whole monitor tick
        for d in ({}, {"wear": None, "spare": None, "media_errors": None},
                  {"wear": "n/a", "spare": "-", "critical_warning": "0x0"},
                  {"spare": 5, "spare_min": None}):
            self.assertEqual(nas._wear_alarms(d), [], "%r produced an alarm" % (d,))

    def test_every_reason_is_reported_not_just_the_first(self):
        bad = nas._wear_alarms({"realloc": 2, "wear": 90, "media_errors": 3,
                                "critical_warning": 1, "spare": 5, "spare_min": 10})
        self.assertEqual(len(bad), 5, "the message named only some of the reasons: %r" % bad)


if __name__ == "__main__":
    unittest.main()
