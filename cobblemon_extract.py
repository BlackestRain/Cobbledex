#!/usr/bin/env python3
"""
Extract Cobblemon spawn data into a human-readable spreadsheet.

Sheet 'Mons': one row per mon -> [tick] | Name | Rarity | Biome | Conditions | Catch chance | Drops
  Multiple spawn entries for a mon are numbered (1., 2., ...) and the numbers line up
  across the Rarity, Biome and Conditions cells. Repeated text becomes "Same as 1".
Sheet 'All spawn entries': one row per spawn entry.

Usage:
    python cobblemon_extract.py --download -o mons.json            # fetch data, write JSON only
    python cobblemon_extract.py --download --ref <tag> -o mons.json
    python cobblemon_extract.py <data_dir> [more_dirs...] [-o out.xlsx] [--include-variants]

--download fetches only the needed data folder from Cobblemon's GitLab repo into a
temporary directory and deletes it afterwards. Any data_dirs given as well load after
it, so datapacks override the base data.
An output name ending in .json writes just the Pokedex JSON (no xlsxwriter needed);
anything else writes the spreadsheet (pip install xlsxwriter) plus the JSON beside it.

Each <data_dir> is any folder that contains (somewhere below it) the `species` and
`spawn_pool_world` folders. Directories load in order; later ones override earlier
ones when a file has the same relative path (like datapacks do).
Alpha and herd spawns are skipped unless --include-variants is given.
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
        species[norm(path.stem)] = data
        if data.get("name"):
            species.setdefault(norm(data["name"]), data)
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
            if variant and not args.include_variants:
                continue
            sp = species.get(norm(base), {})
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

            detail.append({
                "variant": variant,
                "name": name,
                "rarity": rarity,
                "biome": biome,
                "level": dash(entry.get("level", "")),
                "conditions": sentence(parts),
                "catch": sp.get("catchRate", ""),
                "dex": sp.get("nationalPokedexNumber"),
                "types": [t for t in (sp.get("primaryType"), sp.get("secondaryType")) if t],
                "stats": sp.get("baseStats", {}),
                "drops": format_drops(entry.get("drops") or sp.get("drops")),
            })

    detail.sort(key=lambda d: (VARIANT_ORDER.get(d["variant"], 9), d["name"].lower()))

    # one row per mon, dropping exact duplicate spawn entries
    groups = {}
    for d in detail:
        g = groups.setdefault(d["name"], {"catch": d["catch"], "drops": [], "entries": [],
                                              "dex": d["dex"], "types": d["types"], "stats": d["stats"]})
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

    # JSON for the Pokedex viewer (cobblemon_dex.html)
    out = [{"name": n, "dex": g["dex"], "types": g["types"], "stats": g["stats"],
            "catch": g["catch"], "drops": g["drops"],
            "spawns": [dict(zip(("rarity", "biome", "level", "conditions"), e)) for e in g["entries"]]}
           for n, g in groups.items()]
    xlsx = not args.output.lower().endswith(".json")
    jpath = Path(args.output).with_suffix(".json")
    jpath.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
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


    print(f"Wrote {jpath}")
    print(f"Wrote {len(summary)} mons ({len(detail)} spawn entries)" + (f" to {args.output}" if xlsx else ""))


if __name__ == "__main__":
    main()
