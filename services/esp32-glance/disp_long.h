// TFT_eSPI-compatible shim for the LilyGO T-Display-S3 Long (640x180,
// AXS15231B over QSPI). TFT_eSPI cannot drive this controller at all; the
// sketch however is written against TFT_eSPI's API, so this class implements
// exactly the subset it uses on top of an Arduino_GFX full-frame canvas
// (framebuffer lives in PSRAM, pushed to the panel by an explicit flush()).
// Selected with -DNAS_DISPLAY_LONG=1 (the panel's flasher sets it for the
// "T-Display-S3 Long" board choice); the classic build keeps real TFT_eSPI.
#pragma once
#include <Arduino_GFX_Library.h>

// TFT_eSPI text-datum codes (posAnchor's 3x3 grid relies on this numbering)
enum { TL_DATUM = 0, TC_DATUM, TR_DATUM,
       ML_DATUM,     MC_DATUM, MR_DATUM,
       BL_DATUM,     BC_DATUM, BR_DATUM };

#define TFT_BLACK    0x0000
#define TFT_WHITE    0xFFFF
#define TFT_GREEN    0x07E0
#define TFT_YELLOW   0xFFE0
#define TFT_RED      0xF800
#define TFT_DARKGREY 0x7BEF

class TFTCompat {
public:
  void init() {
    pinMode(1, OUTPUT);                     // TFT_BL of the Long
    digitalWrite(1, HIGH);
    bus = new Arduino_ESP32QSPI(12 /*CS*/, 17 /*SCK*/, 13, 18, 21, 14 /*D0-D3*/);
    // RST is 47 (per the library's own LILYGO_T_Display_S3_LONG dev profile);
    // GPIO16 from LilyGO's pins_config is the TOUCH reset — pulsing it here
    // left the touch controller dead
    panel = new Arduino_AXS15231B(bus, 47 /*RST*/, 0, false, 180, 640);
    // ctor takes the PANEL-NATIVE dims (that exact bitmap goes out on flush);
    // rotation=1 remaps DRAWING coords, so width()/height() report 640x180.
    // Passing rotated dims here pushes a 640-wide frame into a 180-wide panel
    // — the screen shows one strip of colored noise (been there).
    gfx = new Arduino_Canvas(180, 640, panel, 0, 0, 1);
    gfx->begin(32000000);                   // >32 MHz corrupts this panel
    gfx->fillScreen(TFT_BLACK);
    gfx->flush();
  }
  void setRotation(uint8_t) {}              // landscape is fixed in the ctor
  uint8_t getRotation() { return 1; }
  int16_t width()  { return gfx->width(); }
  int16_t height() { return gfx->height(); }
  void fillScreen(uint16_t c) { gfx->fillScreen(c); dirty = true; }
  void fillRect(int32_t x, int32_t y, int32_t w, int32_t h, uint16_t c) {
    gfx->fillRect(x, y, w, h, c); dirty = true; }
  void fillRoundRect(int32_t x, int32_t y, int32_t w, int32_t h, int32_t r, uint16_t c) {
    gfx->fillRoundRect(x, y, w, h, r, c); dirty = true; }
  void drawRoundRect(int32_t x, int32_t y, int32_t w, int32_t h, int32_t r, uint16_t c) {
    gfx->drawRoundRect(x, y, w, h, r, c); dirty = true; }
  void drawLine(int32_t x0, int32_t y0, int32_t x1, int32_t y1, uint16_t c) {
    gfx->drawLine(x0, y0, x1, y1, c); dirty = true; }
  void fillCircle(int32_t x, int32_t y, int32_t r, uint16_t c) {
    gfx->fillCircle(x, y, r, c); dirty = true; }
  uint16_t color565(uint8_t r, uint8_t g, uint8_t b) {
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3); }
  void setTextDatum(uint8_t d) { datum = d; }
  void setTextColor(uint16_t fg, uint16_t bg) { tfg = fg; tbg = bg; }
  // TFT_eSPI font number -> classic 6x8 font scale: 1=8px 2=16 4=24 6/7=48 8=72
  int fscale(int f) { return f >= 8 ? 9 : f >= 6 ? 6 : f >= 4 ? 3 : f >= 2 ? 2 : 1; }
  int16_t fontHeight(int f) { return 8 * fscale(f); }
  int16_t textWidth(const String &s, int f) { return 6 * fscale(f) * s.length(); }
  void drawString(const String &s, int32_t x, int32_t y, int f) {
    int sc = fscale(f), w = textWidth(s, f), h = 8 * sc;
    int px = datum % 3 == 1 ? x - w / 2 : (datum % 3 == 2 ? x - w : x);
    int py = datum / 3 == 1 ? y - h / 2 : (datum / 3 == 2 ? y - h : y);
    if (tbg != tfg) gfx->fillRect(px, py, w, h, tbg);   // TFT_eSPI paints text bg
    gfx->setTextSize(sc);
    gfx->setTextColor(tfg);
    gfx->setCursor(px, py);
    gfx->print(s);
    dirty = true;
  }
  void flush() { if (dirty) { gfx->flush(); dirty = false; } }
  Arduino_Canvas *gfx = nullptr;
private:
  Arduino_DataBus *bus = nullptr;
  Arduino_GFX *panel = nullptr;
  uint8_t datum = ML_DATUM;
  uint16_t tfg = TFT_WHITE, tbg = TFT_BLACK;
  bool dirty = false;
};
