// midi-agent-console ESP-NOW dongle. PC <-USB serial-> dongle <-ESP-NOW-> car.
// The PC talks protocol 2 to this board exactly as it would to the car's USB port.
//
//   PC -> car : one protocol line (without '\n') = one unicast ESP-NOW packet
//   car -> PC : the car's chunks (<= 240 B, already containing '\n') are written verbatim
//   local     : "0 ERR LINE_TOO_LONG" for an overlong PC line (dropped, never sent)
//               "! DONGLE mac=..."  at boot
//               "! LINK lost"       PC is sending but nothing came from the car for 2 s
//               "! LINK ok"         first car packet after a "! LINK lost"
// Safety stays on the car: if this board dies or the radio drops, the car's watchdog
// sees no frames and goes SAFE by itself. Fixed channel 1, no encryption.
// Compiled for esp32:esp32:esp32 and esp32:esp32:nologo_esp32c3_super_mini; not yet run.

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

// The car's MAC, from its "! BOOT ... mac=..." line. Packets from any other MAC are ignored.
uint8_t CAR_MAC[6] = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00};

#define CHANNEL 1
#define MAX_LINE 63            // same as the car
#define LINK_TIMEOUT_MS 2000
#define RXQ 8

struct Pkt { uint8_t len; uint8_t data[ESP_NOW_MAX_DATA_LEN]; };
QueueHandle_t rxq = NULL;
volatile uint32_t rxSeq = 0;      // bumped for every car packet (also when the queue is full)
volatile uint32_t rxDropped = 0;

char line[MAX_LINE + 1];
uint8_t n = 0;
bool over = false;
bool carMidLine = false;          // last byte forwarded from the car was not '\n'
unsigned long lastCarByte = 0;    // when we last forwarded car data (loop time)
unsigned long pendingSince = 0;   // first PC line sent since the last car packet; 0 = none
uint32_t seenSeq = 0;
bool linkLost = false;
uint8_t tooLong = 0;              // local errors waiting for a line boundary
bool lostPending = false, okPending = false;
bool espnowOk = false;

// Runs in the WiFi task: filter and copy only. Serial is written from loop() alone, so car
// chunks never interleave with the dongle's own lines.
void onRecv(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
  if (!info || memcmp(info->src_addr, CAR_MAC, 6) != 0 || len <= 0) return;
  rxSeq = rxSeq + 1;
  Pkt p;
  p.len = len > ESP_NOW_MAX_DATA_LEN ? ESP_NOW_MAX_DATA_LEN : len;
  memcpy(p.data, data, p.len);
  if (xQueueSend(rxq, &p, 0) != pdTRUE) rxDropped = rxDropped + 1;
}

void printMac(const uint8_t* m) {
  Serial.printf("%02X:%02X:%02X:%02X:%02X:%02X", m[0], m[1], m[2], m[3], m[4], m[5]);
}

void setup() {
  Serial.begin(115200);
  rxq = xQueueCreate(RXQ, sizeof(Pkt));
  WiFi.mode(WIFI_STA);
  unsigned long t0 = millis();
  while (!WiFi.STA.started() && millis() - t0 < 2000) delay(10);
  espnowOk = rxq && WiFi.STA.started() && WiFi.setChannel(CHANNEL, WIFI_SECOND_CHAN_NONE) == ESP_OK;
  esp_wifi_set_ps(WIFI_PS_NONE);
  if (espnowOk) espnowOk = esp_now_init() == ESP_OK;
  if (espnowOk) {
    esp_now_peer_info_t p = {};
    memcpy(p.peer_addr, CAR_MAC, 6);
    p.channel = CHANNEL;
    p.ifidx = WIFI_IF_STA;
    p.encrypt = false;
    espnowOk = esp_now_add_peer(&p) == ESP_OK && esp_now_register_recv_cb(onRecv) == ESP_OK;
  }
  Serial.print("! DONGLE mac=");
  Serial.print(WiFi.macAddress());
  Serial.print(" ch=");
  Serial.print(CHANNEL);
  Serial.print(" car=");
  printMac(CAR_MAC);
  Serial.println(espnowOk ? " espnow=ok" : " espnow=fail");
}

void loop() {
  unsigned long now = millis();

  // PC -> car
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (over) {
        if (tooLong < 255) tooLong++;   // dropped here; the car never sees it
      } else if (n) {
        if (espnowOk) esp_now_send(CAR_MAC, (const uint8_t*)line, n);
        if (!pendingSince) pendingSince = now ? now : 1;
      }
      n = 0;
      over = false;
    } else if (n < MAX_LINE) {
      line[n++] = c;
    } else {
      over = true;
    }
  }

  // car -> PC, verbatim
  Pkt p;
  while (rxq && xQueueReceive(rxq, &p, 0) == pdTRUE) {
    Serial.write(p.data, p.len);
    carMidLine = p.data[p.len - 1] != '\n';
    lastCarByte = now;
  }

  // link supervision
  uint32_t seq = rxSeq;
  if (seq != seenSeq) {
    seenSeq = seq;
    pendingSince = 0;
    if (linkLost) { linkLost = false; okPending = true; }
  } else if (pendingSince && !linkLost && now - pendingSince > LINK_TIMEOUT_MS) {
    linkLost = true;
    lostPending = true;
  }

  // local lines only at a line boundary (or if a car line was cut off for > 100 ms)
  if (carMidLine && now - lastCarByte > 100) { Serial.println(); carMidLine = false; }
  if (!carMidLine) {
    for (; tooLong; tooLong--) Serial.println("0 ERR LINE_TOO_LONG");
    if (lostPending) { Serial.println("! LINK lost"); lostPending = false; }
    if (okPending) { Serial.println("! LINK ok"); okPending = false; }
  }
}
