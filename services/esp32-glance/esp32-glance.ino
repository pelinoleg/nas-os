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

// Board flavours: the classic T-Display-S3 (170x320 ST7789) renders through
// TFT_eSPI; the T-Display-S3 Long (640x180 AXS15231B, QSPI) is not supported
// by TFT_eSPI at all — it goes through the shim in disp_long.h (Arduino_GFX).
// The NAS panel's flasher sets -DNAS_DISPLAY_LONG=1 for the Long.
#if NAS_DISPLAY_LONG
#include "disp_long.h"
#define dispFlush() tft.flush()
#else
#include <TFT_eSPI.h>
#define dispFlush()
#endif

// Types must precede the FIRST function definition: the Arduino sketch
// preprocessor inserts auto-generated prototypes there, and any prototype
// returning a later-defined struct fails ('Anchor' does not name a type).
struct Anchor { int datum, x, y; };
struct Style { const char *lp, *vp, *up; int ls, vs, us; };

static const char* WIFI_SSID = "your-wifi";
static const char* WIFI_PASS = "your-pass";
static const char* NAS_HOST  = "192.168.1.48";   // NAS IP or hostname
static const char* TOKEN     = "paste-glance-token-here";
static const char* SCREEN_ID = "";               // empty = first screen; ids are shown in the constructor

static uint32_t POLL_MS = 3000;
static uint32_t PAGE_ROTATE_MS = 15000;          // 0 = manual (button) only
#if NAS_DISPLAY_LONG
static const int BTN1 = 0, BTN2 = -1;            // Long: GPIO14/21 belong to QSPI
static const int BL_PIN = 1;                     // backlight, PWM-able
#else
static const int BTN1 = 0, BTN2 = 14;            // T-Display-S3 buttons
static const int BL_PIN = 38;
#endif

// Brightness: a touch starting at the left/right edge turns into a vertical
// slider (drag up = brighter); the level persists in NVS across reboots.
#include <Preferences.h>
Preferences PREFS;
uint8_t BRIGHT = 255;
bool NIGHT = false;              // server says "night window" -> backlight off
uint32_t wokeAt = 0;             // a touch wakes the panel for a minute
void applyBright() {
  bool asleep = NIGHT && (millis() - wokeAt > 60000UL);
  ledcWrite(BL_PIN, asleep ? 0 : BRIGHT);
}
// geometry of the on-screen "Brightness" tile (placed via the constructor);
// BR_X < 0 = tile is not on the current page
int BR_X = -1, BR_Y = 0, BR_W = 0, BR_H = 0;
uint16_t BR_bg = 0, BR_lab = 0;
bool BR_pct = false, BR_lp = false;

// ---- config from flash --------------------------------------------------
// The NAS panel (Настройки → Экран → «Прошить экран») writes a 4 KB block at
// the LAST 4 KB of the 16 MB flash: magic "NASG" + uint16 json length + JSON
// {"ssid","pass","host","token","screen","poll","rotate"}. It overrides the
// compiled constants above, so one prebuilt firmware serves any Wi-Fi/NAS —
// re-flashing the config takes seconds and does not touch the app.
#include <esp_flash.h>
static const uint32_t CFG_ADDR = 0x00FFF000;
String C_ssid = WIFI_SSID, C_pass = WIFI_PASS, C_host = NAS_HOST,
       C_token = TOKEN, C_screen = SCREEN_ID;
void loadCfg() {
  static uint8_t buf[4096];
  if (esp_flash_read(NULL, buf, CFG_ADDR, sizeof buf) != ESP_OK) return;
  if (memcmp(buf, "NASG", 4) != 0) return;
  uint16_t len = buf[4] | (buf[5] << 8);
  if (!len || len > sizeof buf - 6) return;
  JsonDocument j;
  if (deserializeJson(j, (const char*)buf + 6, len)) return;
  if (j["ssid"].is<const char*>())   C_ssid   = j["ssid"].as<const char*>();
  if (j["pass"].is<const char*>())   C_pass   = j["pass"].as<const char*>();
  if (j["host"].is<const char*>())   C_host   = j["host"].as<const char*>();
  if (j["token"].is<const char*>())  C_token  = j["token"].as<const char*>();
  if (j["screen"].is<const char*>()) C_screen = j["screen"].as<const char*>();
  if (j["poll"].is<uint32_t>())      POLL_MS  = max((uint32_t)1000, j["poll"].as<uint32_t>());
  if (j["rotate"].is<uint32_t>())    PAGE_ROTATE_MS = j["rotate"].as<uint32_t>();
  Serial.println("config loaded from flash");
}

