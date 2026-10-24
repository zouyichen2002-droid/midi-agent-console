# midi-agent-console

<!-- mcp-name: io.github.zouyichen2002-droid/midi-agent-console -->

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

其他键盘用 `--profile nanokey2 | launchkey_mini_mk4 | mpk_mini_mk3 | generic`，或者自己写一个 JSON 用 `--profile my.json`。

**拿到新键盘，先跑引导式学习**：它一步步告诉你按哪个控件，按完把映射写成 profile 文件。

```bash
python -m midi_agent_console learn --wizard --out my-keyboard.json
```

依次是：最低键、最高键、每条触摸条或滚轮、Hold 按两下、踏板按两下。
- 用最低键和最高键算出左右分界。
- 从 Hold 按两下的结果判断它是**锁存式**（按一下锁住）还是**按住才生效**的。锁存式的按钮绝不拿来当「是」。
- 键盘没发出来的控件不会写进去，列在 `unverified` 里。

| 操作 | 含义 |
|---|---|
| 右半边（中央 C 及以上）按一下，或踩延音踏板 | **是 / 允许**（MicroLab 的 Hold 是按一下锁住、再按一下松开的，不当「是」用） |
| 左半边按一下 | **否 / 拒绝**（拒绝永远只要一下） |
| 危险操作：右半边**长按 1.5 秒** | 允许；按住期间每 0.4 秒「嘀」一声，到时间才响「是」。轻按无效；**用力砸（力度 ≥124）算「否」**，当作慌了 |
| 白键 C D E F G A B（任意八度） | 选项 1–7 |
| **左半边一把按下 3 个以上的键** | **急停**：解除所有绑定、给板子发 ESTOP、拒绝所有等待中的请求。锁住后，只有人长按才能解除 |
| 触摸条 | 连续量（0–127 / 弯音），可以直接绑到舵机或灯 |

防误按规则：
- 提问前就按着的键不算；
- 问题发出后 0.3 秒内的按键不算；
- 一次按键只回答一个问题，多个请求按到达顺序排队。

声音反馈：提问是上行琶音，「是」是大三度，「否」是小二度，超时是一声低音，急停是低音簇。
- 危险操作的提示音每次随机换一种，免得听惯了；不同的智能体会话有各自的起始音。
- 默认用一直开着的 WASAPI 音频流，实测按下后约 80 ms 出声。不装 `sounddevice` 时退到系统自带的 PlaySound。Windows 自带的 GS 合成器约 277 ms，超过人能察觉的 100 ms，只留作 `--feedback midi` 备用。
- `pip install -e ".[audio]"` 装上 sounddevice；`--quiet` 关掉声音。

**没有 MIDI 键盘也能用**：
- `serve` 找不到键盘时，`ask_confirm` / `ask_choice` 自动改成客户端里的对话框（MCP elicitation）。
- 没有键盘时，`daemon` 会开一个浏览器虚拟键盘，地址是启动时打印的 `/keys?k=…`。可以点击、长按、多指触屏，和真键盘走同一套逻辑。
  - **接了真键盘时默认关闭**，要用加 `--virtual-keys`。
  - 访问它用一把每次启动随机生成的钥匙，只存在内存里，不写文件、不进环境变量，和 hook 的 token 也不是同一个。
  - **局限**：能读到 daemon 控制台输出、或以你的身份运行的程序，都能按虚拟键。所以它只用来试用；必须由人批准的事，请用真键盘。
  - 防卡键：页面按顺序发送、每条带序号，过期的直接丢掉；切走窗口就松开所有键；服务端 1 秒没收到心跳也会自动松开。

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

一个进程独占 MIDI 口，同时提供下面几个接口，只监听 127.0.0.1：
- MCP：`/mcp`，要 token；
- hook：`/hook`；
- 虚拟键盘：`/keys`。

```bash
.venv\Scripts\python -m midi_agent_console daemon --device sim
```

```bash
.venv\Scripts\python -m midi_agent_console token
```

```bash
claude mcp add --transport http midi-console http://127.0.0.1:47811/mcp --header "Authorization: Bearer <token>"
```

