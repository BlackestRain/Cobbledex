#!/usr/bin/env python3
"""
Tiny per-user progress API for the Cobblemon Dex (standard library only).
Run it behind nginx; it listens on 127.0.0.1 only.

  DEX_DATA=/var/lib/cobblemon-dex DEX_PORT=8099 python3 dex_server.py

Each visitor gets a random ID stored in an HttpOnly cookie. The server keeps
{name, caught} per ID in <DEX_DATA>/<id>.json. The ID doubles as a private
"key" so a user can restore their dex on another device.
"""
import json
import os
import re
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

DATA = Path(os.environ.get("DEX_DATA", "dex_users"))
DATA.mkdir(parents=True, exist_ok=True)
HOST, PORT = "127.0.0.1", int(os.environ.get("DEX_PORT", "8099"))
ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_BODY, MAX_CAUGHT = 20_000, 5_000
STATIC = Path(os.environ.get("DEX_STATIC", Path(__file__).parent)).resolve()
INDEX = "cobblemon_dex.html"
TYPES = {".html": "text/html; charset=utf-8", ".json": "application/json",
         ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon"}
LOCK = threading.Lock()


def read_user(uid):
    if not ID_RE.match(uid or ""):
        return None
    try:
        return json.loads((DATA / f"{uid}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None


def write_user(uid, data):
    final = DATA / f"{uid}.json"
    tmp = final.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), "utf-8")
    os.replace(tmp, final)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def static(self, p):
        f = (STATIC / (INDEX if p in ("", "/") else p.lstrip("/"))).resolve()
        if not (f.is_file() and f.suffix.lower() in TYPES
                and STATIC in f.parents and DATA.resolve() not in f.parents):
            return self.send(404, {})
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES[f.suffix.lower()])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def send(self, code, obj, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def uid(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c["dexid"].value if "dexid" in c else None

    def cookie(self, uid):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return f"dexid={uid}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax{secure}"

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return None

    def do_GET(self):
        p = unquote(self.path.split("?")[0])
        if p != "/api/me":
            return self.send(404, {}) if p.startswith("/api/") else self.static(p)
        uid = self.uid()
        u = read_user(uid)
        self.send(200, {"id": uid, **u} if u else {})

    def do_POST(self):
        p, b = self.path.split("?")[0], self.body()
        if not isinstance(b, dict):
            return self.send(400, {"error": "bad request"})
        with LOCK:
            if p == "/api/register":
                name = re.sub(r"[\x00-\x1f<>]", "", str(b.get("name", ""))).strip()[:32]
                if not name:
                    return self.send(400, {"error": "name required"})
                uid = secrets.token_urlsafe(18)
                u = {"name": name, "caught": {}}
                write_user(uid, u)
                return self.send(200, {"id": uid, **u}, self.cookie(uid))
            if p == "/api/restore":
                uid = str(b.get("id", "")).strip()
                u = read_user(uid)
                if not u:
                    return self.send(404, {"error": "unknown key"})
                return self.send(200, {"id": uid, **u}, self.cookie(uid))
            if p == "/api/caught":
                uid = self.uid()
                u = read_user(uid)
                if not u:
                    return self.send(401, {"error": "not logged in"})
                name = str(b.get("name", ""))[:200]
                if not name:
                    return self.send(400, {"error": "name required"})
                if b.get("on"):
                    if len(u["caught"]) >= MAX_CAUGHT:
                        return self.send(400, {"error": "too many entries"})
                    u["caught"][name] = 1
                else:
                    u["caught"].pop(name, None)
                write_user(uid, u)
                return self.send(200, {"ok": True})
        self.send(404, {})


if __name__ == "__main__":
    print(f"Dex API on http://{HOST}:{PORT}, data in {DATA.resolve()}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