// Touch variant (T-Display-S3 Touch / Long): swipe left/right flips pages.
// Set to 0 for boards without a touch panel. CST816 on SDA=18 SCL=17 RST=21.
#define USE_TOUCH 1
#define TOUCH_DEBUG 1        // heartbeat of raw I2C frames to Serial

#if USE_TOUCH
#include <Wire.h>
#if NAS_DISPLAY_LONG
// AXS15231B: the touch controller is BUILT INTO the display chip (I2C 0x3B,
// SDA=15 SCL=10, INT=11) and shares GPIO16 as reset with the display.
// This block follows LilyGO's Arduino_GFX example byte for byte — every
// deviation cost a debugging round:
//   * reset: HIGH 2ms, LOW **100ms**, HIGH 2ms, and it must run BEFORE the
//     panel init (so the display driver gets RST=GFX_NOT_DEFINED);
//   * read: write the 11-byte command, read back 8 bytes;
//   * a frame is valid ONLY when INT fired — free-running polls return a
//     constant 0x03 filler, which is exactly what "touch does not work"
//     looked like from the outside;
//   * layout: b[1] = fingers, b[2]>>4 = event (0x08 = down),
//     long axis = b[2..3], short axis = b[4..5].
static const int TP_SDA = 15, TP_SCL = 10, TP_RST = 16, TP_INT = 11;
static const uint8_t TP_ADDR = 0x3B;
volatile bool TP_IRQ = false;
void IRAM_ATTR tpISR() { TP_IRQ = true; }
void tpReset() {
  pinMode(TP_INT, INPUT_PULLUP);
  attachInterrupt(TP_INT, tpISR, FALLING);
  pinMode(TP_RST, OUTPUT);
  digitalWrite(TP_RST, HIGH); delay(2);
  digitalWrite(TP_RST, LOW);  delay(100);
  digitalWrite(TP_RST, HIGH); delay(2);
  Wire.begin(TP_SDA, TP_SCL);
  Wire.setTimeOut(50);
}
void touchInit() {
  Wire.setTimeOut(50);
  if (TOUCH_DEBUG) {                       // who is actually on this bus?
    String found = "";
    for (uint8_t a = 1; a < 127; a++) {
      Wire.beginTransmission(a);
      if (Wire.endTransmission() == 0) found += " 0x" + String(a, HEX);
    }
    Serial.println("i2c scan:" + (found.length() ? found : String(" nothing")));
  }
}
bool touchRead(int &x, int &y) {
  // read only when the chip says there is something to read
  bool irq = TP_IRQ, low = digitalRead(TP_INT) == LOW;
  if (!irq && !low) return false;
  TP_IRQ = false;
  static const uint8_t cmd[11] = {0xB5, 0xAB, 0xA5, 0x5A, 0, 0, 0, 0x08, 0, 0, 0};
  Wire.beginTransmission(TP_ADDR);
  Wire.write(cmd, sizeof cmd);
  if (Wire.endTransmission() != 0) return false;
  if (Wire.requestFrom((int)TP_ADDR, 8) < 8) return false;
  uint8_t b[8] = {0};
  for (int i = 0; i < 8; i++) b[i] = Wire.read();
  if (TOUCH_DEBUG) {
    static uint32_t logAt = 0;
    if (millis() - logAt > 400) {
      logAt = millis();
      Serial.printf("touch frame %02x %02x %02x %02x %02x %02x\n",
                    b[0], b[1], b[2], b[3], b[4], b[5]);
    }
  }
  uint8_t fingers = b[1], event = b[2] >> 4;
  if (fingers != 1 || event != 0x08) return false;
  y = ((b[2] & 0x0F) << 8) | b[3];         // long axis, 0..639
  x = ((b[4] & 0x0F) << 8) | b[5];         // short axis, 0..179
  return true;
}
#else
static const int TP_SDA = 18, TP_SCL = 17, TP_RST = 21;
static const uint8_t TP_ADDR = 0x15;
void touchInit() {
  pinMode(TP_RST, OUTPUT);
  digitalWrite(TP_RST, LOW); delay(10);
  digitalWrite(TP_RST, HIGH); delay(60);
  Wire.begin(TP_SDA, TP_SCL);
}
// one finger down? -> raw panel coords (portrait orientation)
bool touchRead(int &x, int &y) {
  Wire.beginTransmission(TP_ADDR);
  Wire.write(0x02);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)TP_ADDR, 5) < 5) return false;
  int n = Wire.read();
  int xh = Wire.read(), xl = Wire.read(), yh = Wire.read(), yl = Wire.read();
  if (!(n & 0x0F)) return false;
  x = ((xh & 0x0F) << 8) | xl;
  y = ((yh & 0x0F) << 8) | yl;
  return true;
}
#endif
#endif