把 token 设成环境变量（`setx MIDI_AGENT_CONSOLE_TOKEN <token>`），然后把 hook 写进 Claude Code 设置（会先备份原文件；重复运行不会重复添加）：

```bash
.venv\Scripts\python -m midi_agent_console hooks install
```

重启 Claude Code 之后，权限弹窗出现时 daemon 会在控制台里打印要批准的命令，然后等你按键：
- 普通命令：按一下就行。
- `git push`、`rm -rf`、`curl | sh`、`Remove-Item -Recurse`、改 `.claude/settings`、发布包等：要长按。
- 110 秒没人按：交回终端里原来的弹窗，不会替你做决定。任何失败都不会变成「允许」。
- 你在终端里先答了：Claude Code 会取消 hook，daemon 看到连接断开就立刻丢掉这个请求，不会让下一次按键答到旧请求上。
- **Claude Code auto mode**（2026 年起多数应用默认开启）：普通操作大多不再弹窗。daemon 会在 `PreToolUse` 阶段对危险操作返回 `ask`，强制弹窗交给键盘。要关掉这个行为用 `--no-force-ask`。auto mode 自己拒掉的操作，键盘会响一声「否」。
- 每次决定都记进 `%LOCALAPPDATA%\midi-agent-console\audit.jsonl`，带哈希链，改过或删过就对不上。用 `midi_agent_console audit` 校验。

`daemon --answer-questions`（实验）：用白键回答 AskUserQuestion。撤销 hook：`hooks uninstall`。

**也能接 DSH 和 Codex CLI**：同一个 daemon、同一套规则，见 [docs/integrations.md](docs/integrations.md)。

### 接硬件

把 `--device sim` 换成 `--device COM5`，板子刷 [`firmware/line_protocol`](firmware/line_protocol/line_protocol.ino)。

**ROS 2 机器人**（turtlesim、TurtleBot3、ros2_control 差速底盘）：先在机器人上起 rosbridge_server，再运行下面的命令。

```bash
pip install -e ".[ros]"
```

```bash
python -m midi_agent_console daemon --device ws://robot:9090#tb3-jazzy
```

- profile 有 `turtlesim`、`tb3-humble`、`tb3-jazzy` 三种。
- 每次 DRIVE 都有时限，到时由这边连发零速。原因是真机 TurtleBot3 和 Gazebo 在 cmd_vel 断了以后会保持最后一次的速度。
- 上真机之前先做一次拔网线测试。

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
| `teleop_start` / `teleop_stop` | 把车交给人用键盘开 / 收回（见下文） |
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

## 看得见的模拟小车

`daemon --device sim` 时打开 `http://127.0.0.1:47811/sim`：
- 实时画出小车的位置、朝向、走过的轨迹、舵机角度、LED 颜色，急停时车身变红。
- 模拟器按差速模型积分：满速 0.3 m/s，轮距 12 cm。
- 用键盘开车（teleop）或让外置大脑跑任务时，没有硬件也看得到效果，也可以直接录屏当演示视频。

## 外置大脑：本地模型规划，键盘逐步批准

```bash
python -m midi_agent_console brain --task "往前开一点，然后挥一下手臂" --base-url http://127.0.0.1:8081/v1 --api-key-file llama.key --no-thinking --device sim
```

1. **先规划**：模型先写一份带编号的计划，不调用任何工具。你在键盘上批准一次。
2. **再逐步执行**：模型每次只调用一个工具。每一步动作都要你单独批准：开车和舵机按危险操作处理，要长按 1.5 秒。
3. **执行后观察**：每一步之后，程序自己读一次设备状态（STATE）交还给模型。

约束都在程序里，不靠模型自觉：
- **模型拿不到** `device_send`、急停复位、绑定、提问这类工具。它只能用 drive / servo / led / stop / get_state / finish 这 6 个带类型的工具。
- **范围比设备自报的再收紧一档**：速度不超过 120，单次不超过 1000 ms。程序还会再查一遍，越界的调用不会执行，也不会来问你。
- **第一次「否」、急停或超时就停**：立刻发 STOP、结束本次任务，不让模型换个说法再问。
- **有预算**：最多 6 个动作、12 次模型调用、120 秒。工具名不存在或参数 JSON 坏了不会执行，连续两次就结束。
- **不管怎么结束，最后一定发一次 STOP。**

