# =============================================================================
# content_plan.py — Monthly content plan generator for ksj.lv
# =============================================================================
"""
Generates a monthly content plan (30-31 articles) with unique titles
across all previously published articles.

Endpoint: POST /api/generate-content-plan
Input:
  - targetMonth: "2026-05" (optional, defaults to next month)
  - existingTitles: {"SharePoint": ["title1", ...], ...}
  - articlesPerDay: 1 (default)
  - categories: [...] (optional override)
Output:
  - JSON with articles array ready for SharePoint import
"""

import json
import logging
import os
import random
import calendar
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from article_gen import chat_json

# =============================================================================
# Constants
# =============================================================================
DEFAULT_CATEGORIES = [
    "SharePoint",
    "AI & Copilot",
    "Teams & Copilot",
    "SharePoint no nulles",
    "SPFx/Dev",
    "Microsoft 365 jaunumi",
    "Dro\u0161\u012bba & Atbilst\u012bba",
    "Procesi",
    "Public\u0113\u0161ana & SEO",
    "Power Automate",
    "Power Apps",
    "Teams",
    "Integr\u0101cijas",
]

PHASES = ["L1", "L2", "L3"]
IMAGE_STYLES = ["Photo", "Isometric", "Flat", "3D"]


# =============================================================================
# Helpers
# =============================================================================
def _distribute_categories(categories: List[str], total: int) -> Dict[str, int]:
    """Return {category: count} distributing total across categories (2-3 each)."""
    n = len(categories)
    base = total // n
    extra = total % n
    return {
        cat: base + (1 if i < extra else 0)
        for i, cat in enumerate(categories)
    }


def _is_unique(title: str, existing: List[str], prefix_len: int = 15) -> bool:
    """Check title is not duplicate or too similar (shared prefix)."""
    t = title.lower().strip()
    for ex in existing:
        e = ex.lower().strip()
        if t == e:
            return False
        if len(t) >= prefix_len and t[:prefix_len] == e[:prefix_len]:
            return False
    return True


def _build_batch_prompt(
    target_month: str,
    month_name_lv: str,
    category: str,
    count: int,
    existing_titles: List[str],
    phases: List[str],
    styles: List[str],
) -> dict:
    """Build GPT prompt to generate multiple article plans for one category."""

    existing_block = ""
    if existing_titles:
        titles_list = "\n".join(f"  - {t}" for t in existing_titles[-30:])
        existing_block = (
            f"ESO\u0160IE VIRSRAKSTI kategorij\u0101 \"{category}\" "
            f"(NEIZMANTO l\u012bdz\u012bgus un neatk\u0101rto t\u0113mas!):\n{titles_list}"
        )

    entries = "\n".join(
        f"  #{i+1}: f\u0101ze={phases[i % len(phases)]}, stils={styles[i % len(styles)]}"
        for i in range(count)
    )

    system = (
        "Tu esi Kaspars Jurj\u0101ns \u2014 Latvijas vado\u0161ais SharePoint un Microsoft 365 "
        "konsultants. Tu veido ikm\u0113ne\u0161a satura pl\u0101nu B2B tehnisku rakstu "
        "public\u0113\u0161anai ksj.lv vietn\u0113.\n\n"
        f"UZDEVUMS: Izveido {count} UNIK\u0100LUS raksta pl\u0101na ierakstus "
        f"kategorijai \"{category}\" m\u0113nesim {month_name_lv} ({target_month}).\n\n"
        "PRAS\u012aBAS katram virsrakstam (title):\n"
        "\u2022 Latvie\u0161u valod\u0101, max 58 rakstz\u012bmes\n"
        "\u2022 Satur skaitli (3, 5, 7, 10 utt.)\n"
        "\u2022 Konkr\u0113ts un praktisks \u2014 ne visp\u0101r\u012bgs\n"
        "\u2022 PILN\u012aGI AT\u0160\u0136IR\u012aGS no eso\u0161ajiem virsrakstiem\n"
        "\u2022 Katrs virsraksts par CITU apak\u0161t\u0113mu\n"
        "\u2022 Saist\u012bts ar aktu\u0101liem Microsoft 365/SharePoint jaunumiem, "
        "deadlines vai biznesa vajadz\u012bb\u0101m\n\n"
        "PRAS\u012aBAS p\u0101r\u0113jiem laukiem:\n"
        "\u2022 primary: galven\u0101 t\u0113ma (2-5 v\u0101rdi)\n"
        "\u2022 angle: le\u0146\u0137is/apak\u0161t\u0113ma ar skaitli (5-10 v\u0101rdi)\n"
        "\u2022 audience: m\u0113r\u0137auditorija latvie\u0161u valod\u0101 (1-3 v\u0101rdi)\n\n"
        "ATBILDI TIKAI AR DER\u012aGU JSON:\n"
        "{\"articles\": [{\"title\":..., \"primary\":..., \"angle\":..., \"audience\":...}, ...]}"
    )

    user = (
        f"M\u0113nesis: {target_month} ({month_name_lv})\n"
        f"Kategorija: {category}\n"
        f"Vajadz\u012bgi raksti: {count}\n\n"
        f"Katram rakstam piešķirtā fāze un stils:\n{entries}"
        f"\n\n{existing_block}"
    )

    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 250 * count,
        "temperature": 0.85,
        "response_format": {"type": "json_object"},
    }


