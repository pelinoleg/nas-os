"""How the panel writes secrets, and how it unpacks what it is given.

Three defects from the 2026-08-14 app audit, each measured before the fix:

  * the shared JSON writer created its temp file with plain open() — 0644 under the
    server's umask — and callers chmod'ed the TARGET afterwards. Every secret written
    through it (the display token, the credential store) was world-readable for the
    length of the write, and forever if json.dump raised: a failed save left a 0644
    leftover with the secret inside it;
  * the credential store answered a corrupt file with [], so the panel showed an empty
    list and the next "add" wrote that empty list back over the owner's passwords; and a
    POST body without "creds" wiped it outright;
  * every .zip through the file manager's extract endpoint raised TypeError, because
    shutil hands zip to an unpacker that does not take the tar `filter` argument. The
    endpoint answered 500. Retrying through shutil would have re-dispatched on the file
    name and could extract a swapped-in tar unfiltered, so zipfile is named explicitly.
"""
import glob
import importlib.util
import io
import os
import shutil
import tarfile
import tempfile
import unittest
import zipfile

SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


class JsonWriterModes(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_a_secret_is_written_owner_only(self):
        p = os.path.join(self.d, "secret.json")
        nas._json_save(p, {"token": "s3cr3t"}, mode=0o600)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_a_failed_write_leaves_nothing_behind(self):
        p = os.path.join(self.d, "boom.json")
        with self.assertRaises(TypeError):
            nas._json_save(p, {"token": "s3cr3t", "bad": {1, 2}}, mode=0o600)
        self.assertEqual(glob.glob(p + "*"), [], "a leftover temp file survived the failure")

    def test_ordinary_files_keep_their_mode(self):
        # tightening every config to 0600 would be a silent behaviour change of its own
        p = os.path.join(self.d, "public.json")
        nas._json_save(p, {"a": 1})
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o644)
        os.chmod(p, 0o640)
        nas._json_save(p, {"a": 2})
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o640, "the existing mode was not kept")


class CredentialStore(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._file, self._cfg = nas.CREDS_FILE, nas.NAS_CONFIG
        nas.CREDS_FILE = os.path.join(self.d, "credentials.json")
        nas.NAS_CONFIG = self.d

    def tearDown(self):
        nas.CREDS_FILE, nas.NAS_CONFIG = self._file, self._cfg
        shutil.rmtree(self.d, ignore_errors=True)

    def test_saved_owner_only_and_read_back(self):
        nas.save_creds([{"service": "s", "login": "l", "pass": "test-pw"}])
        self.assertEqual(os.stat(nas.CREDS_FILE).st_mode & 0o777, 0o600)
        self.assertEqual(len(nas.load_creds()), 1)

    def test_a_non_list_is_refused(self):
        nas.save_creds([{"service": "s"}])
        with self.assertRaises(ValueError):
            nas.save_creds("hello")
        self.assertEqual(len(nas.load_creds()), 1, "the store was damaged by a bad write")

    def test_a_corrupt_store_is_kept_aside_not_emptied(self):
        nas.save_creds([{"service": "s"}])
        with open(nas.CREDS_FILE, "w") as f:
            f.write("{not json")
        self.assertEqual(nas.load_creds(), [])
        self.assertTrue(os.path.exists(nas.CREDS_FILE + ".bad"),
                        "the corrupt file was neither kept nor reported")


class Extraction(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "out")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_a_zip_extracts(self):
        z = os.path.join(self.d, "a.zip")
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("good.txt", "data")
        nas._safe_untar(z, self.out)
        self.assertTrue(os.path.exists(os.path.join(self.out, "good.txt")))

    def test_a_zip_cannot_escape(self):
        z = os.path.join(self.d, "evil.zip")
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("../escaped.txt", "nope")
        nas._safe_untar(z, self.out)
        self.assertFalse(os.path.exists(os.path.join(self.d, "escaped.txt")),
                         "a zip member escaped the destination")

    def test_a_tar_still_cannot_escape(self):
        t = os.path.join(self.d, "evil.tar.gz")
        with tarfile.open(t, "w:gz") as f:
            ti = tarfile.TarInfo("../escaped.txt")
            ti.size = 4
            f.addfile(ti, io.BytesIO(b"nope"))
        with self.assertRaises(Exception):
            nas._safe_untar(t, self.out)
        self.assertFalse(os.path.exists(os.path.join(self.d, "escaped.txt")))


if __name__ == "__main__":
    unittest.main()
