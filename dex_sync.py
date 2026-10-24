"""
Receives Pokedex data pushed by the Cobbledex Sync mod and ticks the matching dex tiles.

Used by dex_server.py (see apply_sync_patch.py). Standard library only.

The mod POSTs its whole Pokedex to /api/sync with  Authorization: Bearer <your dex key>.
The key is the same private key the site shows under "my key", so the mod and the site
share one identity. Only OWNED (caught) forms are applied, and only additively: syncing
never un-ticks anything, so manual ticks are safe.
"""
import json
import re
from pathlib import Path

MAX_SYNC_BODY = 2_000_000          # a full dex is ~100-200 KB; dex_server's normal limit is too small
REGION = {"alolan": "alola", "galarian": "galar", "hisuian": "hisui", "paldean": "paldea"}

_cache = {"mtime": None, "map": {}}


def squash(s):
    """Lowercase and drop everything except letters and digits ('Mr. Mime' / 'mr_mime' -> 'mrmime')."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _derive_key(name):
    """Fallback when mons.json has no 'key' field: 'Ninetales (Alolan)' -> 'ninetalesalola'."""
    m = re.match(r"^(.*?)\s*\((.+)\)\s*$", str(name))
    if not m:
        return squash(name)
    form = squash(m.group(2))
    return squash(m.group(1)) + REGION.get(form, form)


def tile_index(static):
    """{ squashed species/form key -> tile name as shown on the site }, rebuilt when mons.json changes."""
    f = Path(static) / "mons.json"
    try:
        mtime = f.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] != mtime:
        try:
            mons = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        index = {}
        for mon in mons:
            name = mon.get("name")
            if not name:
                continue
            key = squash(mon.get("key")) if mon.get("key") else _derive_key(name)
            index.setdefault(key, name)
        _cache.update(mtime=mtime, map=index)
    return _cache["map"]


def apply_sync(user, species_list, index, max_caught):
    """Tick the tiles for every OWNED form. Returns (newly ticked, form keys that matched no tile)."""
    added, unmatched = 0, []
    if not isinstance(species_list, list):
        return added, unmatched
    for entry in species_list[:3000]:
        if not isinstance(entry, dict):
            continue
        sid = squash(entry.get("species"))
        forms = entry.get("forms")
        if not sid or not isinstance(forms, dict):
            continue
        for form, knowledge in forms.items():
            if str(knowledge).upper() != "OWNED":
                continue
            f = squash(form)
            key = sid if f in ("", "normal") else sid + REGION.get(f, f)
            # A form the dex has no separate tile for (cosmetic variants) counts toward the base tile.
            name = index.get(key) or index.get(sid)
            if not name:
                unmatched.append(key)
                continue
            if name not in user["caught"]:
                if len(user["caught"]) >= max_caught:
                    return added, unmatched
                user["caught"][name] = 1
                added += 1
    return added, unmatched


def handle(auth_header, body, read_user, write_user, static, max_caught):
    """Whole /api/sync request. Returns (http status, JSON-able response)."""
    auth = str(auth_header or "")
    uid = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    user = read_user(uid)
    if not user:
        return 401, {"error": "unknown key"}
    index = tile_index(static)
    if not index:
        return 503, {"error": "mons.json not available on the server"}
    added, unmatched = apply_sync(user, body.get("species"), index, max_caught)
    if added:
        write_user(uid, user)
    return 200, {"ok": True, "added": added, "total": len(user["caught"]), "unmatched": unmatched[:20]}
