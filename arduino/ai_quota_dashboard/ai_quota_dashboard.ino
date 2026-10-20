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
constexpr int PAGE_COUNT = 4;

// One device page: headline value plus up to four label/value rows, mirroring
// the magpie-usage-display project's Overview / Requests / Sessions / Context
// pages. The payload always carries exactly PAGE_COUNT pages so the firmware
// can index them without negotiating a count.
struct PageRow {
  char label[16];
  char value[16];
};

struct Page {
  char title[16];
  char bigValue[16];
  char bigLabel[16];
  int8_t bar;   // 0-100 progress bar; -1 = no bar
  PageRow rows[4];
  uint16_t accent;
};

Page pages[PAGE_COUNT] = {
    {"Overview", "--", "--", -1, {{"", ""}, {"", ""}, {"", ""}, {"", ""}}, 0xFBC9},
    {"Requests", "--", "--", -1, {{"", ""}, {"", ""}, {"", ""}, {"", ""}}, 0x06BF},
    {"Sessions", "--", "--", -1, {{"", ""}, {"", ""}, {"", ""}, {"", ""}}, 0x2E9D},
    {"Context", "--", "--", -1, {{"", ""}, {"", ""}, {"", ""}, {"", ""}}, 0xA913},
};

uint8_t currentPage = 0;

char footerBrand[12] = "Magpie";
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
    src = "";
  }
  strncpy(dest, src, destSize - 1);
  dest[destSize - 1] = '\0';
}

void updatePageFromJson(Page &page, JsonVariantConst source) {
  if (source.isNull()) {
    return;
  }
  copyText(page.title, sizeof(page.title), source["t"] | page.title);
  copyText(page.bigValue, sizeof(page.bigValue), source["v"] | "--");
  copyText(page.bigLabel, sizeof(page.bigLabel), source["l"] | "");
  int pct = source["b"] | -1;
  page.bar = (pct >= 0 && pct <= 100) ? static_cast<int8_t>(pct) : -1;
  JsonVariantConst rows = source["r"];
  for (uint8_t i = 0; i < 4; i++) {
    JsonVariantConst row = rows[i];
    copyText(page.rows[i].label, sizeof(page.rows[i].label), row["n"] | "");
    copyText(page.rows[i].value, sizeof(page.rows[i].value), row["v"] | "");
  }
}

bool applyQuotaPayload(const String &payload) {
  debugLog(String("parse payload len=") + payload.length());
  // 753-byte payloads with ~60 small strings need headroom beyond the payload
  // size; ArduinoJson counts this pool in bytes.
  StaticJsonDocument<3072> doc;
  DeserializationError error = deserializeJson(doc, payload);
  if (error) {
    notifyDebug(String("JSON_ERROR ") + error.c_str());
    return false;
  }

  JsonVariantConst footer = doc["f"];
  copyText(footerBrand, sizeof(footerBrand), footer["c"] | "Magpie");
  copyText(footerTokens, sizeof(footerTokens), footer["k"] | "--");
  copyText(footerTime, sizeof(footerTime), footer["t"] | "--:--");

  JsonVariantConst pageList = doc["p"];
  for (uint8_t i = 0; i < PAGE_COUNT; i++) {
    updatePageFromJson(pages[i], pageList[i]);
  }
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
}

void drawFooter() {
  canvas.fillRect(0, 208, SCREEN_W, 32, C_FOOTER_BG);
  canvas.drawFastHLine(0, 208, SCREEN_W, rgb565(31, 53, 68));
  canvas.setTextColor(C_TEXT);
  canvas.setTextDatum(MC_DATUM);
  canvas.drawString(footerBrand, 53, 225, 2);
  canvas.drawFastVLine(106, 216, 18, rgb565(48, 75, 95));
  canvas.drawString(footerTokens, 160, 225, 2);
  canvas.drawFastVLine(214, 216, 18, rgb565(48, 75, 95));
  canvas.drawString(footerTime, 267, 225, 2);
  canvas.setTextDatum(TL_DATUM);
}

void drawBar(int x, int y, int w, uint8_t pct, uint16_t accent) {
  if (pct > 100) {
    pct = 100;
  }
  int fillW = max(5, (w * pct) / 100);

  canvas.fillRoundRect(x, y, w, 10, 5, C_TRACK);
  canvas.drawRoundRect(x, y, w, 10, 5, C_TRACK_EDGE);
  canvas.fillRoundRect(x, y, fillW, 10, 5, accent);
  canvas.drawFastHLine(x + 3, y + 1, max(0, fillW - 6), rgb565(255, 255, 255));
}

