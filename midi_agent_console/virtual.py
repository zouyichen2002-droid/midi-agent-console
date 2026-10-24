"""A browser keyboard for people without MIDI hardware: GET /keys?k=<key> serves the page,
POST /virtual feeds its events into the same Console the real keyboard uses.

Off by default. The daemon turns it on only with --virtual-keys or when no physical
keyboard is found, and then generates a fresh random key that exists only in this
process (never in a file or an environment variable, and different from the hook token).

LIMITATION, stated plainly: anything that can read the daemon's console output or run as
your user can open the page and "press" keys. Virtual keys are for trying things out; for
approvals that must come from a person, use a physical keyboard (which disables them).

Guards: Host must be 127.0.0.1/localhost on our port; the key is required; only our own
page's Origin is accepted. Against stuck keys: events carry a sequence number and stale
ones are dropped; the page releases everything on blur / hide; while a key is held the
page pings every 300 ms and the server releases held keys after 1 s of silence.
"""
from __future__ import annotations

import hmac
import json
import secrets
import threading
import time

from .console import Console
from .events import decode

KEEPALIVE_S = 1.0
VCH = 0x0F  # virtual keys send on MIDI channel 16

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
<span class="no">左半边同时按 3 个键 = 急停</span>（多指触屏，或按住 Shift 在左半边连点）。
切走窗口会松开所有键。</p>
<div id="kb"></div>
<div class="row">
<label>调制条 CC1 <input id="mod" type="range" min="0" max="127" value="0"></label>
<label>弯音条 <input id="pb" type="range" min="-8192" max="8191" value="0"></label>
<button id="hold">踏板 CC64</button>
<button id="estop" style="background:var(--warn);color:#fff;border-color:var(--warn)">急停</button>
</div>
<div id="stale" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);color:#fff;font-size:20px;
display:none;align-items:center;justify-content:center;text-align:center;padding:24px">此页已失效（别的页面接管了虚拟键盘）。<br>请刷新本页。急停按钮仍然有效。</div>
<div id="log"></div>
<script>
const KEY = "__KEY__", LO = 48, HI = 72, SPLIT = __SPLIT__;
const log = t => document.getElementById('log').textContent = t;
// a fresh session id per page load: a reload (seq back to 1) must not look "stale"
const SID = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2));
let seq = 0, chain = Promise.resolve();
function post(body) {  // strictly one request at a time, in order, each with a sequence number
  body.seq = ++seq; body.sid = SID;
  chain = chain.then(() => fetch('/virtual', {method: 'POST', headers: {'Content-Type': 'application/json',
      'X-Virtual-Key': KEY}, body: JSON.stringify(body)})
    .then(r => { if (r.status === 409 && !body.estop) staleNow(); else if (!r.ok) log('error ' + r.status); })
    .catch(() => log('daemon not reachable')));
}
let stale = false;
function staleNow() { if (stale) return; stale = true; releaseAll();
  const s = document.getElementById('stale'); s.style.display = 'flex'; }
const send = (s, d1, d2) => post({status: s, d1: d1, d2: d2});
const kb = document.getElementById('kb'), isBlack = n => [1,3,6,8,10].includes(n % 12);
const names = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];
let x = 0; const W = 36, held = new Set(), sticky = new Set(), els = {};
for (let n = LO; n <= HI; n++) {
  const el = document.createElement('div');
  if (isBlack(n)) { el.className = 'b'; el.style.left = (x - W * 0.3) + 'px'; el.style.width = (W * 0.6) + 'px'; }
  else { el.className = 'w'; el.style.left = x + 'px'; el.style.width = W + 'px';
         el.innerHTML = '<span>' + names[n % 12] + Math.floor(n / 12 - 1) + '</span>'; x += W; }
  el.dataset.n = n; els[n] = el; kb.appendChild(el);
}
kb.style.width = x + 'px';
function on(n) { if (stale || held.has(n)) return; held.add(n); els[n].classList.add('down');
  send(0x90, n, 100); log((n < SPLIT ? 'no-side ' : 'yes-side ') + els[n].textContent + ' down'); }
function off(n) { if (!held.has(n)) return; held.delete(n); els[n].classList.remove('down'); send(0x80, n, 0); }
function releaseAll() { [...held].forEach(off); sticky.clear(); active.clear(); }
setInterval(() => { if (held.size) post({alive: true}); }, 300);
const active = new Map();
kb.addEventListener('pointerdown', e => { const el = e.target.closest('[data-n]'); if (!el) return;
  e.preventDefault(); const n = +el.dataset.n;
  if (e.shiftKey && n < SPLIT) { sticky.add(n); on(n); return; }  // sticky only on the no / e-stop side
  active.set(e.pointerId, n); on(n); });
const up = e => { const n = active.get(e.pointerId); if (n !== undefined) { off(n); active.delete(e.pointerId); }
  if (!e.shiftKey && sticky.size) { sticky.forEach(off); sticky.clear(); } };
