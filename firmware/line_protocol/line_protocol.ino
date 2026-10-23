// midi-agent-console reference firmware, protocol 2. One file, Arduino (AVR) and ESP32.
//
//   request : <id> <VERB> [int ...]\n     id 1..65535, ints -?\d{1,6}, line <= 63 bytes
//   reply   : <id> OK [payload]  |  <id> ERR <CODE> [detail]
//   event   : ! <EVENT> [k=v ...]           (BOOT, WDT, DRIVE_DONE)
//
// Safety lives HERE, not in the PC or the model:
//   * watchdog: no valid frame for HB_TIMEOUT_MS -> SAFE (motors off, servos to safe, LED off)
//   * ESTOP is latched until RESET; motion is refused meanwhile
//   * every servo has its own min/max (out of range = refused, never clamped) and a slew limit
//   * DRIVE always carries a duration and stops by itself
//   * overlong lines are discarded, never executed; garbage numbers are errors, not 0
//   * token-bucket rate limit
//
// Transport: USB serial always works. On ESP32 / ESP32-C3, TRANSPORT_ESPNOW 1 ALSO accepts
// lines over ESP-NOW from ONE paired dongle (espnow_dongle.ino): one packet = one line,
// replies go back to whichever transport the request came from, events go to both.
// Radio frames feed the same watchdog, so a lost radio link ends in SAFE like a pulled cable.
// Compiled for arduino:avr:uno, esp32:esp32:esp32 and esp32:esp32:nologo_esp32c3_super_mini;
// not yet run on a real board.

#if defined(ESP32)
#include <ESP32Servo.h>   // Library Manager: "ESP32Servo"
#else
#include <Servo.h>
#endif
#include <stdlib.h>
#include <string.h>

// ------------------------------------------------------------------ board config
#ifndef LED_BUILTIN
#define LED_BUILTIN 2
#endif
#define FW_VERSION "mac-0.2"
#define HB_TIMEOUT_MS 1000
#define MAX_LINE 63

// 0 = USB serial only (default). 1 = also ESP-NOW (ESP32 targets only). Can be set with
// --build-property "compiler.cpp.extra_flags=-DTRANSPORT_ESPNOW=1" instead of editing.
#ifndef TRANSPORT_ESPNOW
#define TRANSPORT_ESPNOW 0
#endif
#if TRANSPORT_ESPNOW && !defined(ESP32)
#error "TRANSPORT_ESPNOW 1 needs an ESP32 / ESP32-C3 target"
#endif

// Pins per board. ESP32-C3 needs its own branch: it also defines ESP32, but GPIO 18/19
// are its USB pins and 25-27 do not exist. Avoid strapping pins (ESP32: 0/2/12/15,
// C3: 2/8/9), flash pins (C3: 12-17) and UART0 (C3: 20/21).
struct ServoCfg { uint8_t pin; int16_t minDeg, maxDeg, safeDeg, maxDps; };
#if defined(CONFIG_IDF_TARGET_ESP32C3)
const ServoCfg SERVO_CFG[] = {{0, 10, 170, 90, 180}, {1, 10, 170, 90, 180}};
const uint8_t PWMA = 3, AIN1 = 4, AIN2 = 5, PWMB = 10, BIN1 = 6, BIN2 = 7;
#elif defined(ESP32)
const ServoCfg SERVO_CFG[] = {{18, 10, 170, 90, 180}, {19, 10, 170, 90, 180}};
const uint8_t PWMA = 25, AIN1 = 26, AIN2 = 27, PWMB = 14, BIN1 = 33, BIN2 = 13;
#else
const ServoCfg SERVO_CFG[] = {{9, 10, 170, 90, 180}, {10, 10, 170, 90, 180}};
const uint8_t PWMA = 5, AIN1 = 7, AIN2 = 8, PWMB = 6, BIN1 = 4, BIN2 = 2;
#endif
const uint8_t N_SERVOS = sizeof(SERVO_CFG) / sizeof(SERVO_CFG[0]);

// Optional DC motors on a TB6612FNG (STBY tied to 3.3 V) or L298N. Set HAS_DRIVE 0 if not wired.
// DRIVE_PWM_MAX caps the duty: TT motors are rated 3-6 V, so with 2x18650 (8.4 V full) keep ~180.
#define HAS_DRIVE 1
#define DRIVE_MAX_MS 2000
#define DRIVE_PWM_MAX 180

// ------------------------------------------------------------------ transport
#if defined(ESP32) && TRANSPORT_ESPNOW
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

