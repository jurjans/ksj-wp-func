#!/usr/bin/env python3
"""
backfill_money_links.py — one-off: add a tasteful "Work with KSJ" / "Sadarbībai ar KSJ"
conversion block to existing ksj.lv blog posts, routing blog link-equity and readers
to the revenue pages (Privault, pricing, construction, contact).

The live content pipeline (en_article_gen.py) now emits this for NEW EN posts; this
script backfills the ~300 posts that predate it (and the LV side).

SAFE BY DESIGN:
  - Idempotent: skips any post already containing the sentinel comment.
  - Dry-run by default; pass --apply to write.
  - Reuses the repo's WP Application-Password (Basic) auth from local.settings.json.
  - Inserts the block BEFORE an existing "Further reading"/"Papildu lasāmviela"
    section when present, else appends at the end — so it always reads naturally.

Usage:
  python backfill_money_links.py                 # dry-run, all langs
  python backfill_money_links.py --lang en       # dry-run, EN only
  python backfill_money_links.py --apply         # write, all langs
  python backfill_money_links.py --apply --limit 5   # write first 5 (test batch)
"""

import argparse
import base64
import json
import os
import re
import sys
import time

import requests

# Windows consoles default to cp1252 and crash on Latvian chars / em-dash in prints.
# The HTML sent to WP is always UTF-8 (requests encodes JSON); this only fixes stdout.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

SENTINEL = "<!-- ksj-money-block -->"

# --- Revenue targets --------------------------------------------------------
EN_PRIVAULT = "https://ksj.lv/en/privault-copilot-alternative-microsoft-365/"
EN_PRICING = "https://ksj.lv/en/pricing/"
EN_CONSTRUCTION = "https://ksj.lv/en/microsoft-365-automation-construction/"
EN_CONTACT = "https://ksj.lv/en/contact/"

LV_PRIVAULT = "https://ksj.lv/privault-copilot-alternativa-microsoft-365/"
LV_SERVICES = "https://ksj.lv/sharepoint-un-microsoft-365-automatizacijas-risinajumi/"
LV_CONTACT = "https://ksj.lv/kontakti/"

EN_CONSTRUCTION_HINTS = (
    "construction", "contractor", "subcontractor", "invoice approval",
    "site manager", "procurement",
)

_BOX_OPEN = (
    '<div style="border-left:3px solid #B23A2E;background:#FAF8F5;'
    'padding:18px 22px;border-radius:0 8px 8px 0;margin:36px 0;font-family:inherit;">'
)
_A = 'style="color:#B23A2E;font-weight:600;"'


def _eyebrow(label: str) -> str:
    return (
        f'<p style="margin:0 0 8px;font-size:12px;font-weight:700;'
        f'letter-spacing:.08em;text-transform:uppercase;color:#B23A2E;">{label}</p>'
    )


def build_block_en(title: str, link: str) -> str:
    blob = f"{title} {link}".lower()
    construction = any(h in blob for h in EN_CONSTRUCTION_HINTS)
    constr = (
        f', or how we automate <a href="{EN_CONSTRUCTION}" {_A}>Microsoft&nbsp;365 for construction</a>'
        if construction else ""
    )
    body = (
        'KSJ builds private AI and Microsoft&nbsp;365 automation you own, inside your own '
        f'tenant. Meet <a href="{EN_PRIVAULT}" {_A}>Privault, our private Copilot alternative</a>, '
        f'see <a href="{EN_PRICING}" {_A}>transparent pricing</a>{constr} — or '
        f'<a href="{EN_CONTACT}" {_A}>book a 30-minute discovery call</a>.'
    )
    return (
        f"{SENTINEL}\n{_BOX_OPEN}\n{_eyebrow('Work with KSJ')}\n"
        f'<p style="margin:0;color:#5C5750;font-size:15px;line-height:1.65;">{body}</p>\n</div>'
    )


def build_block_lv(title: str, link: str) -> str:
    body = (
        'KSJ veido privātu AI un Microsoft&nbsp;365 automatizāciju, kas pieder jums — jūsu '
        f'pašu vidē. Iepazīstiet <a href="{LV_PRIVAULT}" {_A}>Privault — privāto Copilot '
        f'alternatīvu</a>, apskatiet <a href="{LV_SERVICES}" {_A}>pakalpojumus</a>, vai '
        f'<a href="{LV_CONTACT}" {_A}>piesakieties 30 minūšu konsultācijai</a>.'
    )
    return (
        f"{SENTINEL}\n{_BOX_OPEN}\n{_eyebrow('Sadarbībai ar KSJ')}\n"
        f'<p style="margin:0;color:#5C5750;font-size:15px;line-height:1.65;">{body}</p>\n</div>'
    )


