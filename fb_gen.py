import os
import re
import logging
import azure.functions as func

from article_gen import (
    pick_item,
    extract_meta,
    slugify,
    get_url,
    get_headers,
    http_post_json,
)

# Importē platformas objektus no function_app — function_app importēs fb_gen TIKAI PĒC tam,
# kad app/read_incoming/bad/ok/get_model ir definēti.
from function_app import app, read_incoming, bad, ok, get_model

# =============================================================================
# FB helpers (heading formatting) — pārvietots šeit, lai fb_gen būtu self-contained
# =============================================================================
def has_lv_diacritics(s: str) -> bool:
    return bool(re.search(r"[āčēģīķļņšūžĀČĒĢĪĶĻŅŠŪŽ]", s))


def to_bold_ascii(text: str) -> str:
    normal = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    bold = (
        "𝐀𝐁𝐂𝐃𝐄𝐅𝐆𝐇𝐈𝐉𝐊𝐋𝐌𝐍𝐎𝐏𝐐𝐑𝐒𝐓𝐔𝐕𝐖𝐗𝐘𝐙"
        "𝐚𝐛𝐜𝐝𝐞𝐟𝐠𝐡𝐢𝐣𝐤𝐥𝐦𝐧𝐨𝐩𝐪𝐫𝐬𝐭𝐮𝐯𝐰𝐱𝐲𝐳"
        "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗"
    )
    return text.translate(str.maketrans(normal, bold))


def ensure_heading_caps_or_bold(text: str) -> str:
    lines = text.splitlines()
    if not lines:
        return text
    idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if idx is None:
        return text
    heading = re.sub(r"^[^\w\d#@]+", "", lines[idx]).strip()
    lines[idx] = heading.upper() if has_lv_diacritics(heading) else to_bold_ascii(heading)
    return "\n".join(lines)


