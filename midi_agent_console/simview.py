"""GET /sim: watch the simulated car (pose, trail, servo, LED, e-stop) in a browser, so
teleop and brain demos work with no hardware. GET /sim/state is the JSON it polls.
Read-only; same Host check as the other local pages (no token: it changes nothing)."""
from __future__ import annotations

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sim Car</title>
<style>
:root{--bg:#f6f6f4;--fg:#1d1d1b;--grid:#e2e2dc;--car:#2f6fd6;--trail:#9bb7e8;--warn:#c0392b;--card:#fff;--line:#d9d9d4}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#ececea;--grid:#262626;--car:#6c9cf0;--trail:#34507f;--card:#1d1d1d;--line:#333}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,"Microsoft YaHei",sans-serif}
h1{font-size:18px;margin:0 0 8px}#wrap{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start}
canvas{background:var(--card);border:1px solid var(--line);border-radius:8px;max-width:100%;touch-action:none}
#info{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;min-width:220px}
#info div{font-family:ui-monospace,monospace;font-size:13px}#estop{color:var(--warn);font-weight:700;display:none}
</style></head><body>
<h1>模拟小车 · midi-agent-console</h1>
<div id="wrap"><canvas id="c" width="520" height="520"></canvas>
<div id="info"><div id="estop">急停已锁存</div><div id="pose"></div><div id="drv"></div><div id="srv"></div>
<div id="led">LED <span id="dot" style="display:inline-block;width:12px;height:12px;border-radius:50%;vertical-align:middle;border:1px solid var(--line)"></span></div></div></div>
<script>
const c = document.getElementById('c'), g = c.getContext('2d');
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
let span = 1.0;  // metres from centre to edge; grows to keep the car in view
function draw(s) {
  const far = Math.max(Math.abs(s.x), Math.abs(s.y), ...s.trail.flat().map(Math.abs));
  while (far > span * 0.85) span *= 1.5;
  const W = c.width, k = W / 2 / span, X = x => W / 2 + x * k, Y = y => W / 2 - y * k;
  g.clearRect(0, 0, W, W);
  g.strokeStyle = css('--grid'); g.lineWidth = 1;
  const step = span > 3 ? 1 : span > 1.2 ? 0.5 : 0.25;
  for (let v = -Math.ceil(span / step) * step; v <= span; v += step) {
    g.beginPath(); g.moveTo(X(v), 0); g.lineTo(X(v), W); g.stroke();
    g.beginPath(); g.moveTo(0, Y(v)); g.lineTo(W, Y(v)); g.stroke();
  }
  g.strokeStyle = css('--trail'); g.lineWidth = 2; g.beginPath();
  s.trail.forEach(([x, y], i) => i ? g.lineTo(X(x), Y(y)) : g.moveTo(X(x), Y(y))); g.stroke();
  const th = s.deg * Math.PI / 180, L = Math.max(10, 0.1 * k);
  g.save(); g.translate(X(s.x), Y(s.y)); g.rotate(-th);
  g.fillStyle = s.estopped ? css('--warn') : css('--car');
  g.beginPath(); g.moveTo(L, 0); g.lineTo(-L * 0.7, L * 0.6); g.lineTo(-L * 0.7, -L * 0.6); g.closePath(); g.fill();
  const a = ((s.servos[0] ?? 90) - 90) * Math.PI / 180;  // servo 0 drawn as an arm
  g.strokeStyle = css('--fg'); g.lineWidth = 3; g.beginPath(); g.moveTo(0, 0);
  g.lineTo(Math.cos(a) * L * 1.2, -Math.sin(a) * L * 1.2); g.stroke();
  g.restore();
  document.getElementById('estop').style.display = s.estopped ? 'block' : 'none';
  document.getElementById('pose').textContent = `x ${s.x.toFixed(2)} m  y ${s.y.toFixed(2)} m  ${s.deg.toFixed(0)}°`;
  document.getElementById('drv').textContent = `drive L ${s.drive[0]}  R ${s.drive[1]}`;
  document.getElementById('srv').textContent = 'servos ' + Object.entries(s.servos).map(([i, d]) => `${i}:${d}°`).join('  ');
  const [r, gg, b] = s.leds[0] || [0, 0, 0];
  document.getElementById('dot').style.background = `rgb(${r},${gg},${b})`;
}
async function tick() {
  try { const r = await fetch('/sim/state', {cache: 'no-store'}); if (r.ok) draw(await r.json()); } catch (e) {}
  setTimeout(tick, 100);
}
tick();
</script></body></html>
"""


def make_routes(device, port: int):
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, Response

    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    async def page(request: Request) -> Response:
        if request.headers.get("host") not in hosts:
            return Response(status_code=403)
        if not hasattr(device, "pose"):
            return Response("this device is not simulated", status_code=404)
        return HTMLResponse(PAGE, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    async def state(request: Request) -> Response:
        if request.headers.get("host") not in hosts:
            return Response(status_code=403)
        if not hasattr(device, "pose"):
            return Response(status_code=404)
        return JSONResponse(device.pose(), headers={"Cache-Control": "no-store"})

    return page, state
