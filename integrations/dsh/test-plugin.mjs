// Drives the DSH plugin's handlers against a running daemon, pressing keys through the
// daemon's /virtual endpoint. Usage: node test-plugin.mjs <port>
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createPlugin } from "./index.mjs";

const port = +(process.argv[2] || 47811);
const tok = readFileSync(join(process.env.LOCALAPPDATA, "midi-agent-console", "token"), "ascii").trim();
const key = (st, d1, d2) => fetch(`http://127.0.0.1:${port}/virtual`, {
  method: "POST", headers: { "content-type": "application/json", authorization: `Bearer ${tok}` },
  body: JSON.stringify({ status: st, d1, d2 }) });
const press = async (note, holdMs = 50, afterMs = 600) => {
  await new Promise(r => setTimeout(r, afterMs)); await key(0x90, note, 100);
  await new Promise(r => setTimeout(r, holdMs)); await key(0x80, note, 0);
};
const agent = { session: { header: { id: "s1", cwd: "E:/demo" } } };
const p = createPlugin({ port, approvalTimeoutMs: 4000 });
const next = async () => "NEXT";
const results = {};

await p.preExecute({ callId: "c1", name: "pwsh", arguments: { command: "Get-ChildItem" }, agent }, async () => ({ kind: "allow" }));
press(72);
results.allow = await p.approval({ agent, toolName: "pwsh", callId: "c1" }, next);

await p.preExecute({ callId: "c2", name: "pwsh", arguments: { command: "ls" }, agent }, async () => ({ kind: "allow" }));
press(50);
results.deny = await p.approval({ agent, toolName: "pwsh", callId: "c2" }, next);

results.timeout = await p.approval({ agent, toolName: "pwsh", callId: "c3" }, next);

await p.preExecute({ callId: "c4", name: "pwsh", arguments: { command: "git push origin main" }, agent }, async () => ({ kind: "allow" }));
press(74, 120);  // a tap on a dangerous command must not approve
results.dangerTap = await p.approval({ agent, toolName: "pwsh", callId: "c4" }, next);

await p.preExecute({ callId: "c5", name: "pwsh", arguments: { command: "git push origin main" }, agent }, async () => ({ kind: "allow" }));
press(74, 1800);
results.dangerLong = await p.approval({ agent, toolName: "pwsh", callId: "c5" }, next);

const dead = createPlugin({ port: 1, approvalTimeoutMs: 1000 });
results.daemonDown = await dead.approval({ agent, toolName: "pwsh" }, next);
console.log(JSON.stringify(results));
