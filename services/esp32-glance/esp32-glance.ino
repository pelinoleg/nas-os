// NAS-OS glance display for ESP32 + TFT (TFT_eSPI: ST7789/ILI9341, 240x320).
// Polls /api/glance on the NAS and renders: overall status, tiles grid,
// 24h availability bars. The server decides WHAT to show (settings tab
// "Экран" in the NAS panel) — reflash is never needed to change tiles.
//
// Libraries: TFT_eSPI (configure User_Setup.h for your panel), ArduinoJson 7.
// Fill in the four constants below; token comes from the settings tab.

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <TFT_eSPI.h>

static const char* WIFI_SSID = "your-wifi";
static const char* WIFI_PASS = "your-pass";
static const char* NAS_HOST  = "192.168.1.48";   // NAS IP or hostname
static const char* TOKEN     = "paste-glance-token-here";

static const uint32_t POLL_MS = 5000;

TFT_eSPI tft;
long lastSeq = -1;
uint32_t lastPoll = 0;
uint8_t failures = 0;

// state -> color
uint16_t stColor(const char* s) {
  if (!s) return TFT_DARKGREY;
  if (!strcmp(s, "ok"))   return TFT_GREEN;
  if (!strcmp(s, "warn")) return TFT_YELLOW;
  return TFT_RED;
}

void drawOffline(const char* why) {
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_RED, TFT_BLACK);
  tft.setTextDatum(MC_DATUM);
  tft.drawString("NAS UNREACHABLE", tft.width() / 2, tft.height() / 2 - 10, 4);
  tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
  tft.drawString(why, tft.width() / 2, tft.height() / 2 + 20, 2);
}

void drawGlance(JsonDocument& doc) {
  tft.fillScreen(TFT_BLACK);
  const char* host = doc["host"] | "NAS";
  const char* status = doc["status"] | "warn";

  // header: hostname + status dot
  tft.fillCircle(14, 16, 8, stColor(status));
  tft.setTextDatum(ML_DATUM);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.drawString(host, 30, 16, 4);

  // first problem line (if any)
  JsonArray problems = doc["problems"];
  int y = 34;
  if (problems.size()) {
    tft.setTextColor(TFT_ORANGE, TFT_BLACK);
    tft.drawString(problems[0].as<const char*>(), 8, y + 8, 2);
    y += 20;
  }

  // tiles: 2 columns
  JsonArray tiles = doc["tiles"];
  const int colW = tft.width() / 2, tileH = 44;
  int i = 0;
  for (JsonObject t : tiles) {
    int x = (i % 2) * colW, ty = y + (i / 2) * tileH;
    if (ty + tileH > tft.height() - 26) break;   // keep room for avail bars
    tft.fillCircle(x + 8, ty + 22, 4, stColor(t["state"] | "ok"));
    tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
    tft.drawString(t["label"] | "", x + 18, ty + 10, 2);
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    String val = String(t["value"] | "");
    const char* unit = t["unit"] | "";
    tft.drawString(val, x + 18, ty + 30, 4);
    tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
    tft.drawString(unit, x + 22 + tft.textWidth(val, 4), ty + 34, 2);
    // optional 24h sparkline in the tile's top-right corner
    JsonArray sp = t["spark"];
    int m = sp.size();
    if (m >= 2) {
      const int sw = 42, sh = 14, sx = x + colW - sw - 4, sy = ty + 6;
      float mn = 1e30, mx = -1e30;
      for (int k = 0; k < m; k++) { float v = sp[k].as<float>(); if (v < mn) mn = v; if (v > mx) mx = v; }
      float span = (mx - mn) > 0 ? (mx - mn) : 1;
      int px = -1, py = -1;
      for (int k = 0; k < m; k++) {
        int gx = sx + k * (sw - 1) / (m - 1);
        int gy = sy + sh - 1 - (int)((sp[k].as<float>() - mn) / span * (sh - 1));
        if (px >= 0) tft.drawLine(px, py, gx, gy, TFT_DARKGREY);
        px = gx; py = gy;
      }
    }
    i++;
  }

  // 24h availability strip: bars[] of 0=off 1=local 2=up
  JsonObject avail = doc["avail"];
  if (!avail.isNull()) {
    JsonArray bars = avail["bars"];
    int n = bars.size();
    if (n > 0) {
      int bw = tft.width() / n, by = tft.height() - 18;
      for (int b = 0; b < n; b++) {
        int v = bars[b].as<int>();
        uint16_t c = v == 2 ? TFT_GREEN : (v == 1 ? TFT_YELLOW : (v == 0 ? TFT_RED : TFT_DARKGREY));
        tft.fillRect(b * bw, by, bw - 1, 12, c);
      }
      tft.setTextDatum(MR_DATUM);
      tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
      tft.drawString(String(avail["pct24"].as<float>(), 1) + "% 24h",
                     tft.width() - 2, by - 8, 2);
      tft.setTextDatum(ML_DATUM);
    }
  }
}

void poll() {
  HTTPClient http;
  String url = String("http://") + NAS_HOST + "/api/glance?token=" + TOKEN +
               "&lang=en&seq=" + String(lastSeq);
  http.setTimeout(4000);
  http.begin(url);
  int code = http.GET();
  if (code == 304) { http.end(); failures = 0; return; }   // nothing changed
  if (code != 200) {
    http.end();
    if (++failures >= 3) drawOffline(code > 0 ? ("HTTP " + String(code)).c_str() : "no connection");
    return;
  }
  failures = 0;
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getString());
  http.end();
  if (err) return;
  long seq = doc["seq"] | 0;
  if (seq == lastSeq) return;
  lastSeq = seq;
  drawGlance(doc);
}

void setup() {
  Serial.begin(115200);
  tft.init();
  tft.setRotation(0);
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextDatum(MC_DATUM);
  tft.drawString("connecting...", tft.width() / 2, tft.height() / 2, 2);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    static uint32_t lostAt = 0;
    if (!lostAt) lostAt = millis();
    if (millis() - lostAt > 15000) { drawOffline("wifi lost"); lostAt = millis(); WiFi.reconnect(); }
    delay(200);
    return;
  }
  if (millis() - lastPoll >= POLL_MS || !lastPoll) {
    lastPoll = millis();
    poll();
  }
  delay(50);
}
