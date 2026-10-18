#include <Arduino.h>
#include <ArduinoJson.h>
#include <cstring>
#include <math.h>
#include <string>

// Wio Terminal must use Seeed_Arduino_LCD here. That Seeed-maintained
// display library exposes the TFT_eSPI API with Wio Terminal pin/display
// configuration already patched in.
#include <TFT_eSPI.h>
#include "rpcBLEDevice.h"
#include <BLEAdvertising.h>
#include <BLE2902.h>
#include <BLEServer.h>

TFT_eSPI tft;
TFT_eSPI &canvas = tft;

constexpr int SCREEN_W = 320;
constexpr int SCREEN_H = 240;

struct UsageRow {
  const char *name;
  char bigValue[16];   // headline number, e.g. "78%" (plan) or "13.3M" (today)
  char bigLabel[16];   // caption under the headline, e.g. "week used"
  char line1[16];      // first window label, e.g. "session" or "cost"
  char val1[16];       // first window value, e.g. "21%" or "$5.73"
  int8_t bar1;         // 0-100 progress bar; -1 = text only, no bar
  char line2[16];      // second window label, e.g. "monthly" or "7d"
  char val2[16];       // second window value
  int8_t bar2;
  uint16_t accent;
  bool claudeIcon;
};

// Claude/Codex run against OpenCode Go (not official Anthropic/OpenAI quota),
// so rows show the real Go plan snapshot + local usage.
UsageRow rows[] = {
    {"OpenCode Go", "--", "--", "5h", "--", -1, "monthly", "--", -1, 0xFBC9, true},
    {"Today", "--", "--", "cost", "--", -1, "7d", "--", -1, 0x06BF, false},
};

char footerCost[12] = "--";
char footerTokens[20] = "--";
char footerTime[10] = "--:--";

constexpr char BLE_NAME[] = "Wio AI Quota";
constexpr char SERVICE_UUID[] = "6e400001-b5a3-f393-e0a9-e50e24dcca9e";
constexpr char RX_UUID[] = "6e400002-b5a3-f393-e0a9-e50e24dcca9e";
constexpr char TX_UUID[] = "6e400003-b5a3-f393-e0a9-e50e24dcca9e";

BLEServer *bleServer = nullptr;
BLECharacteristic *txCharacteristic = nullptr;
String rxBuffer;
String pendingPayload;
String lastAppliedPayload;
bool hasPendingPayload = false;
bool bleConnected = false;
bool oldBleConnected = false;

static inline uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b) {
  return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
}

uint16_t blend565(uint16_t from, uint16_t to, uint8_t amount) {
  uint8_t r1 = ((from >> 11) & 0x1F) << 3;
  uint8_t g1 = ((from >> 5) & 0x3F) << 2;
  uint8_t b1 = (from & 0x1F) << 3;
  uint8_t r2 = ((to >> 11) & 0x1F) << 3;
  uint8_t g2 = ((to >> 5) & 0x3F) << 2;
  uint8_t b2 = (to & 0x1F) << 3;

  uint8_t r = r1 + ((static_cast<int>(r2) - r1) * amount) / 255;
  uint8_t g = g1 + ((static_cast<int>(g2) - g1) * amount) / 255;
  uint8_t b = b1 + ((static_cast<int>(b2) - b1) * amount) / 255;
  return rgb565(r, g, b);
}

const uint16_t C_BG_TOP = rgb565(9, 20, 27);
const uint16_t C_BG_BOTTOM = rgb565(0, 4, 8);
const uint16_t C_TEXT = rgb565(228, 239, 255);
const uint16_t C_MUTED = rgb565(159, 185, 206);
const uint16_t C_TRACK = rgb565(20, 35, 48);
const uint16_t C_TRACK_EDGE = rgb565(41, 68, 91);
const uint16_t C_FOOTER_BG = rgb565(0, 8, 13);
const uint16_t C_RULE = rgb565(38, 60, 76);

uint16_t backgroundColorAt(int y) {
  float t = static_cast<float>(y) / (SCREEN_H - 1);
  float topMix = max(0.0f, 1.0f - (t / 0.58f)) * 0.55f;
  uint8_t r = 0 * (1.0f - topMix) + 18 * topMix;
  uint8_t g = 4 * (1.0f - topMix) + 38 * topMix;
  uint8_t b = 8 * (1.0f - topMix) + 48 * topMix;
  return rgb565(r, g, b);
}

