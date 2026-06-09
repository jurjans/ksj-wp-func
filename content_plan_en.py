# =============================================================================
# content_plan_en.py — Monthly EN content plan generator for ksj.lv (/en/)
# =============================================================================
"""
Generates a monthly English-language content plan for the /en/ shopfront.

Parallel to content_plan.py (Latvian): shares no state and is never touched by
the LV path. English-native prompt, AI-led category set, weighted toward the EN
ICP (mid-market EU SMB, 50-300 staff, DE/Nordics; Copilot-alternative wedge).

Endpoint: POST /api/generate-content-plan-en
Input (JSON):
  - targetMonth:   "2026-07"   (optional; defaults to next month)
  - existingItems: [{"Title": "...", "WpCategory": "..."}, ...]   (from the EN list)
  - existingTitles:{"AI for Microsoft 365": ["..."], ...}         (alt. pre-grouped)
  - articlesPerDay: 1          (default 1 -> ~30/month)
  - categories:    [...]       (optional override; even weights if custom)
Output (JSON):
  {"month": "2026-07", "totalArticles": 31, "categories": [...], "articles": [...]}
  each article: title, wpCategory, primary, angle, audience, focusKeyword,
                phase, status, fbStatus, fbImageStyle, language, datums
"""

import calendar
import logging
import random
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from article_gen import chat_json


# =============================================================================
# EN configuration
# =============================================================================
# Canonical EN categories with target weights (must sum to 1.0).
# Names MUST be byte-identical to the WordPress EN category names AND to the
# WpCategory choice values in the EN SharePoint list.
EN_CATEGORY_WEIGHTS: Dict[str, float] = {
    "AI for Microsoft 365":       0.50,
    "AI Governance and Compliance": 0.20,
    "Microsoft 365 Automation":   0.20,
    "Microsoft 365 and SharePoint": 0.10,
}
EN_CATEGORIES: List[str] = list(EN_CATEGORY_WEIGHTS.keys())

PHASES = ["L1", "L2", "L3"]
# EN images follow the photoreal / cinematic single-subject direction.
IMAGE_STYLES = ["Photo", "Isometric", "Flat", "3D"]

DEFAULT_ARTICLES_PER_DAY = 1
TITLE_MAX_LEN = 60

# =============================================================================
# Helpers
# =============================================================================
def _distribute_weighted(weights: Dict[str, float], total: int) -> Dict[str, int]:
    """Distribute `total` across categories by weight; remainder to largest fractions."""
    raw = {c: total * w for c, w in weights.items()}
    floored = {c: int(v) for c, v in raw.items()}
    remainder = total - sum(floored.values())
    by_frac = sorted(weights, key=lambda c: raw[c] - floored[c], reverse=True)
    for i in range(max(0, remainder)):
        floored[by_frac[i % len(by_frac)]] += 1
    return floored


def _is_unique(title: str, existing: List[str]) -> bool:
    """Reject only exact (case-insensitive) duplicates. The shared-prefix heuristic was removed:
    EN titles often share a product-name lead ('Microsoft 365 Copilot:'), which the prefix check
    wrongly flagged as duplicates and suffixed with '(v2)'."""
    t = title.lower().strip()
    return all(t != ex.lower().strip() for ex in existing)


def _group_existing(existing_items: List[dict]) -> Dict[str, List[str]]:
    """Group a flat [{Title, WpCategory}] list into {category: [titles]}."""
    grouped: Dict[str, List[str]] = {}
    for it in existing_items or []:
        cat = (it.get("WpCategory") or it.get("wpCategory") or "").strip()
        title = (it.get("Title") or it.get("title") or "").strip()
        if title:
            grouped.setdefault(cat, []).append(title)
    return grouped


