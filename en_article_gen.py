"""
en_article_gen.py — Lean, English-native article generator for the /en/ pipeline.

A deliberately small companion to the Latvian generator in article_gen.py.
Where the LV path uses an outline -> sections -> top-up -> refine chain, the EN
path produces a complete article in a SINGLE mega-prompt call (plus one optional
corrective retry). For a 1500-2500 word article that is enough to keep one
consistent author voice, and it keeps this module small enough to own on its own.

The Latvian code is NOT touched. Shared infrastructure is reused from article_gen:
    chat_json, sanitize_html, slugify, count_words_from_html, count_tag,
    has_blockquote, response_format_for_model, get_dynamic_max_tokens,
    ensure_wp_tag_ids, pick_item, extract_meta

EN-specific here: the prompts, the quality checks, and EN tag handling. EN tags
deliberately bypass normalize_tags() (the LV whitelist/anchor map), which is keyed
on Latvian slugs and would strip EN tags under ANCHOR_STRICT.

Output JSON matches the LV WpArticle shape, so the existing Power Automate
publishing flow works unchanged:
    title, seoSlug, excerpt, contentHtml, category, tags, tagSlugs,
    focusKeyword, wpTagIds
"""

import json
import logging
import os
import re
from typing import List

from article_gen import (
    chat_json,
    get_headers,
    http_post_json,
    sanitize_html,
    slugify,
    count_words_from_html,
    count_tag,
    has_blockquote,
    response_format_for_model,
    get_dynamic_max_tokens,
    ensure_wp_tag_ids,
    pick_item,
    extract_meta,
    serp_search_cached,
)

import config

__all__ = [
    "generate_en_article",
    "quality_issues_en",
    "EN_SYSTEM_PROMPT",
    "EN_USER_PROMPT",
]

# --- EN target length (smaller than LV DEFAULT_TARGET_WORDS = 5000) ----------
EN_DEFAULT_TARGET_WORDS = int(os.getenv("EN_DEFAULT_TARGET_WORDS", "2000"))
EN_MIN_TARGET_WORDS = int(os.getenv("EN_MIN_TARGET_WORDS", "1500"))
EN_MAX_TARGET_WORDS = int(os.getenv("EN_MAX_TARGET_WORDS", "2500"))
EN_TEMPERATURE = float(os.getenv("EN_TEMPERATURE", "0.4"))
EN_SERP_GL = os.getenv("EN_SERP_GL", "us")
EN_SERP_HL = os.getenv("EN_SERP_HL", "en")

# Reuse the LV RankMath density window so both pipelines agree.
KW_DENSITY_MIN_PCT = config.KW_DENSITY_MIN_PCT
KW_DENSITY_MAX_PCT = config.KW_DENSITY_MAX_PCT

EN_TITLE_POWER_WORDS = (
    "proven", "essential", "complete", "definitive", "effective",
    "practical", "comprehensive", "ultimate", "actionable",
)

EN_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_EN", "gpt-5.1-chat")


def _en_chat_url() -> str:
    """Azure chat-completions URL for the EN-specific deployment (same resource/key as LV, different model)."""
    base = (os.getenv("AZURE_OPENAI_ENDPOINT", "") or "").rstrip("/")
    ver = (os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview") or "").strip()
    return f"{base}/openai/deployments/{EN_DEPLOYMENT}/chat/completions?api-version={ver}"


def _en_chat_json(payload: dict) -> dict:
    """Call the EN deployment and return parsed JSON. Keeps EN on gpt-5.1-chat while LV stays on gpt-4o."""
    outer = http_post_json(_en_chat_url(), get_headers(), payload, timeout_sec=240)
    text = (
        outer.get("output_text")
        or (outer.get("choices", [{}])[0].get("message", {}).get("content"))
    )
    if not text:
        raise RuntimeError(f"No JSON returned by EN model: {str(outer)[:400]}")
    if isinstance(text, str):
        t = text.strip()
        if t.startswith("```"):
            t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t).strip()
        return json.loads(t)
    return text


