# 接小智（xiaozhi-esp32）：语音说意图，琴键来确认

小智设备听你说话，交给云端大模型；大模型通过「MCP 接入点」调用本机的 midi-agent-console；需要确认时，你在键盘上按。

连接用官方示例里的 [`mcp_pipe.py`](https://github.com/78/mcp-calculator)：它在本机起一个 stdio MCP 服务，再通过 WebSocket 连到 xiaozhi.me。不需要公网端口。

> 状态：按小智的文档和 `mcp_pipe.py` 源码写的，还没有在真的小智设备上跑过。

## 步骤

1. 在 xiaozhi.me 控制台进入智能体的「配置角色」，打开「MCP 接入点」，复制 `wss://api.xiaozhi.me/mcp/?token=…`。
   **这个 token 等于你的智能体权限，不要提交到任何仓库**，放进本机的 `.env` 或环境变量。
2. 装 `mcp_pipe.py` 需要的依赖（在 mcp-calculator 的目录里）：

   ```bash
   pip install websockets python-dotenv mcp
   ```

3. 写 `mcp_config.json`：

   ```json
   {"mcpServers": {"midi-console": {"type": "stdio",
     "command": "C:/path/to/midi-agent-console/.venv/Scripts/python.exe",
     "args": ["-m", "midi_agent_console", "serve", "--device", "sim", "--quiet", "--max-wait", "10"]}}}
   ```

4. 启动（PowerShell）：

   ```bash
   $env:MCP_ENDPOINT="wss://api.xiaozhi.me/mcp/?token=..."; $env:MCP_CONFIG="C:\path\to\mcp_config.json"; python mcp_pipe.py
   ```

## 为什么要 `--max-wait 10`

- 开源服务端 xiaozhi-esp32-server 对工具调用用的是 `asyncio.wait_for(..., timeout=30)`（[issue #3461](https://github.com/xinnan-tech/xiaozhi-esp32-server/issues/3461)）。xiaozhi.me 云端的超时没有找到官方说明。
- 一轮语音对话也不适合干等一分钟。`--max-wait 10` 把 `ask_confirm` / `ask_choice` 的等待上限压到 10 秒：超时就返回 timeout，让模型再问一次。

## 注意

- **每次重连都会重启 MCP 服务，也就重新打开串口。** 很多开发板打开串口时会复位，所以固件的上电状态必须是安全状态：v2 固件开机时舵机在安全位、电机不转。
- Windows 上 `mcp_pipe.py` 结束子进程时，清理代码不会执行，STOP 发不出去。这时靠固件看门狗：1 秒没心跳就进入 SAFE。
- 语音没法「松手即停」。小车动作只给有时长的 `DRIVE`（≤2000 ms，固件会再卡一次），不要让语音开 `teleop_start`。
- `RESET`（解除急停）只能由人在键盘上长按，不对语音开放。
- MIDI 口如果已经被 `daemon` 占着，`serve` 会找不到键盘，`ask_*` 就改成对话框。小智没有对话框，所以接小智时只跑 `serve`，不要同时开 `daemon`。
