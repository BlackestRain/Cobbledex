#!/usr/bin/env python3
"""
Extract Cobblemon spawn data into a human-readable spreadsheet and modular JSONs.

Usage:
    python cobblemon_extract.py --download -o mons.json            # fetch data, write JSONs only
    python cobblemon_extract.py --download --ref <tag> -o mons.json
    python cobblemon_extract.py <data_dir> [more_dirs...] [-o out.xlsx] [--include-variants]
"""
import argparse
import json
import re
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

GITLAB_ARCHIVE = "https://gitlab.com/api/v4/projects/cable-mc%2Fcobblemon/repository/archive.zip"
DATA_PATH = "common/src/main/resources/data/cobblemon"


REGION = {"alolan": "alola", "galarian": "galar", "hisuian": "hisui", "paldean": "paldea"}
ADJECTIVE = {v: k for k, v in REGION.items()}  # alola -> alolan
FORM_FIX = {"highplains": "high-plains", "icysnow": "icy-snow", "pokeball": "poke-ball"}


def sprite_slug(name, tokens, base):
    """'Marowak' + ['alolan'] -> 'marowak-alola'; Vivillon patterns -> 'vivillon-river'."""
    clean = lambda s: re.sub(r"[^a-z0-9]+", "-", re.sub(r"[.'\u2019]", "", s.lower())).strip("-")
    parts = [clean(name or norm(base))]
    for t in tokens:
        t = t.split("=", 1)[-1].lower()
        if not t or norm(t) == norm(base):
            continue
        key = re.sub(r"[^a-z0-9]", "", t)
        parts.append(REGION.get(key) or FORM_FIX.get(key) or clean(t))
    return "-".join(p for p in parts if p)


def form_alias(tok):
    """Canonical form key: 'alolan' / 'Alola' -> 'alola', 'galarian' -> 'galar', 'river' -> 'river'."""
    t = norm(str(tok).split("=", 1)[-1])
    return REGION.get(t, t)