def _build_batch_prompt(
    target_month: str,
    month_name: str,
    category: str,
    count: int,
    existing_titles: List[str],
    phases: List[str],
    styles: List[str],
) -> dict:
    """Build the GPT prompt that generates several EN article plans for one category."""

    existing_block = ""
    if existing_titles:
        titles_list = "\n".join(f"  - {t}" for t in existing_titles[-30:])
        existing_block = (
            f'EXISTING TITLES in category "{category}" '
            f"(do NOT reuse or paraphrase these — each new title must cover a different subtopic):\n"
            f"{titles_list}"
        )

    entries = "\n".join(
        f"  #{i + 1}: phase={phases[i % len(phases)]}, style={styles[i % len(styles)]}"
        for i in range(count)
    )

    system = (
        "You are Kaspars Jurjans — a senior Microsoft 365, SharePoint and AI "
        "consultant. You plan a monthly batch of English-language B2B articles for "
        "the ksj.lv/en/ blog, aimed at an international audience.\n\n"
        "AUDIENCE (ICP): IT and operations leaders at mid-market EU companies "
        "(50–300 staff), especially Germany, Denmark and the Nordics. They evaluate "
        "Microsoft 365 Copilot and AI-driven ways to automate document and process "
        "work, and often compare alternatives. They are practical, ROI-driven and "
        "skeptical of hype.\n\n"
        f'TASK: Create {count} UNIQUE article-plan entries for the category '
        f'"{category}" for {month_name} ({target_month}).\n\n'
        "TITLE rules (each \"title\") — write natural, grammatical English headlines, never a mechanical "
        "string of words and numbers:\n"
        "• English, max 60 characters, leads on the focus keyword\n"
        "• Include ONE number where it reads naturally: either a real count for a list ('7 Ways to…', "
        "'5 Steps to…') or a year (2026), placed so the sentence stays grammatical\n"
        "• 'Microsoft 365' and 'Copilot' are PRODUCT NAMES — keep them written in full; never treat the "
        "'365' as the title's count or move it to the front as a number\n"
        "• If the title already contains a real number, do NOT add another\n"
        "• A power word (Proven, Essential, Practical, Definitive, …) only where it fits — not in every title\n"
        "• Concrete and specific; each title a distinct subtopic, different from the existing titles; tied "
        "to current Microsoft 365 / Copilot / AI capabilities or real business needs\n"
        "GOOD: '7 Ways to Automate HR Tasks in Microsoft 365' | 'Microsoft 365 Copilot: A 2026 ROI Guide' "
        "| '5 Steps to Audit AI Compliance in Copilot'\n"
        "BAD: '365 Proven Ways to Automate Workflows' (365 misused as a count) | '6 Definitive Guide to AI "
        "Compliance' (count + singular noun) | 'How 365 Drives AI Ethics' (product number used as a word)\n\n"
        "OTHER fields:\n"
        "• primary: the core topic (2–5 words)\n"
        "• angle: the specific subtopic/angle, with a number (5–10 words)\n"
        "• audience: target role in English (1–3 words, e.g. \"IT Manager\", \"Operations Lead\")\n"
        "• focusKeyword: the SEO focus keyword (2–4 words, lowercase English)\n\n"
        "Respond ONLY with valid JSON:\n"
        '{"articles": [{"title":..., "primary":..., "angle":..., "audience":..., "focusKeyword":...}, ...]}'
    )

    user = (
        f"Month: {target_month} ({month_name})\n"
        f"Category: {category}\n"
        f"Articles needed: {count}\n\n"
        f"Phase/style assigned per article:\n{entries}"
        f"\n\n{existing_block}"
    )

    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 300 * count,
        "temperature": 0.85,
        "response_format": {"type": "json_object"},
    }


# ---------------------------------------------------------------------------
# Holistic title generation
# ---------------------------------------------------------------------------
_SEO_CONTEXT = (
    "Blog: ksj.lv/en — Microsoft 365 / SharePoint / AI consulting for mid-market EU companies "
    "(50-300 staff). ICP: IT Managers, Heads of Ops, CIOs in Germany, Denmark and the Nordics "
    "evaluating Microsoft 365 Copilot and AI-driven automation. "
    "Positioning: Copilot alternative + EU angle (data residency, GDPR/NIS2, own-your-deployment)."
)

