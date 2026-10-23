# 调研：AI 应用开发 × 硬件（软硬件结合），2026-10

本文是 v0.2 改版的依据。调研方式：两轮共 9 路网络调研（第一轮 5 路，第二轮 4 路）、在本地读 30 个开源项目的源码（浅克隆，只读、不运行），再在小红书上做少量只读搜索。

星数以 2026-10 抓取时为准。标「未核实」的条目没有拿到一手来源。

## 1. 结论

1. **「MIDI 键盘 → 智能体 → 硬件」这条链路还没人做全。**
   - 现有的 MIDI 类 agent 项目都只有「状态灯 + 批准键」，例如 [pad-lattice](https://github.com/mrueda/pad-lattice)（Launchpad，接 Codex，3★）、[soundpad](https://github.com/davidef393s/soundpad)、[agent-launchpad](https://github.com/lreimer/agent-launchpad)、[mf3d-cockpit](https://github.com/clearchaos3/mf3d-cockpit)。
   - 现有的 MIDI MCP 几乎都是 AI 往外发 MIDI：[ableton-mcp](https://github.com/ahujasid/ableton-mcp) 约 3.2k★，另有 FL Studio、REAPER、Elektron 的 MCP。
   - 没有一个项目同时具备三点：让智能体主动发问（ask_confirm / ask_choice）、把触摸条或推子当连续参数、再去驱动串口硬件。
2. **实体审批有人做，但闸门要放在 hook 层。** 实体按键给 agent 做审批的项目已经很多，比如 Stream Deck、M5Stick、游戏手柄、Anthropic 自己的 [Claude Desktop Buddy](https://github.com/anthropics/claude-desktop-buddy)（BLE，两个键）。真正有约束力的是 Claude Code 的 `PermissionRequest` hook，MCP 工具层的 `ask_confirm` 模型可以选择不调。所以 v0.2 两层都做了。
3. **硬件安全必须放在固件里，不能靠模型。**
   - Anthropic 2026-10-08 更新的使用政策新增「High-risk Physical Actions」一节（[公告](https://www.anthropic.com/news/2026-usage-policy-update)），要求三条：有人能随时让设备停下；断开连接时设备要进入安全状态；速度、力这类限值由设备本身或独立控制器强制执行。
   - 参考项目里只有 LeKiwi 有真正的看门狗（500 ms，见 `lerobot/robots/lekiwi`）。[so101-mcp](https://github.com/Tibe31/so101-mcp) 一次动作要 40 秒、中途不能打断，README 只叫人「手放在电源上」。这正是实体控制台能补的缺口。
4. **Anthropic MHS（Model Hardware Standard）是真的，但目前只是研究预览。**
   - 2026-08-27 发布（[公告](https://www.anthropic.com/news/model-hardware-standard-research-preview)），只对实验室和制造厂开放，规范和 SDK 都没公开。
   - 我们能做的是在形状上对齐：设备自描述能力和限值（`DESCRIBE`），读和写分开，MCP 只当其中一种接入方式。现在不宣称兼容 MHS。
5. **中文创客圈的现状。**
   - 生态中心是小智 [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)，约 3.2 万★，有设备端 MCP 和云端 MCP 接入点。
   - 最常见的吐槽：ESP32 AI 玩具只是「对讲机」，玩半个月就吃灰，离线不能用，平台受限。
   - 我们能占的位置：确定性的实体确认、连续量调节、离线（本地模型 + 串口）、有用的场景（编曲、灯光、低成本遥操作）。

## 2. 已经按调研落地到 v0.2 的改动

| 来源 | 改动 |
|---|---|
| WinMM 文档：回调里不能再调多媒体函数 | MIDI 回调只负责入队，解码和后续处理放到独立的分发线程 |
| MCP SDK 2.3 源码核对：`to_thread` 取消不掉 | 等按键改成 `asyncio` 队列：可以取消，每秒报一次进度，避免被 Claude Code 判空闲超时 |
| dsh-gamepad 的坑：开始前就按着的键被当成批准 | 提问前已按住的键不算；问题发出后 0.3 秒内的按键也不算 |
| cardputer 分档；三段使能开关的思路 | `dangerous` 模式：长按 1.5 秒才算批准，轻按无效；拒绝永远只要按一下 |
| agent-launchpad 的急停 | 左半区同时按下 3 个以上的键：急停并锁存。所有绑定解除，板子收到 ESTOP，所有等待返回 estop；只有人长按才能解除 |
| 并行工具调用会让一次按键批准多个请求 | MCP 工具和 hook 共用同一把锁，一次只问一件事，按到达顺序排队 |
| soundpad / yolodeck / ClaudeDeck | `daemon` 只绑 127.0.0.1；检查 Host、拒绝带 Origin 的请求、要求 JSON、Bearer token 用 `hmac.compare_digest` 比较；超时返回 204，交回终端提示 |
| cardputer + claude-console RiskClassifier | 危险规则：`rm -rf`、`git push`、`curl \| sh`、`iwr \| iex`、`Remove-Item -Recurse`、发布命令、改 `.claude/settings` 等 |
| LeKiwi 看门狗、OpenCat 超长行处理、xiaozhi 的范围校验 | 线协议 v2：请求带编号；整数严格解析；超长的行回报错、不执行；1 秒没心跳就进入 SAFE；ESTOP 锁存到 RESET；每路舵机有自己的限位和转速上限；DRIVE 必须带时长、到点自动停；令牌桶限速；`DESCRIBE` 自描述 |
| MicroLab mk3 没有能由电脑控制的灯 | 声音反馈：用 Windows 自带的 GS 合成器播提示音，区分提问、是、否、超时、急停 |

## 3. 还没做、按价值排序

1. **实机核对 MicroLab mk3**：用 `learn` 命令。目前触摸条 = CC1、Hold = CC64 只是推测。
2. **固件上真板**：Uno 和 ESP32 都已编译通过，但还没在板子上跑过。
3. **小车示例和演示视频**：
   - BOM 控制在 100 元以内：ESP32 + TB6612 + TT 电机底盘 + SG90。
   - 力度对应车速，触摸条对应转向，延音踏板当「按住才动」的安全踏板。
   - 拍 60–90 秒，发 B 站。
4. **接入小智**：用 [mcp_pipe.py](https://github.com/78/mcp-calculator) 把本服务挂到 xiaozhi.me，做成「语音说意图、琴键确认、推子微调」。
   - 注意：小智每次重连都会重启子进程、重新打开串口。打开串口时板子会复位，所以固件上电状态必须是安全状态（v2 已经是）。
   - 等按键的工具要短超时（≤10 秒）。
5. **网页一键烧录**：ESP Web Tools + GitHub Pages。
6. **没有键盘时的退路**：用 MCP elicitation（SDK 2.3 的 `Resolve` + `Elicit`）在 Claude Code 里弹对话框。
7. **更多键盘的 profile**：MPK Mini、Launchkey（可以回写灯）、nanoKONTROL2，以及国产的 M-VAVE SMK-25 等。
8. **分发**：PyPI + `uvx midi-agent-console`，登记到官方 MCP Registry（`server.json`）。

## 4. 主要参考（本地浅克隆在 `D:\ai-hw-refs\`）

- 实体审批：agentsd、yolodeck、soundpad、agent-launchpad、cardputer-claude-mcp、dsh-gamepad-approval、claude-console、vibesignal、claude-desktop-buddy、pad-lattice
- 机器人和设备端：xiaozhi-esp32、mcp-calculator、lerobot（LeKiwi）、so101-mcp、robot_MCP、reachy_mini、StackChan、OpenCat、ros-mcp-server、ROSA
- MIDI：mcp-server-midi、thias42/midi-mcp、barmoshe/midi-agent、ableton-mcp、fl-studio-mcp、aria、magenta-realtime、rtmidi2
- 规范：[Claude Code hooks](https://code.claude.com/docs/en/hooks)、[MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/changelog)、[Windows MIDI Services](https://microsoft.github.io/MIDI/)

## 5. 第三轮（v0.3）：新发现和据此做的改动

**外部变化**
- **Claude Code 的 auto mode 改成默认开启。** 一手来源：[Anthropic 博客](https://claude.com/blog/auto-mode-default-in-claude-code)，10-22 更新为「下周」在多数应用推开。官方说法是 93% 的提示本来就会被批准，所以普通操作基本不会再弹窗。
  - 改动：daemon 在 `PreToolUse` 阶段对危险操作返回 `ask`，[hooks 文档](https://code.claude.com/docs/en/hooks) 写明这样会强制弹窗；auto mode 拒绝的操作（`PermissionDenied`）响一声「否」。
- **7–10 月新冒出来的同类项目**：nod、FunMedal、tally、hermes-remote-cardputer、dsh-hardware-buddy（DSH 的 A/B 键审批）、agent-estop 等。还是只有「状态 + 批准键」，没有一家同时做到风险分级、锁存急停和硬件执行。

**HCI 和工业依据 → 改动**

| 依据 | 改动 |
|---|---|
| 虚拟按钮实验：多种反馈叠加时，人会以为已经按下、提前松手（TOCHI 2014） | 长按期间每 0.4 秒「嘀」一声，到时间才响「是」 |
| 三段使能开关：用力捏死也会停 | 危险确认时用力砸（力度 ≥124）算「否」 |
| 本机 HTTP MCP 今年出了一串 DNS rebinding CVE；本机任何进程都能调 `/mcp` | `/mcp` 加 Bearer token（SDK 已经检查 Host 和 Origin） |
| 审批要留「收据」 | 每次决定写一条带哈希链的 JSONL，用 `audit` 命令校验 |
| 终端和 hook 并行，谁先答算谁；用户先答了，hook 会被取消 | daemon 发现连接断开就立刻取消等待（这一轮实测时发现过这个 bug，已修） |
| 失败时放行被评为高危（mobissh） | 所有失败都交回原来的弹窗，从不放行 |

**官方手册核对（MicroLab mk3 手册 1.0.0）**
- 两条触控条发弯音和调制（p.10）。
- **Hold 是锁存式**（p.9）：按一下锁住，再按一下松开。所以不再把 Hold 当「是」，改成踏板（CC64，按住才生效，p.20）。
- 手册没写调制条的 CC 号、Hold 发不发 CC64、默认最低音，这几项仍要实机测。
- 新增 profile：nanoKEY2、Launchkey Mini MK4、MPK mini mk3，每个都在 `unverified` 里列出了手册没写的项。

**多个智能体**：Claude Code、DSH、Codex 都能用 Claude Code 的 hook JSON 当唯一协议，见 [integrations.md](integrations.md)。
- DSH：原生插件，`approval/request` → PermissionRequest。DSH 的审批请求里不带参数，所以插件在 `tools/pre-execute` 阶段先按 `callId` 记下来。
- Codex：只能跑命令型 hook，用 `hook-relay` 转发。Codex 会把 `interrupt` / `continue` 当成失败，所以转发时去掉。

**不用键盘也能试**：浏览器虚拟键盘 `/keys`；`serve` 找不到键盘时改用 MCP elicitation 对话框。不走 WebMIDI，因为 Chrome 124 起 WebMIDI 要用户授权，而且 Windows 上浏览器建不了虚拟 MIDI 口。

**还没做**
- 提示音延迟：有个人实测 GS 合成器约 250 ms（未核实），超过 100 ms 的感知门槛，以后可以换成预渲染的 WAV、走 WASAPI 低延迟播放。
- 多会话：给每个会话一段不同的短提示音。
- 危险确认键每次随机指定，用来防习惯化。