# =============================================================================
# Latvian month names
# =============================================================================
LV_MONTHS = {
    1: "janv\u0101ris", 2: "febru\u0101ris", 3: "marts",
    4: "apr\u012blis", 5: "maijs", 6: "j\u016bnijs",
    7: "j\u016blijs", 8: "augusts", 9: "septembris",
    10: "oktobris", 11: "novembris", 12: "decembris",
}


# =============================================================================
# Main generator
# =============================================================================
def generate_content_plan(
    target_month: Optional[str] = None,
    existing_titles: Optional[Dict[str, List[str]]] = None,
    categories: Optional[List[str]] = None,
    articles_per_day: int = 1,
) -> Dict:
    """
    Generate a full month content plan with unique titles.

    Args:
        target_month: "YYYY-MM" format. Defaults to next month.
        existing_titles: {category: [title, ...]} of previously published articles.
        categories: override category list.
        articles_per_day: articles per calendar day (default 1).

    Returns:
        {"month": "2026-05", "totalArticles": 31, "articles": [...]}
    """
    # --- Determine target month ---
    if not target_month:
        now = datetime.utcnow()
        next_m = now.replace(day=28) + timedelta(days=4)
        target_month = next_m.strftime("%Y-%m")

    year = int(target_month.split("-")[0])
    month = int(target_month.split("-")[1])
    num_days = calendar.monthrange(year, month)[1]
    total_articles = num_days * articles_per_day
    month_name_lv = LV_MONTHS.get(month, target_month)

    cats = categories or DEFAULT_CATEGORIES
    existing = existing_titles or {}

    # --- Distribute categories ---
    cat_counts = _distribute_categories(cats, total_articles)
    logging.info(
        "[content_plan] %s: %d articles across %d categories",
        target_month, total_articles, len(cats),
    )

    # --- Generate per category (batched GPT calls) ---
    all_articles: List[dict] = []
    generated_by_cat: Dict[str, List[str]] = {cat: [] for cat in cats}

    for cat in cats:
        count = cat_counts.get(cat, 0)
        if count == 0:
            continue

        existing_for_cat = existing.get(cat, [])

        # Assign phases/styles with rotation
        phases = [random.choice(PHASES) for _ in range(count)]
        styles = [random.choice(IMAGE_STYLES) for _ in range(count)]

        # GPT call
        payload = _build_batch_prompt(
            target_month, month_name_lv, cat, count,
            existing_for_cat + generated_by_cat[cat],
            phases, styles,
        )

        try:
            result = chat_json(payload)
            arts = result.get("articles", [])
        except Exception as e:
            logging.exception("[content_plan] GPT failed for %s: %s", cat, e)
            arts = []

        # Validate and collect
        for i, art in enumerate(arts[:count]):
            title = (art.get("title") or "").strip()
            if not title:
                continue

            # Uniqueness check against all existing + already generated
            all_known = existing_for_cat + generated_by_cat[cat]
            if not _is_unique(title, all_known):
                logging.warning("[content_plan] Non-unique: %s", title)
                # Append suffix to make unique
                title = f"{title} (v2)"

            # Truncate if too long
            if len(title) > 58:
                title = title[:57] + "\u2026"

            generated_by_cat[cat].append(title)

            all_articles.append({
                "title": title,
                "wpCategory": cat,
                "primary": (art.get("primary") or "").strip(),
                "angle": (art.get("angle") or "").strip(),
                "audience": (art.get("audience") or "").strip(),
                "phase": phases[i % len(phases)],
                "status": "Ready",
                "fbStatus": "Planned",
                "fbImageStyle": styles[i % len(styles)],
            })

        # If GPT returned fewer than requested, log it
        if len(arts) < count:
            logging.warning(
                "[content_plan] %s: requested %d, got %d", cat, count, len(arts)
            )

    # --- Assign dates (interleave categories across calendar days) ---
    by_cat: Dict[str, List[dict]] = {}
    for art in all_articles:
        by_cat.setdefault(art["wpCategory"], []).append(art)

    dated: List[dict] = []
    day = 1
    while day <= num_days and any(len(v) > 0 for v in by_cat.values()):
        for cat in cats:
            if by_cat.get(cat) and day <= num_days:
                art = by_cat[cat].pop(0)
                art["datums"] = f"{year}-{month:02d}-{day:02d}"
                dated.append(art)
                day += 1

    dated.sort(key=lambda x: x["datums"])

    return {
        "month": target_month,
        "totalArticles": len(dated),
        "categories": cats,
        "articles": dated,
    }
