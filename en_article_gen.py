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

import requests

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
    DEFAULT_TTL,
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
    "- title: a natural, grammatical English headline that starts with the focus keyword in TITLE CASE "
    "(capitalise each significant word, and keep common acronyms uppercase — HR, EU, AI, GDPR, NIS2, ISO, "
    "ROI, KPI, CEO, CTO, CIO, SaaS, API, M365, …; e.g. 'hr automation copilot' becomes 'HR Automation "
    "Copilot'; you may follow it with ':' or '-'), max 60 characters, professional tone - never "
    "clickbait. Include ONE number where it reads naturally: a real count ('5 Steps to…') or a year "
    "(2026), placed so the title stays grammatical. 'Microsoft 365' and 'Copilot' are product names - "
    "keep them in full and never use the '365' as the title's count. If the title already has a real "
    "number, do not add another. Use a power word only where it fits. GOOD: 'Copilot Alternative: A "
    "2026 EU-Ready Guide', 'Microsoft 365 Copilot Governance in 2026'. BAD: '365 Proven Ways to…' "
    "(365 misused as a count), '6 Definitive Guide to…' (count + singular noun).\n"
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


# Common acronyms that must stay uppercase in titles/excerpts
_TITLE_ACRONYMS = {
    "hr", "eu", "ai", "ml", "us", "uk", "it", "qa", "ux", "ui", "cx",
    "kpi", "roi", "ceo", "cto", "cfo", "cio", "vp", "saas", "api", "sdk",
    "css", "html", "url", "sql", "gdpr", "nis2", "iso", "soc", "b2b", "b2c",
    "crm", "erp", "rest", "json", "xml", "yaml", "csv", "pdf", "spfx",
    "m365", "ms",
}


def _smart_titlecase_keyword(kw: str) -> str:
    """Title-case a focus keyword while preserving common acronyms.

    'hr automation copilot' -> 'HR Automation Copilot'
    'microsoft 365 governance' -> 'Microsoft 365 Governance'
    'gdpr compliance copilot' -> 'GDPR Compliance Copilot'
    """
    if not kw:
        return kw
    out = []
    for w in kw.split():
        if w.lower() in _TITLE_ACRONYMS:
            out.append(w.upper())
        elif w.isdigit() or any(c.isdigit() for c in w):
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)


def _fix_title_opening(text: str, focus_keyword: str) -> str:
    """If text opens with the focus keyword (any case), rewrite that opening in Title Case."""
    if not text or not focus_keyword:
        return text
    fk_lower = focus_keyword.lower()
    if text.lower().startswith(fk_lower):
        return _smart_titlecase_keyword(focus_keyword) + text[len(focus_keyword):]
    return text


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


# ============================================================================
# "Further reading" block — EN parallel to LV "Papildu lasāmviela" in article_gen.py.
# Same architecture:
#   - Internal: KSJ posts via WP REST search, filtered by '/en/' in URL (Polylang).
#   - External: Microsoft Learn docs via SerpApi, HEAD-verified.
#   - Descriptions: GPT-generated in English (uses LV chat_json deployment for cost).
#   - HTML: same two-column flex layout + CTA button, EN labels.
# ============================================================================

def _fetch_ksj_en_related_posts(focus_keyword: str, title: str, limit: int = 4) -> list[dict]:
    """Query ksj.lv WP REST API for related EN posts (Polylang routes EN under '/en/')."""
    api_base = (os.environ.get("WP_API_BASE") or "").rstrip("/")
    if not api_base:
        return []

    results: list[dict] = []
    seen_ids: set = set()
    search_terms = [focus_keyword]
    parts = focus_keyword.split()
    if len(parts) > 1:
        search_terms.append(parts[0])

    for term in search_terms:
        if len(results) >= limit:
            break
        try:
            resp = requests.get(
                f"{api_base}/wp/v2/posts",
                params={
                    "search": term,
                    "per_page": limit + 4,
                    "status": "publish",
                    "_fields": "id,title,link,slug",
                    "orderby": "relevance",
                },
                timeout=8,
            )
            if resp.status_code == 200:
                posts = resp.json()
                for p in posts:
                    pid = p.get("id")
                    link = (p.get("link") or "").strip()
                    ptitle = (p.get("title", {}).get("rendered") or "").strip()
                    if not link or not ptitle or pid in seen_ids:
                        continue
                    # Polylang EN-only filter — EN URLs contain '/en/'
                    if "/en/" not in link:
                        continue
                    if ptitle.lower() == title.lower():
                        continue
                    seen_ids.add(pid)
                    results.append({"url": link, "title": ptitle})
                    if len(results) >= limit:
                        break
        except Exception as e:
            logging.debug(f"[further_reading_en] WP search '{term}' failed: {e}")

    logging.info(f"[further_reading_en] Found {len(results)} KSJ EN related posts")
    return results[:limit]