void debugLog(const String &message) {
  Serial.println(String("[AI_QUOTA] ") + message);
}

void notifyDebug(const String &message) {
  debugLog(message);
  if (!bleConnected || txCharacteristic == nullptr) {
    return;
  }

  String line = message + "\n";
  txCharacteristic->setValue(reinterpret_cast<uint8_t *>(const_cast<char *>(line.c_str())), line.length());
  txCharacteristic->notify();
}

void copyText(char *dest, size_t destSize, const char *src) {
  if (destSize == 0) {
    return;
  }
  if (src == nullptr) {
    src = "--";
  }
  strncpy(dest, src, destSize - 1);
  dest[destSize - 1] = '\0';
}

uint8_t readPercent(JsonVariantConst value, uint8_t fallback) {
  if (value.isNull()) {
    return fallback;
  }
  int pct = value.as<int>();
  if (pct < 0) {
    return 0;
  }
  if (pct > 100) {
    return 100;
  }
  return static_cast<uint8_t>(pct);
}

void updateWindow(JsonVariantConst source, char *label, size_t labelSize, char *val, size_t valSize, int8_t &bar) {
  copyText(label, labelSize, source["n"] | "--");
  copyText(val, valSize, source["v"] | "--");
  int pct = source["b"] | -1;
  bar = (pct >= 0 && pct <= 100) ? static_cast<int8_t>(pct) : -1;
}

void updateRowFromJson(UsageRow &row, JsonVariantConst source) {
  copyText(row.bigValue, sizeof(row.bigValue), source["v"] | "--");
  copyText(row.bigLabel, sizeof(row.bigLabel), source["l"] | "--");
  updateWindow(source["a"], row.line1, sizeof(row.line1), row.val1, sizeof(row.val1), row.bar1);
  updateWindow(source["b"], row.line2, sizeof(row.line2), row.val2, sizeof(row.val2), row.bar2);
}

bool applyQuotaPayload(const String &payload) {
  debugLog(String("parse payload len=") + payload.length());
  StaticJsonDocument<1536> doc;
  DeserializationError error = deserializeJson(doc, payload);
  if (error) {
    notifyDebug(String("JSON_ERROR ") + error.c_str());
    return false;
  }

  JsonVariantConst footer = doc["f"];
  copyText(footerCost, sizeof(footerCost), footer["c"] | "--");
  copyText(footerTokens, sizeof(footerTokens), footer["k"] | "--");
  copyText(footerTime, sizeof(footerTime), footer["t"] | "--:--");

  updateRowFromJson(rows[0], doc["p"]["plan"]);
  updateRowFromJson(rows[1], doc["p"]["today"]);
  notifyDebug("JSON_OK dashboard updated");
  return true;
}

class DashboardServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *server) override {
    bleConnected = true;
    debugLog("BLE connected");
  }

  void onDisconnect(BLEServer *server) override {
    bleConnected = false;
    debugLog("BLE disconnected");
  }
};

class DashboardRxCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *characteristic) override {
    std::string value = characteristic->getValue();
    if (value.empty()) {
      return;
    }

    debugLog(String("RX chunk len=") + value.length());

    for (char c : value) {
      if (c == '\n') {
        pendingPayload = rxBuffer;
        hasPendingPayload = true;
        notifyDebug(String("RX_FRAME len=") + pendingPayload.length());
        rxBuffer = "";
      } else if (rxBuffer.length() < 1400) {
        rxBuffer += c;
      } else {
        notifyDebug("RX_OVERFLOW buffer cleared");
        rxBuffer = "";
      }
    }
  }
};