window.addEventListener('pointerup', up); window.addEventListener('pointercancel', up);
window.addEventListener('blur', releaseAll); window.addEventListener('pagehide', releaseAll);
document.addEventListener('visibilitychange', () => { if (document.hidden) releaseAll(); });
document.getElementById('mod').oninput = e => send(0xB0, 1, +e.target.value);
document.getElementById('pb').oninput = e => { const v = +e.target.value + 8192; send(0xE0, v & 127, v >> 7); };
document.getElementById('pb').onchange = e => { e.target.value = 0; send(0xE0, 0, 64); };
document.getElementById('estop').onclick = () => post({estop: true});  // works even on a stale page
const hold = document.getElementById('hold');
hold.onclick = () => { const onNow = !hold.classList.contains('on'); hold.classList.toggle('on', onNow);
  send(0xB0, 64, onNow ? 127 : 0); };
</script></body></html>
"""


class VirtualKeys:
    def __init__(self, console: Console, port: int, key: str | None = None):
        self.console, self.port = console, port
        self.key = key or secrets.token_urlsafe(24)  # in memory only
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{h}" for h in self.hosts}
        self._lock = threading.Lock()
        self._last_seq = 0
        self._sid: str | None = None
        self._retired: set[str] = set()
        self._last_msg = time.monotonic()
        self._held: set[int] = set()
        self._stop = threading.Event()
        threading.Thread(target=self._watchdog, name="virtual-keys-watchdog", daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/keys?k={self.key}"

    def _key_ok(self, given: str | None) -> bool:
        return hmac.compare_digest((given or "").encode(), self.key.encode())

    def accept(self, m: dict) -> int:
        """Validate one page message and feed it; returns an HTTP status."""
        try:
            seq, sid = int(m["seq"]), str(m.get("sid", ""))[:64]
        except (KeyError, TypeError, ValueError):
            return 400
        if m.get("estop") is True:  # safe direction only: honoured from any session, even a stale one
            self.console.trigger_estop()
            return 204
        orphans: set[int] = set()
        with self._lock:
            if sid != self._sid:  # a new page load takes over; events of the old one are void
                if sid in self._retired:
                    return 409
                if self._sid is not None:
                    self._retired.add(self._sid)
                self._sid, self._last_seq = sid, 0
                orphans, self._held = set(self._held), set()  # keys the old page left down
            if seq <= self._last_seq:
                return 409  # stale or replayed: an out-of-order release must not turn into a hold
            self._last_seq, self._last_msg = seq, time.monotonic()
            if m.get("alive"):
                alive_only = True
            else:
                alive_only = False
        if alive_only:
            for n in orphans:
                self.console.feed(decode(0x80 | VCH, n, 0))
            return 204
        with self._lock:
            try:
                status, d1, d2 = int(m["status"]), int(m.get("d1", 0)), int(m.get("d2", 0))
            except (KeyError, TypeError, ValueError):
                return 400
            if status & 0xF0 not in (0x80, 0x90, 0xB0, 0xE0) or not (0 <= d1 < 128 and 0 <= d2 < 128):
                return 400
            kind = status & 0xF0
            if kind == 0x90 and d2:
                self._held.add(d1)
            elif kind in (0x80, 0x90):
                self._held.discard(d1)
        for n in orphans:
            self.console.feed(decode(0x80 | VCH, n, 0))
        # virtual keys play on their own channel, so they never clear a physical key's state
        self.console.feed(decode((status & 0xF0) | VCH, d1, d2))
        return 204

    def _watchdog(self) -> None:
        while not self._stop.wait(0.2):
            with self._lock:
                stale = bool(self._held) and time.monotonic() - self._last_msg > KEEPALIVE_S
                notes, self._held = (set(self._held), set()) if stale else (set(), self._held)
            for n in notes:  # the page went quiet with keys down: let go
                self.console.feed(decode(0x80 | VCH, n, 0))

    def close(self) -> None:
        self._stop.set()

    def routes(self):
        from starlette.requests import Request
        from starlette.responses import HTMLResponse, Response

        async def keys(request: Request) -> Response:
            if request.headers.get("host") not in self.hosts or not self._key_ok(request.query_params.get("k")):
                return Response(status_code=403)
            html = PAGE.replace("__KEY__", self.key).replace("__SPLIT__", str(self.console.profile.split_note))
            return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                               "Referrer-Policy": "no-referrer",
                                               "Content-Security-Policy": "frame-ancestors 'none'"})

        async def virtual(request: Request) -> Response:
            if request.headers.get("host") not in self.hosts:
                return Response(status_code=403)
            origin = request.headers.get("origin")
            if origin and origin not in self.origins:
                return Response(status_code=403)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return Response(status_code=415)
            if not self._key_ok(request.headers.get("x-virtual-key")):
                return Response(status_code=401)
            try:
                m = json.loads(await request.body())
            except ValueError:
                return Response(status_code=400)
            return Response(status_code=self.accept(m) if isinstance(m, dict) else 400)

        return keys, virtual