HOLISTIC_TITLE_SYSTEM = (
    f"{_SEO_CONTEXT}\n\n"
    "You write SEO-optimised, engaging English article headlines. Rules:\n"
    "- Title OPENS with the focus keyword (verbatim, naturally capitalised)\n"
    "- Each title must contain at least one number (count, year, version, or product number like 365). "
    "STRONGLY PREFER ONE number per title. Multi-number combinations (count + year, count + 'Microsoft "
    "365', etc.) are NOT forbidden but must NOT become a pattern — at most a handful in the WHOLE batch. "
    "If 'Microsoft 365' already gives the title its number, don't pad it with a separate count or year "
    "unless it genuinely adds value.\n"
    "- VARY structures across the batch. No single pattern (colon-based, 'X: N Tactics for Y', "
    "'... for 2026', count + Microsoft 365 combo) should appear in more than ~3-4 titles. Mix declarative, "
    "how-to, question, em-dash, and bare-noun structures deliberately.\n"
    "- Include a power word where it fits naturally (Proven, Essential, Practical, Complete, "
    "Definitive, Critical, Smart, …) — desirable but not forced into every title\n"
    "- AVOID the colon (':'). It is currently overused — most titles should NOT have a colon. Use varied "
    "structures: declarative statements ('Microsoft 365 Copilot Boosts HR Productivity'), 'How X Does Y' "
    "phrasing, em-dashes, or no punctuation at all. At most 2-3 titles in the WHOLE batch may use a colon, "
    "only where it truly cannot be phrased otherwise.\n"
    "- Max 60 characters — write to fit, never truncate\n"
    "- Concrete, specific, measurable outcome — what the reader gains\n"
    "- Professional tone, no clickbait\n"
    "- Every title UNIQUE across the full batch; vary structure deliberately\n"
    "Return ONLY valid JSON: {\"titles\": [\"...\", ...]} in the SAME order as input."
)


def _build_holistic_user_prompt(articles: List[dict], year: int, month_name: str) -> str:
    lines = []
    for i, a in enumerate(articles):
        kw = (a.get("focusKeyword") or a.get("primary") or "").strip()
        pr = (a.get("primary") or "").strip()
        an = (a.get("angle") or "").strip()
        cat = (a.get("wpCategory") or "").strip()
        lines.append(f'{i+1}. keyword="{kw}"; topic="{pr}"; angle="{an}"; category="{cat}"')
    return (
        f"Month: {month_name} {year}. "
        f"Craft {len(articles)} titles for the complete monthly set "
        f"(all unique; vary structure across the batch).\n\n"
        + "\n".join(lines)
    )


def _holistic_titles(articles: List[dict], year: int, month_name: str) -> List[str]:
    """Generate all titles in one call with full mutual visibility."""
    payload = {
        "messages": [
            {"role": "system", "content": HOLISTIC_TITLE_SYSTEM},
            {"role": "user", "content": _build_holistic_user_prompt(articles, year, month_name)},
        ],
        "max_tokens": 100 * len(articles) + 400,
        "temperature": 0.8,
        "response_format": {"type": "json_object"},
    }
    try:
        out = chat_json(payload).get("titles", [])
    except Exception as e:
        logging.warning("[content_plan_en] holistic title pass failed: %s", e)
        return [a.get("title", "") or a.get("primary", "") for a in articles]

    titles = list(out) + [""] * max(0, len(articles) - len(out))
    titles = titles[: len(articles)]
    for i, t in enumerate(titles):
        if not (t or "").strip():
            titles[i] = articles[i].get("title", "") or articles[i].get("primary", "")
    return [str(t).strip() for t in titles]


def _gate_and_fix(
    titles: List[str], articles: List[dict], year: int
) -> List[str]:
    """Deterministic gates (dedup / ≤60 / has digit) + one corrective call."""
    TMAX = 60
    seen: dict = {}
    problems: dict = {}

    for i, t in enumerate(titles):
        t = (t or "").strip()
        issues = []
        if not re.search(r"\d", t):
            issues.append("missing number")
        if len(t) > TMAX:
            issues.append(f"too long ({len(t)}>{TMAX})")
        norm = t.lower()
        if norm in seen:
            issues.append(f"duplicate of #{seen[norm]+1}")
        else:
            seen[norm] = i
        if issues:
            problems[i] = issues

    if not problems:
        return titles

    fix_lines = []
    for i, iss in problems.items():
        a = articles[i]
        kw = (a.get("focusKeyword") or a.get("primary") or "").strip()
        fix_lines.append(
            f'#{i+1} problems=[{"; ".join(iss)}] keyword="{kw}" current="{titles[i]}"'
        )
    ctx = "\n".join(f"#{i+1}: {t}" for i, t in enumerate(titles))
    fix_prompt = (
        f"Fix these {len(problems)} titles. Each must: open with its keyword, contain at least one "
        f"number naturally, be ≤60 chars, be unique, avoid colon unless truly necessary.\n\n"
        f"Titles to fix:\n" + "\n".join(fix_lines) +
        f"\n\nFull set for context:\n{ctx}\n\n"
        'Return ONLY JSON: {"fixes": {"1": "title", "3": "title", ...}} '
        "(keys = 1-based position numbers)"
    )
    try:
        resp = chat_json({
            "messages": [
                {"role": "system", "content": HOLISTIC_TITLE_SYSTEM},
                {"role": "user", "content": fix_prompt},
            ],
            "max_tokens": 80 * len(problems) + 300,
            "temperature": 0.7,
            "response_format": {"type": "json_object"},
        }).get("fixes", {})
        result = list(titles)
        for k, v in resp.items():
            try:
                idx = int(k) - 1
                if 0 <= idx < len(result) and v:
                    result[idx] = str(v).strip()[:TMAX]
            except Exception:
                pass
        return result
    except Exception as e:
        logging.warning("[content_plan_en] gate corrective call failed: %s", e)
        return titles