def _fetch_ms_docs_links_en(focus_keyword: str, limit: int = 4) -> list[dict]:
    """SerpApi search for learn.microsoft.com EN docs, HEAD-verified."""
    if not focus_keyword:
        return []

    results: list[dict] = []
    seen_urls: set = set()

    try:
        # site: restriction dominates over geo params in serp_search_cached
        ms_query = f"{focus_keyword} site:learn.microsoft.com/en-us"
        serp = serp_search_cached(ms_query, ttl=DEFAULT_TTL * 4)
        if serp:
            for r in serp.get("organic_results", [])[:limit * 2]:
                link = (r.get("link") or "").strip()
                title = (r.get("title") or "").strip()
                if not link or link in seen_urls:
                    continue
                if "learn.microsoft.com/en-us" not in link:
                    continue
                seen_urls.add(link)
                results.append({"url": link, "title": title})
                if len(results) >= limit + 2:
                    break
    except Exception as e:
        logging.warning(f"[further_reading_en] MS docs SerpApi failed: {e}")

    # HEAD verify — drop dead links
    verified: list[dict] = []
    for item in results:
        try:
            resp = requests.head(item["url"], timeout=5, allow_redirects=True)
            if resp.status_code < 400:
                verified.append(item)
                if len(verified) >= limit:
                    break
        except Exception:
            continue

    logging.info(
        f"[further_reading_en] MS docs: {len(results)} found, {len(verified)} verified"
    )
    return verified[:limit]


def _generate_en_link_descriptions(
    ksj_links: list[dict],
    ms_links: list[dict],
    focus_keyword: str,
    article_title: str,
) -> dict:
    """GPT-generated English descriptions and clean titles for the link block."""
    if not ksj_links and not ms_links:
        return {"ksj": [], "ms": []}

    ksj_items_text = "\n".join(
        f'- {i+1}. "{item["title"]}" ({item["url"]})'
        for i, item in enumerate(ksj_links)
    )
    ms_items_text = "\n".join(
        f'- {i+1}. "{item["title"]}" ({item["url"]})'
        for i, item in enumerate(ms_links)
    )

    system = (
        "You generate concise English descriptions and titles for blog reference links. "
        "Reply ONLY with valid JSON."
    )
    user = (
        f'Article title: "{article_title}"\n'
        f'Focus keyword: "{focus_keyword}"\n\n'
        f"KSJ ARTICLES (write 1-2 sentence English description of how each relates to the main topic):\n"
        f"{ksj_items_text}\n\n"
        f"MICROSOFT RESOURCES (clean up each title to 5-10 English words + 1-sentence description):\n"
        f"{ms_items_text}\n\n"
        f"JSON format:\n"
        f'{{\n'
        f'  "ksj": [{{"index": 1, "desc": "Short English description..."}}],\n'
        f'  "ms": [{{"index": 1, "title_en": "Clean English title", "desc": "Short description..."}}]\n'
        f'}}'
    )

    try:
        payload = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 800,
            "temperature": 0.4,
        }
        outer = chat_json(payload)

        ksj_result = []
        ksj_descs = {d["index"]: d.get("desc", "") for d in (outer.get("ksj") or [])}
        for i, item in enumerate(ksj_links):
            desc = ksj_descs.get(i + 1, "")
            ksj_result.append({"url": item["url"], "title": item["title"], "desc": desc})

        ms_result = []
        ms_data = {d["index"]: d for d in (outer.get("ms") or [])}
        for i, item in enumerate(ms_links):
            d = ms_data.get(i + 1, {})
            title_en = d.get("title_en", "") or item["title"]
            desc = d.get("desc", "")
            ms_result.append({"url": item["url"], "title_en": title_en, "desc": desc})

        return {"ksj": ksj_result, "ms": ms_result}
    except Exception as e:
        logging.warning(f"[further_reading_en] GPT descriptions failed: {e}")
        return {
            "ksj": [{"url": l["url"], "title": l["title"], "desc": ""} for l in ksj_links],
            "ms": [{"url": l["url"], "title_en": l["title"], "desc": ""} for l in ms_links],
        }