# =============================================================================
# Prompts
# =============================================================================
EN_SYSTEM_PROMPT = (
    "You are Kaspars Jurjans - a Microsoft 365, SharePoint and AI-automation "
    "consultant with 15+ years of experience and 200+ delivered projects. "
    "You write in-depth, technical B2B articles in native English for IT leaders "
    "and business-process owners at mid-market companies (50-300 staff) in "
    "Germany, Denmark and the Nordics. The content leads with AI on Microsoft 365 "
    "(grounded, cited Copilot alternatives) and is backed by practical M365 and "
    "SharePoint automation.\n"
    "Return ONLY valid JSON matching the schema: title, seoSlug, excerpt, "
    "contentHtml, category, tags, tagSlugs, focusKeyword.\n\n"
    "DEPTH (most important):\n"
    "- Write a genuinely thorough article. Each <h3> section is 200-300 words and "
    "must contain a concrete scenario, specific numbers, and at least one real "
    "Microsoft 365 UI path or configuration step.\n"
    "- Cover the topic so completely the reader needs no other source.\n"
    "- Length is a by-product of depth. NEVER pad with filler, restated points, "
    "generic intros or SEO boilerplate. A dense 1600-word article beats a padded "
    "2500-word one.\n\n"
    "ACCURACY (hard rule):\n"
    "- Use only real, existing Microsoft 365 / SharePoint features, menus and UI "
    "labels. NEVER invent a setting, toggle, menu name or API. If unsure of the exact "
    "UI path, describe the action in general terms instead of inventing a label.\n\n"
    "WRITING PRINCIPLES:\n"
    "1. Back every claim with a concrete scenario or a number. Don't write "
    "'improves productivity' - write 'cuts document-search time from 12 minutes "
    "to 45 seconds'.\n"
    "2. Use real Microsoft 365 terms and UI paths (e.g., 'Document Library -> "
    "Settings -> Versioning settings').\n"
    "3. Each section follows: problem -> solution -> steps -> result.\n"
    "4. Give ROI as ranges with context (e.g., '15-30% less time on document "
    "approval - at a company with 50+ staff').\n"
    "5. Avoid hedging words 'can', 'may', 'might' - give a concrete instruction.\n\n"
    "POSITIONING (EU angle):\n"
    "- Audience is EU/EEA mid-market. Where the topic touches AI, Copilot, governance or data, "
    "lead on EU-specific differentiators: EU/EEA data residence, own-your-deployment / self-hosting "
    "control, and GDPR & NIS2 compliance - the angle that sets a grounded Copilot alternative apart "
    "from US-centric positioning.\n"
    "- Apply only where relevant; never force GDPR/NIS2/residence into topics where it does not belong "
    "(e.g. a plain SharePoint how-to).\n\n"
    "STRUCTURE:\n"
    "- Intro (<h2>) + 6-10 logical <h3> sections\n"
    "- 2+ lists (<ul>/<ol>, 4-7 items each)\n"
    "- 1 short <blockquote> summarising the key takeaway/ROI\n"
    "- Each section ends with a sentence that leads into the next\n\n"
    "SEO (RankMath):\n"
    "- title: starts with the focus keyword (capitalize it naturally as a title; you may follow it "
    "with ':' or '-'), max 60 characters, professional tone - never clickbait. A number is welcome but "
    "NOT required, and an existing number like '365' or '2026' or a specific quantity already counts - "
    "do NOT force a listicle count onto the title when it reads fine without one. Use a power word "
    "(Proven, Essential, Practical, Definitive, …) only where it fits naturally. Prefer varied, natural "
    "phrasing over the repetitive 'Number + PowerWord + Topic' pattern. Examples: 'Copilot Alternative: "
    "A Practical EU-Ready Guide' or 'Microsoft 365 Copilot Governance in 2026'.\n"
    "- excerpt: starts with the focus keyword, max 160 characters (meta description)\n"
    "- seoSlug: lowercase ASCII, words separated by '-', contains the focus keyword\n"
    "- contentHtml: the first sentence begins with the focus keyword; use the focus "
    "keyword in at least 2 <h3> headings; keep keyword density 1-1.5% (organic, "
    "never stuffed)\n\n"
    "FORBIDDEN: clickbait, AI throat-clearing ('In this article we will explore'), "
    "empty promises, <a> tags, inline styles.\n\n"
    "FORMATTING: only <h2>,<h3>,<p>,<ul>,<ol>,<li>,<strong>,<em>,<code>,<pre>,"
    "<blockquote>,<br>.\n"
    "TAGS: 3-6 English domain terms (concepts/techniques) - exclude the category "
    "and audience labels. Give each tag an ASCII slug (lowercase, '-' between words)."
)