# =============================================================================
# Main generator
# =============================================================================
def generate_en_content_plan(
    target_month: Optional[str] = None,
    existing_titles: Optional[Dict[str, List[str]]] = None,
    existing_items: Optional[List[dict]] = None,
    categories: Optional[List[str]] = None,
    articles_per_day: int = DEFAULT_ARTICLES_PER_DAY,
) -> Dict:
    """
    Generate a full month of EN content with unique titles, weighted by category.

    Returns: {"month", "totalArticles", "categories", "articles": [...]}.
    """
    # --- Determine target month ---
    if not target_month:
        now = datetime.utcnow()
        nxt = now.replace(day=28) + timedelta(days=4)
        target_month = nxt.strftime("%Y-%m")

    year = int(target_month.split("-")[0])
    month = int(target_month.split("-")[1])
    num_days = calendar.monthrange(year, month)[1]
    total_articles = num_days * max(1, articles_per_day)
    month_name = calendar.month_name[month]  # English month name

    # --- Categories + weights ---
    cats = categories or EN_CATEGORIES
    if categories and set(categories) != set(EN_CATEGORIES):
        weights = {c: 1.0 / len(cats) for c in cats}          # even split for overrides
    else:
        weights = {c: EN_CATEGORY_WEIGHTS[c] for c in cats}   # canonical EN weighting

    existing = existing_titles or _group_existing(existing_items or [])

    cat_counts = _distribute_weighted(weights, total_articles)
    logging.info(
        "[content_plan_en] %s: %d articles across %d categories (weighted)",
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
        phases = [random.choice(PHASES) for _ in range(count)]
        styles = [random.choice(IMAGE_STYLES) for _ in range(count)]

        payload = _build_batch_prompt(
            target_month, month_name, cat, count,
            existing_for_cat + generated_by_cat[cat],
            phases, styles,
        )

        try:
            result = chat_json(payload)
            arts = result.get("articles", [])
        except Exception as e:
            logging.exception("[content_plan_en] GPT failed for %s: %s", cat, e)
            arts = []

        for i, art in enumerate(arts[:count]):
            title = (art.get("title") or "").strip()
            if not title:
                continue

            all_known = existing_for_cat + generated_by_cat[cat]
            if not _is_unique(title, all_known):
                logging.warning("[content_plan_en] Non-unique: %s", title)
                title = f"{title} (v2)"

            if len(title) > TITLE_MAX_LEN:
                title = title[: TITLE_MAX_LEN - 1] + "\u2026"

            generated_by_cat[cat].append(title)

            all_articles.append({
                "title": title,
                "wpCategory": cat,
                "primary": (art.get("primary") or "").strip(),
                "angle": (art.get("angle") or "").strip(),
                "audience": (art.get("audience") or "").strip(),
                "focusKeyword": (art.get("focusKeyword") or "").strip(),
                "phase": phases[i % len(phases)],
                "status": "Ready",
                "fbStatus": "Planned",
                "fbImageStyle": styles[i % len(styles)],
                "language": "en",
            })

        if len(arts) < count:
            logging.warning(
                "[content_plan_en] %s: requested %d, got %d", cat, count, len(arts)
            )

    # --- Holistic title pass + deterministic gates ---
    _raw = _holistic_titles(all_articles, year, month_name)
    _final = _gate_and_fix(_raw, all_articles, year)
    for _a, _t in zip(all_articles, _final):
        if _t:
            _a["title"] = _t

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
