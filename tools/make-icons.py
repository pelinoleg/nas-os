#!/usr/bin/env python3
"""Render the NAS-OS brand mark: web/icon.svg plus every PNG size the panel serves.

Why a renderer instead of a checked-in PNG set: the SVG and the PNGs have to agree, and
the only way to guarantee that is to generate both from ONE description of the shape.
The shape lives in LAYERS below; everything else is mechanical.

stdlib only, on purpose — this runs on the NAS itself, which has no rsvg/inkscape/PIL and
should not grow a toolchain to redraw an icon. Shapes are signed distance functions, so
antialiasing falls out of the distance (alpha = how far inside the edge the pixel centre
is) instead of needing supersampling.

Usage: python3 tools/make-icons.py [web_dir]
"""
import math, os, struct, sys, zlib

# --- the mark ---------------------------------------------------------------
# Two stacked drive bays with a live indicator each — the mark that was already in the
# panel header before the Raspberry Pi support came out, kept because it is the one that
# looks right there. Solid fills rather than outlines: at 16 px a 14-unit stroke is under
# a pixel wide and the whole mark went grey and thin in a browser tab. The red is a softer,
# lighter #c8506b — the old #c51a4a was picked to match a berry that is no longer here.
#
# The art box is 256x232, NOT square, exactly as web/icon.svg has always been: the header
# and every other CSS size rule are written against that viewBox. The square PNGs pad it
# vertically instead of changing it, so the SVG stays byte-faithful to what the browser
# has been rendering all along.
#
# Everything is drawn from LAYERS below, painted in order onto a TRANSPARENT ground. Each
# layer is a colour, an opacity and a signed distance function in art-box units, so every
# size is the same drawing rather than a resampled screenshot of a bigger one.
RED   = "#c8506b"      # bay fill
GREEN = "#7bcb59"      # live indicator
LIGHT = "#ffffff"      # the slot cut into the bay

ART_W, ART_H = 256.0, 232.0
BAYS   = ((26.0, 34.0, 204.0, 72.0, 18.0), (26.0, 126.0, 204.0, 72.0, 18.0))
DOTS   = ((62.0, 70.0, 13.0), (62.0, 162.0, 13.0))
SLOTS  = ((104.0, 62.0, 96.0, 16.0, 8.0), (104.0, 154.0, 96.0, 16.0, 8.0))
SLOT_A = 0.62            # the slot reads as a lighter cut in the fill, not a second colour


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _sd_round_rect(x, y, rx, ry, rw, rh, r):
    """Signed distance to a rounded rectangle given by corner + size."""
    cx, cy = rx + rw / 2.0, ry + rh / 2.0
    hx, hy = rw / 2.0 - r, rh / 2.0 - r
    dx, dy = abs(x - cx) - hx, abs(y - cy) - hy
    return math.hypot(max(dx, 0.0), max(dy, 0.0)) + min(max(dx, dy), 0.0) - r


def _sd_segment(x, y, ax, ay, bx, by):
    pax, pay = x - ax, y - ay
    bax, bay = bx - ax, by - ay
    h = (pax * bax + pay * bay) / (bax * bax + bay * bay)
    h = 0.0 if h < 0.0 else (1.0 if h > 1.0 else h)
    return math.hypot(pax - bax * h, pay - bay * h)


def _sd_circle(x, y, cx, cy, r):
    return math.hypot(x - cx, y - cy) - r


def _sd_min(x, y, fns):
    return min(f(x, y) for f in fns)


# painted in order onto transparent: the solid bays, then what sits inside them
LAYERS = (
    (RED,   1.0,     lambda x, y: _sd_min(x, y, [
        lambda x, y, r=r: _sd_round_rect(x, y, *r) for r in BAYS])),
    (GREEN, 1.0,     lambda x, y: _sd_min(x, y, [
        lambda x, y, d=d: _sd_circle(x, y, *d) for d in DOTS])),
    (LIGHT, SLOT_A,  lambda x, y: _sd_min(x, y, [
        lambda x, y, s=s: _sd_round_rect(x, y, *s) for s in SLOTS])),
)


def _cov(d, scale):
    """Distance (mark units) -> pixel coverage 0..1. `scale` = mark units per pixel."""
    a = 0.5 - d / scale
    return 0.0 if a <= 0.0 else (1.0 if a >= 1.0 else a)