#define NOW_CHANNEL 1        // fixed; the dongle uses the same
#define NOW_CHUNK 240        // ESP-NOW v1 payload limit is 250 B (ESP_NOW_MAX_DATA_LEN)
#define NOW_RXQ 8            // packets buffered between the WiFi task and loop()
// The dongle's MAC, from its "! DONGLE mac=..." line. Packets from any other MAC are ignored.
const uint8_t DONGLE_MAC[6] = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00};

struct NowPkt { uint8_t len; char data[MAX_LINE + 1]; };   // len > MAX_LINE: overlong, no data
QueueHandle_t nowRx = NULL;
volatile uint32_t nowDropped = 0;   // queue full: the PC sees a reply timeout for that id
bool nowOk = false;

// Runs in the WiFi task: filter and copy, nothing else (no parsing, printing or outputs).
void onNowRecv(const esp_now_recv_info_t* info, const uint8_t* data, int n) {
  if (!info || memcmp(info->src_addr, DONGLE_MAC, 6) != 0 || n <= 0) return;
  while (n > 0 && (data[n - 1] == '\n' || data[n - 1] == '\r')) n--;
  NowPkt p;
  if (n > MAX_LINE) {
    p.len = MAX_LINE + 1;
  } else {
    p.len = (uint8_t)n;
    memcpy(p.data, data, n);
  }
  if (xQueueSend(nowRx, &p, 0) != pdTRUE) nowDropped = nowDropped + 1;
}

// Buffers replies and sends them to the dongle in chunks <= NOW_CHUNK, one send per '\n'.
class NowOut : public Print {
 public:
  size_t write(uint8_t c) override {
    b[n++] = c;
    if (c == '\n' || n >= NOW_CHUNK) flush();
    return 1;
  }
  using Print::write;
  void flush() override {
    if (!n) return;
    if (nowOk)
      for (uint8_t t = 0; t < 3 && esp_now_send(DONGLE_MAC, b, n) == ESP_ERR_ESPNOW_NO_MEM; t++) delay(2);
    n = 0;
  }
 private:
  uint8_t b[NOW_CHUNK];
  uint8_t n = 0;
};
NowOut nowOut;

// Events (BOOT, WDT, DRIVE_DONE) go to USB serial and the radio.
class TeeOut : public Print {
 public:
  size_t write(uint8_t c) override { Serial.write(c); nowOut.write(c); return 1; }
  using Print::write;
};
TeeOut teeOut;

Print* out = &Serial;   // where replies to the current request go
#define OUT (*out)
#define EVT teeOut

void nowBegin() {
  nowRx = xQueueCreate(NOW_RXQ, sizeof(NowPkt));
  WiFi.mode(WIFI_STA);
  unsigned long t0 = millis();
  while (!WiFi.STA.started() && millis() - t0 < 2000) delay(10);
  nowOk = nowRx && WiFi.STA.started() && WiFi.setChannel(NOW_CHANNEL, WIFI_SECOND_CHAN_NONE) == ESP_OK;
  esp_wifi_set_ps(WIFI_PS_NONE);   // no modem sleep: lower, steadier latency
  if (nowOk) nowOk = esp_now_init() == ESP_OK;
  if (nowOk) {
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, DONGLE_MAC, 6);
    peer.channel = NOW_CHANNEL;
    peer.ifidx = WIFI_IF_STA;
    peer.encrypt = false;
    nowOk = esp_now_add_peer(&peer) == ESP_OK && esp_now_register_recv_cb(onNowRecv) == ESP_OK;
  }
}
#else
#define OUT Serial
#define EVT Serial
#endif

// ------------------------------------------------------------------ state
enum St : uint8_t { RUN, SAFE, ESTOPPED };
St st = RUN;
bool armed = false;               // watchdog starts with the first HB / motion frame
unsigned long lastFrame = 0, lastSlew = 0, lastRefill = 0, driveDeadline = 0;
bool driving = false;
int16_t driveL = 0, driveR = 0;
uint8_t ledRgb[3] = {0, 0, 0};
uint8_t tokens = 20;              // burst 20, +1 every 10 ms -> 100 frames/s

Servo servos[N_SERVOS];
long curMd[N_SERVOS], tgtMd[N_SERVOS];   // milli-degrees, no float drift
bool attached[N_SERVOS];

char buf[MAX_LINE + 1];
uint8_t len = 0;
bool overflow = false;

