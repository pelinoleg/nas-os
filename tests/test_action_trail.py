"""What the panel writes into its own action history.

The history is the only record of what was done to this box. It logged "disk renamed" and
"trash emptied" while the four most consequential things in the panel left no trace at all:

  * a shell on the box — running in a group with docker and sudo — was recorded nowhere.
    The only record was ~/.bash_history, which anyone holding that shell can erase;
  * deleting a systemd unit;
  * killing a process;
  * every system setting: hostname, timezone, journald size, ufw, fail2ban,
    unattended-upgrades.

_act_title decides both what is written and whether anything is written at all — returning
None means the action passes unrecorded — so it is worth a test of its own, per action.
"""
import importlib.util
import os
import unittest

SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


class ActionsThatMustLeaveATrace(unittest.TestCase):

    def title(self, path, body=None):
        return nas._act_title(path, body or {})

    def test_opening_a_shell_is_recorded(self):
        t = self.title("/ws/term")
        self.assertTrue(t, "the most powerful thing in the panel is still unlogged")
        self.assertIn("Terminal", t)

    def test_a_shell_inside_a_container_names_the_container(self):
        self.assertIn("immich_server", self.title("/ws/term", {"exec": "immich_server"}))

    def test_deleting_a_unit_names_the_unit(self):
        t = self.title("/api/unit/delete", {"unit": "nas-stacks.service"})
        self.assertTrue(t)
        self.assertIn("nas-stacks.service", t)

    def test_killing_a_process_names_it(self):
        t = self.title("/api/process/kill", {"pid": 1234, "name": "smbd"})
        self.assertTrue(t)
        self.assertIn("1234", t)
        self.assertIn("smbd", t)

    def test_a_system_setting_records_the_new_value(self):
        t = self.title("/api/sysconf", {"key": "hostname", "value": "mininas"})
        self.assertTrue(t, "hostname, timezone, ufw and fail2ban went unrecorded")
        self.assertIn("hostname", t)
        self.assertIn("mininas", t)

    def test_a_setting_sent_under_the_other_field_name_is_still_recorded(self):
        # the client sends "val" for some settings and "value" for others
        self.assertIn("Europe/Madrid",
                      self.title("/api/sysconf", {"key": "timezone", "val": "Europe/Madrid"}))

    def test_power_actions_are_recorded(self):
        self.assertIn("Reboot", self.title("/api/power", {"action": "reboot"}))
        self.assertIn("Shutdown", self.title("/api/power", {"action": "poweroff"}))

    def test_a_display_action_is_recorded_because_no_session_is_behind_it(self):
        # the glance token authorises these without a login: the log line is the only trace
        t = self.title("/api/glance/act", {"a": "backup", "name": "photos"})
        self.assertTrue(t)
        self.assertIn("backup", t)
        self.assertIn("photos", t)

    def test_unmounting_says_which_way_it_went(self):
        self.assertIn("Unmounted", self.title("/api/disk/mount",
                                              {"target": "/mnt/disk1", "unmount": True}))
        self.assertIn("Mounted", self.title("/api/disk/mount", {"target": "/mnt/disk1"}))


class ActionsThatAreDeliberatelyQuiet(unittest.TestCase):

    def test_an_unknown_path_is_not_logged(self):
        self.assertIsNone(nas._act_title("/api/stats", {}))
        self.assertIsNone(nas._act_title("/api/disks", {}))

    def test_a_dry_run_format_is_not_logged(self):
        # the panel asks "what would this do" before every format
        self.assertIsNone(nas._act_title("/api/disk/format", {"dev": "/dev/sdb", "dry": True}))
        self.assertTrue(nas._act_title("/api/disk/format", {"dev": "/dev/sdb"}),
                        "a real format went unrecorded")

    def test_an_unknown_power_action_is_not_logged(self):
        self.assertIsNone(nas._act_title("/api/power", {"action": "hibernate"}))


class TitlesSurviveAMissingBody(unittest.TestCase):
    """log_action passes whatever the client sent; a title that raises would take the
    request down with it — and the action would happen anyway, just unlogged."""

    def test_no_action_raises_on_an_empty_body(self):
        paths = ["/ws/term", "/api/unit/delete", "/api/unit/save", "/api/process/kill",
                 "/api/sysconf", "/api/power", "/api/glance/act", "/api/systemd",
                 "/api/stack/action", "/api/container/action", "/api/docker/prune",
                 "/api/disk/format", "/api/disk/eject", "/api/disk/mount",
                 "/api/disk/mount-dev", "/api/disk/label", "/api/disk/spindown",
                 "/api/disk/smart-test"]
        for p in paths:
            nas._act_title(p, {})
            nas._act_title(p, {"unit": None, "name": None, "pid": None, "value": None})


if __name__ == "__main__":
    unittest.main()