# =============================================================================
# FB copy generator (deklarēta Azure funkcija — izmanto app no function_app)
# =============================================================================
@app.function_name(name="generate_fb_copy")
@app.route(route="generate-fb-copy", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def generate_fb_copy(req: func.HttpRequest) -> func.HttpResponse:
    incoming = read_incoming(req)
    if not incoming:
        return bad(400, error="Invalid JSON body")

    item = pick_item(incoming)
    meta = extract_meta(item)

    title = (meta.get("titleHint") or item.get("Title") or "").strip()
    primary = (meta.get("primary") or "").strip()
    angle = (meta.get("angle") or "").strip()
    audience = (meta.get("audience") or "").strip()

    wp_link = (incoming.get("wpLink") or "").strip()
    book_link = (incoming.get("bookLink") or os.getenv("BOOK_LINK", "https://book.jurjans.dev")).strip()
    style = (incoming.get("style") or "emoji-bullets").strip()

    def _slugify(t: str) -> str:
        return slugify(t).replace("_", "-")

    tags_csv = (meta.get("tagsCsv") or item.get("Tags") or incoming.get("tagsCsv") or "").strip()
    tags = [t.strip() for t in re.split(r"[#,;/\|\s]+", tags_csv) if t.strip()]
    base_tags = [
        "#" + _slugify(t)
        for t in tags
        if _slugify(t) not in {"sharepoint", "teams", "power-automate", "power-apps", "ai-copilot", "m365"}
    ]
    if not base_tags:
        base_tags = ["#sharepointpremium", "#microsoft365", "#automatizacija", "#latvija"]

    seo_slug = (
        meta.get("SeoSlug") or meta.get("seoSlug") or item.get("SeoSlug") or item.get("seoSlug") or ""
    ).strip().lstrip("/")
    cta_link = f"https://ksj.lv/{seo_slug}" if seo_slug else (wp_link or book_link)

    title_clean = re.sub(r"\bL\d+\s*:?\s*", "", title, flags=re.I).strip()

    SYS = (
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

    USR = (
        f"Tēma: {title_clean}\n"
        f"Primārā doma: {primary}\n"
        f"Leņķis: {angle}\n"
        f"Auditorija: {audience}\n"
        f"Stils: {style}\n"
        f"Saite (CTA): {cta_link}\n"
        f"Ieteiktie hashtagi: {' '.join(base_tags[:4])}\n"
        "Ģenerē pēc norādītajiem marķieriem [HOOK]…[TAGS]."
    )

    url, headers, model = get_url(), get_headers(), get_model()
    payload = {"messages": [{"role": "system", "content": SYS}, {"role": "user", "content": USR}], "max_tokens": 600, "temperature": 0.2}
    try:
        outer = http_post_json(url, headers, payload, timeout_sec=45)
        text = (outer.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
    except Exception as e:
        return bad(502, error="llm_error", message=str(e))
    if not text:
        return bad(502, error="empty_fb_copy")

    blocks = {"HOOK": "", "PAIN": "", "SOLUTION": "", "BENEFIT": "", "CTA": "", "TAGS": ""}
    for line in text.splitlines():
        m = re.match(
            r"^[^\S\r\n]*\[(HOOK|PAIN|SOLUTION|BENEFIT|CTA|TAGS)\]\s*[:\-]?\s*(.*)$", line.strip(), flags=re.I
        )
        if m:
            blocks[m.group(1).upper()] = m.group(2).strip()

    def _combined_len(d):
        return sum(len(d[k]) for k in ("HOOK", "PAIN", "SOLUTION", "BENEFIT"))

    if _combined_len(blocks) >= 80:
        ordered = ["HOOK", "PAIN", "SOLUTION", "BENEFIT", "CTA", "TAGS"]
        base_txt = "\n\n".join([blocks[k] for k in ordered if blocks.get(k)]).strip()
    else:
        base_txt = re.sub(r"\[(HOOK|PAIN|SOLUTION|BENEFIT|CTA|TAGS)\]\s*[:\-]?\s*", "", text, flags=re.I)
        base_txt = re.sub(r"\n{3,}", "\n\n", base_txt).strip()

    m = re.search(r"https?://\S+", base_txt)
    found_url = (m.group(0) if m else cta_link).strip()
    body = re.sub(r"^.*https?://\S+.*$\n?", "", base_txt, flags=re.M).strip()

    if len(body.replace("\n", " ").strip()) < 350:
        booster = (
            "Praktiski ieguvumi: mazāk manuālas šķirošanas, ātrāka dokumentu atrašana "
            "un skaidrāka atbildība komandā."
        )
        body = (body + ("\n\n" if body else "") + booster).strip()

    txt = body.strip()
    if not txt.endswith("\n"):
        txt += "\n\n"
    txt += f"Lasi vairāk šeit: {found_url}"

    if "#" not in txt:
        txt += "\n" + " ".join(base_tags[:5])

    def format_first_real_heading(s: str) -> str:
        lines = s.splitlines()
        for i, raw in enumerate(lines):
            t = raw.strip()
            if not t or t.lower().startswith("lasi vairāk šeit") or t.startswith("#"):
                continue
            head = re.sub(r"^[^\w\d#@]+", "", t).strip()
            lines[i] = head.upper() if has_lv_diacritics(head) else to_bold_ascii(head)
            break
        return "\n".join(lines)

    txt = format_first_real_heading(txt)

    nonempty = [ln for ln in txt.splitlines() if ln.strip()]
    if len(nonempty) > 9:
        txt = "\n\n".join(nonempty[:8] + [nonempty[-1]])

   # return ok(message=txt, hashtags=base_tags[:6], wpLink=cta_link, cta=cta_link)

       # Nodrošinām, ka message satur hashtags (ja LLM tos neielika)
    if "#" not in txt:
        txt = txt + "\n\n" + " ".join(base_tags[:5])

    # Atgriežam jaunā formāta laukus (message, hashtags, wpLink, cta)
    return ok(message=txt, hashtags=base_tags[:6], wpLink=cta_link, cta=cta_link)
    # Un papildus atstājam veco formātu (title, copy, link, tags) priekš backward compat.
    # return ok(
    #     # jaunais formāts (primārais)
    #     message=txt,
    #     hashtags=base_tags[:6],
    #     wpLink=cta_link,
    #     cta=cta_link,
    #     # vecais formāts — lai esošie klienti nesairst
    #     title=title_clean,
    #     copy=txt,
    #     link=cta_link,
    #     tags=base_tags[:6],
    # )

