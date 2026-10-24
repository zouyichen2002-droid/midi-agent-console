# 接入各个智能体

三家都走同一条路：daemon 的 `POST /hook`，协议就是 Claude Code 的 hook JSON。所以审批逻辑只有一份：
- 危险等级、长按、急停、按到达顺序排队；
- 没人按就交回原来的弹窗。

先起 daemon，再生成 token：

```bash
python -m midi_agent_console daemon --device sim
```

```bash
python -m midi_agent_console token
```

| 智能体 | 接法 | 状态 |
|---|---|---|
| Claude Code | http hook，用 `hooks install` 写进设置 | 按官方文档格式做了端到端测试；还没在真的 Claude Code 里装过 |
| DSH（DeepSeek Harness） | 原生插件 `integrations/dsh/index.mjs` | 用真 daemon 测过插件的两个处理器（6 种情况）；还没挂进真 DSH |
| Codex CLI | command hook → `hook-relay --dialect codex` | 用 stdin/stdout 真跑过；还没在真 Codex 里装过 |

## Claude Code

```bash
setx MIDI_AGENT_CONSOLE_TOKEN <token>
```

```bash
python -m midi_agent_console hooks install
```

- `hooks install` 会写入 `PermissionRequest`（等按键，130 秒）和 `PreToolUse`（只用来判急停，5 秒）两个 http hook。
- 写之前会备份原文件；重复运行不会重复添加；`hooks uninstall` 删除。
- 装完要重启 Claude Code。

## DSH

DSH 的审批请求（`approval/request`）里**不带工具参数**。所以插件先在 `tools/pre-execute` 记下每次调用的参数，审批时再按 `callId` 取出来，一起发给 daemon。

| daemon 的回答 | DSH 收到 |
|---|---|
| 允许 | `allowed-once` |
| 拒绝 | `rejected` |
| 没人按 / daemon 没开 | `next()`，DSH 自己的网页审批照常出现 |

急停锁住期间，`tools/pre-execute` 直接拒绝所有工具。

在 DSH profile 的 `cordis.patch.yml` 里，放进和其它本地插件同一个 `insert` 列表：

```yaml
    - id: midi-agent-console
      name: E:/midi-agent-console/integrations/dsh/index.mjs
      config:
        port: 47811
```

改完重启 DSH。插件在 DSH 主进程里运行，不在沙箱里，可以直接访问 127.0.0.1。

插件自测：不需要 DSH，需要 daemon 在跑，而且开着虚拟键（不接键盘，或者加 `--virtual-keys`）。第二个参数填 daemon 启动时打印的 `/keys?k=…` 里那把钥匙：

```bash
node integrations/dsh/test-plugin.mjs 47811 <virtual-key>
```

待实测：
- 加了 `prepend` 之后，本插件是否真的排在网页审批前面；
- 一次调用没有走审批时，`calls` 表的清理时机（现在的做法是超过 500 条就淘汰最老的）。

## Codex CLI

Codex 的 hook 只能跑命令，所以用 `hook-relay`：读 stdin，转发给 `/hook`，把结果打到 stdout，退出码一律 0。daemon 没开或没人按时什么都不输出，Codex 照常弹自己的提示。

在 Codex 方言下，`hook-relay` 会去掉 `interrupt` 和 `continue`，因为 Codex 会把这两个字段当成失败。

`~/.codex/hooks.json`（按 [官方文档](https://learn.chatgpt.com/docs/hooks)；没审核过的 hook 不会跑，要先在 `/hooks` 里信任它）：

```json
{
  "hooks": {
    "PermissionRequest": [{"hooks": [{"type": "command", "timeout": 135,
      "command": "python -m midi_agent_console hook-relay --dialect codex",
      "commandWindows": "C:\\path\\to\\.venv\\Scripts\\python.exe -m midi_agent_console hook-relay --dialect codex"}]}],
    "PreToolUse": [{"hooks": [{"type": "command", "timeout": 5,
      "command": "python -m midi_agent_console hook-relay --dialect codex",
      "commandWindows": "C:\\path\\to\\.venv\\Scripts\\python.exe -m midi_agent_console hook-relay --dialect codex"}]}]
  }
}
```

- PermissionRequest 只在 Codex 本来就要问审批时才触发，所以 `approval_policy` 不能设成 `never`，可以用 `on-request`。
- Codex 的 `apply_patch` 会从补丁里取出文件路径，再走一遍危险路径规则；删除文件一律按危险处理。
