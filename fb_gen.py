"""
Facebook copy generator — pure business logic.

Takes article metadata and returns structured FB post content.
No Azure Functions dependencies — called by function_app route handler.
"""

import os
import re
import logging

from article_gen import (
    pick_item,
    extract_meta,
    slugify,
    get_url,
    get_headers,
    http_post_json,
)

from image_gen import get_model


# =============================================================================
# Unicode heading helpers (FB doesn't support bold, so we use math bold chars)
# =============================================================================
def has_lv_diacritics(s: str) -> bool:
    return bool(re.search(r"[āčēģīķļņšūžĀČĒĢĪĶĻŅŠŪŽ]", s))


def to_bold_unicode(text: str) -> str:
    """Convert ASCII letters/digits to Unicode Mathematical Bold."""
    normal = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    bold = (
        "𝐀𝐁𝐂𝐃𝐄𝐅𝐆𝐇𝐈𝐉𝐊𝐋𝐌𝐍𝐎𝐏𝐐𝐑𝐒𝐓𝐔𝐕𝐖𝐗𝐘𝐙"
        "𝐚𝐛𝐜𝐝𝐞𝐟𝐠𝐡𝐢𝐣𝐤𝐥𝐦𝐧𝐨𝐩𝐪𝐫𝐬𝐭𝐮𝐯𝐰𝐱𝐲𝐳"
        "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗"
    )
    return text.translate(str.maketrans(normal, bold))


def format_heading(text: str) -> str:
    """UPPER-case if Latvian diacritics present, else Unicode bold."""
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        t = raw.strip()
        if not t or t.lower().startswith("lasi vairāk šeit") or t.startswith("#"):
            continue
        head = re.sub(r"^[^\w\d#@]+", "", t).strip()
        lines[i] = head.upper() if has_lv_diacritics(head) else to_bold_unicode(head)
        break
    return "\n".join(lines)


# =============================================================================
# Tag helpers
# =============================================================================
SKIP_TAGS = {"sharepoint", "teams", "power-automate", "power-apps", "ai-copilot", "m365"}
DEFAULT_TAGS = ["#sharepointpremium", "#microsoft365", "#automatizacija", "#latvija"]


def _build_hashtags(tags_csv: str) -> list[str]:
    """Build hashtag list from comma/space-separated tag string."""
    tags = [t.strip() for t in re.split(r"[#,;/|\s]+", tags_csv) if t.strip()]
    slug_tags = [
        "#" + slugify(t).replace("_", "-")
        for t in tags
        if slugify(t).replace("_", "-") not in SKIP_TAGS
    ]
    return slug_tags if slug_tags else DEFAULT_TAGS


# =============================================================================
# LLM prompt
# =============================================================================
SYSTEM_PROMPT = (
    "Tu raksti latviski Facebook B2B ierakstus. Mērķis: skaidrs, īss, cilvēcīgs, ar spēcīgu ieinteresēšanu.\n"
    "IZVADU FORMATĒ AR MARĶIERIEM (vienā blokā):\n"
    "[HOOK] viens īss teikums, jautājums vai atpazīstama situācija (bez emoji sākumā).\n"
    "[PAIN] 2 īsi teikumi: tipiska sāpe/ķēpa ikdienā (piemērs no biroja ikdienas).\n"
    "[SOLUTION] 1-2 teikumi: kā risinājums palīdz (vienkāršiem vārdiem), bez reklāmas toņa.\n"
    "[BENEFIT] 1-2 teikumi: konkrēts ieguvums ar skaitlisku norādi/diapazonu (piem., 10-20%).\n"
    "[CTA] uzraksti tieši šādi: Lasi vairāk šeit: {LINK}  (LINK ir vienīgais URL tajā pašā rindā).\n"
    "[TAGS] 2-4 atbilstoši hashtagi vienā rindā.\n"
    "Noteikumi: 500-800 rakstzīmes kopā; bez clickbait; ignorē L1/L2/L3; raksti latviski.\n"
    "Padoms: [PAIN]/[SOLUTION]/[BENEFIT] veido plūstoši (īsi teikumi), lai lasās kā stāsts."
)

MIN_BODY_CHARS = 350
MAX_OUTPUT_LINES = 9

BOOSTER_TEXT = (
    "Praktiski ieguvumi: mazāk manuālas šķirošanas, ātrāka dokumentu atrašana "
    "un skaidrāka atbildība komandā."
)