// ------------------------------------------------------------------ outputs
void motor(uint8_t pwm, uint8_t in1, uint8_t in2, int v) {
#if HAS_DRIVE
  digitalWrite(in1, v > 0);
  digitalWrite(in2, v < 0);
  analogWrite(pwm, abs(v));
#endif
}

void motorsOff() {
  driving = false;
  driveL = driveR = 0;
  motor(PWMA, AIN1, AIN2, 0);
  motor(PWMB, BIN1, BIN2, 0);
}

void writeLed() {
  uint8_t m = max(ledRgb[0], max(ledRgb[1], ledRgb[2]));
  analogWrite(LED_BUILTIN, m);
}

void attachAll() {
  for (uint8_t i = 0; i < N_SERVOS; i++) {
    if (!attached[i]) {
      servos[i].attach(SERVO_CFG[i].pin);
      attached[i] = true;
    }
    servos[i].write((int)(curMd[i] / 1000));
  }
}

void enterSafe() {
  st = SAFE;
  motorsOff();
  for (uint8_t i = 0; i < N_SERVOS; i++) tgtMd[i] = (long)SERVO_CFG[i].safeDeg * 1000;
  ledRgb[0] = ledRgb[1] = ledRgb[2] = 0;
  writeLed();
}

const char* stName() { return st == RUN ? "RUN" : st == SAFE ? "SAFE" : "ESTOP"; }

// ------------------------------------------------------------------ replies
void okHead(long id) { OUT.print(id); OUT.print(F(" OK")); }
void ok(long id) { okHead(id); OUT.println(); }
void err(long id, const __FlashStringHelper* code) {
  OUT.print(id); OUT.print(F(" ERR ")); OUT.println(code);
}

// strict integer: optional '-', 1..6 digits, nothing else. 0 ok, 1 BAD_INT, 2 RANGE
uint8_t parseStrict(const char* s, long lo, long hi, long* out) {
  const char* p = s;
  if (*p == '-') p++;
  uint8_t n = 0;
  while (*p) {
    if (*p < '0' || *p > '9') return 1;
    p++;
    if (++n > 6) return 1;
  }
  if (n == 0) return 1;
  long v = atol(s);
  if (v < lo || v > hi) return 2;
  *out = v;
  return 0;
}

void describe(long id) {
  okHead(id);
  OUT.print(F(" {\"proto\":2,\"fw\":\"" FW_VERSION "\",\"hb_timeout_ms\":"));
  OUT.print(HB_TIMEOUT_MS);
  OUT.print(F(",\"max_line\":63,\"max_rate_hz\":100,\"servos\":["));
  for (uint8_t i = 0; i < N_SERVOS; i++) {
    if (i) OUT.print(',');
    OUT.print(F("{\"i\":")); OUT.print(i);
    OUT.print(F(",\"min\":")); OUT.print(SERVO_CFG[i].minDeg);
    OUT.print(F(",\"max\":")); OUT.print(SERVO_CFG[i].maxDeg);
    OUT.print(F(",\"safe\":")); OUT.print(SERVO_CFG[i].safeDeg);
    OUT.print(F(",\"max_dps\":")); OUT.print(SERVO_CFG[i].maxDps);
    OUT.print(F(",\"unit\":\"deg\"}"));
  }
  OUT.print(F("],\"leds\":[{\"i\":0,\"min\":0,\"max\":255}],\"drive\":"));
#if HAS_DRIVE
  OUT.print(F("{\"min\":")); OUT.print(-DRIVE_PWM_MAX); OUT.print(F(",\"max\":")); OUT.print(DRIVE_PWM_MAX);
  OUT.print(F(",\"max_ms\":")); OUT.print(DRIVE_MAX_MS);
  OUT.print(F(",\"unit\":\"pwm\"}"));
#else
  OUT.print(F("null"));
#endif
  OUT.println(F(",\"safe_state\":\"motors 0, servos -> safe, leds off\",\"user_only\":[\"RESET\"]}"));
}

void state(long id) {
  okHead(id);
  OUT.print(F(" STATE st=")); OUT.print(stName());
  for (uint8_t i = 0; i < N_SERVOS; i++) {
    OUT.print(F(" s")); OUT.print(i); OUT.print('=');
    OUT.print(curMd[i] / 1000); OUT.print('>'); OUT.print(tgtMd[i] / 1000);
  }
  OUT.print(F(" led0=")); OUT.print(ledRgb[0]); OUT.print(',');
  OUT.print(ledRgb[1]); OUT.print(','); OUT.print(ledRgb[2]);
  long left = driving ? (long)(driveDeadline - millis()) : 0;
  OUT.print(F(" drv=")); OUT.print(driveL); OUT.print(','); OUT.print(driveR);
  OUT.print(','); OUT.println(left < 0 ? 0 : left);
}

