// NAS-OS glance display for ESP32 + TFT.
// Tested target: LilyGO T-Display-S3 (170x320, ST7789) — in TFT_eSPI pick the
// bundled Setup206_LilyGo_T_Display_S3.h. For the T-Display-S3 **Long**
// (640x180, AXS15231B) TFT_eSPI does not support the panel: take LilyGO's
// library from github.com/Xinyuan-LilyGO/T-Display-S3-Long (or Arduino_GFX)
// and swap the draw calls in the "rendering primitives" section — the rest of
// this sketch is display-agnostic.
//
// The server (Настройки → Экран) decides pages, tiles, sizes and order; the
// device only renders. BOOT (GPIO0) or KEY (GPIO14) flips pages; pages also
// auto-rotate every PAGE_ROTATE_MS (0 = off).
//
// Libraries: TFT_eSPI + ArduinoJson 7.

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <TFT_eSPI.h>

static const char* WIFI_SSID = "your-wifi";
static const char* WIFI_PASS = "your-pass";
static const char* NAS_HOST  = "192.168.1.48";   // NAS IP or hostname
static const char* TOKEN     = "paste-glance-token-here";
static const char* SCREEN_ID = "";               // empty = first screen; ids are shown in the constructor

static const uint32_t POLL_MS = 5000;
static const uint32_t PAGE_ROTATE_MS = 15000;    // 0 = manual (button) only
static const int BTN1 = 0, BTN2 = 14;            // T-Display-S3 buttons

TFT_eSPI tft;
JsonDocument DOC;              // last payload (kept for page redraws)
bool haveDoc = false;
long lastSeq = -1;
int page = 0;
uint32_t lastPoll = 0, lastFlip = 0, lastOkMs = 0;
uint8_t failures = 0;
bool stale = false;            // polls are failing: keep last data + red badge

uint16_t stColor(const char* s) {
  if (!s) return TFT_DARKGREY;
  if (!strcmp(s, "ok"))   return TFT_GREEN;
  if (!strcmp(s, "warn")) return TFT_YELLOW;
  return TFT_RED;
}

// red "offline Nm" badge in the top-right corner: the NAS stopped answering,
// tiles below are the last known state (better than wiping the screen)
void drawStaleBadge() {
  uint32_t mins = (millis() - lastOkMs) / 60000UL;
  String s = "offline " + String(mins) + "m";
  int tw = tft.textWidth(s, 2);
  int x = tft.width() - tw - 14, y = 2;
  tft.fillRoundRect(x, y, tw + 12, 18, 5, TFT_RED);
  tft.setTextDatum(MC_DATUM);
  tft.setTextColor(TFT_WHITE, TFT_RED);
  tft.drawString(s, x + (tw + 12) / 2, y + 9, 2);
  tft.setTextDatum(ML_DATUM);
}

void drawOffline(const char* why) {
  tft.fillScreen(TFT_BLACK);
  tft.setTextDatum(MC_DATUM);
  tft.setTextColor(TFT_RED, TFT_BLACK);
  tft.drawString("NAS UNREACHABLE", tft.width() / 2, tft.height() / 2 - 10, 4);
  tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
  tft.drawString(why, tft.width() / 2, tft.height() / 2 + 20, 2);
  tft.setTextDatum(ML_DATUM);
}

void drawSpark(JsonArray sp, int x, int y, int w, int h) {
  int m = sp.size();
  if (m < 2) return;
  float mn = 1e30, mx = -1e30;
  for (int k = 0; k < m; k++) { float v = sp[k].as<float>(); if (v < mn) mn = v; if (v > mx) mx = v; }
  float span = (mx - mn) > 0 ? (mx - mn) : 1;
  int px = -1, py = -1;
  for (int k = 0; k < m; k++) {
    int gx = x + k * (w - 1) / (m - 1);
    int gy = y + h - 1 - (int)((sp[k].as<float>() - mn) / span * (h - 1));
    if (px >= 0) tft.drawLine(px, py, gx, gy, TFT_DARKGREY);
    px = gx; py = gy;
  }
}

