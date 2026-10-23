"""A browser keyboard for people without MIDI hardware: GET /keys serves the page,
POST /virtual feeds its events into the same Console the real keyboard uses.

Same guards as /hook: Host must be 127.0.0.1/localhost on our port, the token is
required, and only our own page's Origin is accepted (a foreign page cannot read the
token, and its cross-origin JSON POST is blocked by the browser's preflight)."""
from __future__ import annotations

import hmac
import json

from .console import Console
from .events import decode

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Virtual Keys</title>
<style>
:root{--bg:#f6f6f4;--fg:#1d1d1b;--white:#fff;--black:#222;--line:#bbb;--down:#4a7bd0;--warn:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--white:#e8e8e6;--black:#000;--line:#555;--down:#6c9cf0}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
h1{font-size:18px;margin:0 0 4px}.hint{opacity:.75;margin:0 0 12px}
#kb{position:relative;height:180px;user-select:none;touch-action:none;max-width:100%;overflow-x:auto}
.w,.b{position:absolute;top:0;border:1px solid var(--line);border-radius:0 0 6px 6px;box-sizing:border-box}
.w{background:var(--white);height:180px;z-index:1}.b{background:var(--black);height:110px;z-index:2}
.down{background:var(--down)!important}.w span{position:absolute;bottom:6px;width:100%;text-align:center;color:#555;font-size:11px}
.row{display:flex;gap:16px;flex-wrap:wrap;margin-top:16px;align-items:center}
label{display:flex;gap:8px;align-items:center}input[type=range]{width:180px}
button{padding:8px 14px;border-radius:6px;border:1px solid var(--line);background:var(--white);color:#1d1d1b}
button.on{background:var(--down);color:#fff}#log{font-family:ui-monospace,monospace;font-size:12px;opacity:.8;margin-top:12px}
.no{color:var(--warn)}
</style></head><body>
<h1>Virtual Keys · midi-agent-console</h1>
<p class="hint">左半边（C4 以下）= 否；右半边或踩踏板 = 是；危险操作要<b>按住 1.5 秒</b>；白键 C–B = 选项 1–7；
<span class="no">左半边同时按 3 个键 = 急停</span>（多指触屏或按住 Shift 连点）。</p>
<div id="kb"></div>
<div class="row">
<label>调制条 CC1 <input id="mod" type="range" min="0" max="127" value="0"></label>
<label>弯音条 <input id="pb" type="range" min="-8192" max="8191" value="0"></label>
<button id="hold">踏板 CC64</button>
</div>
<div id="log"></div>
<script>
const TOKEN = "__TOKEN__", LO = 48, HI = 72, SPLIT = __SPLIT__;
const log = t => document.getElementById('log').textContent = t;
async function send(s, d1, d2) {
  try {
    const r = await fetch('/virtual', {method: 'POST', headers: {'Content-Type': 'application/json',
      'Authorization': 'Bearer ' + TOKEN}, body: JSON.stringify({status: s, d1: d1, d2: d2})});
    if (!r.ok) log('error ' + r.status);
  } catch (e) { log('daemon not reachable'); }
}
const kb = document.getElementById('kb'), isBlack = n => [1,3,6,8,10].includes(n % 12);
const names = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];
let x = 0; const W = 36, held = new Set(), sticky = new Set();
for (let n = LO; n <= HI; n++) {
  const el = document.createElement('div');
  if (isBlack(n)) { el.className = 'b'; el.style.left = (x - W * 0.3) + 'px'; el.style.width = (W * 0.6) + 'px'; }
  else { el.className = 'w'; el.style.left = x + 'px'; el.style.width = W + 'px';
         el.innerHTML = '<span>' + names[n % 12] + Math.floor(n / 12 - 1) + '</span>'; x += W; }
  el.dataset.n = n; kb.appendChild(el);
}
kb.style.width = x + 'px';
function on(el) { const n = +el.dataset.n; if (held.has(n)) return; held.add(n); el.classList.add('down');
  send(0x90, n, 100); log((n < SPLIT ? 'no-side ' : 'yes-side ') + el.textContent + ' down'); }
function off(el) { const n = +el.dataset.n; if (!held.has(n)) return; held.delete(n); el.classList.remove('down');
  send(0x80, n, 0); }
const active = new Map();
kb.addEventListener('pointerdown', e => { const el = e.target.closest('[data-n]'); if (!el) return;
  e.preventDefault(); if (e.shiftKey) { sticky.add(el); on(el); return; }
  active.set(e.pointerId, el); on(el); });
const up = e => { const el = active.get(e.pointerId); if (el) { off(el); active.delete(e.pointerId); }
  if (!e.shiftKey && sticky.size) { sticky.forEach(off); sticky.clear(); } };
window.addEventListener('pointerup', up); window.addEventListener('pointercancel', up);
document.getElementById('mod').oninput = e => send(0xB0, 1, +e.target.value);
document.getElementById('pb').oninput = e => { const v = +e.target.value + 8192; send(0xE0, v & 127, v >> 7); };
document.getElementById('pb').onchange = e => { e.target.value = 0; send(0xE0, 0, 64); };
const hold = document.getElementById('hold');
hold.onclick = () => { const onNow = !hold.classList.contains('on'); hold.classList.toggle('on', onNow);
  send(0xB0, 64, onNow ? 127 : 0); };
</script></body></html>
"""


def make_routes(console: Console, token: str, port: int):
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, Response

    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}

    async def keys(request: Request) -> Response:
        if request.headers.get("host") not in hosts:
            return Response(status_code=403)
        html = PAGE.replace("__TOKEN__", token).replace("__SPLIT__", str(console.profile.split_note))
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                           "Content-Security-Policy": "frame-ancestors 'none'"})

    async def virtual(request: Request) -> Response:
        if request.headers.get("host") not in hosts:
            return Response(status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            return Response(status_code=403)
        if not request.headers.get("content-type", "").startswith("application/json"):
            return Response(status_code=415)
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), f"Bearer {token}".encode()):
            return Response(status_code=401)
        try:
            m = json.loads(await request.body())
            status, d1, d2 = int(m["status"]), int(m.get("d1", 0)), int(m.get("d2", 0))
        except (ValueError, KeyError, TypeError):
            return Response(status_code=400)
        if status & 0xF0 not in (0x80, 0x90, 0xB0, 0xE0) or not (0 <= d1 < 128 and 0 <= d2 < 128):
            return Response(status_code=400)
        console.feed(decode(status, d1, d2))
        return Response(status_code=204)

    return keys, virtual
