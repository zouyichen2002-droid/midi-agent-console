// midi-agent-console plugin for DeepSeek Harness (DSH): answer DSH approval prompts
// from the MIDI keyboard. Plain ESM, Node built-ins only.
//
//   tools/pre-execute  -> remembers each call's arguments (DSH's approval request
//                         carries none), and denies everything while the MIDI
//                         emergency stop is latched.
//   approval/request   -> forwarded to the midi-agent-console daemon as a Claude Code
//                         style PermissionRequest; allow -> 'allowed-once',
//                         deny -> 'rejected'; no key / daemon down -> next(), so the
//                         normal DSH prompt (web UI) still answers.
//
// Requires `python -m midi_agent_console daemon` running on the same machine.
import { readFileSync } from "node:fs";
import { join } from "node:path";

export const name = "midi-agent-console";
export const inject = ["tools"];

const DEFAULTS = { port: 47811, approvalTimeoutMs: 130_000, preTimeoutMs: 5_000 };
const calls = new Map(); // callId -> arguments, bounded

function token() {
  const dir = process.env.LOCALAPPDATA || join(process.env.HOME || "", ".local", "state");
  return readFileSync(join(dir, "midi-agent-console", "token"), "ascii").trim();
}

async function post(cfg, payload, ms, signal) {
  const signals = [AbortSignal.timeout(ms)];
  if (signal) signals.push(signal);
  try {
    const r = await fetch(`http://127.0.0.1:${cfg.port}/hook`, {
      method: "POST",
      signal: AbortSignal.any(signals),
      headers: { "content-type": "application/json", authorization: `Bearer ${token()}` },
      body: JSON.stringify({ ...payload, x_agent: "dsh" }),
    });
    return r.status === 200 ? await r.json() : null;
  } catch {
    return null; // daemon down, timeout or cancelled: no opinion
  }
}

function base(agent) {
  const h = agent?.session?.header ?? {};
  return { session_id: h.id ?? "", cwd: h.cwd ?? "", transcript_path: "" };
}

export function createPlugin(config = {}) {
  const cfg = { ...DEFAULTS, ...config };
  return {
    async preExecute(exec, next) {
      calls.set(exec.callId, exec.arguments ?? {});
      if (calls.size > 500) calls.delete(calls.keys().next().value);
      const j = await post(cfg, { ...base(exec.agent), hook_event_name: "PreToolUse",
                                  tool_name: exec.name, tool_input: exec.arguments ?? {} }, cfg.preTimeoutMs);
      const d = j?.hookSpecificOutput;
      if (d?.permissionDecision === "deny") return { kind: "deny", reason: d.permissionDecisionReason || "MIDI e-stop" };
      return next();
    },
    async approval(req, next) {
      const toolInput = (req.callId && calls.get(req.callId)) || {};
      const j = await post(cfg, { ...base(req.agent), hook_event_name: "PermissionRequest",
                                  tool_name: req.toolName, tool_input: toolInput,
                                  reason: req.reason ?? "" }, cfg.approvalTimeoutMs, req.signal);
      if (req.callId) calls.delete(req.callId);
      const behavior = j?.hookSpecificOutput?.decision?.behavior;
      if (behavior === "allow") return "allowed-once";
      if (behavior === "deny") return "rejected";
      return next();
    },
  };
}

export function apply(ctx, config = {}) {
  const p = createPlugin(config);
  ctx.on("tools/pre-execute", (exec, next) => p.preExecute(exec, next), { prepend: true });
  ctx.on("approval/request", (req, next) => p.approval(req, next), { prepend: true });
}