// ------------------------------------------------------------------ command handling
void handle(char* line) {
  for (char* c = line; *c; c++)
    if (*c < 0x20 || *c > 0x7E) { err(0, F("BAD_CHAR")); return; }

  char* tok[7];
  uint8_t n = 0;
  for (char* t = strtok(line, " "); t && n < 7; t = strtok(NULL, " ")) tok[n++] = t;
  if (n < 2) { err(0, F("BAD_ID")); return; }
  long id;
  if (parseStrict(tok[0], 1, 65535, &id)) { err(0, F("BAD_ID")); return; }

  if (tokens == 0) { err(id, F("RATE")); return; }
  tokens--;
  lastFrame = millis();   // serial and radio frames feed the same watchdog

  const char* v = tok[1];
  uint8_t argc = n - 2;
  long a[4];

  struct Spec { const char* name; uint8_t argc; bool motion; };
  static const Spec specs[] = {
    {"HELLO", 0, false}, {"DESCRIBE", 0, false}, {"HB", 0, false}, {"STATE", 0, false},
    {"PING", 0, false}, {"STOP", 0, false}, {"ESTOP", 0, false}, {"RESET", 0, false},
    {"SERVO", 2, true}, {"LED", 4, true}, {"DRIVE", 3, true}};
  const Spec* sp = NULL;
  for (uint8_t i = 0; i < sizeof(specs) / sizeof(specs[0]); i++)
    if (!strcmp(v, specs[i].name)) sp = &specs[i];
  if (!sp) { err(id, F("UNKNOWN_CMD")); return; }
  if (argc != sp->argc) { err(id, F("BAD_ARGC")); return; }

  if (!strcmp(v, "SERVO")) {
    long i;
    uint8_t r = parseStrict(tok[2], 0, N_SERVOS - 1, &i);
    if (r) { r == 1 ? err(id, F("BAD_INT")) : err(id, F("RANGE")); return; }
    r = parseStrict(tok[3], SERVO_CFG[i].minDeg, SERVO_CFG[i].maxDeg, &a[1]);
    if (r) { r == 1 ? err(id, F("BAD_INT")) : err(id, F("RANGE")); return; }
    a[0] = i;
  } else if (!strcmp(v, "LED")) {
    for (uint8_t k = 0; k < 4; k++) {
      uint8_t r = parseStrict(tok[2 + k], 0, k == 0 ? 0 : 255, &a[k]);
      if (r) { r == 1 ? err(id, F("BAD_INT")) : err(id, F("RANGE")); return; }
    }
  } else if (!strcmp(v, "DRIVE")) {
#if !HAS_DRIVE
    err(id, F("NO_MOTOR")); return;
#endif
    for (uint8_t k = 0; k < 3; k++) {
      uint8_t r = parseStrict(tok[2 + k], k < 2 ? -DRIVE_PWM_MAX : 1, k < 2 ? DRIVE_PWM_MAX : DRIVE_MAX_MS, &a[k]);
      if (r) { r == 1 ? err(id, F("BAD_INT")) : err(id, F("RANGE")); return; }
    }
  }

  if (sp->motion && st == ESTOPPED) { err(id, F("ESTOP")); return; }
  if (sp->motion || !strcmp(v, "HB")) armed = true;
  if (sp->motion && st == SAFE) st = RUN;  // SAFE is not latched: a fresh command resumes

  if (!strcmp(v, "HELLO")) {
    okHead(id); OUT.print(F(" HELLO proto=2 fw=" FW_VERSION " hb_ms=")); OUT.println(HB_TIMEOUT_MS);
  } else if (!strcmp(v, "DESCRIBE")) {
    describe(id);
  } else if (!strcmp(v, "HB")) {
    okHead(id); OUT.print(F(" HB st=")); OUT.println(stName());
  } else if (!strcmp(v, "STATE")) {
    state(id);
  } else if (!strcmp(v, "PING")) {
    okHead(id); OUT.println(F(" PONG"));
  } else if (!strcmp(v, "STOP")) {
    motorsOff();
    for (uint8_t i = 0; i < N_SERVOS; i++) tgtMd[i] = curMd[i];
    ok(id);
  } else if (!strcmp(v, "ESTOP")) {
    st = ESTOPPED;
    motorsOff();
    for (uint8_t i = 0; i < N_SERVOS; i++) {
      tgtMd[i] = curMd[i];
      if (attached[i]) { servos[i].detach(); attached[i] = false; }
    }
    ok(id);
  } else if (!strcmp(v, "RESET")) {
    if (st == ESTOPPED) { st = RUN; attachAll(); }
    ok(id);
  } else if (!strcmp(v, "SERVO")) {
    tgtMd[a[0]] = a[1] * 1000;
    ok(id);
  } else if (!strcmp(v, "LED")) {
    ledRgb[0] = a[1]; ledRgb[1] = a[2]; ledRgb[2] = a[3];
    writeLed();
    ok(id);
  } else if (!strcmp(v, "DRIVE")) {
    driveL = a[0]; driveR = a[1];
    motor(PWMA, AIN1, AIN2, driveL);
    motor(PWMB, BIN1, BIN2, driveR);
    driving = true;
    driveDeadline = millis() + a[2];
    ok(id);
  }
}

