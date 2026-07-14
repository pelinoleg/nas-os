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

// LilyGO's QSPI init for this panel is FOUR commands — display off, sleep in,
// sleep out, display on — because the panel's own firmware already holds the
// full register config (their long table is only used on the parallel-SPI
// build). Arduino_GFX's stock axs15231b_180640 table reprograms dozens of
// registers instead, and with it the TOUCH half of the AXS15231B never wakes:
// it ACKs on I2C and returns a constant 0x03 filler forever. Same picture,
// working touch.
static const uint8_t axs_long_qspi_init[] = {
    BEGIN_WRITE, WRITE_COMMAND_8, 0x28, END_WRITE,   // display off
    DELAY, 20,
    BEGIN_WRITE, WRITE_COMMAND_8, 0x10, END_WRITE,   // sleep in
    BEGIN_WRITE, WRITE_COMMAND_8, 0x11, END_WRITE,   // sleep out
    DELAY, 200,
    BEGIN_WRITE, WRITE_COMMAND_8, 0x29, END_WRITE,   // display on
};

class TFTCompat {
public:
  void init() {
    pinMode(1, OUTPUT);                     // TFT_BL of the Long
    digitalWrite(1, HIGH);
    bus = new Arduino_ESP32QSPI(12 /*CS*/, 17 /*SCK*/, 13, 18, 21, 14 /*D0-D3*/);
    // GPIO16 resets the WHOLE AXS15231B — display AND touch (LilyGO lists it as
    // both TFT_QSPI_RST and TOUCH_RES). LilyGO's working example does BOTH: a
    // manual pulse before I2C (tpReset() in the sketch) AND the driver's own
    // reset inside begin() — the touch block only produces frames when the
    // panel init follows a driver-timed reset. Dropping either one leaves the
    // chip ACKing on I2C but answering a constant 0x03 filler.
    // NOTE: the touch half of this chip stayed dead (I2C ACK + constant 0x03
    // filler, INT never asserted) under the stock table, LilyGO's minimal QSPI
    // init, both I2C dialects and LilyGO's exact reset timings — so the init
    // table is NOT the culprit and we keep the stock one, which renders best.
    panel = new Arduino_AXS15231B(bus, GFX_NOT_DEFINED, 0, false, 180, 640);
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
  // Fonts. The style sizes come from the constructor, where they are CSS
  // font-size px — and CSS cap height is ~0.72 of that, while u8g2's fubNN cap
  // height IS NN. Mapping px->fubPX made the panel render visibly bigger than
  // the preview; we pick the font whose CAP height matches the CSS one, so the
  // canvas and the device finally agree. Legacy TFT_eSPI font numbers (1..8,
  // used by fixed calls) map onto the same ladder.
  static const int NF = 9;
  const uint8_t *fontAt(int i) {
    switch (i) {
      case 0: return u8g2_font_helvR10_tf;   // cap 8
      case 1: return u8g2_font_fub11_tf;
      case 2: return u8g2_font_fub14_tf;
      case 3: return u8g2_font_fub17_tf;
      case 4: return u8g2_font_fub20_tf;
      case 5: return u8g2_font_fub25_tf;
      case 6: return u8g2_font_fub30_tf;
      case 7: return u8g2_font_fub35_tf;
      default: return u8g2_font_fub42_tf;
    }
  }
  int capAt(int i)  { static const int C[NF] = {8, 11, 14, 17, 20, 25, 30, 35, 42}; return C[i]; }
  int totAt(int i)  { static const int T[NF] = {11, 14, 18, 22, 26, 32, 39, 45, 54}; return T[i]; }
  int fidx(int f) {
    int px = f;
    if (f <= 9) px = f >= 8 ? 48 : f >= 6 ? 34 : f >= 4 ? 22 : 14;  // legacy numbers
    int want = (px * 72 + 50) / 100;                                // CSS cap height
    int best = 0, bd = 9999;
    for (int i = 0; i < NF; i++) {
      int d = abs(capAt(i) - want);
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }
  const uint8_t *ufont(int f) { return fontAt(fidx(f)); }
  int asc(int f) { return capAt(fidx(f)); }
  int16_t fontHeight(int f) { return totAt(fidx(f)); }
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