void drawPage(const Page &page, uint8_t index) {
  drawGradientBackground();
  canvas.setTextDatum(TL_DATUM);

  // Title bar: page name left, "n/4" page indicator right, rule below.
  canvas.setTextColor(page.accent);
  canvas.drawString(page.title, 16, 8, 2);
  canvas.setTextDatum(TR_DATUM);
  canvas.setTextColor(C_MUTED);
  canvas.drawString(String(index + 1) + "/" + PAGE_COUNT, 304, 8, 2);
  canvas.setTextDatum(TL_DATUM);
  canvas.drawFastHLine(16, 34, 288, C_RULE);

  // Left column: headline value, caption, optional progress bar.
  canvas.setTextColor(page.accent);
  canvas.drawString(page.bigValue, 16, 52, 4);
  canvas.setTextColor(C_MUTED);
  canvas.drawString(page.bigLabel, 16, 92, 2);
  if (page.bar >= 0) {
    drawBar(16, 116, 130, static_cast<uint8_t>(page.bar), page.accent);
  }

  // Right column: four label/value rows.
  canvas.drawFastVLine(164, 46, 140, rgb565(28, 49, 63));
  for (uint8_t i = 0; i < 4; i++) {
    int y = 48 + i * 34;
    canvas.setTextColor(C_MUTED);
    canvas.drawString(page.rows[i].label, 178, y, 2);
    canvas.setTextDatum(TR_DATUM);
    canvas.setTextColor(C_TEXT);
    canvas.drawString(page.rows[i].value, 304, y, 2);
    canvas.setTextDatum(TL_DATUM);
  }

  // Hint line: how to switch pages.
  canvas.setTextColor(rgb565(90, 115, 135));
  canvas.drawString("< > switch page", 16, 176, 2);

  drawFooter();
}

void setupButtons() {
  pinMode(WIO_5S_LEFT, INPUT_PULLUP);
  pinMode(WIO_5S_RIGHT, INPUT_PULLUP);
  pinMode(WIO_KEY_A, INPUT_PULLUP);
  pinMode(WIO_KEY_B, INPUT_PULLUP);
  pinMode(WIO_KEY_C, INPUT_PULLUP);
  pinMode(WIO_5S_PRESS, INPUT_PULLUP);
}

void pollButtons() {
  // Static debounce state: a key held down only fires once per press. LEFT /
  // RIGHT cycle; A/B/C jump straight to pages 0-2 and the 5-way press opens
  // the last page, so every page is one click away.
  static bool leftWasDown = false;
  static bool rightWasDown = false;
  static bool aWasDown = false;
  static bool bWasDown = false;
  static bool cWasDown = false;
  static bool pressWasDown = false;
  static uint32_t lastEdgeMs = 0;
  bool changed = false;

  bool leftDown = digitalRead(WIO_5S_LEFT) == LOW;
  bool rightDown = digitalRead(WIO_5S_RIGHT) == LOW;
  bool aDown = digitalRead(WIO_KEY_A) == LOW;
  bool bDown = digitalRead(WIO_KEY_B) == LOW;
  bool cDown = digitalRead(WIO_KEY_C) == LOW;
  bool pressDown = digitalRead(WIO_5S_PRESS) == LOW;

  if (millis() - lastEdgeMs < 150) {
    return;
  }

  if (leftDown && !leftWasDown) {
    currentPage = (currentPage + PAGE_COUNT - 1) % PAGE_COUNT;
    changed = true;
  }
  if (rightDown && !rightWasDown) {
    currentPage = (currentPage + 1) % PAGE_COUNT;
    changed = true;
  }
  if (aDown && !aWasDown) {
    currentPage = 0;
    changed = true;
  }
  if (bDown && !bWasDown) {
    currentPage = 1;
    changed = true;
  }
  if (cDown && !cWasDown) {
    currentPage = 2;
    changed = true;
  }
  if (pressDown && !pressWasDown) {
    currentPage = 3;
    changed = true;
  }

  if (changed) {
    lastEdgeMs = millis();
    debugLog(String("page -> ") + currentPage);
    drawPage(pages[currentPage], currentPage);
  }

  leftWasDown = leftDown;
  rightWasDown = rightDown;
  aWasDown = aDown;
  bWasDown = bDown;
  cWasDown = cDown;
  pressWasDown = pressDown;
}

void setup() {
  Serial.begin(115200);
  delay(300);
  debugLog("boot");
  tft.begin();
  tft.setRotation(3);
  tft.setSwapBytes(true);

  canvas.setTextWrap(false);
  setupButtons();
  setupBle();
  drawPage(pages[currentPage], currentPage);
}

void loop() {
  pollButtons();

  if (hasPendingPayload) {
    String payload = pendingPayload;
    hasPendingPayload = false;
    if (payload == lastAppliedPayload) {
      notifyDebug("JSON_DUPLICATE no redraw");
    } else if (applyQuotaPayload(payload)) {
      lastAppliedPayload = payload;
      drawPage(pages[currentPage], currentPage);
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
