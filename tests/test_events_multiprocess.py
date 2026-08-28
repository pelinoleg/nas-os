"""The event journal has more than one writer, and used to lose entries because of it.

The panel, the Kopia snapshot driver (`nas-web.py kopia-snap`, its own transient unit), the
black-box recorder and the settings-drill unit are SEPARATE processes. `_events` was a
per-process cache read once and never re-read, `_events_save` rewrote the whole file, and
`_events_lock` is a thread lock that guards none of that — so whoever saved last silently
overwrote everybody else's entries.

Found on 2026-08-27: three Kopia runs finished with errors and `notify_err` on, and the
journal held ZERO events from any of them, while the panel's own «scheduled run was missed»
event (same process as the journal writer) was sitting right there. Silence from a backup is
indistinguishable from success, which makes this the worst place in the box to drop a record.
"""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


class EventsCacheTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        patches = [mock.patch.object(nas, "NAS_CONFIG", self.td.name),
                   mock.patch.object(nas, "EVENTS_FILE",
                                     os.path.join(self.td.name, "events.json")),
                   mock.patch.object(nas, "_events", None),
                   mock.patch.object(nas, "_events_stat", None),
                   mock.patch.object(nas, "load_monitor", return_value={"events": {}})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _disk(self):
        with open(nas.EVENTS_FILE) as f:
            return json.load(f)

    def _titles(self):
        return [i["title"] for i in self._disk()["items"]]

    def test_an_outside_write_is_picked_up_not_overwritten(self):
        """The core of the bug: another process appends, we append, both survive."""
        nas.log_event("action", "from the panel")
        outside = self._disk()                       # what the other process starts from
        outside["seq"] += 1
        outside["items"].append({"id": outside["seq"], "t": int(time.time()), "event": "kp_err",
                                 "title": "Kopia: backup problem", "msg": "", "lvl": "warn",
                                 "cond": 0, "kind": "backup", "desk": True})
        tmp = nas.EVENTS_FILE + ".other"
        with open(tmp, "w") as f:
            json.dump(outside, f)
        os.replace(tmp, nas.EVENTS_FILE)             # exactly how _events_save lands a write

        nas.log_event("action", "later, from the panel")
        self.assertEqual(self._titles(),
                         ["from the panel", "Kopia: backup problem", "later, from the panel"])

    def test_ids_do_not_collide_after_an_outside_write(self):
        nas.log_event("action", "one")
        d = self._disk()
        d["seq"] += 1
        d["items"].append({"id": d["seq"], "t": int(time.time()), "event": "x", "title": "outside",
                           "msg": "", "lvl": "info", "cond": 0, "kind": "system", "desk": False})
        tmp = nas.EVENTS_FILE + ".other"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, nas.EVENTS_FILE)
        nas.log_event("action", "two")
        ids = [i["id"] for i in self._disk()["items"]]
        self.assertEqual(len(ids), len(set(ids)), "an id was reused: %r" % (ids,))

    def test_seen_marker_from_another_process_is_not_rolled_back(self):
        for i in range(3):
            nas.log_event("action", "e%d" % i)
        d = self._disk()
        d["seen"] = d["seq"]                          # the panel marked them read
        tmp = nas.EVENTS_FILE + ".other"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, nas.EVENTS_FILE)
        nas.log_event("action", "new one")
        self.assertEqual(self._disk()["seen"], d["seen"])

    def test_unchanged_file_is_not_reread_needlessly(self):
        nas.log_event("action", "one")
        with mock.patch("builtins.open", side_effect=AssertionError("re-read")):
            nas._events_load()                        # same inode/mtime/size → serve the cache

    def test_lock_file_is_not_world_readable(self):
        nas.log_event("action", "one")
        st = os.stat(nas.EVENTS_FILE + ".lock")
        self.assertEqual(st.st_mode & 0o077, 0)

    def test_a_broken_lock_still_records_the_event(self):
        # degrade to the old thread-only behaviour rather than lose the event
        with mock.patch.object(nas.fcntl, "flock", side_effect=OSError("no locks")):
            nas.log_event("action", "must survive")
        self.assertIn("must survive", self._titles())


if __name__ == "__main__":
    unittest.main()