# Insert before an existing "Further reading"/"Papildu lasāmviela" H2, else append.
_READING_RE = re.compile(
    r'<h2[^>]*>\s*(?:Further reading|Papildu las[āa]mviela)', re.IGNORECASE
)


def insert_block(content: str, block: str) -> str:
    m = _READING_RE.search(content)
    if m:
        return content[:m.start()].rstrip() + "\n\n" + block + "\n\n" + content[m.start():]
    return content.rstrip() + "\n\n" + block


# --- WP auth (mirrors article_gen._wp_auth_headers, basic scheme) ------------
def load_local_settings() -> None:
    """Populate os.environ from local.settings.json 'Values' for keys not already set."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "local.settings.json")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for k, v in (data.get("Values") or {}).items():
            os.environ.setdefault(k, str(v))
    except Exception as e:
        print(f"[warn] could not read local.settings.json: {e}", file=sys.stderr)


def wp_headers() -> dict:
    scheme = (os.getenv("WP_AUTH_SCHEME", "jwt") or "jwt").lower()
    if scheme == "basic":
        b64 = os.getenv("WP_BASIC_AUTH_B64")
        if not b64:
            user = os.getenv("WP_USER", "")
            pw = os.getenv("WP_APP_PASSWORD", "")
            if not (user and pw):
                sys.exit("Missing WP_USER / WP_APP_PASSWORD (or WP_BASIC_AUTH_B64).")
            b64 = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return {"Authorization": f"Basic {b64}", "Content-Type": "application/json"}
    token = os.getenv("WP_TOKEN", "")
    if not token:
        sys.exit("Missing WP_TOKEN (or set WP_AUTH_SCHEME=basic).")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def api_base() -> str:
    base = (os.getenv("WP_API_BASE", "") or "").rstrip("/")
    if not base:
        sys.exit("Missing WP_API_BASE.")
    return base


def fetch_all_posts(base: str, headers: dict) -> list:
    posts, page = [], 1
    while True:
        r = requests.get(
            f"{base}/wp/v2/posts",
            headers=headers,
            params={
                "per_page": 100, "page": page,
                "status": "publish,future", "context": "edit",
                "_fields": "id,link,status,title,content",
                "orderby": "date", "order": "desc",
            },
            timeout=30,
        )
        if r.status_code == 400:  # past last page
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        posts.extend(batch)
        total_pages = int(r.headers.get("X-WP-TotalPages", page))
        if page >= total_pages:
            break
        page += 1
    return posts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--lang", choices=["en", "lv", "both"], default="both")
    ap.add_argument("--limit", type=int, default=0, help="cap number of posts changed")
    args = ap.parse_args()

    load_local_settings()
    base, headers = api_base(), wp_headers()

    posts = fetch_all_posts(base, headers)
    print(f"Fetched {len(posts)} posts (publish+future).")

    changed = skipped = errors = 0
    sample_shown = False
    for p in posts:
        pid = p["id"]
        link = (p.get("link") or "")
        title = (p.get("title", {}) or {}).get("raw", "")
        content = (p.get("content", {}) or {}).get("raw", "")
        lang = "en" if "/en/" in link else "lv"

        if args.lang != "both" and lang != args.lang:
            continue
        if SENTINEL in content:
            skipped += 1
            continue

        block = build_block_en(title, link) if lang == "en" else build_block_lv(title, link)
        new_content = insert_block(content, block)

        if not sample_shown:
            print("\n--- SAMPLE BLOCK (%s) for post %d: %s ---" % (lang, pid, title[:70]))
            print(block)
            print("--- end sample ---\n")
            sample_shown = True

        if not args.apply:
            changed += 1
            if args.limit and changed >= args.limit:
                break
            continue

        try:
            r = requests.post(
                f"{base}/wp/v2/posts/{pid}",
                headers=headers, json={"content": new_content}, timeout=30,
            )
            r.raise_for_status()
            changed += 1
            print(f"[ok] {lang} {pid} {title[:60]}")
            time.sleep(0.3)
        except Exception as e:
            errors += 1
            print(f"[ERR] {pid}: {e}", file=sys.stderr)

        if args.limit and changed >= args.limit:
            break

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"\n{mode}: would change={changed if not args.apply else ''} "
          f"changed={changed} skipped(existing)={skipped} errors={errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