`--base-url` 可以填任何 OpenAI 兼容接口：llama.cpp（要开 `--jinja`）、Ollama（`/v1`）、DeepSeek。测试里用的是写好回复脚本的假模型，见 `tests/test_brain.py`。真模型还没跑过。
## 用键盘开小车（teleop）

不经过大模型，人直接开。

```bash
python -m midi_agent_console teleop --device COM5
```

智能体也可以用 `teleop_start` 把车交给人。开始前会先在键盘上问一句；`teleop_stop` 把键盘还回来。

- **按住右半边的键开车**：白键前进，黑键后退；按得越重越快（力度对应 PWM 90–180）。弯音条转向。
- **松开最后一个键立刻停**（只发一次 STOP）。加速有缓坡，每拍最多加 40；停车不缓。
- 每 50 ms 重发一次 `DRIVE l r 300`：线断了，车最多再走 300 ms 就自己停；1 秒收不到心跳，固件看门狗再兜一次。
- 开车期间**键盘不回答任何问题**：hook 交回终端，MCP 返回 busy。这样开车的按键不会被误当成批准。
- 左半边一把按下仍然是急停，优先级最高。

整套零件 60–100 元（ESP32-C3 SuperMini + TB6612 + 2WD TT 底盘 + SG90），接线见[一键刷写页](site/index.html)。各零件的价格是经验区间，没有核实。

## 现状（v0.5.0）

**已验证**
- 自动测试：`pytest` 150 多项（Windows 11 + Python 3.13），含 [tests/test_safety.py](tests/test_safety.py) 的安全回归测试，每一项都对应代码审查里复现过的一个真实问题。CI 跑 Windows 和 Linux，Python 3.10 和 3.13；另外校验 `server.json` 是否符合官方 schema。
- 端到端（真 daemon 进程 + 真 HTTP）：
  - Claude Code 格式的 hook：允许 / 拒绝 / 超时交回 / 危险操作长按；
  - Codex 方言的 `hook-relay`（stdin/stdout 真跑）；
  - DSH 插件的两个处理器，6 种情况：允许、拒绝、没人按交回、危险操作轻按无效、长按通过、daemon 没开交回；
  - `/mcp` 不带 token 返回 401；带 token 时 `ask_choice` 和危险确认都正常（砸 = 否，长按 1.53 s = 是）；
  - 审计日志的哈希链校验通过；
  - 浏览器虚拟键盘在真浏览器里点击、长按。
- 固件：Uno、ESP32、ESP32-C3 SuperMini 都编译通过；C3 走的是自己的引脚分支。打开 ESP-NOW 的版本和中转器也编译通过，一共 7 种组合。
- 外置大脑：用写好回复脚本的假模型测过 7 种情况。
- 提示音：WASAPI 流约 80 ms，GS 合成器约 277 ms。两者在同一个回采点测，差值可信，绝对值只作参考。

**未验证**
- MicroLab mk3 实机。官方手册没写的项列在 [profiles.py](midi_agent_console/profiles.py) 的 `unverified` 里，用 `learn` 命令核对。
- 固件在真板子上运行、真车行驶。
- 在真的 Claude Code / DSH / Codex 里装上走一遍（目前是按各自的文档格式模拟请求测的）。
- 一键刷写页在 GitHub Pages 上的效果（需要在仓库设置里开 Pages）。
- 小智接入（文档见 [docs/xiaozhi.md](docs/xiaozhi.md)）。

## 路线图

1. 实机核对 MicroLab、上板、上车，拍 60–90 秒演示；
2. ESP-NOW 无线实机联调（两端固件都已编译通过，配对步骤见 [docs/espnow.md](docs/espnow.md)）；
3. 用真模型跑一遍外置大脑；
4. 发布到 PyPI，登记 MCP Registry：`server.json` 和发布流程都准备好了，还需要维护者在 PyPI 上做一次可信发布设置。

调研依据和各项来源见 [docs/research-2026-10.md](docs/research-2026-10.md)。
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