EN_USER_PROMPT = (
    "TOPIC: {title}\n"
    "Subject: {primary}\n"
    "Angle: {angle}\n"
    "Audience: {audience}\n"
    "{keywordInstruction}\n"
    "Category: {wpCategory}\n"
    "Target length: about {targetWords} words, minimum 1500. Reach it through depth "
    "(scenarios, numbers, real Microsoft 365 steps) - never through filler or "
    "repetition. A short or generic article will be rejected.\n\n"
    "Write ONE complete, in-depth article in native English, following every system "
    "rule. Each of the 6-10 <h3> sections must be substantial (200-300 words) with a "
    "concrete scenario, specific numbers, and at least one real Microsoft 365 UI step. "
    "End with a quantified takeaway (time / money / percentage).\n\n"
    "Return ONLY valid JSON:\n"
    "{{\n"
    '  "title": "starts with the focus keyword, contains a number and a power word, max 60 chars",\n'
    '  "seoSlug": "lowercase-ascii-with-dashes-containing-the-focus-keyword",\n'
    '  "excerpt": "starts with the focus keyword, max 160 chars",\n'
    '  "contentHtml": "<h2>...</h2><p>...</p><h3>...</h3><p>...</p>...",\n'
    '  "category": "{wpCategory}",\n'
    '  "tags": ["3-6 English domain terms"],\n'
    '  "tagSlugs": ["ascii-slug"],\n'
    '  "focusKeyword": "the focus keyword you chose"\n'
    "}}"
)

EN_RETRY_USER = (
    "The previous version had these issues:\n{issues}\n\n"
    "Fix exactly these issues and keep the rest of the content unchanged. "
    "Return the full corrected JSON."
)


# =============================================================================
# Quality validation (EN counterpart to article_gen.quality_issues)
# =============================================================================
def _first_text(html: str, n: int = 200) -> str:
    """Return the first n chars of tag-stripped, lowercased text from html."""
    txt = re.sub(r"<[^>]+>", " ", html or "")
    txt = re.sub(r"\s+", " ", txt).strip().lower()
    return txt[:n]


def quality_issues_en(data: dict, target_words: int) -> List[str]:
    """Validate an EN article dict; return human-readable issue strings (empty = pass).

    Same RankMath-aligned checks as the LV validator, with English issue strings,
    EN ROI signals, and a corrected 'keyword near the start' rule that is checked
    on stripped text rather than the raw HTML (which always begins with a tag).
    """
    issues: List[str] = []
    content = data.get("contentHtml", "")
    w = count_words_from_html(content)
    min_w, max_w = int(target_words * 0.85), int(target_words * 1.15)

    if not (min_w <= w <= max_w):
        issues.append(f"Length {w} words; target {target_words} (±15%).")
    if count_tag(content, "h3") < 6:
        issues.append("Too few <h3> sections (need >=6).")
    if (count_tag(content, "ul") + count_tag(content, "ol")) < 2:
        issues.append("Too few lists (need >=2).")
    if not has_blockquote(content):
        issues.append("Missing a short <blockquote> with the key takeaway/ROI.")
    if not re.search(r"(\d+\s?%|[€$£]|\bhour|\bminute|\bday|\bweek)", content, flags=re.I):
        issues.append("Missing ROI signals (%, currency, hours/minutes).")

    ew = len(re.findall(r"\S+", data.get("excerpt", "")))
    if ew < 14 or ew > 60:
        issues.append("Excerpt is not 1-2 sentences (~14-35 words).")

    fk = data.get("focusKeyword", "")
    if fk:
        title = data.get("title", "")
        if not title.lower().startswith(fk.lower()):
            issues.append(f"Title does not start with focus keyword: '{fk}'")
        if not re.search(r"\d", title):
            issues.append("Title contains no number")
        if len(title) > 60:
            issues.append(f"Title too long ({len(title)} > 60 chars)")

        excerpt = data.get("excerpt", "")
        if not excerpt.lower().startswith(fk.lower()):
            issues.append(f"Excerpt does not start with focus keyword: '{fk}'")
        if len(excerpt) > 160:
            issues.append(f"Excerpt too long ({len(excerpt)} > 160 chars)")

        if slugify(fk) not in data.get("seoSlug", ""):
            issues.append(f"URL slug missing focus keyword: '{slugify(fk)}'")

        if fk.lower() not in _first_text(content, 200):
            issues.append(f"Focus keyword not near the start of the content: '{fk}'")

        kw_count = content.lower().count(fk.lower())
        density = (kw_count / max(1, w)) * 100
        if density < KW_DENSITY_MIN_PCT:
            issues.append(f"Keyword density too low ({density:.2f}% < {KW_DENSITY_MIN_PCT}%)")
        elif density > KW_DENSITY_MAX_PCT:
            issues.append(f"Keyword density too high ({density:.2f}% > {KW_DENSITY_MAX_PCT}%)")

        kw_in_h = len(re.findall(fr"<h[23][^>]*>.*?{re.escape(fk)}.*?</h[23]>", content, re.I))
        if kw_in_h < 2:
            issues.append(f"Focus keyword appears in only {kw_in_h} subheadings (need >=2)")

    return issues