#if NAS_DISPLAY_LONG
TFTCompat tft;
#else
TFT_eSPI tft;
#endif
JsonDocument DOC;              // last payload (kept for page redraws)
bool haveDoc = false;
long lastSeq = -1;
int page = 0;
uint32_t lastPoll = 0, lastFlip = 0, lastOkMs = 0;
uint8_t failures = 0;
bool stale = false;            // polls are failing: keep last data + red badge

// status hues come from the panel (Оформление -> «Цвета статусов»), so the
// external screens match the desktop and the wall panel instead of shouting
// in stock RGB green/yellow/red
uint16_t C_OK = TFT_GREEN, C_WARN = TFT_YELLOW, C_BAD = TFT_RED;
uint16_t hex565s(const char* h, uint16_t fb) {
  if (!h || h[0] != '#' || strlen(h) < 7) return fb;
  long v = strtol(h + 1, nullptr, 16);
  return (uint16_t)((((v >> 16) & 0xF8) << 8) | (((v >> 8) & 0xFC) << 3) | ((v & 0xFF) >> 3));
}
uint16_t stColor(const char* s) {
  if (!s) return TFT_DARKGREY;
  if (!strcmp(s, "ok"))   return C_OK;
  if (!strcmp(s, "warn")) return C_WARN;
  return C_BAD;
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
  dispFlush();
}

// The slider IS the tile: the bar fills the whole area (no label, no padding
// — a title and margins only stole the thing you actually grab). Label / "%"
// appear only when the inspector explicitly asks for them.
void drawBrightBody() {
  if (BR_X < 0) return;
  int tx = BR_X, ty = BR_Y + (BR_lp ? 20 : 0), tw = BR_W, th = BR_H - (BR_lp ? 20 : 0);
  if (th < 8) { ty = BR_Y; th = BR_H; }
  int r = tw < th ? tw / 4 : th / 4; if (r > 10) r = 10;
  tft.fillRoundRect(tx, ty, tw, th, r, 0x18E3);
  int fill = th * BRIGHT / 255;
  if (fill < 4) fill = 4;
  tft.fillRoundRect(tx, ty + th - fill, tw, fill, r, TFT_YELLOW);
  if (BR_pct) {
    String pc = String((BRIGHT * 100 + 127) / 255) + "%";
    tft.setTextDatum(MC_DATUM);
    tft.setTextColor(TFT_BLACK, TFT_YELLOW);
    tft.drawString(pc, tx + tw / 2, ty + th - fill / 2, 12);
    tft.setTextDatum(ML_DATUM);
  }
  dispFlush();
}

void drawOffline(const char* why) {
  tft.fillScreen(TFT_BLACK);
  tft.setTextDatum(MC_DATUM);
  tft.setTextColor(TFT_RED, TFT_BLACK);
  tft.drawString("NAS UNREACHABLE", tft.width() / 2, tft.height() / 2 - 10, 4);
  tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
  tft.drawString(why, tft.width() / 2, tft.height() / 2 + 20, 2);
  tft.setTextDatum(ML_DATUM);
  dispFlush();
}

