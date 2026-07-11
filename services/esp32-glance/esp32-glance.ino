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

static const uint32_t POLL_MS = 5000;
static const uint32_t PAGE_ROTATE_MS = 15000;    // 0 = manual (button) only
static const int BTN1 = 0, BTN2 = 14;            // T-Display-S3 buttons

TFT_eSPI tft;
JsonDocument DOC;              // last payload (kept for page redraws)
bool haveDoc = false;
long lastSeq = -1;
int page = 0;
uint32_t lastPoll = 0, lastFlip = 0;
uint8_t failures = 0;

uint16_t stColor(const char* s) {
  if (!s) return TFT_DARKGREY;
  if (!strcmp(s, "ok"))   return TFT_GREEN;
  if (!strcmp(s, "warn")) return TFT_YELLOW;
  return TFT_RED;
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

// one tile inside a w×h cell; big = double-height fonts (size "l")
void drawTile(JsonObject t, int x, int y, int w, int h, bool big) {
  tft.fillCircle(x + 8, y + h / 2, big ? 5 : 4, stColor(t["state"] | "ok"));
  tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
  tft.drawString(t["label"] | "", x + 18, y + (big ? 12 : 9), 2);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  String val = String(t["value"] | "");
  int vfont = big ? 6 : 4;
  // font 6 is digits-only in TFT_eSPI; fall back if the value has other chars
  if (vfont == 6) for (unsigned i = 0; i < val.length(); i++)
    if (!strchr("0123456789.-", val[i])) { vfont = 4; break; }
  int vy = y + (big ? h - 22 : h - 13);
  tft.drawString(val, x + 18, vy, vfont);
  tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
  tft.drawString(t["unit"] | "", x + 22 + tft.textWidth(val, vfont), vy + (big ? 8 : 4), 2);
  JsonArray sp = t["spark"];
  if (sp.size() >= 2) drawSpark(sp, x + w - 48, y + 4, 44, big ? 22 : 14);
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

  // flow layout: "l" = full row (tall), "m" = 1/cols row, "s" = same but slim.
  // wide panel (T-Display-S3 Long, 640 px) gets 3 columns, narrow gets 2.
  int cols = tft.width() >= 480 ? 3 : 2;
  int colW = tft.width() / cols;
  int y = 28, x = 0, rowH = 0;
  const int hL = 52, hM = 42, hS = 28;
  for (JsonObject t : pg["tiles"].as<JsonArray>()) {
    const char* sz = t["size"] | "m";
    bool big = !strcmp(sz, "l");
    int th = big ? hL : (!strcmp(sz, "s") ? hS : hM);
    int tw = big ? tft.width() : colW;
    if (big && x > 0) { y += rowH; x = 0; rowH = 0; }          // "l" starts a fresh row
    if (x + tw > tft.width()) { y += rowH; x = 0; rowH = 0; }
    if (y + th > tft.height() - 14) break;                      // keep room for avail strip
    drawTile(t, x, y, tw, th, big);
    x += tw; rowH = max(rowH, th);
    if (big) { y += th; x = 0; rowH = 0; }
  }
  JsonObject avail = DOC["avail"];
  if (!avail.isNull()) drawAvail(avail, tft.height() - 12);
}

void poll() {
  HTTPClient http;
  String url = String("http://") + NAS_HOST + "/api/glance?token=" + TOKEN +
               "&lang=en&seq=" + String(lastSeq);
  http.setTimeout(4000);
  http.begin(url);
  int code = http.GET();
  if (code == 304) { http.end(); failures = 0; return; }
  if (code != 200) {
    http.end();
    if (++failures >= 3) { haveDoc = false; drawOffline(code > 0 ? ("HTTP " + String(code)).c_str() : "no connection"); }
    return;
  }
  failures = 0;
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