// ------------------------------------------------------------------ setup / loop
void setup() {
  Serial.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
#if HAS_DRIVE
  pinMode(AIN1, OUTPUT); pinMode(AIN2, OUTPUT); pinMode(BIN1, OUTPUT); pinMode(BIN2, OUTPUT);
  pinMode(PWMA, OUTPUT); pinMode(PWMB, OUTPUT);
#endif
  motorsOff();
  for (uint8_t i = 0; i < N_SERVOS; i++) {
    curMd[i] = tgtMd[i] = (long)SERVO_CFG[i].safeDeg * 1000;  // power-up state is the safe state
    attached[i] = false;
  }
  attachAll();
  writeLed();
#if defined(ESP32) && TRANSPORT_ESPNOW
  nowBegin();
  EVT.print(F("! BOOT proto=2 fw=" FW_VERSION " mac="));
  EVT.print(WiFi.macAddress());
  EVT.print(F(" ch=")); EVT.print(NOW_CHANNEL);
  EVT.printf(" peer=%02X:%02X:%02X:%02X:%02X:%02X", DONGLE_MAC[0], DONGLE_MAC[1], DONGLE_MAC[2],
             DONGLE_MAC[3], DONGLE_MAC[4], DONGLE_MAC[5]);
  EVT.println(nowOk ? F(" espnow=ok") : F(" espnow=fail"));
#else
  EVT.println(F("! BOOT proto=2 fw=" FW_VERSION));
#endif
  lastSlew = lastRefill = millis();
}

void loop() {
  unsigned long now = millis();

  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      if (overflow) err(0, F("LINE_TOO_LONG"));  // discarded, never executed
      else if (len) { buf[len] = 0; handle(buf); }
      len = 0;
      overflow = false;
    } else if (c != '\r') {
      if (len < MAX_LINE) buf[len++] = c;
      else overflow = true;
    }
  }

#if defined(ESP32) && TRANSPORT_ESPNOW
  NowPkt p;
  while (nowRx && xQueueReceive(nowRx, &p, 0) == pdTRUE) {
    out = &nowOut;   // replies to a radio request go back over the radio
    if (p.len > MAX_LINE) err(0, F("LINE_TOO_LONG"));
    else if (p.len) { p.data[p.len] = 0; handle(p.data); }
    nowOut.flush();
    out = &Serial;
  }
#endif

  while (now - lastRefill >= 10) {
    lastRefill += 10;
    if (tokens < 20) tokens++;
  }

  // Re-read the clock: handling input just set lastFrame = millis(), which can be later
  // than the `now` taken at the top; unsigned now - lastFrame would wrap and trip the
  // watchdog right after every command. Compare signed, against a fresh clock.
  now = millis();
  if (armed && st == RUN && (long)(now - lastFrame) > HB_TIMEOUT_MS) {
    enterSafe();
    EVT.println(F("! WDT"));
  }

  if (driving && (long)(now - driveDeadline) >= 0) {
    motorsOff();
    EVT.println(F("! DRIVE_DONE"));
  }

  if (now - lastSlew >= 20) {
    unsigned long dt = now - lastSlew;
    lastSlew = now;
    if (st != ESTOPPED) {
      for (uint8_t i = 0; i < N_SERVOS; i++) {
        long step = (long)SERVO_CFG[i].maxDps * (long)dt;  // deg/s * ms = milli-degrees
        long d = tgtMd[i] - curMd[i];
        if (d > step) d = step;
        if (d < -step) d = -step;
        if (d) {
          curMd[i] += d;
          servos[i].write((int)(curMd[i] / 1000));
        }
      }
    }
  }
}
