"""The panel serves the owner's own files back on the origin their session lives on.

That is the whole problem in one sentence. Everything that reaches this box — a file
dropped in a share, an upload, something Syncthing brought over — can be asked for again
through /api/fs/raw, and whatever the panel says it is, the browser believes. A .html
sitting in a share came back as text/html on the panel's origin: same cookie, same session,
full API. Nobody had to break in; they had to put a file on the NAS.

So: files that a browser would execute are handed over as downloads, every response says
"do not re-interpret this", and the panel stops announcing its Python build to anyone who
asks before logging in.

The upload path gets the same treatment from the other side: it was bounded by nothing but
the disk, and a pool folder lives on ONE branch — so "the disk" was the wrong number too.
"""
import importlib.util
import os
import shutil
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "nas_web", os.path.join(os.path.dirname(os.path.dirname(__file__)), "nas-web.py"))
nas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nas)


class FakeWire:
    """Enough of a request handler to record what _sendraw would put on the wire."""

    def __init__(self, path, download=False, rng=None):
        self.headers = {"Range": rng} if rng else {}
        self.sent, self.code, self.body = {}, None, b""
        self.errors = []
        nas.H._sendraw(self, path, download)

    # --- the bits of BaseHTTPRequestHandler _sendraw touches ---
    def send_response(self, code):
        self.code = code

    def send_header(self, k, v):
        self.sent[k] = v

    def end_headers(self):
        pass

    def send_error(self, code):
        self.errors.append(code)

    @property
    def wfile(self):
        return self

    def write(self, chunk):
        self.body += chunk


class WhatTheBrowserIsToldAFileIs(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def file(self, name, data=b"x"):
        p = os.path.join(self.d, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_html_from_a_share_is_not_served_as_html(self):
        w = FakeWire(self.file("readme.html", b"<script>fetch('/api/power')</script>"))
        self.assertEqual(w.sent["Content-Type"], "application/octet-stream",
                         "a file from a share would run on the panel's own origin")
        self.assertEqual(w.sent.get("Content-Security-Policy"), "sandbox")

    def test_svg_counts_as_executable_too(self):
        # SVG carries <script> and browsers run it when the document is served inline
        w = FakeWire(self.file("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>"))
        self.assertEqual(w.sent["Content-Type"], "application/octet-stream")

    def test_ordinary_files_are_still_served_as_themselves(self):
        # the fix must not turn the file manager's preview into a download prompt
        for name, want in (("photo.jpg", "image/jpeg"), ("clip.mp4", "video/mp4"),
                           ("notes.txt", "text/plain"), ("book.pdf", "application/pdf")):
            w = FakeWire(self.file(name))
            self.assertEqual(w.sent["Content-Type"], want, name)
            self.assertNotIn("Content-Security-Policy", w.sent, name)

    def test_range_requests_still_work(self):
        w = FakeWire(self.file("clip.mp4", b"0123456789"), rng="bytes=2-5")
        self.assertEqual(w.code, 206)
        self.assertEqual(w.sent["Content-Range"], "bytes 2-5/10")
        self.assertEqual(w.body, b"2345")

    def test_a_download_still_downloads(self):
        w = FakeWire(self.file("readme.html"), download=True)
        self.assertIn("attachment", w.sent["Content-Disposition"])

    def test_a_missing_file_is_a_404_not_a_crash(self):
        w = FakeWire(os.path.join(self.d, "gone.txt"))
        self.assertEqual(w.errors, [404])


class HeadersOnEveryResponse(unittest.TestCase):

    def test_the_three_headers_are_added_before_the_blank_line(self):
        sent = []

        class Probe:
            send_header = staticmethod(lambda k, v: sent.append((k, v)))
        # call the real override with a stand-in for the base class's own end_headers
        real_base_end = nas.BaseHTTPRequestHandler.end_headers
        try:
            nas.BaseHTTPRequestHandler.end_headers = lambda self: None
            nas.H.end_headers(Probe())
        finally:
            nas.BaseHTTPRequestHandler.end_headers = real_base_end
        got = dict(sent)
        self.assertEqual(got.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(got.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertEqual(got.get("Referrer-Policy"), "same-origin")

    def test_the_python_build_is_not_announced(self):
        # this line goes out on 401s too, i.e. to anyone who can reach the port
        self.assertEqual(nas.H.sys_version, "",
                         "the panel tells an unauthenticated caller its Python version")


class UploadsFitWhereTheyLand(unittest.TestCase):
    """_fsjob_free_at answers per BRANCH inside a pool, which is the room a folder really
    has — the union figure would let an upload fill one disk while claiming space."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_the_room_is_measured_where_the_file_lands(self):
        free = nas._fsjob_free_at(self.d)
        self.assertGreater(free, 0)
        self.assertLessEqual(free, shutil.disk_usage(self.d).free)


if __name__ == "__main__":
    unittest.main()