void drawSparkC(JsonArray sp, int x, int y, int w, int h, uint16_t col) {
  int m = sp.size();
  if (m < 2) return;
  float mn = 1e30, mx = -1e30;
  for (int k = 0; k < m; k++) { float v = sp[k].as<float>(); if (v < mn) mn = v; if (v > mx) mx = v; }
  float span = (mx - mn) > 0 ? (mx - mn) : 1;
  int px = -1, py = -1;
  for (int k = 0; k < m; k++) {
    int gx = x + k * (w - 1) / (m - 1);
    int gy = y + h - 1 - (int)((sp[k].as<float>() - mn) / span * (h - 1));
    if (px >= 0) {
      tft.drawLine(px, py, gx, gy, col);
      tft.drawLine(px, py + 1, gx, gy + 1, col);   // 2px line reads better
    }
    px = gx; py = gy;
  }
}
void drawSpark(JsonArray sp, int x, int y, int w, int h) {
  drawSparkC(sp, x, y, w, h, TFT_DARKGREY);
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

// requested font-size (device px) -> font handle. The Long passes pixels
// straight through (its shim has ten font sizes — matches the constructor);
// the classic quantizes to TFT_eSPI's built-in fonts.
int pickFont(int px, const String &s) {
#if NAS_DISPLAY_LONG
  return px < 10 ? 10 : (px > 64 ? 64 : px);
#else
  if (px > 33) {  // font 6 is digits-only
    bool dig = true;
    for (unsigned i = 0; i < s.length() && dig; i++) dig = strchr("0123456789.-", s[i]);
    return dig ? 6 : 4;
  }
  return px > 17 ? 4 : 2;
#endif
}

// 9-grid position ("tl".."br") -> TFT datum + anchor point inside the tile
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
Style defStyle(const char* size) {
  if (!strcmp(size, "s")) return {"cl", "cr", "val", 10, 12, 9};
  if (!strcmp(size, "l")) return {"tl", "c",  "val", 11, 26, 11};
  return {"tl", "c", "val", 10, 17, 10};
}

void tileDot(JsonObject st, int x, int y, int w, const char* state) {
  if (st["nd"] | 0) return;                  // toggled off in the inspector
  tft.fillCircle(x + w - 9, y + 8, 3, stColor(state));
}
// extra caption: the tile's "note" (host name, profile, hottest disk...) at a
// 9-grid position of its own; hidden unless the inspector places it
void tileNote(JsonObject t, int x, int y, int w, int h, uint16_t c, uint16_t tbg) {
  JsonObject st = t["st"];
  const char* np = st["np"] | "hide";
  const char* note = t["note"] | "";
  if (!strcmp(np, "hide") || !note[0]) return;
  Anchor a = posAnchor(np, x, y, w, h);
  tft.setTextDatum(a.datum);
  tft.setTextColor(c, tbg);
  tft.drawString(note, a.x, a.y, pickFont(st["ls"] | 10, ""));
  tft.setTextDatum(ML_DATUM);
}

// Tile representation ("k" style key, set in the constructor's inspector):
// value = classic text, gauge = semicircle meter, bars = uptime-kuma history
// strip, spark = big chart. Auto: bars when the tile carries raw.bars
// (availability, watched hosts), else value.
const char* tileKind(JsonObject t) {
  const char* k = t["st"]["k"] | "";
  if (k[0]) return k;
  if (t["raw"]["bars"].is<JsonArray>()) return "bars";
  return "value";
}

float tilePct(JsonObject t) {                         // 0..100 for gauges
  JsonObject raw = t["raw"];
  if (raw["pct"].is<float>()) return raw["pct"].as<float>();
  if (raw["c"].is<float>())   return raw["c"].as<float>();   // temperature ≈ %
  return String(t["value"] | "").toFloat();
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
  const char* state = t["state"] | "ok";
  // a colour screen should USE colour: the value inherits the state colour
  // when the tile is unwell, unless the user pinned an explicit colour
  if (!st["vc"].is<const char*>() && strcmp(state, "ok"))
    valC = stColor(state);
  const char* kind = tileKind(t);

  if (!strcmp(t["id"] | "", "bright")) {
    // the slider tile: server sends an empty shell, the device owns the value
    BR_X = x; BR_Y = y; BR_W = w; BR_H = h; BR_bg = tbg; BR_lab = labC;
    // chrome is OPT-IN: only an explicit inspector choice brings it back
    BR_lp = st["lp"].is<const char*>() && strcmp(st["lp"] | "hide", "hide");
    BR_pct = st["vp"].is<const char*>() && strcmp(st["vp"] | "hide", "hide");
    if (BR_lp) {
      tft.setTextDatum(TL_DATUM);
      tft.setTextColor(labC, tbg);
      tft.drawString(t["label"] | "", x + 4, y + 2, pickFont(ls, ""));
    }
    drawBrightBody();
    tft.setTextDatum(ML_DATUM);
    return;
  }

  JsonObject raw = t["raw"];
  if (raw["rxh"].is<const char*>() && raw["txh"].is<const char*>()) {
    // network up/down: two rows, download green / upload red, no arrow glyphs
    if (strcmp(lp, "hide")) {
      tft.setTextDatum(TL_DATUM); tft.setTextColor(labC, tbg);
      tft.drawString(t["label"] | "", x + 7, y + 4, pickFont(ls, ""));
    }
    if (strcmp(vp, "hide")) {
      String rx = String(raw["rxh"] | ""), txs = String(raw["txh"] | "");
      int f = pickFont(vs, rx);
      int cy = y + h / 2 + (strcmp(lp, "hide") ? 0 : 8);
      int gapw = 10;
      // ONE line while it fits (that is what a wide tile is for); only a tile
      // too narrow for both values falls back to two rows
      bool one = tft.textWidth(rx, f) + gapw + tft.textWidth(txs, f) <= w - 12
                 && tft.fontHeight(f) <= h - (strcmp(lp, "hide") ? 20 : 4);
      if (one) {
        int wr = tft.textWidth(rx, f), wt = tft.textWidth(txs, f);
        int lx = x + (w - (wr + gapw + wt)) / 2;
        tft.setTextDatum(ML_DATUM);
        tft.setTextColor(C_OK, tbg);
        tft.drawString(rx, lx, cy, f);
        tft.setTextColor(C_BAD, tbg);
        tft.drawString(txs, lx + wr + gapw, cy, f);
      } else {
        for (int guard = 0; guard < 8 && f > 10; guard++) {   // shrink to fit
          int wid = max(tft.textWidth(rx, f), tft.textWidth(txs, f));
          if (wid <= w - 12 && tft.fontHeight(f) * 2 + 4 <= h - (strcmp(lp, "hide") ? 20 : 4)) break;
          f = pickFont(f - 3, rx);
        }
        tft.setTextDatum(MC_DATUM);
        tft.setTextColor(C_OK, tbg);
        tft.drawString(rx, x + w / 2, cy - tft.fontHeight(f) / 2 - 1, f);
        tft.setTextColor(C_BAD, tbg);
        tft.drawString(txs, x + w / 2, cy + tft.fontHeight(f) / 2 + 1, f);
      }
    }
    tft.setTextDatum(ML_DATUM);
    tileNote(t, x, y, w, h, uniC, tbg);
    tileDot(st, x, y, w, state);
    return;
  }

  if (!strcmp(kind, "bars")) {
    // uptime-kuma style: label left, value right, history bars fill the body
    if (strcmp(lp, "hide")) {
      tft.setTextDatum(TL_DATUM); tft.setTextColor(labC, tbg);
      tft.drawString(t["label"] | "", x + 7, y + 4, pickFont(ls, ""));
    }
    if (strcmp(vp, "hide")) {
      String v = String(t["value"] | "");
      if ((t["unit"] | (const char*)"")[0]) v += " " + String(t["unit"] | "");
      tft.setTextDatum(TR_DATUM); tft.setTextColor(valC, tbg);
      tft.drawString(v, x + w - 16, y + 4, pickFont(ls, v));
    }
    JsonArray bars = t["raw"]["bars"];
    int n = bars.size();
    if (n) {
      int by = y + 22, bh2 = h - 29;
      if (bh2 < 8) { by = y + h - 13; bh2 = 9; }
      for (int b = 0; b < n; b++) {
        int xa = x + 7 + b * (w - 14) / n, xb = x + 7 + (b + 1) * (w - 14) / n;
        int v2 = bars[b].as<int>();
        uint16_t c = v2 == 2 ? C_OK : (v2 == 1 ? C_WARN : (v2 == 0 ? C_BAD : 0x39E7));
        int w2 = xb - xa - 2; if (w2 < 1) w2 = 1;
        tft.fillRoundRect(xa, by, w2, bh2, w2 > 4 ? 2 : 0, c);
      }
    }
    tft.setTextDatum(ML_DATUM);
    tileNote(t, x, y, w, h, uniC, tbg);
    tileDot(st, x, y, w, state);
    return;
  }

  if (!strcmp(kind, "gauge")) {
    // semicircle meter like the wall screen's speedometers
    float pct = tilePct(t);
    if (pct < 0) pct = 0; if (pct > 100) pct = 100;
    int cx = x + w / 2, cy = y + h - 12;
    int R = (w - 24) / 2; if (R > h - 26) R = h - 26; if (R < 14) R = 14;
    int thick = R / 4 + 2;
    for (int a = 0; a <= 180; a += 2) {
      float rad = a * 3.14159f / 180.0f;
      int px = cx - (int)(cosf(rad) * (R - thick / 2));
      int py = cy - (int)(sinf(rad) * (R - thick / 2));
      bool lit = a <= pct * 1.8f;
      tft.fillCircle(px, py, thick / 2, lit ? stColor(state) : 0x2965);
    }
    String val = String(t["value"] | "");
    String unit = String(t["unit"] | "");
    if (strcmp(vp, "hide")) {
      tft.setTextDatum(BC_DATUM); tft.setTextColor(valC, tbg);
      // value + unit glued right after it, small and dim — no corner clutter
      if (unit.length() && strcmp(up, "hide")) {
        int vf = pickFont(vs, val), uf = pickFont(us, unit);
        int tot = tft.textWidth(val, vf) + 4 + tft.textWidth(unit, uf);
        tft.setTextDatum(BL_DATUM);
        tft.drawString(val, cx - tot / 2, cy + 1, vf);
        tft.setTextColor(uniC, tbg);
        tft.drawString(unit, cx - tot / 2 + tft.textWidth(val, vf) + 4, cy + 1, uf);
      } else {
        tft.drawString(val, cx, cy + 1, pickFont(vs, val));
      }
    }
    if (strcmp(lp, "hide")) {
      tft.setTextDatum(TL_DATUM); tft.setTextColor(labC, tbg);
      tft.drawString(t["label"] | "", x + 7, y + 4, pickFont(ls, ""));
    }
    tft.setTextDatum(ML_DATUM);
    tileNote(t, x, y, w, h, uniC, tbg);
    tileDot(st, x, y, w, state);
    return;
  }

  if (!strcmp(kind, "spark") && t["spark"].size() >= 2) {
    // big chart: header row + the whole body is the graph
    if (strcmp(lp, "hide")) {
      tft.setTextDatum(TL_DATUM); tft.setTextColor(labC, tbg);
      tft.drawString(t["label"] | "", x + 7, y + 4, pickFont(ls, ""));
    }
    if (strcmp(vp, "hide")) {
      String v = String(t["value"] | "") + " " + String(t["unit"] | "");
      tft.setTextDatum(TR_DATUM); tft.setTextColor(valC, tbg);
      tft.drawString(v, x + w - 16, y + 4, pickFont(ls, v));
    }
    drawSparkC(t["spark"], x + 7, y + 22, w - 14, h - 29, stColor(state));
    tft.setTextDatum(ML_DATUM);
    tileNote(t, x, y, w, h, uniC, tbg);
    tileDot(st, x, y, w, state);
    return;
  }
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
  tileNote(t, x, y, w, h, uniC, tbg);
  tileDot(st, x, y, w, state);
  JsonArray sp = t["spark"];
  if (sp.size() >= 2 && h >= 52) drawSpark(sp, x + 8, y + h - 16, w - 16, 11);
}

void drawAvail(JsonObject avail, int y) {
  JsonArray bars = avail["bars"];
  int n = bars.size();
  if (!n) return;
  // spread the remainder across slots — width/n floored left a bald stripe
  // on the right (640/48 loses 16 px, 640/96 loses a whole 64)
  for (int b = 0; b < n; b++) {
    int xa = b * tft.width() / n, xb = (b + 1) * tft.width() / n;
    int v = bars[b].as<int>();
    uint16_t c = v == 2 ? C_OK : (v == 1 ? C_WARN : (v == 0 ? C_BAD : TFT_DARKGREY));
    tft.fillRect(xa, y, xb - xa - 1, 10, c);
  }
}

void drawPage() {
  if (!haveDoc) return;
  JsonArray pages = DOC["pages"];
  if (!pages.size()) return;
  if (page >= (int)pages.size()) page = 0;
  JsonObject pg = pages[page];

  tft.fillScreen(TFT_BLACK);
  BR_X = -1;                       // re-registered by the tile if it is drawn
  // header: status dot, host, page name + dots
  // wide panels (Long 640px): a bold f4 host name read as a billboard —
  // slimmer f2 leaves the space to the tiles
  int hf = tft.width() >= 600 ? 2 : 4;
  tft.fillCircle(12, 13, tft.width() >= 600 ? 5 : 7, stColor(DOC["status"] | "warn"));
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.drawString(DOC["host"] | "NAS", 26, 13, hf);
  tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
  String pname = pg["name"] | "";
  if (pages.size() > 1) {
    for (int i = 0; i < (int)pages.size(); i++)
      tft.fillCircle(tft.width() - 12 - i * 15, 13, 4, i == page ? TFT_WHITE : TFT_DARKGREY);
    tft.drawString(pname, tft.width() - 18 - pages.size() * 15 - tft.textWidth(pname, 2), 13, 2);
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
  if ((scrInfo["avail"] | true) && !avail.isNull()) drawAvail(avail, tft.height() - 12);
  dispFlush();
}

void poll() {
  HTTPClient http;
  String url = String("http://") + C_host + "/api/glance?token=" + C_token +
               "&lang=en&seq=" + String(lastSeq);
  if (C_screen.length()) url += String("&screen=") + C_screen;
  http.setTimeout(4000);
  http.begin(url);
  int code = http.GET();
  static int lastCode = -1;                 // log only transitions, not every poll
  if (code != lastCode) { lastCode = code; Serial.println("poll HTTP " + String(code)); }
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
  JsonObject col = DOC["colors"];
  if (!col.isNull()) {
    C_OK   = hex565s(col["ok"]     | "", TFT_GREEN);
    C_WARN = hex565s(col["warn"]   | "", TFT_YELLOW);
    C_BAD  = hex565s(col["danger"] | "", TFT_RED);
  }
  bool n = DOC["night"] | false;
  if (n != NIGHT) { NIGHT = n; applyBright(); }
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
#if !NAS_DISPLAY_LONG
  // T-Display-S3: GPIO15 is PWR_EN of the LCD rail — without driving it HIGH
  // the panel stays BLACK while the firmware runs fine (wifi, polls, serial).
  // On the Long GPIO15 is the touch SDA — must be left alone.
  pinMode(15, OUTPUT);
  digitalWrite(15, HIGH);
#endif
  Serial.begin(115200);
  loadCfg();                                // flash-config beats compiled defaults
#if NAS_DISPLAY_LONG && USE_TOUCH
  tpReset();                                // reset + I2C BEFORE the panel init
#endif
  pinMode(BTN1, INPUT_PULLUP);
  if (BTN2 >= 0) pinMode(BTN2, INPUT_PULLUP);
  tft.init();
  PREFS.begin("glance", false);
  BRIGHT = PREFS.getUChar("br", 255);
  ledcAttach(BL_PIN, 5000, 8);               // takes over the BL pin as PWM
  applyBright();
  tft.setRotation(1);                       // landscape; use 0 for portrait
  tft.fillScreen(TFT_BLACK);
  tft.setTextDatum(ML_DATUM);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.drawString("connecting...", 10, tft.height() / 2, 2);
  dispFlush();
  Serial.println("display up " + String(tft.width()) + "x" + String(tft.height())
                 + ", wifi ssid=" + C_ssid);
  Serial.println("wifi mode...");
  WiFi.mode(WIFI_STA);
  Serial.println("wifi begin...");
  WiFi.begin(C_ssid.c_str(), C_pass.c_str());
  Serial.println("touch init...");
#if USE_TOUCH
  touchInit();
#endif
  Serial.println("setup done");
}

void loop() {
  static int b1 = HIGH, b2 = HIGH;
  int n1 = digitalRead(BTN1), n2 = BTN2 >= 0 ? digitalRead(BTN2) : HIGH;
  if (b1 == HIGH && n1 == LOW) flipPage(+1);
  if (b2 == HIGH && n2 == LOW) flipPage(-1);
  b1 = n1; b2 = n2;

#if USE_TOUCH
  // touch: inside the "Brightness" tile = slider, otherwise swipe flips pages
  static bool touching = false, briMode = false;
  static int tx0 = 0, ty0 = 0, tx = 0, ty = 0;
  static uint32_t touchT0 = 0;
  int px, py;
  if (touchRead(px, py)) {
    // Raw touch is panel-portrait; which way it maps onto our landscape frame
    // is a per-revision coin toss (guessing it cost two failed builds). So we
    // do not guess: on finger-down we test all four flips and keep the one
    // that lands inside the Brightness tile, then reuse it for the drag.
    static uint8_t map_ = 0;             // bit0: mirror long, bit1: mirror short
    auto mapX = [&](uint8_t m) { return (m & 1) ? tft.width() - 1 - py : py; };
    auto mapY = [&](uint8_t m) { return (m & 2) ? tft.height() - 1 - px : px; };
    if (!touching) {
      touching = true; tx0 = px; ty0 = py; touchT0 = millis();
      briMode = false;
      if (BR_X >= 0) {
        for (uint8_t m = 0; m < 4; m++) {
          int lx = mapX(m), ly = mapY(m);
          if (lx >= BR_X && lx <= BR_X + BR_W && ly >= BR_Y && ly <= BR_Y + BR_H) {
            map_ = m; briMode = true; break;
          }
        }
      }
      Serial.println("touch raw " + String(px) + "," + String(py)
                     + (briMode ? " -> slider map " + String(map_) : ""));
    }
    if (millis() - touchT0 > 20000) {    // ghost touch: controller reports a
      touching = false; briMode = false; // finger forever -> release the fuse
    }
    tx = px; ty = py;
    wokeAt = millis(); applyBright();    // ночью касание будит подсветку
    if (briMode) {
      int ly = mapY(map_);
      int lvl = (BR_Y + BR_H - ly) * 255 / (BR_H > 0 ? BR_H : 1);
      BRIGHT = lvl < 8 ? 8 : (lvl > 255 ? 255 : lvl);
      applyBright();
      drawBrightBody();                  // partial redraw: no page repaint
    }
  } else if (touching) {
    touching = false;
    if (briMode) {
      briMode = false;
      PREFS.putUChar("br", BRIGHT);
    } else {
      int dl = ty - ty0;                 // raw long axis, direction as before
      if (abs(dl) > 50) flipPage(dl < 0 ? +1 : -1);
    }
  }
  // only an ACTIVE slider drag pauses polling (a repaint mid-drag yanked the
  // slider); a plain touch or a ghost point must not starve the data loop
  if (touching && briMode) { delay(30); return; }
  if (touching && PAGE_ROTATE_MS) lastFlip = millis();   // no auto-flip mid-touch
#endif

  if (WiFi.status() != WL_CONNECTED) {
    static uint32_t lostAt = 0;
    static uint32_t logAt = 0;
    if (!lostAt) lostAt = millis();
    if (millis() - logAt > 3000) { logAt = millis(); Serial.println("wifi status " + String(WiFi.status())); }
    if (millis() - lostAt > 15000) { drawOffline("wifi lost"); lostAt = millis(); WiFi.reconnect(); }
    delay(100);
    return;
  }
  static bool up = false;
  if (!up) { up = true; Serial.println("wifi up, ip " + WiFi.localIP().toString()); }
  if (PAGE_ROTATE_MS && haveDoc && millis() - lastFlip >= PAGE_ROTATE_MS) flipPage(+1);
  if (millis() - lastPoll >= POLL_MS || !lastPoll) { lastPoll = millis(); poll(); }
  delay(30);
}
