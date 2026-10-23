# 无线：ESP-NOW 中转器

```
电脑 ──USB 串口── 中转器（ESP32 / C3） ──ESP-NOW── 小车（ESP32 / C3）
```

电脑这边什么都不用改：中转器的串口和小车自己的 USB 口用的是同一份线协议 v2，`--device COM7` 指向中转器就行。

> 状态：两份固件都编译通过（ESP32 和 C3 SuperMini），**还没在实机上跑过**。延迟、丢包率、两块板子能不能连通，都没测。

## 安全不变

- 小车**同时**收 USB 串口和 ESP-NOW 的命令。两条路的帧喂的是同一个看门狗：无线断了和拔线一样，1 秒后进入 SAFE，并发出 `! WDT`。
- 小车只认配对的那一个中转器 MAC，别的包直接丢掉。
- 中转器挂了或者断电，小车照样靠自己的看门狗停下来。

## 配对

1. **先烧中转器**：`firmware/espnow_dongle`，`CAR_MAC` 可以先留全 0。打开串口，记下 `! DONGLE mac=…`。
2. **烧小车**：`firmware/line_protocol`。在 `DONGLE_MAC` 里填第 1 步的 MAC，编译时打开无线：

   ```bash
   arduino-cli compile -b esp32:esp32:nologo_esp32c3_super_mini --build-property "compiler.cpp.extra_flags=-DTRANSPORT_ESPNOW=1" firmware/line_protocol
   ```

   从小车的 USB 串口记下 `! BOOT … mac=…`，并确认 `peer=` 是中转器的 MAC、`espnow=ok`。
3. **回填中转器**：把小车的 MAC 填进中转器的 `CAR_MAC`，重新烧一次，确认 `car=` 对了。
4. **换电池跑**：拔掉小车的 USB，用电池供电。电脑连中转器：`--device COM7`。
5. **验一遍**：
   - 关掉小车电源，中转器大约 2 秒后报 `! LINK lost`；
   - 让电脑停止发心跳（关掉 daemon），小车 1 秒后进入 SAFE。

## 细节

- **一行协议对应一个 ESP-NOW 包。** 一行最长 63 字节，超长的行中转器在本地就丢掉，回 `0 ERR LINE_TOO_LONG`，不会发给小车。
- **小车的回复按 ≤240 字节一块分包发**（ESP-NOW v1 单包上限 250 字节）。DESCRIBE 有 300 多字节，会拆成两包。单播一般按顺序到，但不保证一定到。丢了一包，电脑会收到缺半截的行，那次请求按超时处理。
- **信道固定为 1，不加密。** 如果以后让小车连路由器，路由器的信道会把这个设置覆盖掉。
- **收包回调只做过滤和拷贝**，放进一个 8 格队列，真正的处理在 `loop()` 里。原因是回调跑在 WiFi 任务里，要尽量短。