# =============================================================================
# EN tag handling (bypass the LV whitelist/anchor map)
# =============================================================================
def _normalize_en_tags(data: dict, limit: int = 6) -> dict:
    """Keep the model's EN tags; slugify slugs to ASCII, pair with names, dedupe, cap."""
    names = data.get("tags") or []
    slugs = data.get("tagSlugs") or []
    out_names: List[str] = []
    out_slugs: List[str] = []
    seen = set()
    for i, name in enumerate(names):
        name = (name or "").strip()
        if not name:
            continue
        raw_slug = slugs[i] if i < len(slugs) and slugs[i] else name
        slug = slugify(raw_slug)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out_names.append(name)
        out_slugs.append(slug)
        if len(out_names) >= limit:
            break
    data["tags"] = out_names
    data["tagSlugs"] = out_slugs
    return data


# =============================================================================
# Main entry point
# =============================================================================
def _chat_json_resilient(payload: dict, attempts: int = 2) -> dict:
    """chat_json with a retry on an empty/unparseable response.

    chat_json raises RuntimeError on empty content and ValueError on bad JSON
    (the transient empty-response failure mode). Retry once before giving up.
    """
    last_err = None
    for n in range(attempts):
        try:
            return _en_chat_json(payload)
        except (RuntimeError, ValueError) as e:
            last_err = e
            logging.warning("[en] chat_json attempt %d/%d failed: %s", n + 1, attempts, e)
    raise last_err


def _en_keyword_signals(meta: dict) -> dict:
    """Real EN Google search signals (related searches + PAA) to inform keyword choice.
    Reuses serp_search_cached (Redis cache + monthly guard + rate limit). {} if unavailable."""
    primary = (meta.get("primary") or "").strip()
    angle = (meta.get("angle") or "").strip()
    q = (f"{primary} {angle}").strip() or (meta.get("titleHint") or "").strip()
    if not q:
        return {}
    try:
        serp = serp_search_cached(q[:90], gl=EN_SERP_GL, hl=EN_SERP_HL)
    except Exception as e:
        logging.warning("[en] SerpApi keyword signals failed: %s", e)
        return {}
    if not serp:
        return {}
    related = [(rs.get("query") or "").strip() for rs in serp.get("related_searches", [])[:6]]
    paa = [(x.get("question") or "").strip() for x in serp.get("related_questions", [])[:6]]
    return {"related_searches": [r for r in related if r], "paa": [p for p in paa if p]}