def _build_en_reading_html(
    ksj_links: list[dict],
    ms_links: list[dict],
    focus_keyword: str,
) -> str:
    """Build the EN 'Further reading' HTML block — two-column flex + CTA button."""
    if not ksj_links and not ms_links:
        return ""

    ksj_items_html = ""
    for item in ksj_links:
        desc_html = ""
        if item.get("desc"):
            desc_html = f'<br>\n          <small style="color:#666;">{item["desc"]}</small>'
        ksj_items_html += (
            f'        <li style="margin-bottom:8px;">\n'
            f'          <a href="{item["url"]}">{item["title"]}</a>{desc_html}\n'
            f'        </li>\n'
        )

    ms_items_html = ""
    for item in ms_links:
        title = item.get("title_en") or item.get("title") or ""
        desc_html = ""
        if item.get("desc"):
            desc_html = f'<br>\n          <small style="color:#666;">{item["desc"]}</small>'
        ms_items_html += (
            f'        <li style="margin-bottom:8px;">\n'
            f'          <a href="{item["url"]}" target="_blank" rel="noopener noreferrer">{title}</a>{desc_html}\n'
            f'        </li>\n'
        )

    cta_topic = focus_keyword
    if len(cta_topic) > 40:
        cta_topic = " ".join(cta_topic.split()[:4])
    cta_text = f"Contact KSJ about {cta_topic}"

    ksj_col = ""
    if ksj_items_html:
        ksj_col = (
            f'    <div style="flex:1 1 320px;min-width:240px;">\n'
            f'      <strong>Related KSJ articles</strong>\n'
            f'      <ul style="margin:8px 0 14px 18px;padding:0;color:#222;">\n'
            f'{ksj_items_html}'
            f'      </ul>\n'
            f'    </div>\n'
        )

    ms_col = ""
    if ms_items_html:
        ms_col = (
            f'    <div style="flex:1 1 320px;min-width:240px;">\n'
            f'      <strong>Official resources</strong>\n'
            f'      <ul style="margin:8px 0 14px 18px;padding:0;color:#222;">\n'
            f'{ms_items_html}'
            f'      </ul>\n'
            f'    </div>\n'
        )

    html = (
        f'<h2>Further reading</h2>\n'
        f'<div style="border:1px solid #e6e6e6;padding:22px;border-radius:8px;'
        f'background:#fbfbfb;font-family:Arial,Helvetica,sans-serif;color:#222;margin-top:28px;">\n'
        f'  <div style="display:flex;flex-wrap:wrap;gap:24px;">\n'
        f'{ksj_col}{ms_col}'
        f'  </div>\n'
        f'  <p style="text-align:center;margin:18px 0 0;">\n'
        f'    <a href="https://ksj.lv/en/contact/" '
        f'style="display:inline-block;background:#2b8a3e;color:#ffffff;'
        f'padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:600;'
        f'font-size:15px;line-height:1.4;">{cta_text}</a>\n'
        f'  </p>\n'
        f'</div>'
    )
    return html


def build_further_reading_en(meta: dict, focus_keyword: str, title: str) -> str:
    """Orchestrate the EN 'Further reading' block — parallel to LV build_papildu_lasamviela.
    Returns empty string when no links found (graceful skip)."""
    ksj_links = _fetch_ksj_en_related_posts(focus_keyword, title, limit=4)
    ms_links = _fetch_ms_docs_links_en(focus_keyword, limit=4)

    if not ksj_links and not ms_links:
        logging.info("[further_reading_en] No links found, skipping block")
        return ""

    enriched = _generate_en_link_descriptions(ksj_links, ms_links, focus_keyword, title)

    return _build_en_reading_html(
        ksj_links=enriched.get("ksj") or ksj_links,
        ms_links=enriched.get("ms") or ms_links,
        focus_keyword=focus_keyword,
    )


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

    # Title Case the opening focus keyword in title and excerpt;
    # focusKeyword field stays lowercase for SEO/RankMath tracking.
    if data.get("title"):
        data["title"] = _fix_title_opening(data["title"], focus_keyword)
    if data.get("excerpt"):
        data["excerpt"] = _fix_title_opening(data["excerpt"], focus_keyword)

    data = _normalize_en_tags(data)

    # Append "Further reading" block (EN parallel to LV "Papildu lasāmviela")
    try:
        reading_block = build_further_reading_en(
            meta=meta,
            focus_keyword=focus_keyword,
            title=data.get("title", ""),
        )
        if reading_block:
            data["contentHtml"] = data["contentHtml"].rstrip() + "\n\n" + reading_block
            logging.info("[en] Appended 'Further reading' block")
    except Exception as e:
        logging.warning(f"[en] Further reading block failed (skipping): {e}")

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
