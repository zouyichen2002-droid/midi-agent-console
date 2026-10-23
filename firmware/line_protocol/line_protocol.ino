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
// Compiled for arduino:avr:uno and esp32:esp32:esp32; not yet run on a real board.

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

struct ServoCfg { uint8_t pin; int16_t minDeg, maxDeg, safeDeg, maxDps; };
#if defined(ESP32)
const ServoCfg SERVO_CFG[] = {{18, 10, 170, 90, 180}, {19, 10, 170, 90, 180}};
#else
const ServoCfg SERVO_CFG[] = {{9, 10, 170, 90, 180}, {10, 10, 170, 90, 180}};
#endif
const uint8_t N_SERVOS = sizeof(SERVO_CFG) / sizeof(SERVO_CFG[0]);

// Optional DC motors on a TB6612FNG / L298N style driver. Set HAS_DRIVE 0 if not wired.
#define HAS_DRIVE 1
#define DRIVE_MAX_MS 2000
#if defined(ESP32)
const uint8_t PWMA = 25, AIN1 = 26, AIN2 = 27, PWMB = 14, BIN1 = 12, BIN2 = 13;
#else
const uint8_t PWMA = 5, AIN1 = 7, AIN2 = 8, PWMB = 6, BIN1 = 4, BIN2 = 2;
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
void okHead(long id) { Serial.print(id); Serial.print(F(" OK")); }
void ok(long id) { okHead(id); Serial.println(); }
void err(long id, const __FlashStringHelper* code) {
  Serial.print(id); Serial.print(F(" ERR ")); Serial.println(code);
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
  Serial.print(F(" {\"proto\":2,\"fw\":\"" FW_VERSION "\",\"hb_timeout_ms\":"));
  Serial.print(HB_TIMEOUT_MS);
  Serial.print(F(",\"max_line\":63,\"max_rate_hz\":100,\"servos\":["));
  for (uint8_t i = 0; i < N_SERVOS; i++) {
    if (i) Serial.print(',');
    Serial.print(F("{\"i\":")); Serial.print(i);
    Serial.print(F(",\"min\":")); Serial.print(SERVO_CFG[i].minDeg);
    Serial.print(F(",\"max\":")); Serial.print(SERVO_CFG[i].maxDeg);
    Serial.print(F(",\"safe\":")); Serial.print(SERVO_CFG[i].safeDeg);
    Serial.print(F(",\"max_dps\":")); Serial.print(SERVO_CFG[i].maxDps);
    Serial.print(F(",\"unit\":\"deg\"}"));
  }
  Serial.print(F("],\"leds\":[{\"i\":0,\"min\":0,\"max\":255}],\"drive\":"));
#if HAS_DRIVE
  Serial.print(F("{\"min\":-255,\"max\":255,\"max_ms\":")); Serial.print(DRIVE_MAX_MS);
  Serial.print(F(",\"unit\":\"pwm\"}"));
#else
  Serial.print(F("null"));
#endif
  Serial.println(F(",\"safe_state\":\"motors 0, servos -> safe, leds off\",\"user_only\":[\"RESET\"]}"));
}

void state(long id) {
  okHead(id);
  Serial.print(F(" STATE st=")); Serial.print(stName());
  for (uint8_t i = 0; i < N_SERVOS; i++) {
    Serial.print(F(" s")); Serial.print(i); Serial.print('=');
    Serial.print(curMd[i] / 1000); Serial.print('>'); Serial.print(tgtMd[i] / 1000);
  }
  Serial.print(F(" led0=")); Serial.print(ledRgb[0]); Serial.print(',');
  Serial.print(ledRgb[1]); Serial.print(','); Serial.print(ledRgb[2]);
  long left = driving ? (long)(driveDeadline - millis()) : 0;
  Serial.print(F(" drv=")); Serial.print(driveL); Serial.print(','); Serial.print(driveR);
  Serial.print(','); Serial.println(left < 0 ? 0 : left);
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
  lastFrame = millis();

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
      uint8_t r = parseStrict(tok[2 + k], k < 2 ? -255 : 1, k < 2 ? 255 : DRIVE_MAX_MS, &a[k]);
      if (r) { r == 1 ? err(id, F("BAD_INT")) : err(id, F("RANGE")); return; }
    }
  }

  if (sp->motion && st == ESTOPPED) { err(id, F("ESTOP")); return; }
  if (sp->motion || !strcmp(v, "HB")) armed = true;
  if (sp->motion && st == SAFE) st = RUN;  // SAFE is not latched: a fresh command resumes

  if (!strcmp(v, "HELLO")) {
    okHead(id); Serial.print(F(" HELLO proto=2 fw=" FW_VERSION " hb_ms=")); Serial.println(HB_TIMEOUT_MS);
  } else if (!strcmp(v, "DESCRIBE")) {
    describe(id);
  } else if (!strcmp(v, "HB")) {
    okHead(id); Serial.print(F(" HB st=")); Serial.println(stName());
  } else if (!strcmp(v, "STATE")) {
    state(id);
  } else if (!strcmp(v, "PING")) {
    okHead(id); Serial.println(F(" PONG"));
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
  lastSlew = lastRefill = millis();
  Serial.println(F("! BOOT proto=2 fw=" FW_VERSION));
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

  while (now - lastRefill >= 10) {
    lastRefill += 10;
    if (tokens < 20) tokens++;
  }

  if (armed && st == RUN && now - lastFrame > HB_TIMEOUT_MS) {
    enterSafe();
    Serial.println(F("! WDT"));
  }

  if (driving && (long)(now - driveDeadline) >= 0) {
    motorsOff();
    Serial.println(F("! DRIVE_DONE"));
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
