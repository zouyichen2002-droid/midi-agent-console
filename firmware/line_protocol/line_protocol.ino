// midi-agent-console line protocol, reference firmware for Arduino / ESP32.
// NOT yet tested on real hardware (no board on the dev machine) - please report results.
//
//   PING              -> PONG
//   SERVO <i> <deg>   -> OK      servo i on SERVO_PINS[i], 0-180
//   LED <i> <r> <g> <b> -> OK    LED 0 = on-board LED, brightness = max(r, g, b)
//   STATE             -> one line summary
// 115200 baud, newline-terminated ASCII. The PC side validates ranges too.

#if defined(ESP32)
#include <ESP32Servo.h>   // Library Manager: "ESP32Servo"
#else
#include <Servo.h>
#endif

#ifndef LED_BUILTIN
#define LED_BUILTIN 2
#endif

const int SERVO_PINS[] = {9, 10};  // change for your wiring (ESP32: e.g. 18, 19)
const int N_SERVOS = sizeof(SERVO_PINS) / sizeof(SERVO_PINS[0]);
Servo servos[N_SERVOS];
int servoDeg[N_SERVOS];
int ledLevel = 0;

String line;

void setup() {
  Serial.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
  for (int i = 0; i < N_SERVOS; i++) {
    servos[i].attach(SERVO_PINS[i]);
    servoDeg[i] = 90;
    servos[i].write(90);
  }
}

void handle(String cmd) {
  cmd.trim();
  int a[4] = {0, 0, 0, 0};
  int n = 0;
  int sp = cmd.indexOf(' ');
  String op = sp < 0 ? cmd : cmd.substring(0, sp);
  String rest = sp < 0 ? "" : cmd.substring(sp + 1);
  while (rest.length() && n < 4) {
    int s = rest.indexOf(' ');
    a[n++] = (s < 0 ? rest : rest.substring(0, s)).toInt();
    rest = s < 0 ? "" : rest.substring(s + 1);
  }

  if (op == "PING") {
    Serial.println("PONG");
  } else if (op == "SERVO" && n == 2 && a[0] >= 0 && a[0] < N_SERVOS && a[1] >= 0 && a[1] <= 180) {
    servoDeg[a[0]] = a[1];
    servos[a[0]].write(a[1]);
    Serial.println("OK");
  } else if (op == "LED" && n == 4 && a[0] == 0) {
    ledLevel = max(a[1], max(a[2], a[3]));
    analogWrite(LED_BUILTIN, constrain(ledLevel, 0, 255));
    Serial.println("OK");
  } else if (op == "STATE") {
    Serial.print("led=");
    Serial.print(ledLevel);
    for (int i = 0; i < N_SERVOS; i++) {
      Serial.print(" servo");
      Serial.print(i);
      Serial.print("=");
      Serial.print(servoDeg[i]);
    }
    Serial.println();
  } else {
    Serial.print("ERR ");
    Serial.println(cmd);
  }
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      handle(line);
      line = "";
    } else if (c != '\r' && line.length() < 64) {
      line += c;
    }
  }
}