# =============================================================================
# Core generator
# =============================================================================
def generate_fb_copy(incoming: dict) -> dict:
    """
    Generate Facebook post copy from article metadata.

    Args:
        incoming: dict with article fields (primary, angle, audience, etc.)
                  plus optional wpLink, bookLink, style, tagsCsv, SeoSlug.

    Returns:
        dict with keys: message, hashtags, wpLink, cta

    Raises:
        RuntimeError on LLM errors or empty output.
    """
    item = pick_item(incoming)
    meta = extract_meta(item)

    title = (meta.get("titleHint") or item.get("Title") or "").strip()
    primary = (meta.get("primary") or "").strip()
    angle = (meta.get("angle") or "").strip()
    audience = (meta.get("audience") or "").strip()

    wp_link = (incoming.get("wpLink") or "").strip()
    book_link = (
        incoming.get("bookLink")
        or os.getenv("BOOK_LINK", "https://book.jurjans.dev")
    ).strip()
    style = (incoming.get("style") or "emoji-bullets").strip()

    # Tags
    tags_csv = (
        meta.get("tagsCsv") or item.get("Tags") or incoming.get("tagsCsv") or ""
    ).strip()
    base_tags = _build_hashtags(tags_csv)

    # CTA link
    seo_slug = (
        meta.get("SeoSlug") or meta.get("seoSlug")
        or item.get("SeoSlug") or item.get("seoSlug") or ""
    ).strip().lstrip("/")
    cta_link = f"https://ksj.lv/{seo_slug}" if seo_slug else (wp_link or book_link)

    # Clean title (remove L1/L2/L3 prefixes)
    title_clean = re.sub(r"\bL\d+\s*:?\s*", "", title, flags=re.I).strip()

    # --- LLM call ---
    user_msg = (
        f"Tēma: {title_clean}\n"
        f"Primārā doma: {primary}\n"
        f"Leņķis: {angle}\n"
        f"Auditorija: {audience}\n"
        f"Stils: {style}\n"
        f"Saite (CTA): {cta_link}\n"
        f"Ieteiktie hashtagi: {' '.join(base_tags[:4])}\n"
        "Ģenerē pēc norādītajiem marķieriem [HOOK]…[TAGS]."
    )

    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 600,
        "temperature": 0.2,
    }

    outer = http_post_json(get_url(), get_headers(), payload, timeout_sec=45)
    text = (
        outer.get("choices", [{}])[0].get("message", {}).get("content", "")
    ).strip()
    if not text:
        raise RuntimeError("LLM returned empty FB copy")

    # --- Parse structured blocks ---
    blocks = {k: "" for k in ("HOOK", "PAIN", "SOLUTION", "BENEFIT", "CTA", "TAGS")}
    for line in text.splitlines():
        m = re.match(
            r"^[^\S\r\n]*\[(HOOK|PAIN|SOLUTION|BENEFIT|CTA|TAGS)\]\s*[:\-]?\s*(.*)$",
            line.strip(),
            flags=re.I,
        )
        if m:
            blocks[m.group(1).upper()] = m.group(2).strip()

    content_len = sum(len(blocks[k]) for k in ("HOOK", "PAIN", "SOLUTION", "BENEFIT"))

    if content_len >= 80:
        ordered = ["HOOK", "PAIN", "SOLUTION", "BENEFIT", "CTA", "TAGS"]
        base_txt = "\n\n".join(blocks[k] for k in ordered if blocks.get(k)).strip()
    else:
        # Fallback: strip markers and use raw text
        base_txt = re.sub(
            r"\[(HOOK|PAIN|SOLUTION|BENEFIT|CTA|TAGS)\]\s*[:\-]?\s*",
            "", text, flags=re.I,
        )
        base_txt = re.sub(r"\n{3,}", "\n\n", base_txt).strip()

    # Extract URL from text, fall back to cta_link
    url_match = re.search(r"https?://\S+", base_txt)
    found_url = (url_match.group(0) if url_match else cta_link).strip()

    # Remove URL lines from body
    body = re.sub(r"^.*https?://\S+.*$\n?", "", base_txt, flags=re.M).strip()

    # Boost if too short
    if len(body.replace("\n", " ").strip()) < MIN_BODY_CHARS:
        body = (body + ("\n\n" if body else "") + BOOSTER_TEXT).strip()

    # Assemble final text
    txt = body.strip()
    if not txt.endswith("\n"):
        txt += "\n\n"
    txt += f"Lasi vairāk šeit: {found_url}"

    if "#" not in txt:
        txt += "\n" + " ".join(base_tags[:5])

    # Format heading
    txt = format_heading(txt)

    # Trim to max lines
    nonempty = [ln for ln in txt.splitlines() if ln.strip()]
    if len(nonempty) > MAX_OUTPUT_LINES:
        txt = "\n\n".join(nonempty[:MAX_OUTPUT_LINES - 1] + [nonempty[-1]])

    # Ensure hashtags present
    if "#" not in txt:
        txt = txt + "\n\n" + " ".join(base_tags[:5])

    return {
        "message": txt,
        "hashtags": base_tags[:6],
        "wpLink": cta_link,
        "cta": cta_link,
    }
