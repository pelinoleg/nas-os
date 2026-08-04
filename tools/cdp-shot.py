#!/usr/bin/env python3
"""Minimal Chrome DevTools Protocol client on the stdlib alone.

The box has chromium but no websocket-client/selenium/playwright, and the panel
sits behind an HttpOnly session cookie that JS cannot set — so a screenshot needs
Network.setCookie, which needs CDP. ~90 lines is cheaper than a dependency.
"""
import base64, json, os, socket, struct, subprocess, sys, time, urllib.request


class WS:
    def __init__(self, url):
        # ws://127.0.0.1:PORT/devtools/page/ID
        rest = url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.s = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
                        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.id = 0

    def _recv(self, n):
        while len(self.buf) < n:
            c = self.s.recv(65536)
            if not c:
                raise IOError("closed")
            self.buf += c
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, method, params=None):
        self.id += 1
        payload = json.dumps({"id": self.id, "method": method,
                              "params": params or {}}).encode()
        mask = os.urandom(4)
        ln = len(payload)
        hdr = b"\x81"
        if ln < 126:
            hdr += bytes([0x80 | ln])
        elif ln < 65536:
            hdr += b"\xfe" + struct.pack(">H", ln)
        else:
            hdr += b"\xff" + struct.pack(">Q", ln)
        self.s.sendall(hdr + mask +
                       bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        return self.id

    def frame(self):
        b0, b1 = self._recv(2)
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._recv(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._recv(8))[0]
        return json.loads(self._recv(ln))

    def call(self, method, params=None, timeout=60):
        want = self.send(method, params)
        end = time.time() + timeout
        while time.time() < end:
            m = self.frame()
            if m.get("id") == want:
                if "error" in m:
                    raise RuntimeError("%s: %s" % (method, m["error"]))
                return m.get("result", {})
        raise TimeoutError(method)


def launch(port=9222, profile="/tmp/cdp-profile"):
    subprocess.run(["pkill", "-f", "remote-debugging-port=%d" % port],
                   capture_output=True)
    subprocess.run(["rm", "-rf", profile], capture_output=True)
    p = subprocess.Popen(
        ["chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
         "--hide-scrollbars", "--remote-debugging-port=%d" % port,
         "--user-data-dir=" + profile, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port,
                                   timeout=1).read()
            return p
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("chromium did not open its debug port")


def shot(url, out, cookie=None, w=1440, h=900, wait=6.0, theme="dark",
         port=9222, js=None):
    proc = launch(port)
    try:
        # /json/new needs PUT on current Chromium (POST -> 405); reuse the tab the
        # browser already opened instead of asking for a new one.
        tabs = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/json/list" % port, timeout=10).read())
        pages = [t for t in tabs if t.get("type") == "page"]
        if not pages:
            raise RuntimeError("no page target")
        tgt = pages[0]
        ws = WS(tgt["webSocketDebuggerUrl"])
        ws.call("Page.enable")
        ws.call("Network.enable")
        ws.call("Emulation.setDeviceMetricsOverride",
                {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})
        ws.call("Emulation.setEmulatedMedia",
                {"features": [{"name": "prefers-color-scheme", "value": theme}]})
        if cookie:
            host = url.split("://", 1)[1].split("/")[0].split(":")[0]
            ws.call("Network.setCookie", {"name": "nasauth", "value": cookie,
                                          "domain": host, "path": "/"})
        ws.call("Page.navigate", {"url": url})
        time.sleep(wait)
        if js:
            r = ws.call("Runtime.evaluate",
                        {"expression": js, "awaitPromise": True,
                         "returnByValue": True})
            print(json.dumps(r.get("result", {}).get("value"), ensure_ascii=False))
            time.sleep(1.5)
        img = ws.call("Page.captureScreenshot", {"format": "png"})
        open(out, "wb").write(base64.b64decode(img["data"]))
        print("saved", out)
    finally:
        proc.terminate()


if __name__ == "__main__":
    a = dict(x.split("=", 1) for x in sys.argv[1:] if "=" in x)
    shot(a["url"], a.get("out", "shot.png"), a.get("cookie"),
         int(a.get("w", 1440)), int(a.get("h", 900)), float(a.get("wait", 6)),
         a.get("theme", "dark"), js=a.get("js"))