def render(size):
    """RGBA bytes for one square icon: the art box centred, transparent around it."""
    layers = [(_hex(c), o, f) for c, o, f in LAYERS]
    scale = ART_W / size                 # art units per output pixel
    off_y = (ART_W - ART_H) / 2.0        # pad the shorter axis instead of stretching
    rows = []
    for py in range(size):
        y = (py + 0.5) * scale - off_y
        row = bytearray()
        for px in range(size):
            x = (px + 0.5) * scale
            r = g = b = a = 0.0
            for col, op, sdf in layers:
                sa = _cov(sdf(x, y), scale) * op
                if sa <= 0.0:
                    continue
                # src-over, straight (not premultiplied) alpha
                na = sa + a * (1.0 - sa)
                r = (col[0] * sa + r * a * (1.0 - sa)) / na
                g = (col[1] * sa + g * a * (1.0 - sa)) / na
                b = (col[2] * sa + b * a * (1.0 - sa)) / na
                a = na
            row += bytes((int(r + 0.5), int(g + 0.5), int(b + 0.5), int(a * 255 + 0.5)))
        rows.append(bytes(row))
    return rows


def write_png(path, size):
    rows = render(size)
    raw = b"".join(b"\0" + r for r in rows)          # filter byte 0 per scanline
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    return len(png)


SVG = '''<svg xmlns="http://www.w3.org/2000/svg" aria-label="NAS" role="img" viewBox="0 0 {aw:g} {ah:g}">
  <!-- NAS-OS brand mark: two stacked drive bays, one live indicator each. Generated by
       tools/make-icons.py, which renders every PNG size from these same numbers; edit the
       shape THERE, not here, or the SVG and the icons drift apart. -->
  <g fill="{red}">
    <rect x="{b0x:g}" y="{b0y:g}" width="{b0w:g}" height="{b0h:g}" rx="{b0r:g}"/>
    <rect x="{b1x:g}" y="{b1y:g}" width="{b1w:g}" height="{b1h:g}" rx="{b1r:g}"/>
  </g>
  <g fill="{green}">
    <circle cx="{d0x:g}" cy="{d0y:g}" r="{d0r:g}"/>
    <circle cx="{d1x:g}" cy="{d1y:g}" r="{d1r:g}"/>
  </g>
  <g fill="{light}" opacity="{sa:g}">
    <rect x="{s0x:g}" y="{s0y:g}" width="{s0w:g}" height="{s0h:g}" rx="{s0r:g}"/>
    <rect x="{s1x:g}" y="{s1y:g}" width="{s1w:g}" height="{s1h:g}" rx="{s1r:g}"/>
  </g>
</svg>
'''


def main():
    web = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
    with open(os.path.join(web, "icon.svg"), "w") as f:
        f.write(SVG.format(
            aw=ART_W, ah=ART_H, red=RED, green=GREEN, light=LIGHT, sa=SLOT_A,
            b0x=BAYS[0][0], b0y=BAYS[0][1], b0w=BAYS[0][2], b0h=BAYS[0][3], b0r=BAYS[0][4],
            b1x=BAYS[1][0], b1y=BAYS[1][1], b1w=BAYS[1][2], b1h=BAYS[1][3], b1r=BAYS[1][4],
            d0x=DOTS[0][0], d0y=DOTS[0][1], d0r=DOTS[0][2],
            d1x=DOTS[1][0], d1y=DOTS[1][1], d1r=DOTS[1][2],
            s0x=SLOTS[0][0], s0y=SLOTS[0][1], s0w=SLOTS[0][2], s0h=SLOTS[0][3], s0r=SLOTS[0][4],
            s1x=SLOTS[1][0], s1y=SLOTS[1][1], s1w=SLOTS[1][2], s1h=SLOTS[1][3], s1r=SLOTS[1][4]))
    print("icon.svg")
    for name, size in (("favicon-16.png", 16), ("favicon-32.png", 32),
                       ("icon-16.png", 16), ("icon-32.png", 32),
                       ("icon-180.png", 180), ("icon-192.png", 192),
                       ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        n = write_png(os.path.join(web, name), size)
        print("%-22s %4dpx  %6d B" % (name, size, n))


if __name__ == "__main__":
    main()
