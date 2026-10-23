# midi-agent-console

**把一台几十块钱的 MIDI 键盘，变成 AI 智能体的实体控制台；再让智能体通过它去控制外部硬件。**

*A MIDI keyboard as a physical, human-in-the-loop console for AI agents — answer Claude Code permission prompts by touch, let the agent ask you questions you answer with keys, steer with touch strips, and let it drive external hardware (servos, LEDs, a small robot) behind firmware-enforced limits and a physical emergency stop.*

现有的 MIDI 类 agent 项目，多数只做「状态灯 + 批准键」；现有的 MIDI MCP，多数是 AI 往外发音符。
这个项目做的是另一个方向：**人 → MIDI → AI → 硬件**。调研依据见 [docs/research-2026-10.md](docs/research-2026-10.md)。

```
 ┌──────────────┐ USB-MIDI ┌───────────────────────────────┐  MCP (HTTP / stdio)  ┌───────────────┐
 │ MIDI 键盘     │────────▶│ midi-agent-console daemon      │◀────────────────────▶│ Claude Code / │
 │ 琴键 触摸条    │         │ · ask_confirm / ask_choice     │  PermissionRequest   │ 本地大模型      │
 │ Hold 踏板     │         │ · 急停（左半区一把按下）         │◀──── hook (HTTP) ────│               │
 └──────────────┘         │ · 控件 → 硬件 实时绑定           │                      └───────────────┘
        ▲ 提示音            └──────────────┬────────────────┘
        └ Windows GS 合成器                │ 串口 · 线协议 v2（心跳 / 限位 / 急停在固件里）
                                          ▼
                                 ESP32 / Arduino：舵机 · 灯 · 小车
```

## 键盘怎么用（MicroLab mk3，25 键，中央 C = C4）

| 操作 | 含义 |
|---|---|
| 右半边（中央 C 及以上）按一下，或按 Hold | **是 / 允许** |
| 左半边按一下 | **否 / 拒绝**（拒绝永远只要一下） |
| 危险操作：右半边**长按 1.5 秒** | 允许；轻按无效，防止误碰 |
| 白键 C D E F G A B（任意八度） | 选项 1–7 |
| **左半边一把按下 3 个以上的键** | **急停**：解除所有绑定、给板子发 ESTOP、拒绝所有等待中的请求。锁住后，只有人长按才能解除 |
| 触摸条 | 连续量（0–127 / 弯音），可以直接绑到舵机或灯 |

防误按规则：
- 提问前就按着的键不算；
- 问题发出后 0.3 秒内的按键不算；
- 一次按键只回答一个问题，多个请求按到达顺序排队。

声音反馈：提问是上行琶音，「是」是大三度，「否」是小二度，超时是一声低音，急停是低音簇。用 `--quiet` 关掉。

## 快速开始（Windows，Python 3.10+）

```bash
git clone https://github.com/zouyichen2002-droid/midi-agent-console
cd midi-agent-console
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[serial]"
.venv\Scripts\python -m midi_agent_console ports
```

### 方式 A：只用 MCP 工具（stdio）

```bash
claude mcp add midi-console -- C:\path\to\midi-agent-console\.venv\Scripts\python.exe -m midi_agent_console serve --device sim
```

### 方式 B：daemon。键盘同时回答 Claude Code 的权限弹窗（推荐）

一个进程独占 MIDI 口，同时提供 MCP（`/mcp`）和 hook（`/hook`），只监听 127.0.0.1。

```bash
.venv\Scripts\python -m midi_agent_console daemon --device sim
```

```bash
claude mcp add --transport http midi-console http://127.0.0.1:47811/mcp
```

```bash
.venv\Scripts\python -m midi_agent_console token
```

把打印出来的 token 设成环境变量（`setx MIDI_AGENT_CONSOLE_TOKEN <token>`），然后把 hook 写进 Claude Code 设置（会先备份原文件；重复运行不会重复添加）：

```bash
.venv\Scripts\python -m midi_agent_console hooks install
```

重启 Claude Code 之后，权限弹窗出现时 daemon 会在控制台里打印要批准的命令，然后等你按键：
- 普通命令：按一下就行。
- `git push`、`rm -rf`、`curl | sh`、`Remove-Item -Recurse`、改 `.claude/settings`、发布包等：要长按。
- 110 秒没人按：交回终端里原来的弹窗，不会替你做决定。

`daemon --answer-questions`（实验）：用白键回答 AskUserQuestion。撤销 hook：`hooks uninstall`。

### 接硬件

