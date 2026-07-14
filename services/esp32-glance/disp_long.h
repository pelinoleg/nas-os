// TFT_eSPI-compatible shim for the LilyGO T-Display-S3 Long (640x180,
// AXS15231B over QSPI). TFT_eSPI cannot drive this controller at all; the
// sketch however is written against TFT_eSPI's API, so this class implements
// exactly the subset it uses on top of an Arduino_GFX full-frame canvas
// (framebuffer lives in PSRAM, pushed to the panel by an explicit flush()).
// Selected with -DNAS_DISPLAY_LONG=1 (the panel's flasher sets it for the
// "T-Display-S3 Long" board choice); the classic build keeps real TFT_eSPI.
#pragma once
// U8g2 must be installed: Arduino_GFX enables its u8g2-font engine (and the
// smooth Latin fonts we use below come straight from the U8g2 library) only
// when <U8g2lib.h> is present. The linker keeps just the referenced fonts.
#include <U8g2lib.h>
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
    // RST is 16 — and it resets the WHOLE AXS15231B, display and touch alike
    // (LilyGO lists it as both TFT_QSPI_RST and TOUCH_RES). Verified on the
    // board: text rendered with 16; with 47 (the library's dev profile) plus a
    // touch-side reset pulse AFTER init the panel went black — the "touch
    // reset" was wiping the display init. Only the display driver may own it.
    panel = new Arduino_AXS15231B(bus, 16 /*RST*/, 0, false, 180, 640);
    // ctor takes the PANEL-NATIVE dims (that exact bitmap goes out on flush);
    // rotation=1 remaps DRAWING coords, so width()/height() report 640x180.
    // Passing rotated dims here pushes a 640-wide frame into a 180-wide panel
    // — the screen shows one strip of colored noise (been there).
    gfx = new Arduino_Canvas(180, 640, panel, 0, 0, 1);
    gfx->begin(32000000);                   // >32 MHz corrupts this panel
    // labels arrive as UTF-8 ("60 °C", "Host · x"): without the decoder each
    // multi-byte char printed as two garbage glyphs; _tf fonts carry Latin-1
    gfx->setUTF8Print(true);
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
  // TFT_eSPI font number -> a real vector-quality u8g2 bitmap font (the classic
  // 6x8 GFX font scaled 3-9x read as Minecraft on a 640 px panel). fubNN's cap
  // height is NN px; ascent/total are fixed per font so rows don't jitter
  // between strings with and without descenders.
  const uint8_t *ufont(int f) {
    return f >= 8 ? u8g2_font_fub42_tf : f >= 6 ? u8g2_font_fub30_tf
         : f >= 4 ? u8g2_font_fub17_tf : u8g2_font_helvR10_tf;
  }
  int asc(int f) { return f >= 8 ? 42 : f >= 6 ? 30 : f >= 4 ? 17 : 10; }
  int16_t fontHeight(int f) { return f >= 8 ? 55 : f >= 6 ? 39 : f >= 4 ? 22 : 14; }
  int16_t textWidth(const String &s, int f) {
    gfx->setFont(ufont(f));
    gfx->setTextSize(1);
    int16_t bx, by; uint16_t bw, bh;
    gfx->getTextBounds(s, 0, 0, &bx, &by, &bw, &bh);
    return bw + (bx > 0 ? bx : 0);
  }
  void drawString(const String &s, int32_t x, int32_t y, int f) {
    int w = textWidth(s, f), h = fontHeight(f);
    int px = datum % 3 == 1 ? x - w / 2 : (datum % 3 == 2 ? x - w : x);
    int py = datum / 3 == 1 ? y - h / 2 : (datum / 3 == 2 ? y - h : y);
    if (tbg != tfg) gfx->fillRect(px, py, w, h, tbg);   // TFT_eSPI paints text bg
    gfx->setTextColor(tfg);
    gfx->setCursor(px, py + asc(f));                    // u8g2 draws from baseline
    gfx->print(s);
    gfx->setFont((const GFXfont *)nullptr);             // back to default for safety
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