void setupBle() {
  debugLog("BLE init start");
  BLEDevice::init(BLE_NAME);
  bleServer = BLEDevice::createServer();
  bleServer->setCallbacks(new DashboardServerCallbacks());

  BLEService *service = bleServer->createService(SERVICE_UUID);
  txCharacteristic = service->createCharacteristic(TX_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  txCharacteristic->addDescriptor(new BLE2902());

  BLECharacteristic *rxCharacteristic = service->createCharacteristic(
      RX_UUID,
      BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  rxCharacteristic->setCallbacks(new DashboardRxCallbacks());

  service->start();
  bleServer->getAdvertising()->addServiceUUID(SERVICE_UUID);
  bleServer->getAdvertising()->start();
  debugLog(String("BLE advertising as ") + BLE_NAME);
}

void drawGradientBackground() {
  for (int y = 0; y < SCREEN_H; y++) {
    canvas.drawFastHLine(0, y, SCREEN_W, backgroundColorAt(y));
  }

  canvas.drawFastHLine(16, 112, 288, C_RULE);
  canvas.drawFastVLine(144, 22, 183, rgb565(28, 49, 63));
  canvas.drawFastHLine(0, 208, SCREEN_W, rgb565(31, 53, 68));
  canvas.fillRect(0, 209, SCREEN_W, 31, C_FOOTER_BG);
}

void restoreBackgroundRect(int x, int y, int w, int h) {
  int startY = max(0, y);
  int endY = min(SCREEN_H, y + h);
  int startX = max(0, x);
  int width = min(SCREEN_W - startX, w);

  if (width <= 0) {
    return;
  }

  for (int yy = startY; yy < endY; yy++) {
    canvas.drawFastHLine(startX, yy, width, backgroundColorAt(yy));
  }
}

void drawStaticOverlaysForRect(int y, int h) {
  int y2 = y + h;
  if (y <= 112 && y2 >= 112) {
    canvas.drawFastHLine(16, 112, 288, C_RULE);
  }
  if (y <= 208 && y2 >= 208) {
    canvas.drawFastHLine(0, 208, SCREEN_W, rgb565(31, 53, 68));
  }

  int vx1 = max(y, 22);
  int vx2 = min(y2, 205);
  if (vx2 > vx1) {
    canvas.drawFastVLine(144, vx1, vx2 - vx1, rgb565(28, 49, 63));
  }
}

void clearRowArea(int y) {
  restoreBackgroundRect(0, y - 2, SCREEN_W, 94);
  drawStaticOverlaysForRect(y - 2, 94);
}

void clearFooterArea() {
  canvas.fillRect(0, 209, SCREEN_W, 31, C_FOOTER_BG);
  canvas.drawFastHLine(0, 208, SCREEN_W, rgb565(31, 53, 68));
}

void drawThickLine(int x1, int y1, int x2, int y2, uint8_t thickness, uint16_t color) {
  int radius = thickness / 2;
  for (int dx = -radius; dx <= radius; dx++) {
    for (int dy = -radius; dy <= radius; dy++) {
      if (dx * dx + dy * dy <= radius * radius) {
        canvas.drawLine(x1 + dx, y1 + dy, x2 + dx, y2 + dy, color);
      }
    }
  }
}

void drawClaudeIcon(int cx, int cy, uint16_t color) {
  for (int i = 0; i < 8; i++) {
    float a = i * 0.785398f;
    int x1 = cx + cos(a) * 2;
    int y1 = cy + sin(a) * 2;
    int x2 = cx + cos(a) * 7;
    int y2 = cy + sin(a) * 7;
    drawThickLine(x1, y1, x2, y2, 3, color);
    canvas.fillCircle(x2, y2, 2, color);
  }
  canvas.fillCircle(cx, cy, 3, rgb565(0, 8, 13));
}

void drawCodexIcon(int cx, int cy, uint16_t color) {
  for (int i = 0; i < 6; i++) {
    float a = -1.570796f + i * 1.047198f;
    int px = cx + cos(a) * 5;
    int py = cy + sin(a) * 5;
    canvas.drawCircle(px, py, 4, color);
    canvas.drawCircle(px, py, 3, color);
  }
  canvas.fillCircle(cx, cy, 3, rgb565(0, 8, 13));
  canvas.drawCircle(cx, cy, 2, color);
}

void drawBar(int x, int y, int w, uint8_t pct, uint16_t accent, uint16_t glow) {
  if (pct > 100) {
    pct = 100;
  }
  int fillW = max(5, (w * pct) / 100);

  canvas.fillRoundRect(x, y, w, 8, 4, C_TRACK);
  canvas.drawRoundRect(x, y, w, 8, 4, C_TRACK_EDGE);
  canvas.fillRoundRect(x, y, fillW, 8, 4, accent);
  canvas.drawFastHLine(x + 3, y + 1, max(0, fillW - 6), rgb565(255, 255, 255));
  canvas.drawFastHLine(x + 2, y + 11, max(0, fillW - 4), glow);
}

void drawRow(const UsageRow &row, int y) {
  const int leftCenterX = 80;
  const int rightX = 154;
  const int rightW = 144;

  canvas.setTextDatum(TL_DATUM);
  int nameW = canvas.textWidth(row.name, 2);
  int brandW = 17 + 6 + nameW;
  int brandX = leftCenterX - brandW / 2;

  if (row.claudeIcon) {
    drawClaudeIcon(brandX + 8, y + 11, row.accent);
  } else {
    drawCodexIcon(brandX + 8, y + 11, row.accent);
  }

  canvas.setTextColor(C_TEXT);
  canvas.drawString(row.name, brandX + 23, y + 1, 2);

  // Headline number in font 4 (full ASCII — the seven-segment font 7 has no
  // letters, so "13.3M"/"78%" would render blank with it).
  canvas.setTextColor(row.accent);
  int bigW = canvas.textWidth(row.bigValue, 4);
  canvas.drawString(row.bigValue, leftCenterX - bigW / 2, y + 24, 4);
  canvas.setTextColor(C_MUTED);
  canvas.drawString(row.bigLabel, leftCenterX - canvas.textWidth(row.bigLabel, 2) / 2, y + 66, 2);

  // First window line, with an optional progress bar below it.
  canvas.setTextColor(C_TEXT);
  canvas.drawString(row.line1, rightX, y + 15, 2);
  canvas.setTextDatum(TR_DATUM);
  canvas.drawString(row.val1, rightX + rightW, y + 15, 2);
  canvas.setTextDatum(TL_DATUM);
  if (row.bar1 >= 0) {
    drawBar(rightX, y + 35, rightW, static_cast<uint8_t>(row.bar1), row.accent, row.accent);
  }

  // Second window line.
  canvas.setTextColor(C_TEXT);
  canvas.drawString(row.line2, rightX, y + 51, 2);
  canvas.setTextDatum(TR_DATUM);
  canvas.drawString(row.val2, rightX + rightW, y + 51, 2);
  canvas.setTextDatum(TL_DATUM);
  if (row.bar2 >= 0) {
    drawBar(rightX, y + 71, rightW, static_cast<uint8_t>(row.bar2), row.accent, row.accent);
  }
}

void drawFooter() {
  canvas.setTextColor(C_TEXT);
  canvas.setTextDatum(MC_DATUM);
  canvas.drawString(String("Today ") + footerCost, 70, 225, 2);
  canvas.drawFastVLine(119, 216, 18, rgb565(48, 75, 95));
  canvas.drawString(footerTokens, 169, 225, 2);
  canvas.drawFastVLine(219, 216, 18, rgb565(48, 75, 95));
  canvas.drawString(footerTime, 257, 225, 2);
  canvas.setTextDatum(TL_DATUM);
}

void drawDashboard() {
  drawGradientBackground();
  drawRow(rows[0], 16);
  drawRow(rows[1], 116);
  drawFooter();
}

void drawDashboardDataOnly() {
  clearRowArea(16);
  drawRow(rows[0], 16);
  clearRowArea(116);
  drawRow(rows[1], 116);
  clearFooterArea();
  drawFooter();
}

void setup() {
  Serial.begin(115200);
  delay(300);
  debugLog("boot");
  tft.begin();
  tft.setRotation(3);
  tft.setSwapBytes(true);

  canvas.setTextWrap(false);
  setupBle();
  drawDashboard();
}

void loop() {
  if (hasPendingPayload) {
    String payload = pendingPayload;
    hasPendingPayload = false;
    if (payload == lastAppliedPayload) {
      notifyDebug("JSON_DUPLICATE no redraw");
    } else if (applyQuotaPayload(payload)) {
      lastAppliedPayload = payload;
      drawDashboardDataOnly();
    }
  }

  if (!bleConnected && oldBleConnected) {
    delay(500);
    bleServer->getAdvertising()->start();
    oldBleConnected = bleConnected;
    debugLog("BLE advertising restarted");
  }

  if (bleConnected && !oldBleConnected) {
    oldBleConnected = bleConnected;
  }
}
