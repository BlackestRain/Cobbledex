#!/usr/bin/env python3
"""
Tiny per-user progress API for the Cobblemon Dex (standard library only).
Also serves the page, mons.json and sprites. Listens on 127.0.0.1 only; run it
behind nginx.

  DEX_DATA=/var/lib/cobbledex DEX_PORT=8099 python3 dex_server.py

Each visitor gets a random ID stored in an HttpOnly cookie. The server keeps
{name, caught} per ID in <DEX_DATA>/<id>.json. The ID doubles as a private
"key" so a user can restore their dex on another device.

Sprites: /sprites/<slug>.png (forms, e.g. marowak-alola) and /sprites/<dex>.png
are downloaded from PokeAPI on first request and cached in <DEX_DATA>/sprites.
Sprites placed by hand in <DEX_STATIC>/sprites take priority.
"""
import json
import os
import re
import secrets
import threading
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

DATA = Path(os.environ.get("DEX_DATA", "dex_users")).resolve()
DATA.mkdir(parents=True, exist_ok=True)
HOST, PORT = "127.0.0.1", int(os.environ.get("DEX_PORT", "8099"))
ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_BODY, MAX_CAUGHT = 20_000, 5_000
LOCK = threading.Lock()

STATIC = Path(os.environ.get("DEX_STATIC", Path(__file__).parent)).resolve()
INDEX = "cobblemon_dex.html"
TYPES = {".html": "text/html; charset=utf-8", ".json": "application/json",
         ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon"}

SPRITES = DATA / "sprites"
SPRITE_RE = re.compile(r"^/sprites/([a-z0-9]+(?:-[a-z0-9]+)*)\.png$")
SPRITE_BASE = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/"
POKEAPI = "https://pokeapi.co/api/v2/pokemon/"
MISSING = set()  # sprites the source answered 404 for (until restart)


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


def get(url, limit):
    # PokeAPI's API host rejects Python's default User-Agent with a 403
    req = urllib.request.Request(url, headers={"User-Agent": "cobbledex/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read(limit)


def fetch_sprite(name):
    """Download a sprite from PokeAPI. Returns (png bytes or None, definitely_missing)."""
    try:
        if name.isdigit():
            url = SPRITE_BASE + f"{name}.png"
        else:  # forms are separate entries on PokeAPI (e.g. marowak-alola)
            url = json.loads(get(POKEAPI + name, 2_000_000))["sprites"]["front_default"]
            if not url or not url.startswith("https://raw.githubusercontent.com/PokeAPI/"):
                return None, True
        img = get(url, 500_000)
        return (img, False) if img.startswith(b"\x89PNG") else (None, True)
    except urllib.error.HTTPError as e:
        return None, e.code == 404
    except Exception:
        return None, False  # network trouble: don't remember it as missing


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

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

    def send_file(self, f, ctype, cache):
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def static(self, p):
        f = (STATIC / (INDEX if p in ("", "/") else p.lstrip("/"))).resolve()
        if not (f.is_file() and f.suffix.lower() in TYPES
                and STATIC in f.parents and DATA not in f.parents):
            return self.send(404, {})
        self.send_file(f, TYPES[f.suffix.lower()], "no-cache")

    def sprite(self, name):
        f = SPRITES / f"{name}.png"
        if not f.is_file():
            hand = STATIC / "sprites" / f"{name}.png"  # hand-placed sprites win
            if hand.is_file():
                f = hand
            elif name in MISSING:
                return self.send(404, {})
            else:
                img, gone = fetch_sprite(name)
                if not img:
                    if gone:
                        MISSING.add(name)
                    return self.send(404, {})
                SPRITES.mkdir(exist_ok=True)
                tmp = f.with_suffix(f".{secrets.token_hex(4)}.tmp")
                tmp.write_bytes(img)
                os.replace(tmp, f)
        self.send_file(f, "image/png", "public, max-age=2592000")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        p = unquote(self.path.split("?")[0])
        m = SPRITE_RE.match(p)
        if m:
            return self.sprite(m.group(1))
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
    print(f"Dex API on http://{HOST}:{PORT}, data in {DATA}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
