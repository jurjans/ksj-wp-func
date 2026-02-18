# file: config_lint.py
import json, sys, csv, os

ROOT = os.path.dirname(__file__)
CONFIG_DIR = os.path.join(ROOT, "config")

TAGS = os.path.join(CONFIG_DIR, "tags.json")
AMAP = os.path.join(CONFIG_DIR, "anchor_map.json")
CSV  = os.path.join(ROOT, "Facebook-20-10-2025.csv")  # nomaini, ja vajag

PER_POST_TAG_LIMIT = int(os.getenv("PER_POST_TAG_LIMIT","3"))

def load_json(p): 
    with open(p, "r", encoding="utf-8") as f: 
        return json.load(f)

def main():
    tags = load_json(TAGS)
    wl = {t["slug"]: t["name"] for t in tags}
    amap = load_json(AMAP)

    # 1) anchor_map -> whitelist
    errors = []
    warnings = []
    for anchor, slugs in amap.items():
        if not isinstance(slugs, list):
            errors.append(f"[anchor_map] {anchor}: vērtība nav saraksts")
            continue
        # limit
        if len(slugs) > PER_POST_TAG_LIMIT:
            warnings.append(f"[anchor_map] {anchor}: {len(slugs)} slugi > limit {PER_POST_TAG_LIMIT}")
        # whitelist pārbaude
        for s in slugs:
            if s not in wl:
                errors.append(f"[anchor_map] {anchor}: slug '{s}' NAV whitelistā")

    # 2) CSV SeoSlug pārklājums
    not_covered = set()
    if os.path.exists(CSV):
        with open(CSV, newline="", encoding="utf-8") as f:
            rdr = csv.DictReader(f)
            for row in rdr:
                seo = (row.get("SeoSlug") or "").strip().lower()
                if seo and seo not in amap:
                    not_covered.add(seo)

    # 3) whitelist “neizmantotie” (pēc izvēles)
    used = {s for sl in amap.values() for s in sl if isinstance(sl, list)}
    unused = sorted([s for s in wl.keys() if s not in used])

    # 4) Report
    print("=== VALIDATION REPORT ===")
    if errors:
        print("\nErrors:")
        for e in errors: print(" -", e)
    else:
        print("\nErrors: none ✅")

    if warnings:
        print("\nWarnings:")
        for w in warnings: print(" -", w)
    else:
        print("\nWarnings: none")

    if not_covered:
        print("\nSeoSlug NAV mapē (pievieno anchor_map.json):")
        for a in sorted(not_covered): print(" -", a)
    else:
        print("\nVisi CSV SeoSlug ir mapē ✅ (vai CSV nebija)")

    print("\nWhitelist slugi, kas šobrīd netiek izmantoti (ok, ja apzināti):")
    for s in unused: print(" -", s)

if __name__ == "__main__":
    main()
