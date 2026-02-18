import io
import logging
from typing import Optional

import mammoth
import yake
from bs4 import BeautifulSoup


def _get_plain_text(html: str) -> str:
    """
    No HTML izvelk tikai tekstu, saglabājot atstarpes.
    """
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(" ", strip=True)


def extract_focus_keyword_from_html(html: str, language: str = "lv") -> Optional[str]:
    """
    Atrod 1 galveno frāzi (focus keyword) no HTML satura, izmantojot YAKE.

    Atgriež frāzi vai None, ja neko jēdzīgu nevar atrast.
    """
    text = _get_plain_text(html)

    if not text or len(text) < 40:
        # pārāk maz teksta, lai YAKE strādātu jēdzīgi
        return None

    # YAKE konfigurācija – max 3 vārdu frāzes, top 10
    kw_extractor = yake.KeywordExtractor(
        lan=language,
        n=3,
        top=10,
        dedupLim=0.9,
    )
    candidates = kw_extractor.extract_keywords(text)  # [(frāze, score)]

    if not candidates:
        return None

    # Filtrējam ārā pārāk īsus/pārāk garus variantus
    filtered = []
    for phrase, score in candidates:
        token_count = len(phrase.split())
        if 1 <= token_count <= 4:
            filtered.append((phrase, score))

    if not filtered:
        filtered = candidates

    # YAKE: mazāks score = svarīgāks
    filtered.sort(key=lambda x: x[1])
    focus_kw = filtered[0][0].strip()

    if not focus_kw:
        return None

    return focus_kw


def has_inline_images(html: str) -> bool:
    """
    Atgriež True, ja HTML satur <img> tagus.
    """
    soup = BeautifulSoup(html, "html.parser")
    return soup.find("img") is not None


def convert_docx_to_html(docx_bytes: bytes) -> dict:
    """
    Pieņem DOCX faila bytes un atgriež:

      - html: pilnu HTML fragmentu ar wrapper div,
      - focus_keyword: SEO focus keyword (var būt None),
      - excerpt: pirmo pietiekami garo rindkopu (var būt None),
      - needs_image: bool, vai rakstam vajag ģenerētu attēlu
                     (True, ja HTML nav <img>).
    """
    if not docx_bytes:
        raise ValueError("Empty DOCX bytes")

    try:
        # DOCX → HTML ar Mammoth
        with io.BytesIO(docx_bytes) as docx_file:
            result = mammoth.convert_to_html(docx_file)

        html = result.value or ""
        messages = result.messages or []

        # Mammoth ziņojumi (debugam)
        for m in messages:
            logging.info("mammoth: %s", m)

        # Virsrakstu remaps: <h1> → <h2>
        html = html.replace("<h1>", "<h2>").replace("</h1>", "</h2>")

        # Tabulām pievieno klasi
        html = html.replace("<table>", '<table class="ksj-table">')

        # Wrapper, lai vieglāk stilēt WP pusē
        wrapped_html = f'<div class="ksj-docx-content">\n{html}\n</div>'

        # Focus keyword
        focus_kw = extract_focus_keyword_from_html(wrapped_html, language="lv")

        # Excerpt – pirmā pietiekami gara <p> rindkopa
        soup = BeautifulSoup(wrapped_html, "html.parser")
        excerpt: Optional[str] = None
        for p in soup.find_all("p"):
            text = p.get_text(" ", strip=True)
            if len(text) >= 40:  # minimālais garums, vajadzības gadījumā var regulēt
                excerpt = text
                break

        # Vai vajag ģenerētu attēlu (nav inline <img>)?
        needs_image = not has_inline_images(wrapped_html)

        return {
            "html": wrapped_html,
            "focus_keyword": focus_kw,
            "excerpt": excerpt,
            "needs_image": needs_image,
        }

    except Exception:
        logging.exception("Error in convert_docx_to_html")
        raise
