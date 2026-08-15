"""What the settings archive promises must also come back out of it.

The backup has two halves that are written in different places and were never checked
against each other: _bk_sources decides what goes IN, and the restore map decides where
each member comes back OUT. A member nobody claims is not an error — it is skipped, the
restore reports success, and the setting is simply gone.

That is what happened to four things at once on 2026-08-14: the cloud tokens (rclone.conf),
the kopia repository, the SMB passwords and the Immich standby config. The dialog offered
those sections, the archive carried the files, the map had no entry, and the only way to
find out was to restore onto a fresh box and discover every profile pointing at nothing.

So the invariant tested here is the join itself: everything the collector packs must have
a destination and a section. It is derived from the collector's own source rather than
listed by hand, so a file added to the archive tomorrow is covered tomorrow — and writing
it found one more of these on 2026-08-15, still live: /etc/samba/nas-shares.conf, the file
every panel-managed share is defined in and the one smb.conf `include`s, was collected into
every archive, offered under "Shared folders", validated by the backup drill — and had no
way back. A restored box would have started Samba, served nothing, and pointed at a file
that was not there.

The disaster card is the same story one level up: it is the document read on a dead box,
by someone who cannot verify it, and it used to promise contents the archive did not have.
It now describes the archive that exists — which only stays true if it is rebuilt when a
new archive appears.
"""
import importlib.util
import inspect
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


def _collected_names():
    """Every archive name _bk_sources writes as a literal.

    Read out of the collector's own source rather than by calling it: a call answers only
    for the files that happen to exist on the machine running the test, so on a build
    server it would answer "nothing" and the check below would pass by being empty."""
    src = inspect.getsource(nas._bk_sources)
    return sorted(set(re.findall(
        r'"((?:etc|var|opt|root|nas-config|reference)/[^"]+)"', src)))


def _probe(nm):
    """The member name to ask about. Some literals are the ROOT of a directory that gets
    walked whole ("etc/nas-wizard"), so what lands in the archive is a file below it."""
    return nm if nas._bk_dest(nm) else nm + "/example.conf"


class WhatGoesInMustComeOut(unittest.TestCase):
    """The join between the three lists that must agree and live hundreds of lines apart:
    what is collected (_bk_sources), what the dialog offers (_BK_SECTIONS), and where it
    is put back (_bk_dest)."""

    def test_the_collector_names_enough_files_to_be_worth_checking(self):
        # guards the check below against silently becoming empty
        self.assertGreater(len(_collected_names()), 10)

    def test_everything_collected_can_be_restored(self):
        for nm in _collected_names():
            if nm.startswith("reference/"):
                continue
            self.assertIsNotNone(
                nas._bk_dest(_probe(nm)),
                "%s is packed into every archive and nothing claims it on the way back — "
                "the restore reports success and the setting is gone" % nm)

    def test_everything_collected_is_offered_by_a_section(self):
        # a file with no section is dropped by a selective restore without a word, so
        # "carried but not offered" fails in the same silent way
        for nm in _collected_names():
            if nm.startswith("reference/"):
                continue
            self.assertNotEqual(nas._bk_section(_probe(nm)), "other",
                                "%s is in the archive but no section offers it" % nm)

    def test_the_shares_come_back_with_the_config_that_includes_them(self):
        # smb.conf carries `include = /etc/samba/nas-shares.conf`; restoring one without
        # the other leaves Samba running and serving nothing
        self.assertEqual(nas._bk_dest("etc/samba/smb.conf"), "/etc/samba/smb.conf")
        self.assertEqual(nas._bk_dest("etc/samba/nas-shares.conf"),
                         "/etc/samba/nas-shares.conf")

    def test_the_files_that_were_silently_dropped(self):
        # named one by one because these four were the actual loss, and a future edit of
        # the map is exactly how they would go missing again
        for nm, dest in (("etc/nas-os/rclone.conf", "/etc/nas-os/rclone.conf"),
                         ("etc/nas-os/kopia.json", "/etc/nas-os/kopia.json"),
                         ("etc/nas-os/kopia/repository.config", "/etc/nas-os/kopia/repository.config"),
                         ("etc/nas-os/smb-users.json", "/etc/nas-os/smb-users.json"),
                         ("etc/nas-os/immich-standby.json", "/etc/nas-os/immich-standby.json")):
            self.assertEqual(nas._bk_dest(nm), dest)

    def test_a_prefix_maps_the_whole_subtree(self):
        self.assertEqual(nas._bk_dest("opt/stacks/immich/.env"),
                         os.path.join(nas.STACKS_DIR, "immich/.env"))
        self.assertEqual(nas._bk_dest("var/lib/syncthing/config.xml"),
                         os.path.join(nas.ST_HOME, "config.xml"))
        self.assertEqual(nas._bk_dest("nas-config/desktop.json"),
                         os.path.join(nas.NAS_CONFIG, "desktop.json"))
        self.assertEqual(nas._bk_dest("etc/nas-wizard/notify.conf"),
                         "/etc/nas-wizard/notify.conf")

    def test_reference_material_is_deliberately_not_restored(self):
        # netplan/NetworkManager/fstab are tied to this machine's hardware and UUIDs:
        # they are carried for a human to read, never written back over a fresh system
        for nm in ("reference/etc/fstab",
                   "reference/etc/netplan/01-netcfg.yaml",
                   "reference/etc/NetworkManager/system-connections/home-wifi.nmconnection"):
            self.assertIsNone(nas._bk_dest(nm), "%s would be applied blindly" % nm)

    def test_the_map_is_also_a_whitelist(self):
        # the restore walks a tar the box did not necessarily write
        for nm in ("etc/passwd", "root/.ssh/authorized_keys", "usr/bin/python3",
                   "manifest.json", "", "nas-configuration/x.json"):
            self.assertIsNone(nas._bk_dest(nm), "%r found a way out of the map" % (nm,))