def generate_en_article(item: dict) -> dict:
    """Generate one English-native WordPress article from a SharePoint item.

    Flow: single mega-prompt call -> sanitize -> validate -> (optional) one
    corrective retry -> EN tag normalization -> resolve WP tag IDs.

    Returns the WpArticle-shaped dict (plus wpTagIds) for the Power Automate flow.
    """
    meta = extract_meta(pick_item(item))

    # Target length, clamped to the EN window.
    try:
        target_words = int(
            item.get("targetWords") or meta.get("targetWords") or EN_DEFAULT_TARGET_WORDS
        )
    except Exception:
        target_words = EN_DEFAULT_TARGET_WORDS
    target_words = max(EN_MIN_TARGET_WORDS, min(EN_MAX_TARGET_WORDS, target_words))

    # Focus keyword: explicit field wins, else the primary topic.
    focus_keyword = (meta.get("focusKeyword") or meta.get("primary") or "").strip()

    title_hint = (meta.get("titleHint") or meta.get("primary") or "").strip()
    category = (
        meta.get("wpCategory") or os.getenv("EN_DEFAULT_CATEGORY", "Microsoft 365")
    ).strip()
    audience = (
        meta.get("audience")
        or "IT leaders and business-process owners at mid-market companies "
           "(50-300 staff) in Germany, Denmark and the Nordics"
    )

    signals = _en_keyword_signals(meta)
    if signals.get("related_searches") or signals.get("paa"):
        sig_lines = []
        if signals.get("related_searches"):
            sig_lines.append("Related Google searches (how people actually search): " + "; ".join(signals["related_searches"]))
        if signals.get("paa"):
            sig_lines.append("People Also Ask: " + " | ".join(signals["paa"]))
        keyword_instruction = (
            "Choose the single best SEO focus keyword (2-4 words, lowercase) for THIS article. "
            "Strongly prefer a phrasing that matches how people actually search (see REAL SEARCH SIGNALS) "
            "while staying accurate to the topic; the seed is only a hint.\n"
            f"Seed hint: {focus_keyword}\n"
            "REAL SEARCH SIGNALS:\n" + "\n".join(sig_lines) + "\n"
            "Build the title, excerpt, slug, first sentence, >=2 H3 headings and 1-1.5% density "
            "around YOUR chosen keyword, and return it in focusKeyword."
        )
        logging.info("[en] keyword signals: %d related, %d PAA",
                     len(signals.get("related_searches", [])), len(signals.get("paa", [])))
    else:
        keyword_instruction = (
            f"Focus keyword: {focus_keyword}\n"
            "Build the title, excerpt, slug, first sentence, >=2 H3 headings and density around it, "
            "and return it in focusKeyword."
        )

    user_msg = EN_USER_PROMPT.format(
        title=title_hint,
        primary=meta.get("primary", ""),
        angle=meta.get("angle", ""),
        audience=audience,
        keywordInstruction=keyword_instruction,
        wpCategory=category,
        targetWords=target_words,
    )

    messages = [
        {"role": "system", "content": EN_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    base_payload = {
        "messages": messages,
        "max_completion_tokens": get_dynamic_max_tokens(target_words),
        "response_format": {"type": "json_object"},
    }

    logging.info(
        "[en] generating: kw='%s' target=%d category='%s'",
        focus_keyword, target_words, category,
    )

    data = _chat_json_resilient(base_payload)
    data["contentHtml"] = sanitize_html(data.get("contentHtml", ""))

    issues = quality_issues_en(data, target_words)
    if issues:
        logging.info("[en] retry to fix %d issue(s): %s", len(issues), "; ".join(issues)[:300])
        retry_messages = messages + [
            {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
            {"role": "user", "content": EN_RETRY_USER.format(
                issues="\n".join(f"- {i}" for i in issues)
            )},
        ]
        try:
            data2 = _chat_json_resilient({**base_payload, "messages": retry_messages})
            data2["contentHtml"] = sanitize_html(data2.get("contentHtml", ""))
            # Keep the retry only if it does not introduce more problems.
            if len(quality_issues_en(data2, target_words)) <= len(issues):
                data = data2
        except Exception as e:
            logging.warning("[en] retry failed, keeping first version: %s", e)

    # Carry through category + focus keyword, then normalize EN tags.
    data.setdefault("category", category)
    data.setdefault("focusKeyword", focus_keyword)
    data = _normalize_en_tags(data)

    # Resolve WP tag IDs (same helper the LV finalize uses), if WP is configured.
    data["wpTagIds"] = []
    try:
        if data.get("tags") and data.get("tagSlugs") and config.WP_API_BASE:
            data["wpTagIds"] = ensure_wp_tag_ids(
                config.WP_API_BASE,
                config.WP_TOKEN,
                names=data["tags"],
                slugs=data["tagSlugs"],
            )
    except Exception as e:
        logging.warning("[en] ensure_wp_tag_ids failed: %s", e)

    return data