// "#RRGGBB[AA]" -> RGB565; alpha is blended against bg (r,g,b) because the
// panel has no real transparency. fb = fallback when absent/invalid.
uint16_t hexBlend(const char* s, uint16_t fb, uint8_t br, uint8_t bgc, uint8_t bb) {
  if (!s || s[0] != '#') return fb;
  size_t n = strlen(s);
  if (n != 7 && n != 9) return fb;
  long v = strtol(s + 1, nullptr, 16);
  uint8_t r, g, b, a = 255;
  if (n == 9) { a = v & 0xFF; v >>= 8; }
  r = (v >> 16) & 0xFF; g = (v >> 8) & 0xFF; b = v & 0xFF;
  if (a < 255) {
    r = (r * a + br * (255 - a)) / 255;
    g = (g * a + bgc * (255 - a)) / 255;
    b = (b * a + bb * (255 - a)) / 255;
  }
  return tft.color565(r, g, b);
}
uint16_t hex565(const char* s, uint16_t fb) { return hexBlend(s, fb, 0, 0, 0); }
// tile background as 8-bit RGB, for blending text alpha over it
void tileBgRGB(const char* bgs, uint8_t &r, uint8_t &g, uint8_t &b) {
  r = 0x14; g = 0x16; b = 0x1c;                       // default card ≈ 0x10A2
  if (!strcmp(bgs, "none")) { r = g = b = 0; return; }
  if (bgs[0] == '#' && strlen(bgs) >= 7) {
    long v = strtol(bgs + 1, nullptr, 16);
    if (strlen(bgs) == 9) v >>= 8;
    r = (v >> 16) & 0xFF; g = (v >> 8) & 0xFF; b = v & 0xFF;
  }
}

// requested font-size (device px) -> nearest TFT_eSPI font
int pickFont(int px, const String &s) {
  if (px > 33) {  // font 6 is digits-only
    bool dig = true;
    for (unsigned i = 0; i < s.length() && dig; i++) dig = strchr("0123456789.-", s[i]);
    return dig ? 6 : 4;
  }
  return px > 17 ? 4 : 2;
}

// 9-grid position ("tl".."br") -> TFT datum + anchor point inside the tile
struct Anchor { int datum, x, y; };
Anchor posAnchor(const char* p, int x, int y, int w, int h) {
  int col = 1, row = 1;                                // default centre
  if (p && strlen(p) >= 1) {
    if (p[0] == 't') row = 0; else if (p[0] == 'b') row = 2;
    char c = p[strlen(p) - 1];
    if (c == 'l') col = 0; else if (c == 'r') col = 2;
    if (!strcmp(p, "c")) { col = 1; row = 1; }
  }
  static const int DAT[3][3] = {{TL_DATUM, TC_DATUM, TR_DATUM},
                                {ML_DATUM, MC_DATUM, MR_DATUM},
                                {BL_DATUM, BC_DATUM, BR_DATUM}};
  int ax = col == 0 ? x + 5 : (col == 1 ? x + w / 2 : x + w - 5);
  int ay = row == 0 ? y + 3 : (row == 1 ? y + h / 2 : y + h - 3);
  return {DAT[row][col], ax, ay};
}

// per-size defaults, overridden by the tile's "st" style from the server
struct Style { const char *lp, *vp, *up; int ls, vs, us; };
Style defStyle(const char* size) {
  if (!strcmp(size, "s")) return {"cl", "cr", "val", 10, 12, 9};
  if (!strcmp(size, "l")) return {"tl", "c",  "val", 11, 26, 11};
  return {"tl", "c", "val", 10, 17, 10};
}