def download(ref, dest):
    """Download just the Cobblemon data folder at <ref> (branch or tag) into dest."""
    url = f"{GITLAB_ARCHIVE}?sha={urllib.parse.quote(ref)}&path={DATA_PATH}"
    print(f"Downloading Cobblemon data ({ref})...")
    req = urllib.request.Request(url, headers={"User-Agent": "cobbledex-extract"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            blob = r.read()
    except Exception as e:
        sys.exit(f"Download failed: {e}")
    with zipfile.ZipFile(BytesIO(blob)) as z:
        z.extractall(dest)
    print(f"  {len(blob) / 1e6:.1f} MB downloaded")


LANG_FILE = "common/src/main/resources/assets/cobblemon/lang/en_us.json"


def fetch_move_names(ref):
    """Move display names ('powdersnow' -> 'Powder Snow') from Cobblemon's English lang file.
    Non-fatal: returns {} if it can't be fetched."""
    url = (f"https://gitlab.com/api/v4/projects/cable-mc%2Fcobblemon/repository/files/"
           f"{urllib.parse.quote(LANG_FILE, safe='')}/raw?ref={urllib.parse.quote(ref)}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cobbledex-extract"})
        with urllib.request.urlopen(req, timeout=60) as r:
            lang = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  could not fetch move names ({e}); movenames.json not written", file=sys.stderr)
        return {}, {}
    out, descs = {}, {}
    for k, v in lang.items():
        m = re.fullmatch(r"cobblemon\.move\.([a-z0-9_]+?)(\.desc)?", k)
        if m and isinstance(v, str):
            (descs if m.group(2) else out)[m.group(1).replace("_", "")] = v
    return out, descs


SHOWDOWN_URLS = ["https://play.pokemonshowdown.com/data/moves.json",
                 "https://play.pokemonshowdown.com/data/moves.js"]


def fetch_showdown_moves():
    """Move stats from Pokemon Showdown (the data Cobblemon battles are built on).
    Keys are lowercase ids matching moves.json ('babydolleyes'). Returns {} on failure."""
    raw = None
    for url in SHOWDOWN_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cobbledex-extract"})
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode("utf-8")
            if url.endswith(".json"):
                raw = json.loads(text)
            else:  # moves.js: exports.BattleMovedex = {...};  -> quote bare keys, then parse
                body = text[text.index("{"):text.rindex("}") + 1]
                body = re.sub(r"([{,]\s*)([A-Za-z0-9_]+)(\s*:)", r'\1"\2"\3', body)
                body = re.sub(r",(\s*[}\]])", r"\1", body)  # trailing commas
                raw = json.loads(body)
            break
        except Exception as e:
            print(f"  could not read {url}: {e}", file=sys.stderr)
    if not isinstance(raw, dict):
        return {}
    out = {}
    for mid, m in raw.items():
        if not isinstance(m, dict) or "name" not in m:
            continue
        acc = m.get("accuracy")
        sec = m.get("secondary") or {}
        entry = {
            "name": m.get("name"),
            "type": str(m.get("type", "")).lower(),
            "cat": str(m.get("category", "")).lower(),
            "power": m.get("basePower") or 0,
            "accuracy": acc if isinstance(acc, (int, float)) and not isinstance(acc, bool) else 0,
            "pp": m.get("pp") or 0,
            "priority": m.get("priority") or 0,
            "effectChance": sec.get("chance", 0) if isinstance(sec, dict) else 0,
            "target": words(str(m.get("target", ""))),
            "desc": m.get("shortDesc") or m.get("desc") or "",
        }
        out[re.sub(r"[^a-z0-9]", "", mid.lower())] = {k: v for k, v in entry.items() if v not in ("", 0, None)}
    return out


# ----------------------------------------------------------------- name helpers
DEX_PREFIX = re.compile(r"^\d+[\s_-]+")
VARIANT_RE = re.compile(r"[_-](alpha|herd)$", re.I)
VARIANT_ORDER = {"": 0, "alpha": 1, "herd": 2}


def norm(s):
    """Normalise a species reference: drop namespace and leading dex number, lowercase."""
    s = str(s).strip().lower().split(":")[-1]
    return DEX_PREFIX.sub("", s)


def pretty(s):
    """Display name: drop namespace and dex number, tidy capitalisation."""
    s = DEX_PREFIX.sub("", str(s).strip().split(":")[-1]).replace("_", " ")
    return s.title() if s.islower() else s


def form_label(tokens, base):
    """['vivillon_wings=river'] -> 'River'; drops repeated species names and keys."""
    out = []
    for t in tokens:
        if "=" in t:
            t = t.split("=", 1)[1]
        if not t or norm(t) == norm(base):
            continue
        p = pretty(t)
        if p not in out:
            out.append(p)
    return " ".join(out)


def split_variant(s):
    """'0571_zoroark_alpha' -> ('0571_zoroark', 'alpha'); no suffix -> (s, '')."""
    m = VARIANT_RE.search(str(s).strip())
    if not m:
        return str(s), ""
    return str(s)[:m.start()], m.group(1).lower()


# ------------------------------------------------------- human-readable text
def words(camel):
    """'neededBaseBlocks' -> 'needed base blocks'"""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", camel).lower()


def label(v):
    """'#cobblemon:is_coast' -> 'coast'; 'shipwreck_coves/submerged_cove' -> 'submerged cove'"""
    if isinstance(v, list):
        return ", ".join(label(x) for x in v)
    s = str(v).lstrip("#").split(":")[-1].split("/")[-1]
    s = re.sub(r"^is_", "", s)
    return s.replace("_", " ")


def block_label(v):
    out = []
    for item in (v if isinstance(v, list) else [v]):
        lab = label(item)
        if str(item).startswith("#") and "block" not in lab:
            lab += " blocks"
        out.append(lab)
    return ", ".join(out)


def dash(text):
    """Use an en dash in numeric ranges: 35-48 -> 35–48"""
    return re.sub(r"(?<=\d)-(?=\d)", "–", str(text))


CONTEXTS = {
    "grounded": "on the ground",
    "submerged": "underwater",
    "surface": "at the water surface",
    "seafloor": "on the sea floor",
    "lavafloor": "on the lava floor",
    "lavasurface": "on the lava surface",
}
TIMES = {"day": "daytime", "night": "nighttime"}
BOOLS = {
    "canSeeSky": ("open sky", "no view of the sky"),
    "isRaining": ("raining", "not raining"),
    "isThundering": ("thunderstorm", "no thunderstorm"),
    "isSlimeChunk": ("slime chunk", "not a slime chunk"),
}
RANGES = {
    "sky light": ("minSkyLight", "maxSkyLight"),
    "light level": ("minLight", "maxLight"),
    "Y level": ("minY", "maxY"),
    "lure level": ("minLureLevel", "maxLureLevel"),
    "water depth": ("minDepth", "maxDepth"),
}


def rng(name, lo, hi, neg=False):
    if neg:
        if lo is not None and hi is None:
            return f"{name} below {lo}"
        if hi is not None and lo is None:
            return f"{name} above {hi}"
        return f"not {name} {lo}–{hi}"
    if lo is not None and hi is not None:
        return f"{name} {lo}–{hi}"
    if lo is not None:
        return f"{name} {lo}+"
    return f"{name} up to {hi}"


def describe(cond, neg=False):
    """Turn a condition (or anticondition) dict into a list of plain-English phrases."""
    cond = {k: v for k, v in cond.items() if k != "biomes"}
    out = []
    for name, (lo_k, hi_k) in RANGES.items():
        lo, hi = cond.pop(lo_k, None), cond.pop(hi_k, None)
        if lo is not None or hi is not None:
            out.append(rng(name, lo, hi, neg))
    no = "not " if neg else ""
    for k, v in cond.items():
        if k in BOOLS and isinstance(v, bool):
            out.append(BOOLS[k][0 if v != neg else 1])
        elif k == "timeRange":
            out.append(no + TIMES.get(str(v), label(v)))
        elif k == "fluid":
            out.append(f"{no}in {label(v)}")
        elif k == "neededBaseBlocks":
            out.append(f"{no}on {block_label(v)}")
        elif k == "neededNearbyBlocks":
            out.append(f"{no}near {block_label(v)}")
        elif k == "structures":
            out.append(f"{no}inside {label(v)}")
        elif k == "dimensions":
            out.append(f"{no}dimension: {label(v)}")
        elif isinstance(v, bool):
            out.append(words(k) if v != neg else f"not {words(k)}")
        else:
            out.append(f"{no}{words(k)}: {label(v)}")
    return out


def sentence(parts):
    text = "; ".join(parts)
    return text[:1].upper() + text[1:] if text else ""


def biome_text(biomes, anti):
    def names(v):
        return ", ".join(label(b).title() for b in (v if isinstance(v, list) else [v]))
    text = names(biomes) if biomes else "Any biome"
    if anti:
        text += f" (except {names(anti)})"
    return text


def format_drops(drops):
    if not drops:
        return ""
    entries = drops.get("entries", []) if isinstance(drops, dict) else drops
    lines = []
    for e in entries:
        text = label(e.get("item", "?")).title()
        qty = e.get("quantityRange") or e.get("quantity")
        if qty:
            text += f" ×{dash(qty)}"
        pct = e.get("percentage")
        if isinstance(pct, (int, float)):
            text += f" ({pct:g}% chance)"
        lines.append("• " + text)
    amount = drops.get("amount") if isinstance(drops, dict) else None
    head = f"{amount} roll{'s' if amount != 1 else ''}:\n" if amount else ""
    return head + "\n".join(lines)


def numbered(items, collapse=False):
    """Number multi-entry cells. With collapse, identical lines say 'Same as N'
    and a cell whose entries are all identical is shown once, unnumbered."""
    if len(items) == 1 or (collapse and len(set(items)) == 1):
        return items[0]
    seen, lines = {}, []
    for i, t in enumerate(items, 1):
        if collapse and t in seen:
            t = f"Same as {seen[t]}"
        else:
            seen.setdefault(t, i)
        lines.append(f"{i}. {t}")
    return "\n".join(lines)


# ----------------------------------------------------------------- file loading
def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  skipped {path}: {e}", file=sys.stderr)
        return None


def collect(roots, folder_name):
    """Find every <folder_name>/**/*.json under the roots.
    Returns {relative_path: (path, json)}; later roots override earlier."""
    found = {}
    for root in roots:
        for folder in Path(root).rglob(folder_name):
            if not folder.is_dir():
                continue
            for path in folder.rglob("*.json"):
                data = load_json(path)
                if data is not None:
                    found[(folder_name, str(path.relative_to(folder)))] = (path, data)
    return found


def merge_preset(entry, presets):
    """Apply spawn_detail_presets underneath the entry's own values."""
    merged = dict(entry)
    for name in entry.get("presets", []):
        preset = presets.get(name.replace("cobblemon:", ""))
        if not preset:
            continue
        for key, val in preset.items():
            if key in ("condition", "anticondition") and isinstance(val, dict):
                combined = dict(val)
                combined.update(merged.get(key, {}))
                merged[key] = combined
            elif key not in merged:
                merged[key] = val
    return merged


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="*", help="data directories (base first, then datapacks)")
    ap.add_argument("--download", action="store_true",
                    help="fetch base Cobblemon data from GitLab into a temp folder (deleted afterwards)")
    ap.add_argument("--ref", default="main", help="branch or tag to download (default: main)")
    ap.add_argument("-o", "--output", default="cobblemon_spawns.xlsx")
    ap.add_argument("--include-variants", action="store_true",
                    help="include alpha and herd spawns (excluded by default)")
    args = ap.parse_args()

    tmp = tempfile.TemporaryDirectory(prefix="cobblemon-")
    roots = list(args.roots)
    if args.download:
        download(args.ref, tmp.name)
        roots.insert(0, tmp.name)
    if not roots:
        ap.error("give at least one data directory, or use --download")

    species_files = collect(roots, "species")
    spawn_files = collect(roots, "spawn_pool_world")
    preset_files = collect(roots, "spawn_detail_presets")

    species = {}
    for (_, _rel), (path, data) in species_files.items():
        base_name = norm(data.get("name", path.stem))
        species[base_name] = data
        species[norm(path.stem)] = data

        # Unpack nested forms cleanly
        for form in data.get("forms", []):
            form_name = form.get("name", "")
            if not form_name:
                continue
            # Base data with the form's own values layered over it
            merged_form = {**data, **form}
            merged_form["name"] = data.get("name", path.stem)  # keep species name ("Ninetales"), not form name ("Alola")
            merged_form["primaryType"] = form.get("primaryType") or data.get("primaryType")
            merged_form["secondaryType"] = form.get("secondaryType")  # don't inherit base's 2nd type
            merged_form["_key"] = f"{base_name}_{form_alias(form_name)}"
            merged_form.pop("forms", None)

            # Index by every way a spawn file might refer to this form:
            # the form name ("Alola") and its aspects ("alolan"), both normalised to one alias.
            aliases = {form_alias(form_name), norm(form_name)}
            aliases |= {form_alias(a) for a in form.get("aspects", [])}
            aliases |= {norm(a) for a in form.get("aspects", [])}
            for a in aliases:
                for stem in {path.stem, base_name}:
                    species[norm(f"{stem}_{a}")] = merged_form
                    species[norm(f"{stem}-{a}")] = merged_form
        data["_key"] = base_name
    presets = {path.stem: data for (_, _rel), (path, data) in preset_files.items()}

    tmp.cleanup()  # everything is loaded into memory now
    print(f"{len(species)} species, {len(spawn_files)} spawn files, {len(presets)} presets")

    detail = []
    for (_, _rel), (path, data) in sorted(spawn_files.items()):
        if data.get("enabled") is False:
            continue
        for raw in data.get("spawns", []):
            entry = merge_preset(raw, presets)

            poke = str(entry.get("pokemon") or entry.get("species") or path.stem)
            tokens = poke.split()
            base, variant = split_variant(tokens[0])
            if not variant:
                variant = split_variant(path.stem)[1] or split_variant(entry.get("id", ""))[1]

            # Check for aspect/form tokens (e.g., 'alolan')
            form_token = ""
            for t in tokens[1:]:
                if "=" in t:
                    form_token = t.split("=")[1]
                else:
                    form_token = t

            if variant and not args.include_variants:
                continue
            if norm(base) == "empty":
                continue

            # Robust lookup checking form-specific keys first
            fa = form_alias(form_token) if form_token else ""
            lookup_keys = [
                norm(f"{base}_{fa}") if fa else "",
                norm(f"{base}-{fa}") if fa else "",
                norm(f"{base}_{form_token}") if form_token else "",
                norm(f"{base}_{variant}") if variant else "",
                norm(f"{base}-{variant}") if variant else "",
                norm(poke),
                norm(base)
            ]

            sp = {}
            for k in lookup_keys:
                if k and k in species:
                    sp = species[k]
                    break
            if not sp:
                sp = species.get(norm(base), {})
            elif form_token and sp.get("_key") == norm(base):
                print(f"  form '{form_token}' of '{base}' not found, used base data ({path.name})", file=sys.stderr)

            if not sp:
                print(f"  no species file found for '{base}' ({path.name})", file=sys.stderr)

            name = pretty(sp.get("name", base))
            form = form_label(tokens[1:], base)
            if form:
                name += f" ({form})"
            if variant:
                name += f" ({variant.title()})"

            bucket = str(entry.get("bucket", "?")).capitalize()
            weight = entry.get("weight")
            rarity = f"{bucket} (weight {weight:g})" if isinstance(weight, (int, float)) else bucket

            cond = entry.get("condition", {}) or {}
            anti = entry.get("anticondition", {}) or {}
            biome = biome_text(cond.get("biomes"), anti.get("biomes"))

            parts = []
            if entry.get("context"):
                parts.append(CONTEXTS.get(str(entry["context"]).lower(), label(entry["context"])))
            parts += describe(cond)
            parts += describe(anti, neg=True)

            # Explicitly extract types from the resolved species/form block
            mon_types = [t for t in (sp.get("primaryType"), sp.get("secondaryType")) if t]

            key = norm(base) + (f"_{fa}" if fa and sp.get("_key") != norm(base) else "")
            detail.append({
                "key": key,
                "variant": variant,
                "name": name,
                "rarity": rarity,
                "biome": biome,
                "level": dash(entry.get("level", "")),
                "conditions": sentence(parts),
                "catch": sp.get("catchRate", ""),
                "dex": sp.get("nationalPokedexNumber"),
                "slug": sprite_slug(sp.get("name"), tokens[1:], base),
                "types": mon_types,
                "stats": sp.get("baseStats", {}),
                "drops": format_drops(entry.get("drops") or sp.get("drops")),
            })

    detail.sort(key=lambda d: (VARIANT_ORDER.get(d["variant"], 9), d["name"].lower()))

    # one row per mon, dropping exact duplicate spawn entries
    groups = {}
    for d in detail:
        g = groups.setdefault(d["name"], {"catch": d["catch"], "drops": [], "entries": [],
                                              "dex": d["dex"], "key": d["key"], "slug": d["slug"], "types": d["types"], "stats": d["stats"]})
        key = (d["rarity"], d["biome"], d["level"], d["conditions"])
        if key not in g["entries"]:
            g["entries"].append(key)
        if d["drops"] and d["drops"] not in g["drops"]:
            g["drops"].append(d["drops"])

    summary = []
    for name, g in groups.items():
        ents = g["entries"]
        levels = {e[2] for e in ents if e[2]}
        rarity = numbered([e[0] for e in ents])
        biome = numbered([e[1] for e in ents], collapse=True)
        if len(levels) > 1:  # levels differ: show them per entry
            conds = [(f"Level {e[2]}. " if e[2] else "") + (e[3] or "No special conditions")
                     for e in ents]
            cond = numbered(conds, collapse=True)
        else:
            cond = numbered([e[3] or "No special conditions" for e in ents], collapse=True)
            if levels:
                cond = f"Level {levels.pop()}\n" + cond
        summary.append(["", name, rarity, biome, cond, g["catch"], "\n\n".join(g["drops"])])

    # Format evolution rules accurately capturing exact items, levels, and regional biome conditions
    def format_evo_rule(e, is_outgoing=True, base_name=""):
        res = e.get("result", "")
        frm = e.get("from", base_name)
        variant = str(e.get("variant", "")).lower()

        level = (
            e.get("requiredLevel")
            or e.get("level")
            or e.get("condition", {}).get("minLevel")
            or e.get("conditions", {}).get("minLevel")
        )
        if not level and isinstance(e.get("requirements"), list):
            for req in e["requirements"]:
                if isinstance(req, dict) and (req.get("variant") == "level" or "level" in req):
                    level = req.get("level") or req.get("minLevel")
                    break

        raw_item = None
        biome_req = None

        def search_conditions(obj):
            nonlocal raw_item, biome_req
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if any(w in k.lower() for w in ["item", "stone", "interact", "holding", "requireditem"]):
                        if isinstance(v, str) and not any(b in v.lower() for b in ["biome", "dimension", "sky", "raining", "is_"]):
                            if not raw_item:
                                raw_item = v
                    if any(w in k.lower() for w in ["biome", "biomes", "biomecondition"]):
                        if v:
                            biome_req = label(v)
                    search_conditions(v)
            elif isinstance(obj, list):
                for sub in obj:
                    search_conditions(sub)
            elif isinstance(obj, str):
                if ":" in obj:
                    if any(kw in obj.lower() for kw in ["stone", "item", "cuff", "wreath", "augurite", "block", "coat", "rock", "scale", "cloth", "orb"]):
                        if not raw_item:
                            raw_item = obj
                    elif "biome" in obj.lower() or "regional" in obj.lower():
                        biome_req = label(obj)

        search_conditions(e)

        item_name = ""
        if raw_item:
            s_item = str(raw_item).split(":")[-1].replace("_", " ").strip()
            if not any(bad in s_item.lower() for bad in ["biome", "dimension", "is_"]):
                item_name = s_item.title()

        if not item_name and "item" in variant:
            parts = variant.split(":")
            if len(parts) > 1 and not any(bad in parts[1].lower() for bad in ["biome", "dimension"]):
                item_name = parts[1].replace("_", " ").title()

        biome_str = ""
        if biome_req:
            clean_bio = biome_req.replace("Evolution Regional Pikachu ", "").replace("Biomes", "").strip()
            if "alola" in clean_bio.lower():
                biome_str = " in an Alola biome"
            elif "hisui" in clean_bio.lower():
                biome_str = " in a Hisui biome"
            elif "galar" in clean_bio.lower():
                biome_str = " in a Galar biome"
            else:
                biome_str = f" in {clean_bio}"

        if is_outgoing:
            action = f"Evolves into {pretty(res)}"
        else:
            action = f"Evolves from {pretty(frm)}"

        condition_str = ""
        if level:
            condition_str = f" at level {level}"
        elif "trade" in variant:
            condition_str = " when traded"
        elif item_name or "item" in variant or "interact" in variant:
            item_desc = f" a {item_name}" if item_name and item_name.lower() != "item" else " an evolution item"
            condition_str = f" when given{item_desc}{biome_str}"
        elif "friendship" in variant or "friend" in str(e).lower():
            time_cond = e.get("condition", {}).get("timeRange") or e.get("timeRange")
            time_str = f" during {time_cond}" if time_cond else ""
            condition_str = f" with high friendship{time_str}"
        elif level is None and ("level" in variant or not variant):
            if level:
                condition_str = f" at level {level}"
            else:
                condition_str = ""
        else:
            clean_var = variant.replace("_", " ")
            condition_str = f" ({clean_var})"

        return action + condition_str

    # Build bidirectional evolution text map per species/form
    def evo_key(ref):
        """'ninetales alolan' -> 'ninetales_alola'; 'ninetales' -> 'ninetales'."""
        toks = str(ref).split()
        k = norm(split_variant(toks[0])[0])
        return f"{k}_{form_alias(toks[-1])}" if len(toks) > 1 else k

    def display(sp, fallback):
        key = sp.get("_key", norm(fallback))
        nm = pretty(sp.get("name", fallback))
        if "_" in key and key != norm(sp.get("name", fallback)):
            f = key.split("_", 1)[1]
            nm += f" ({ADJECTIVE.get(f, f).title()})"
        return nm

    evo_sentences = {}
    seen = set()
    for n, sp in species.items():
        if id(sp) in seen:  # same dict is indexed under several aliases
            continue
        seen.add(id(sp))
        evos = sp.get("evolutions", [])
        base_name = display(sp, n)
        my_key = sp.get("_key", norm(n))
        lines = []

        for e in evos:
            res = e.get("result", "")
            if res:
                lines.append(format_evo_rule(e, is_outgoing=True))
                evo_sentences.setdefault(evo_key(res), []).append(
                    format_evo_rule({**e, "from": base_name}, is_outgoing=False))

        if lines:
            evo_sentences.setdefault(my_key, []).extend(lines)

    # 1. Core Pokédex Summary (mons.json)
    mons_out = []
    for n, g in groups.items():
        evos = evo_sentences.get(g["key"], [])
        mons_out.append({
            "name": n, "dex": g["dex"], "slug": g["slug"], "types": g["types"], "stats": g["stats"],
            "catch": g["catch"], "drops": g["drops"],
            "evolution_text": ". ".join(evos) + ("." if evos else ""),
            "spawns": [dict(zip(("rarity", "biome", "level", "conditions"), e)) for e in g["entries"]]
        })
    Path("mons.json").write_text(json.dumps(mons_out, indent=1, ensure_ascii=False), encoding="utf-8")

    # 2. Moves Data (moves.json)
    moves_out = {}
    for n, sp in species.items():
        if sp.get("moves"):
            moves_out[norm(n)] = sp.get("moves")
    for n, g in groups.items():
        sp = species.get(norm(n), {})
        if sp.get("moves"):
            moves_out[n.lower()] = sp.get("moves")
    Path("moves.json").write_text(json.dumps(moves_out, indent=1, ensure_ascii=False), encoding="utf-8")

    # 3. Move stats + display names (movedata.json, movenames.json) from Pokemon Showdown,
    #    with Cobblemon's own English lang file (if downloaded) taking priority for names.
    move_data = fetch_showdown_moves()
    names = {mid: d.pop("name") for mid, d in move_data.items() if "name" in d}
    if args.download:
        lang_names, lang_descs = fetch_move_names(args.ref)
        names.update(lang_names)
        for mid, d in lang_descs.items():
            if mid in move_data and not move_data[mid].get("desc"):
                move_data[mid]["desc"] = d
    if move_data:
        Path("movedata.json").write_text(json.dumps(move_data, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote movedata.json ({len(move_data)} moves)")
    else:
        print("  could not fetch move stats from Pokemon Showdown; movedata.json not written", file=sys.stderr)
    if names:
        Path("movenames.json").write_text(json.dumps(names, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote movenames.json ({len(names)} moves)")

    xlsx = not args.output.lower().endswith(".json")
    if xlsx:
        import xlsxwriter
        wb = xlsxwriter.Workbook(args.output)
        header = wb.add_format({"bold": True, "bg_color": "#DDDDDD", "border": 1})
        wrap = wb.add_format({"text_wrap": True, "valign": "top"})
        tick = wb.add_format({"valign": "top", "align": "center", "font_size": 14})

        ws = wb.add_worksheet("Mons")
        headers = ["", "Name", "Rarity", "Biome", "Conditions", "Catch chance", "Drops"]
        for c, h in enumerate(headers):
            ws.write(0, c, h, header)
        for r, row in enumerate(summary, start=1):
            for c, val in enumerate(row):
                if c == 0:
                    ws.write(r, 0, "\u2610", tick)
                else:
                    ws.write(r, c, val, wrap)
        ws.data_validation(1, 0, max(len(summary), 1), 0,
                           {"validate": "list", "source": ["\u2610", "\u2611"]})
        for c, width in enumerate([5, 22, 24, 40, 60, 13, 34]):
            ws.set_column(c, c, width)
        ws.freeze_panes(1, 0)
        ws.autofilter(0, 0, len(summary), len(headers) - 1)

        ws2 = wb.add_worksheet("All spawn entries")
        h2 = ["Name", "Rarity", "Biome", "Level", "Conditions", "Catch chance", "Drops"]
        for c, h in enumerate(h2):
            ws2.write(0, c, h, header)
        for r, d in enumerate(detail, start=1):
            for c, k in enumerate(["name", "rarity", "biome", "level", "conditions", "catch", "drops"]):
                ws2.write(r, c, d[k], wrap)
        for c, width in enumerate([22, 24, 40, 10, 60, 13, 34]):
            ws2.set_column(c, c, width)
        ws2.freeze_panes(1, 0)
        ws2.autofilter(0, 0, len(detail), len(h2) - 1)

        wb.close()

    print(f"Wrote mons.json and moves.json")
    print(f"Wrote {len(summary)} mons ({len(detail)} spawn entries)" + (f" to {args.output}" if xlsx else ""))


if __name__ == "__main__":
    main()
