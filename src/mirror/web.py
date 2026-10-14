"""Optional HTTP trigger.  Fails closed when MIRROR_TOKEN is unset.

Everything this module does is enqueue an event — it holds no state and never
touches mpv directly, so it stays decoupled from the supervisor.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .state import Event

PAGE = """<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mirror</title>
<style>
 body{font:16px system-ui;background:#111;color:#eee;margin:0;padding:24px}
 button{font:inherit;padding:18px;margin:6px 0;width:100%;border:0;border-radius:8px;background:#333;color:#fff}
 button:active{background:#555}
 input{padding:12px;width:100%;box-sizing:border-box;margin-bottom:12px;border-radius:8px;border:1px solid #444;background:#000;color:#eee}
 #s{color:#888;margin-top:12px}
</style>
<h1>Mirror</h1>
<input id="t" type="password" placeholder="token">
<button onclick="go('scare/1')">Scare 1</button>
<button onclick="go('scare/2')">Scare 2</button>
<button onclick="go('calm')">Back to calm</button>
<div id="s"></div>
<script>
const t=document.getElementById('t'),s=document.getElementById('s');
t.value=localStorage.getItem('mirrorToken')||'';
async function go(p){
  localStorage.setItem('mirrorToken',t.value);
  try{
    const r=await fetch('/api/'+p,{method:'POST',headers:{'X-Auth-Token':t.value}});
    s.textContent=p+' -> '+r.status;
  }catch(e){s.textContent='error: '+e}
}
</script>
"""


class WebTrigger(threading.Thread):
    def __init__(self, cfg, events, log: logging.Logger | None = None):
        super().__init__(name="web", daemon=True)
        self.cfg = cfg
        self.events = events
        self.log = log or logging.getLogger("mirror.web")
        self._server: ThreadingHTTPServer | None = None

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()

    def run(self) -> None:
        cfg = self.cfg["web"]
        if not cfg["enabled"]:
            self.log.info("web trigger disabled by configuration")
            return

        token = os.environ.get("MIRROR_TOKEN", "").strip()
        if not token:
            self.log.warning("MIRROR_TOKEN unset; web trigger disabled (fails closed)")
            return

        events = self.events
        log = self.log

        class Handler(BaseHTTPRequestHandler):
            server_version = "mirror"

            def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
                log.debug("http %s", fmt % args)

            def _authorised(self) -> bool:
                supplied = self.headers.get("X-Auth-Token", "")
                return hmac.compare_digest(supplied, token)

            def _send(self, status: int, body: bytes, ctype: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 - stdlib signature
                if self.path in ("/", "/index.html"):
                    self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self):  # noqa: N802 - stdlib signature
                if not self._authorised():
                    self._send(403, b"forbidden", "text/plain")
                    return
                path = self.path.rstrip("/")
                if path.startswith("/api/scare/"):
                    action = f"scare:{path.rsplit('/', 1)[1]}"
                elif path == "/api/calm":
                    action = "calm"
                elif path == "/api/pir":
                    events.put(Event("pir"))
                    self._send(200, b'{"ok":true}', "application/json")
                    return
                else:
                    self._send(404, b"not found", "text/plain")
                    return
                events.put(Event("action", action))
                self._send(200, json.dumps({"ok": True, "action": action}).encode(),
                           "application/json")

        try:
            server = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), Handler)
        except OSError as exc:
            self.log.error("cannot bind %s:%s — %s", cfg["bind"], cfg["port"], exc)
            return

        self._server = server
        self.log.info("web trigger on http://%s:%s/", cfg["bind"], cfg["port"])
        server.serve_forever()