把 `--device sim` 换成 `--device COM5`，板子刷 [`firmware/line_protocol`](firmware/line_protocol/line_protocol.ino)。

## MCP 工具

| 工具 | 作用 |
|---|---|
| `ask_confirm(question, dangerous)` | 等你在键盘上回答是 / 否；`dangerous=true` 时要长按 |
| `ask_choice(question, options)` | 用白键选 1–7 |
| `wait_for_note` | 等任意一个键 |
| `read_controls` / `recent_events` | 触摸条、踏板的当前值，按着的键，最近的事件 |
| `device_describe` | 板子自报的能力：每路舵机的限位、安全位、转速上限，电机限制，心跳超时 |
| `device_send` | `SERVO` / `LED` / `DRIVE l r ms` / `STOP` / `ESTOP` / `STATE`，先按设备能力校验；`RESET` 只留给人 |
| `bind_control` / `unbind_control` | 把触摸条直接绑到硬件，例如 `mod_strip → SERVO 0 {v}`，范围默认取舵机自己的限位 |
| `emergency_stop` / `reset_emergency_stop` | 急停；解除必须由人长按确认 |
| `console_status` | 连接状态、绑定、急停状态 |

等按键期间每秒报一次进度，客户端取消时立刻清理，不会留下继续占着按键的等待。

## 硬件线协议 v2（安全放在固件里）

```
请求  <id> <VERB> [int ...]\n         回复  <id> OK [...] | <id> ERR <CODE>     事件  ! WDT / ! DRIVE_DONE / ! BOOT
```

- **看门狗**：1 秒没收到有效帧，就进入 SAFE：电机停，舵机回安全位，灯灭。电脑端每 300 ms 发一次心跳，进程崩了、线拔了，板子会自己停下来。
- **ESTOP** 锁存，直到收到 RESET；锁存期间所有运动命令都回 `ERR ESTOP`。
- **舵机**：每路有自己的 min/max，越界是拒绝，不夹紧；按 `max_dps` 限速转动。
- **DRIVE** 必须带时长（≤2000 ms），到点自动停。
- **解析**：超长的行回 `LINE_TOO_LONG`，绝不执行；`+5`、`9_0`、全角数字一律报错；令牌桶限速 100 帧/秒。
- **上电状态就是安全状态**：很多板子在打开串口时会复位，所以这一点必须保证。

这些规则对应 Anthropic 2026-10 使用政策里「High-risk Physical Actions」的要求：人随时能让设备停下；断开连接时设备进入安全状态；限值由设备本身强制执行。不过本项目是学习和演示用的，**不要接门锁、继电器、电源这类东西**，舵机电源最好另加一个硬件开关。

## 现状（v0.2.0）

**已验证**
- 自动测试：`pytest` 36 项（Windows 11 + Python 3.13）。
- 端到端：起真实的 daemon 进程，用 loopMIDI 回环口注入按键，走真实的 HTTP：
  - 允许、拒绝、超时交回终端、危险命令长按，四种结果都对；
  - MCP over HTTP 的 `ask_choice` 也通过。
- 固件：`arduino-cli` 编译通过。Uno 用 24% 的程序空间、23% 的内存；ESP32 用 21%。

**未验证**
- MicroLab mk3 实机的每个控件编号（用 `learn` 命令核对）；
- 固件在真板子上运行；
- 在真实的 Claude Code 里走一遍 hook（目前是按官方文档的格式模拟请求测的）；
- AskUserQuestion 的回答格式。

## 路线图

见 [docs/research-2026-10.md](docs/research-2026-10.md) 第 3 节：
1. 实机核对；
2. 固件上板；
3. 100 元以内的小车示例和 B 站演示；
4. 接入小智；
5. 网页一键烧录；
6. 没有键盘时用 MCP elicitation 兜底；
7. 更多键盘的 profile；
8. 发到 PyPI 和 MCP Registry。

## 和类似项目的区别

| | 本项目 | Claude Desktop Buddy | pad-lattice / soundpad / agent-launchpad |
|---|---|---|---|
| 输入 | 25 键 + 触摸条 + 踏板 | 2 个键（BLE） | Launchpad 格子 |
| 智能体主动提问 / 选项 / 连续量 | ✅ MCP 工具 | ❌ | ❌ |
| 权限弹窗 | ✅ hook，分危险等级 | ✅ | ✅ |
| 驱动外部硬件 | ✅ 线协议 v2 + 固件安全 | ❌ | ❌ |
| 物理急停 | ✅ 一把按下，锁存 | ❌ | agent-launchpad 有 |

## 协议

MIT © 小鳄鱼aligator