void drawTile(JsonObject t, int x, int y, int w, int h) {
  JsonObject st = t["st"];
  Style d = defStyle(t["size"] | "m");
  const char* lp = st["lp"] | d.lp;
  const char* vp = st["vp"] | d.vp;
  const char* up = st["up"] | d.up;
  int ls = st["ls"] | d.ls, vs = st["vs"] | d.vs, us = st["us"] | d.us;
  int bw = st["bw"] | 1;
  const char* bgs = st["bg"] | "";
  bool noBg = !strcmp(bgs, "none");
  uint8_t br, bgc, bb;
  tileBgRGB(bgs, br, bgc, bb);                        // blended-alpha base
  uint16_t bg = noBg ? TFT_BLACK : hexBlend(bgs, 0x10A2, 0, 0, 0);
  uint16_t line = hexBlend(st["bc"] | "", 0x2965, br, bgc, bb);
  if (!noBg) tft.fillRoundRect(x, y, w, h, 7, bg);
  for (int k = 0; k < bw; k++) tft.drawRoundRect(x + k, y + k, w - 2 * k, h - 2 * k, 7 - k, line);
  uint16_t tbg = noBg ? TFT_BLACK : bg;
  uint16_t labC = hexBlend(st["lc"] | "", TFT_DARKGREY, br, bgc, bb);
  uint16_t valC = hexBlend(st["vc"] | "", TFT_WHITE, br, bgc, bb);
  uint16_t uniC = hexBlend(st["uc"] | "", TFT_DARKGREY, br, bgc, bb);
  // label
  if (strcmp(lp, "hide")) {
    Anchor a = posAnchor(lp, x, y, w, h);
    tft.setTextDatum(a.datum);
    tft.setTextColor(labC, tbg);
    tft.drawString(t["label"] | "", a.x, a.y, pickFont(ls, ""));
  }
  // value (+ attached unit when up == "val")
  String val = String(t["value"] | "");
  String unit = String(t["unit"] | "");
  if (strcmp(vp, "hide")) {
    int vf = pickFont(vs, val);
    Anchor a = posAnchor(vp, x, y, w, h);
    if (!strcmp(up, "valb") && unit.length()) {
      // unit centred under the value, both around the value's anchor
      int uf = pickFont(us, unit);
      int vh = tft.fontHeight(vf), uh = tft.fontHeight(uf);
      int cy = a.datum / 3 == 0 ? a.y + (vh + uh) / 2 : (a.datum / 3 == 2 ? a.y - (vh + uh) / 2 : a.y);
      int cx = a.datum % 3 == 0 ? a.x + tft.textWidth(val, vf) / 2 :
               (a.datum % 3 == 2 ? a.x - tft.textWidth(val, vf) / 2 : a.x);
      tft.setTextDatum(BC_DATUM);
      tft.setTextColor(valC, tbg);
      tft.drawString(val, cx, cy, vf);
      tft.setTextDatum(TC_DATUM);
      tft.setTextColor(uniC, tbg);
      tft.drawString(unit, cx, cy + 1, uf);
    } else if (!strcmp(up, "val") && unit.length()) {
      int uf = pickFont(us, unit);
      int total = tft.textWidth(val, vf) + 5 + tft.textWidth(unit, uf);
      int lx = a.datum % 3 == 0 ? a.x : (a.datum % 3 == 1 ? a.x - total / 2 : a.x - total);
      int dy = a.datum / 3 == 0 ? 0 : (a.datum / 3 == 1 ? 0 : 0);
      tft.setTextDatum(a.datum / 3 == 0 ? TL_DATUM : (a.datum / 3 == 1 ? ML_DATUM : BL_DATUM));
      tft.setTextColor(valC, tbg);
      tft.drawString(val, lx, a.y + dy, vf);
      tft.setTextColor(uniC, tbg);
      tft.drawString(unit, lx + tft.textWidth(val, vf) + 5, a.y + dy, uf);
    } else {
      tft.setTextDatum(a.datum);
      tft.setTextColor(valC, tbg);
      tft.drawString(val, a.x, a.y, vf);
    }
  }
  // detached unit
  if (strcmp(up, "val") && strcmp(up, "valb") && strcmp(up, "hide") && unit.length()) {
    Anchor a = posAnchor(up, x, y, w, h);
    tft.setTextDatum(a.datum);
    tft.setTextColor(uniC, tbg);
    tft.drawString(unit, a.x, a.y, pickFont(us, unit));
  }
  tft.setTextDatum(ML_DATUM);
  tft.fillCircle(x + w - 9, y + 8, 3, stColor(t["state"] | "ok"));
  JsonArray sp = t["spark"];
  if (sp.size() >= 2 && h >= 52) drawSpark(sp, x + 8, y + h - 16, w - 16, 11);
}

void drawAvail(JsonObject avail, int y) {
  JsonArray bars = avail["bars"];
  int n = bars.size();
  if (!n) return;
  int bw = tft.width() / n;
  for (int b = 0; b < n; b++) {
    int v = bars[b].as<int>();
    uint16_t c = v == 2 ? TFT_GREEN : (v == 1 ? TFT_YELLOW : (v == 0 ? TFT_RED : TFT_DARKGREY));
    tft.fillRect(b * bw, y, bw - 1, 10, c);
  }
}

