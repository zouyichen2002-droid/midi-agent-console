# midi-agent-console

**把一台几十块钱的 MIDI 键盘，变成 AI 智能体的实体控制台；再让智能体通过它去控制外部硬件。**

*A MIDI keyboard as a physical console for AI agents (via MCP) — approve, choose and steer by touch, and let the agent drive external hardware (LEDs, servos, robots) over serial.*

现有的 MIDI MCP（Ableton / FL Studio / 合成器控制）几乎都是 **AI → MIDI**：让模型去弹、去写音符。
这个项目反过来：**人 → MIDI → AI → 硬件**。

```
 ┌────────────┐  USB-MIDI  ┌──────────────────────────┐  MCP (stdio) ┌──────────────┐
 │ MIDI 键盘   │──────────▶│  midi-agent-console      │◀────────────▶│ Claude Code / │
 │ 琴键 触摸条 │            │  · 实时控件状态            │              │ 本地大模型     │
 │ Hold 踏板   │            │  · ask_confirm/ask_choice │              └──────────────┘
 └────────────┘            │  · 控件→硬件 实时绑定       │  串口 / 模拟
                           └─────────────┬────────────┘─────────────▶ ESP32 / Arduino
                                         │                            舵机 · 灯 · 小机器人
```

## 能做什么

| MCP 工具 | 作用 |
|---|---|
| `ask_confirm` | 智能体做高风险操作前，**等你在键盘上点头**：右半边（中央 C 及以上）或 Hold = 是，左半边 = 否 |
| `ask_choice` | 给 2–7 个选项，**按白键选**：C=1 D=2 E=3 F=4 G=5 A=6 B=7，任意八度 |
| `wait_for_note` | 等任意一个键 |
| `read_controls` | 读触摸条 / 滚轮 / 踏板的当前位置（连续值）和按住的键 |
| `recent_events` | 最近的 MIDI 事件 |
| `device_send` | 给硬件发命令：`PING` / `STATE` / `LED i r g b` / `SERVO i 角度`（先校验范围，不能发任意字节） |
| `bind_control` | **把触摸条直接绑到硬件**，例如 `mod_strip → SERVO 0 {v}`：手指滑一下舵机就转，不经过模型 |
| `console_status` / `unbind_control` | 状态、解绑 |

没有硬件也能跑：默认 `--device sim` 用模拟设备。

## 快速开始（Windows，Python 3.10+）

```bash
git clone https://github.com/zouyichen2002-droid/midi-agent-console
cd midi-agent-console
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[serial]"
.venv\Scripts\python -m midi_agent_console ports
```

接到 Claude Code（把路径换成你的）：

```bash
claude mcp add midi-console -- C:\path\to\midi-agent-console\.venv\Scripts\python.exe -m midi_agent_console serve --device sim
```

接了板子就把 `--device sim` 换成 `--device COM5`（或 `COM5@115200`），板子刷 [`firmware/line_protocol`](firmware/line_protocol/line_protocol.ino)。

其他命令：

```bash
python -m midi_agent_console monitor            # 打印键盘发出的每条消息
python -m midi_agent_console learn              # 逐个碰控件，记下它发的是什么
python -m midi_agent_console --port loopMIDI serve
```

Windows 上直接用系统自带的 winmm（ctypes），**不需要 python-rtmidi**（它没有 3.13+ 的 Windows 安装包）。
macOS / Linux 用 `pip install -e ".[mido]"` 走 mido + rtmidi。

## 支持的键盘

- **Arturia MicroLab mk3**（开发用机）：弯音条 = `pitch_strip`，调制条 = `mod_strip`（CC1），Hold = CC64。
  这几个映射是这类键盘的常规默认值，**还没在实机上逐个核对**，用 `learn` 确认后改 [`profiles.py`](midi_agent_console/profiles.py)。
- 其他键盘：`--profile generic`，或写一个 JSON profile 用 `--profile my.json`。

## 现状（v0.1.0）

- ✅ 已验证：Windows 11 + Python 3.13，用 loopMIDI 回环口注入 MIDI，真实 MCP 客户端走 stdio 调用全部流程（确认、选择、触摸条绑定模拟舵机、非法命令被拒）；`pytest` 8 项通过。
- ⏳ 未验证：MicroLab mk3 实机的每个控件编号；固件在真板子上（还没有板子，没编译过）。

## 路线图

1. 实机核对 MicroLab mk3 的映射；加更多键盘的 profile
2. 固件实测：ESP32 + 舵机 + WS2812 灯带
3. 「机器人外置大脑」示例：电脑上的大模型规划，键盘做人在环里的确认与微调，ESP32 小车执行
4. 本地大模型（llama.cpp / Ollama）直接调用的示例
5. BLE-MIDI、Windows MIDI Services（MIDI 2.0）

## 协议

MIT © 小鳄鱼aligator