class ArchiveRoundTrip(unittest.TestCase):
    """Build a real archive from a fake box, then ask the restore side about every
    member it contains."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.box = os.path.join(self.d, "box")
        os.makedirs(self.box)
        self._dir, self._src = nas.settings_backup_dir, nas._bk_sources
        self._card, self._log = nas.disaster_build, nas.log_event
        self.cards = []
        nas.settings_backup_dir = lambda: self.d
        nas.disaster_build = lambda: self.cards.append(1)
        nas.log_event = lambda *a, **k: None
        nas._bk_sources = lambda: [(self.make(src), arc) for src, arc in (
            ("webauth.json", "etc/nas-os/webauth.json"),
            ("rclone.conf", "etc/nas-os/rclone.conf"),
            ("config.xml", "var/lib/syncthing/config.xml"),
            ("env", "opt/stacks/immich/.env"),
            ("fstab", "reference/etc/fstab"),
            ("desktop.json", "nas-config/desktop.json"))]

    def tearDown(self):
        nas.settings_backup_dir, nas._bk_sources = self._dir, self._src
        nas.disaster_build, nas.log_event = self._card, self._log
        shutil.rmtree(self.d, ignore_errors=True)

    def make(self, name):
        p = os.path.join(self.box, name)
        with open(p, "w") as f:
            f.write("content of " + name)
        return p

    def test_every_member_of_a_fresh_archive_is_accounted_for(self):
        r = nas.settings_backup_make()
        self.assertTrue(r["ok"])
        with tarfile.open(os.path.join(self.d, r["name"])) as tf:
            names = tf.getnames()
        for nm in names:
            if nm == "manifest.json" or nm.startswith("reference/"):
                continue
            self.assertIsNotNone(nas._bk_dest(nm),
                                 "%s is packed into every archive and restores nowhere" % nm)

    def test_the_archive_is_owner_only(self):
        r = nas.settings_backup_make()
        p = os.path.join(self.d, r["name"])
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600,
                         "the panel password hash and the Samba password database are "
                         "readable by every local user")

    def test_a_new_archive_rebuilds_the_disaster_card(self):
        # the card names the NEWEST archive and lists what it holds, so every new archive
        # leaves the card on disk describing the previous one
        nas.settings_backup_make()
        self.assertEqual(len(self.cards), 1,
                         "the card the owner reads on a dead box describes the wrong archive")

    def test_the_manifest_lists_what_was_packed(self):
        r = nas.settings_backup_make()
        with tarfile.open(os.path.join(self.d, r["name"])) as tf:
            mf = json.loads(tf.extractfile("manifest.json").read())
        self.assertEqual(sorted(mf["files"]),
                         sorted(["etc/nas-os/webauth.json", "etc/nas-os/rclone.conf",
                                 "var/lib/syncthing/config.xml", "opt/stacks/immich/.env",
                                 "reference/etc/fstab", "nas-config/desktop.json"]))


class DisasterCardTellsTheTruth(unittest.TestCase):
    """The sentence in the card that describes the archive. A recovery instruction that
    misdescribes the archive is worse than none: it is read under pressure, by someone who
    cannot check it."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._dir = nas.settings_backup_dir
        nas.settings_backup_dir = lambda: self.d

    def tearDown(self):
        nas.settings_backup_dir = self._dir
        shutil.rmtree(self.d, ignore_errors=True)

    def archive(self, names, stamp="20260815-101500"):
        p = os.path.join(self.d, "nas-settings-%s.tar.gz" % stamp)
        with tarfile.open(p, "w:gz") as tf:
            for nm in names:
                ti = tarfile.TarInfo(nm); ti.size = 3
                tf.addfile(ti, io.BytesIO(b"abc"))
        return p

    def test_it_says_what_the_archive_actually_holds(self):
        self.archive(["nas-config/disaster-card.md", "etc/nas-os/webauth.json",
                      "etc/samba/smb.conf",
                      "reference/etc/NetworkManager/system-connections/home-wifi"])
        line = nas._disaster_archive_line()
        self.assertIn("panel password", line)
        self.assertIn("Wi-Fi", line)
        self.assertIn("NOT hold", line, "nothing was reported as missing")
        self.assertIn("stack composes", line.split("NOT hold")[1],
                      "an absent section was listed as present")

    def test_it_names_the_directory_the_archive_lives_in(self):
        # read on a dead box: a file name nobody can locate is not an instruction
        self.archive(["etc/nas-os/webauth.json"])
        self.assertIn(self.d, nas._disaster_archive_line())

    def test_it_says_the_wifi_profile_will_not_restore_itself(self):
        # The archive carries the profile, the restore never writes it back (it is tied to
        # this machine's hardware), and the dialog never even offers it. On a box whose
        # only link is Wi-Fi, "holds Wi-Fi profiles" alone reads as "the network comes
        # back" — and the owner finds out with no panel to ask.
        self.archive(["etc/nas-os/webauth.json", "reference/etc/fstab",
                      "reference/etc/NetworkManager/system-connections/home-wifi"])
        line = nas._disaster_archive_line()
        self.assertIn("READING ONLY", line)
        self.assertIn("home-wifi", line)
        self.assertIn("/etc/NetworkManager/system-connections/", line,
                      "the card does not say where to put it by hand")

    def test_a_complete_archive_promises_nothing_extra(self):
        self.archive(["nas-config/disaster-card.md", "etc/nas-os/webauth.json",
                      "etc/samba/smb.conf", "var/lib/syncthing/config.xml",
                      "reference/etc/NetworkManager/system-connections/home-wifi",
                      "opt/stacks/immich/.env", "etc/nas-os/nas-backup.json",
                      "root/.ssh/nas-backup"])
        self.assertNotIn("NOT hold", nas._disaster_archive_line())

    def test_it_describes_the_newest_archive(self):
        self.archive(["etc/nas-os/webauth.json"], stamp="20260101-000000")
        newest = self.archive(["etc/samba/smb.conf"], stamp="20260815-101500")
        self.assertIn(os.path.basename(newest), nas._disaster_archive_line())

    def test_no_archive_at_all_is_said_plainly(self):
        line = nas._disaster_archive_line()
        self.assertIn("NO settings archive", line)
        self.assertIn("BEFORE you need it", line)

    def test_an_unreadable_archive_does_not_break_the_card(self):
        with open(os.path.join(self.d, "nas-settings-20260815-101500.tar.gz"), "w") as f:
            f.write("this is not a tarball")
        self.assertIn("Could not read", nas._disaster_archive_line())


if __name__ == "__main__":
    unittest.main()