void drawPage() {
  if (!haveDoc) return;
  JsonArray pages = DOC["pages"];
  if (!pages.size()) return;
  if (page >= (int)pages.size()) page = 0;
  JsonObject pg = pages[page];

  tft.fillScreen(TFT_BLACK);
  // header: status dot, host, page name + dots
  tft.fillCircle(12, 13, 7, stColor(DOC["status"] | "warn"));
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.drawString(DOC["host"] | "NAS", 26, 13, 4);
  tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
  String pname = pg["name"] | "";
  if (pages.size() > 1) {
    for (int i = 0; i < (int)pages.size(); i++)
      tft.fillCircle(tft.width() - 10 - i * 12, 13, 3, i == page ? TFT_WHITE : TFT_DARKGREY);
    tft.drawString(pname, tft.width() - 14 - pages.size() * 12 - tft.textWidth(pname, 2), 13, 2);
  }

  // free mode: the constructor supplies x/y/w/h per tile, overlap is fine.
  // grid mode: flow layout — "l" full row, "m" card, "s" slim row; wide panel
  // (T-Display-S3 Long, 640 px) gets 3 columns. "gap" insets every card.
  JsonObject scrInfo = DOC["screen"];
  int gap = scrInfo["gap"] | 0;
  bool freeMode = !strcmp(scrInfo["mode"] | "flow", "free");
  if (freeMode) {
    for (JsonObject t : pg["tiles"].as<JsonArray>()) {
      int tx = t["x"] | 0, ty = t["y"] | 28, tw = t["w"] | 100, th = t["h"] | 46;
      drawTile(t, tx, ty, tw, th);
    }
  } else {
    int cols = tft.width() >= 480 ? 3 : 2;
    int colW = tft.width() / cols;
    int y = 28, x = 0, rowH = 0;
    const int hL = 66, hM = 50, hS = 20;
    for (JsonObject t : pg["tiles"].as<JsonArray>()) {
      const char* sz = t["size"] | "m";
      bool big = !strcmp(sz, "l"), slim = !strcmp(sz, "s");
      int th = big ? hL : (slim ? hS : hM);
      int tw = big ? tft.width() : colW;
      if (big && x > 0) { y += rowH; x = 0; rowH = 0; }        // "l" starts a fresh row
      if (x + tw > tft.width()) { y += rowH; x = 0; rowH = 0; }
      int bot = (scrInfo["avail"] | true) ? 14 : 2;
      if (y + th > tft.height() - bot) break;                   // keep room for avail strip
      drawTile(t, x + 2 + gap / 2, y + 2 + gap / 2, tw - 4 - gap, th - 4 - gap);
      x += tw; rowH = max(rowH, th);
      if (big) { y += th; x = 0; rowH = 0; }
    }
  }
  JsonObject avail = DOC["avail"];
  if (!(scrInfo["avail"] | true)) return;      // strip disabled for this screen
  if (!avail.isNull()) drawAvail(avail, tft.height() - 12);
}

void poll() {
  HTTPClient http;
  String url = String("http://") + NAS_HOST + "/api/glance?token=" + TOKEN +
               "&lang=en&seq=" + String(lastSeq);
  if (SCREEN_ID[0]) url += String("&screen=") + SCREEN_ID;
  http.setTimeout(4000);
  http.begin(url);
  int code = http.GET();
  if (code == 304) {
    http.end(); failures = 0; lastOkMs = millis();
    if (stale) { stale = false; drawPage(); }        // link is back — clear badge
    return;
  }
  if (code != 200) {
    http.end();
    if (++failures >= 2) {
      if (!haveDoc) drawOffline(code > 0 ? ("HTTP " + String(code)).c_str() : "no connection");
      else { stale = true; drawStaleBadge(); }       // keep data, show outage age
    }
    return;
  }
  failures = 0; lastOkMs = millis();
  if (stale) { stale = false; lastSeq = -1; }        // force redraw with fresh data
  DeserializationError err = deserializeJson(DOC, http.getString());
  http.end();
  if (err) return;
  long seq = DOC["seq"] | 0;
  haveDoc = true;
  if (seq == lastSeq) return;
  lastSeq = seq;
  drawPage();
}

void flipPage(int dir) {
  JsonArray pages = DOC["pages"];
  if (!haveDoc || pages.size() < 2) return;
  page = (page + dir + pages.size()) % pages.size();
  lastFlip = millis();
  drawPage();
  if (stale) drawStaleBadge();
}

void setup() {
  Serial.begin(115200);
  pinMode(BTN1, INPUT_PULLUP);
  pinMode(BTN2, INPUT_PULLUP);
  tft.init();
  tft.setRotation(1);                       // landscape; use 0 for portrait
  tft.fillScreen(TFT_BLACK);
  tft.setTextDatum(ML_DATUM);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.drawString("connecting...", 10, tft.height() / 2, 2);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

void loop() {
  static int b1 = HIGH, b2 = HIGH;
  int n1 = digitalRead(BTN1), n2 = digitalRead(BTN2);
  if (b1 == HIGH && n1 == LOW) flipPage(+1);
  if (b2 == HIGH && n2 == LOW) flipPage(-1);
  b1 = n1; b2 = n2;

  if (WiFi.status() != WL_CONNECTED) {
    static uint32_t lostAt = 0;
    if (!lostAt) lostAt = millis();
    if (millis() - lostAt > 15000) { drawOffline("wifi lost"); lostAt = millis(); WiFi.reconnect(); }
    delay(100);
    return;
  }
  if (PAGE_ROTATE_MS && haveDoc && millis() - lastFlip >= PAGE_ROTATE_MS) flipPage(+1);
  if (millis() - lastPoll >= POLL_MS || !lastPoll) { lastPoll = millis(); poll(); }
  delay(30);
}
