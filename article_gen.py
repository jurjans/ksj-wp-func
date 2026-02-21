import os
import re
import json
import logging
from typing import List, Tuple

# --- SerpApi / kešēšanas konfigurācija (PASTĪT PĒC IMPORTIEM) -------------
from datetime import datetime
import time

DEFAULT_TTL = 60 * 60 * 48   # 48 hours
TOP_N_DEFAULT = 6            # top-N candidates to enrich by default
MONTHLY_GUARD = 200          # safety cap for SerpApi calls per month

# try to init redis client if settings present
_redis = None
try:
    import redis as _redis_lib
    REDIS_HOST = os.getenv("REDIS_HOST")
    REDIS_PORT = int(os.getenv("REDIS_PORT") or 6380)
    REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
    REDIS_DB = int(os.getenv("REDIS_DB") or 0)
    if REDIS_HOST and REDIS_PASSWORD is not None:
        _redis = _redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, db=REDIS_DB, ssl=True)
except Exception:
    _redis = None

def redis_get(key: str):
    if not _redis:
        return None
    v = _redis.get(key)
    try:
        return json.loads(v) if v else None
    except Exception:
        return None

def redis_set(key: str, value, ttl: int = DEFAULT_TTL):
    if not _redis:
        return
    try:
        _redis.set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
    except Exception:
        return

def redis_get_int(key: str):
    if not _redis:
        return 0
    try:
        v = _redis.get(key)
        return int(v) if v else 0
    except Exception:
        return 0

def redis_incr_month(count: int = 1):
    if not _redis:
        return 0
    key = "serpapi:month:" + datetime.utcnow().strftime("%Y%m")
    val = _redis.incrby(key, count)
    # keep a TTL so it expires next month
    try:
        _redis.expire(key, 60*60*24*45)
    except Exception:
        pass
    return int(val)

def incr_counter(key: str, window_sec: int = 60):
    if not _redis:
        return 0
    try:
        with _redis.pipeline() as pipe:
            pipe.incr(key, 1)
            pipe.expire(key, window_sec)
            res = pipe.execute()
        return int(res[0])
    except Exception:
        return 0

def can_call_serpapi(max_per_minute: int = 30) -> bool:
    """Simple global throttle (returns False if over limit)."""
    if not _redis:
        return True
    key = "serpapi:rate:minute"
    cnt = incr_counter(key, window_sec=60)
    return cnt <= max_per_minute

# Safe SerpApi caller with monthly guard and cache behavior
import requests

SERPAPI_KEY = os.getenv("SERPAPI_KEY")

def serp_search_cached(q: str, ttl: int = DEFAULT_TTL, max_per_minute: int = 30) -> dict:
    """Return SerpApi JSON for query q. Uses Redis cache, rate-limits calls; increments monthly counter on real call."""
    if not SERPAPI_KEY:
        return {}
    cache_key = "serp:" + q.replace(" ", "_")[:200]
    res = redis_get(cache_key)
    if res is not None:
        return res

    # monthly guard
    current_month = redis_get_int("serpapi:month:" + datetime.utcnow().strftime("%Y%m"))
    if current_month + 1 > MONTHLY_GUARD:
        return {}

    if not can_call_serpapi(max_per_minute=max_per_minute):
        return {}

    url = "https://serpapi.com/search.json"
    params = {"q": q, "engine": "google", "api_key": SERPAPI_KEY, "num": 10, "gl": "lv", "hl": "lv"}
    backoff = [1, 2, 4]
    for wait in backoff:
        try:
            r = requests.get(url, params=params, timeout=8)
            if r.status_code == 200:
                j = r.json()
                redis_set(cache_key, j, ttl)
                redis_incr_month(1)
                return j
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(wait)
                continue
            break
        except requests.RequestException:
            time.sleep(wait)
            continue
    return {}
# -------------------------------------------------------------------------

# =============================================================================
# Palīgfunkcijas, kas vajadzīgas tikai rakstu ģenerēšanai
# (šīs drīkst iznest ārā no function_app.py)
# =============================================================================

ALLOWED_TAGS = {
    "h2",
    "h3",
    "p",
    "ul",
    "ol",
    "li",
    "strong",
    "em",
    "code",
    "pre",
    "blockquote",
    "br",
}
TAG_RE = re.compile(r"</?([a-zA-Z0-9]+)(\s+[^>]*)?>", re.IGNORECASE)


def sanitize_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)

    def _repl(m: re.Match):
        tag = m.group(1).lower()
        return f"<{'' if m.group(0)[1] != '/' else '/'}{tag}>" if tag in ALLOWED_TAGS else ""

    return TAG_RE.sub(_repl, html)


import unicodedata


def slugify(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9\-\s]", "", s)
    s = re.sub(r"\s+", "-", s.strip().lower())
    return re.sub(r"-{2,}", "-", s)


# ==== LLM HTTP helperi =======================================================

import ssl
import urllib.request


LLM_HTTP_TIMEOUT = int(os.getenv("LLM_HTTP_TIMEOUT", "1500"))


def is_azure_openai() -> bool:
    return bool(os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_API_KEY"))


def get_url() -> str:
    if is_azure_openai():
        base = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
        ver = os.getenv("AZURE_OPENAI_API_VERSION", "2024-11-20")
        dep = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
        return f"{base}/openai/deployments/{dep}/chat/completions?api-version={ver}"
    else:
        base = os.getenv("OAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        return f"{base}/chat/completions"


def get_headers() -> dict:
    if is_azure_openai():
        return {"Content-Type": "application/json", "api-key": os.getenv("AZURE_OPENAI_API_KEY", "")}
    return {"Content-Type": "application/json", "Authorization": f"Bearer {os.getenv('OAI_API_KEY','')}"}


def http_post_json(url: str, headers: dict, body: dict, timeout_sec: int = LLM_HTTP_TIMEOUT) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def force_json_from_text(text: str):
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t, flags=re.S)
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j != -1 and j > i:
        candidate = t[i : j + 1]
        return json.loads(candidate)
    return json.loads(t)


def chat_json(payload: dict) -> dict:
    url, headers = get_url(), get_headers()
    outer = http_post_json(url, headers, payload, timeout_sec=LLM_HTTP_TIMEOUT)
    text = (
        outer.get("output_text")
        or outer.get("output")
        or (outer.get("choices", [{}])[0].get("message", {}).get("content"))
    )
    if not text and isinstance(outer.get("output", []), list):
        try:
            text = outer["output"][0]["content"][0].get("text")
        except Exception:
            pass
    if not text:
        raise RuntimeError(f"No JSON returned by model: {str(outer)[:400]}")
    return force_json_from_text(text) if isinstance(text, str) else text


# =============================================================================
# Konfigurācija raksta ģenerēšanai
# =============================================================================

USE_JSON_SCHEMA = (os.getenv("USE_JSON_SCHEMA", "1") or "1").strip().lower() not in {"0", "false"}
MAX_TOKENS_MAIN = int(os.getenv("WP_MAX_TOKENS", "25000"))
FORCE_SHORT_MODE = (os.getenv("FORCE_SHORT_MODE", "false").lower() in {"1", "true", "yes"})

ARTICLE_SYSTEM_PROMPT = (
    "Tu esi Kaspars Jurjāns — Latvijas vadošais SharePoint un Microsoft 365 konsultants "
    "ar 15+ gadu pieredzi un 200+ veiksmīgi īstenotiem projektiem. "
    "Tu raksti padziļinātus tehniskus rakstus B2B auditorijai "
    "(IT vadītāji, biznesa procesu īpašnieki) latviešu valodā.\n"
    "Atgriez TIKAI derīgu JSON pēc shēmas: title, seoSlug, excerpt, contentHtml, "
    "category, tags, tagSlugs, focusKeyword.\n\n"
    "RAKSTĪŠANAS PRINCIPI:\n"
    "1. Katru apgalvojumu pamato ar konkrētu scenāriju vai skaitli. "
    "Neraksti 'uzlabo produktivitāti' — raksti 'samazina dokumentu meklēšanas laiku "
    "no 12 minūtēm uz 45 sekundēm'.\n"
    "2. Izmanto reālus Microsoft 365 terminus un UI elementus "
    "(piem., 'Document Library → Settings → Versioning settings').\n"
    "3. Katrai sadaļai ir struktūra: problēma → risinājums → soļi → rezultāts.\n"
    "4. ROI datus sniedz kā diapazonus ar kontekstu "
    "(piem., '15-30% mazāk laika pavada dokumentu saskaņošanai — "
    "uzņēmumā ar 50+ darbiniekiem').\n"
    "5. Neizmanto vārdus: 'var', 'iespējams', 'varētu' — aizstāj ar konkrētu instrukciju.\n\n"
    "STRUKTURĒTĪBA:\n"
    "• Ievads (<h2>) + 7-12 loģiskas sadaļas ar <h3>\n"
    "• 2+ saraksti (<ul>/<ol>, katrā 4-7 punkti)\n"
    "• 1 īss <blockquote> ar galveno domu/ROI kopsavilkumu\n"
    "• Katra sadaļa beidzas ar pārejas teikumu uz nākamo\n\n"
    "AIZLIEGTS: clickbait, AI-stilistika ('šajā rakstā aplūkosim'), "
    "tukši solījumi, angliski heading nosaukumi, <a> birkas, inline stili.\n\n"
    "FORMATĒŠANA: Atļauts tikai <h2>,<h3>,<p>,<ul>,<ol>,<li>,<strong>,<em>,"
    "<code>,<pre>,<blockquote>,<br>.\n"
    "TAGI: 3-6 domēna termini latviski (jēdzieni/tehnikas), ne iekļauj kategoriju, "
    "auditoriju vai L1/L2/L3 marķierus. Katram tagam dod ASCII slug (mazie burti, '-' starp vārdiem)."
)


SECTION_SYSTEM_PROMPT = (
    "Tu esi Kaspars Jurjāns — SharePoint un Microsoft 365 konsultants ar 15+ gadu pieredzi. "
    "Raksti vienu raksta sadaļu latviski B2B auditorijai (IT vadītāji, biznesa procesu īpašnieki).\n"
    "OBLIGĀTĀ STRUKTŪRA šai sadaļai:\n"
    "• 1 ievada rindkopa: kāpēc šī tēma ir svarīga (ar konkrētu problēmu)\n"
    "• 2-3 rindkopas ar praktiskiem soļiem (izmanto precīzus Microsoft 365 UI terminus, "
    "piem., 'Document Library → Settings → Versioning settings')\n"
    "• 1 saraksts (ul/ol) ar 4-7 punktiem\n"
    "• 1 ROI rindkopa ar skaitļiem kā diapazoni (piem., '15-30% mazāk laika — uzņēmumā ar 50+ darbiniekiem')\n"
    "• 1 noslēguma teikums, kas savieno ar nākamo sadaļu\n"
    "KVALITĀTE: Katru apgalvojumu pamato ar scenāriju vai skaitli. "
    "Neraksti 'uzlabo produktivitāti' — raksti 'samazina dokumentu meklēšanas laiku no 12 min uz 45 sek'. "
    "Neizmanto vārdus: 'var', 'iespējams', 'varētu' — aizstāj ar konkrētu instrukciju.\n"
    "AIZLIEGTS: clickbait, AI-stilistika ('šajā sadaļā aplūkosim'), tukši solījumi, <a> birkas, inline stili.\n"
    "KONTEKSTS:\n"
    "- Kategorija: {wpCategory}\n"
    "- Focus keyword: {focusKeyword}\n"
    "- Iepriekšējās sadaļas jau satur: {previousSections}\n"
    "NEATKĀRTO tēmas, piemērus vai datus, kas jau ir iepriekšējās sadaļās.\n"
    "Atgriez TIKAI JSON: {{\"sectionHtml\": \"<p>...</p>\"}}"
)


def _min_length_from_target_words(target_words: int) -> int:
    """Heiristika: ~4.8 rakstzīmes uz vārdu; min 6000."""
    try:
        return max(6000, int(target_words * 4.8))
    except Exception:
        return 6000


def response_format_for_model(target_words: int) -> dict:
    if USE_JSON_SCHEMA:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "WpArticle",
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string", "minLength": 40, "maxLength": 90},
                        "seoSlug": {
                            "type": "string",
                            "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
                            "description": "latviski bez diakritikām, '-' starp vārdiem",
                        },
                        "excerpt": {"type": "string", "minLength": 80, "maxLength": 420},
                        "contentHtml": {"type": "string", "minLength": _min_length_from_target_words(target_words)},
                        "category": {"type": "string"},
                        "tags": {
                            "type": "array",
                            "minItems": 3,
                            "maxItems": 6,
                            "items": {"type": "string", "minLength": 3},
                        },
                        "tagSlugs": {
                            "type": "array",
                            "minItems": 3,
                            "maxItems": 6,
                            "items": {"type": "string", "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
                        },
                        "focusKeyword": {"type": "string", "minLength": 3, "maxLength": 60},
                    },
                    "required": ["title", "seoSlug", "excerpt", "contentHtml", "category", "tags", "tagSlugs", "focusKeyword"],
                },
            },
        }
    return {"type": "json_object"}


# =============================================================================
# Meta & quality helpers
# =============================================================================

def pick_item(payload: dict) -> dict:
    if isinstance(payload, dict) and isinstance(payload.get("value"), list) and payload["value"]:
        return payload["value"][0]
    return payload


def extract_meta(item: dict) -> dict:
    return {
        "titleHint": item.get("Title") or item.get("{Name}"),
        "primary": item.get("Prim_x0101_r_x0101__x0020_t_x011") or item.get("primary"),
        "angle": item.get("Apak_x0161_t_x0113_ma_x002f_Le_x") or item.get("angle"),
        "audience": item.get("Auditorija_x002f_Amats") or item.get("audience"),
        "wpCategory": item.get("WpCategory") or item.get("wpCategory") or "SharePoint",
        "tagsCsv": item.get("Tags") or item.get("tagsCsv") or item.get("hashtags"),
        "seoSlugHint": item.get("SeoSlug") or item.get("seoSlugHint"),
        "videoPrompt": item.get("Video_Prompt") or item.get("videoPrompt"),
        "hashtags": item.get("hashtags"),
        "phase": item.get("F_x0101_ze", {}).get("Value") if isinstance(item.get("F_x0101_ze"), dict) else item.get("phase"),
        "focusKeyword": item.get("FocusKeyword") or item.get("focusKeyword") or "",
    }


def count_words_from_html(html: str) -> int:
    txt = re.sub(r"<[^>]+>", " ", html or "")
    return len(re.findall(r"\S+", txt))


def count_tag(html: str, tag: str) -> int:
    return len(re.findall(fr"<{tag}\b", html or "", flags=re.I))


def has_blockquote(html: str) -> bool:
    return bool(re.search(r"<blockquote>\s*[^<]{12,400}\s*</blockquote>", html or "", flags=re.I | re.S))


def normalize_lv_headings(html: str) -> str:
    if not html:
        return html
    SP = r"(?:\s|&nbsp;|\u00A0|[--—])"
    patterns = [
        (rf"\bcall{SP}*to{SP}*action(?:s)?\b\s*:?", "Aicinājums rīkoties"),
        (rf"\bcall{SP}*[-]{0,1}{SP}*to{SP}*[-]{0,1}{SP}*action(?:s)?\b\s*:?", "Aicinājums rīkoties"),
        (r"\bcta\b\s*:?", "Aicinājums rīkoties"),
        (r"\bconclusions?\b\s*:?", "Secinājumi"),
        (r"\bsummary\b\s*:?", "Kopsavilkums"),
        (r"\bkey\s*takeaway(s)?\b\s*:?", "Galvenie secinājumi"),
        (r"\bnext\s*steps?\b\s*:?", "Nākamie soļi"),
        (r"\bbest\s*practices?\b\s*:?", "Labākā prakse"),
        (r"\boverview\b\s*:?", "Pārskats"),
        (r"\bintroduction\b\s*:?", "Ievads"),
        (r"\bfaq(s)?\b\s*:?", "Biežāk uzdotie jautājumi"),
    ]

    def apply_map(s: str) -> str:
        out = s
        for pat, repl in patterns:
            out = re.sub(pat, repl, out, flags=re.I)
        return out

    def _repl_h(m: re.Match) -> str:
        tag = m.group(1)
        inner = m.group(2)
        return f"<{tag}>{apply_map(inner)}</{tag}>"

    html = re.sub(r"<(h2|h3)>(.*?)</\1>", _repl_h, html, flags=re.I | re.S)

    def _repl_ps(m: re.Match) -> str:
        tag = m.group(1)
        inner = m.group(2)
        return f"<p><{tag}>{apply_map(inner)}</{tag}></p>"

    html = re.sub(r"<p>\s*<(strong|em)>(.*?)</\1>\s*</p>", _repl_ps, html, flags=re.I | re.S)
    html = apply_map(html)
    return html


def quality_issues(data: dict, target_words: int) -> List[str]:
    issues: List[str] = []
    content = data.get("contentHtml", "")
    w = count_words_from_html(content)
    min_w, max_w = int(target_words * 0.85), int(target_words * 1.15)

    if not (min_w <= w <= max_w):
        issues.append(f"Garums {w} vārdi; mērķis {target_words} (±15%).")

    if count_tag(content, "h3") < 6:
        issues.append("Nepietiek <h3> sadaļu (vajag ≥6).")

    if (count_tag(content, "ul") + count_tag(content, "ol")) < 2:
        issues.append("Nepietiek sarakstu (vajag ≥2).")

    if not has_blockquote(content):
        issues.append("Trūkst īsa <blockquote> ar galveno domu/ROI.")

    if not re.search(r"(\d+\s?%|€|\bstund|\bmin)", content, flags=re.I):
        issues.append("Trūkst ROI pazīmju (%, €, stundas/minūtes).")

    ew = len(re.findall(r"\S+", data.get("excerpt", "")))
    if ew < 14 or ew > 60:
        issues.append("Excerpt nav 1-2 teikumi (~14-35 vārdi).")

    focus_keyword = data.get("focusKeyword", "")
    if focus_keyword:
        title = data.get("title", "")
        if not title.lower().startswith(focus_keyword.lower()):
            issues.append(f"Title nesākas ar focus keyword: '{focus_keyword}'")
        if not re.search(r"\d", title):
            issues.append("Title nesatur skaitli")
        if len(title) > 60:
            issues.append(f"Title pārāk garš ({len(title)} > 60 rakstzīmes)")

        excerpt = data.get("excerpt", "")
        if not excerpt.lower().startswith(focus_keyword.lower()):
            issues.append(f"Excerpt nesākas ar focus keyword: '{focus_keyword}'")
        if len(excerpt) > 160:
            issues.append(f"Excerpt pārāk garš ({len(excerpt)} > 160 rakstzīmes)")

        seo_slug = data.get("seoSlug", "")
        focus_slug = slugify(focus_keyword)
        if focus_slug not in seo_slug:
            issues.append(f"URL nesatur focus keyword: '{focus_slug}'")

        if not content.lower().startswith(focus_keyword.lower()):
            issues.append(f"Saturs nesākas ar focus keyword: '{focus_keyword}'")

        keyword_count = content.lower().count(focus_keyword.lower())
        keyword_density = (keyword_count / max(1, w)) * 100
        if keyword_density < 1.0:
            issues.append(f"Keyword blīvums pārāk zems ({keyword_density:.2f}% < 1.0%)")
        elif keyword_density > 1.5:
            issues.append(f"Keyword blīvums pārāk augsts ({keyword_density:.2f}% > 1.5%)")

        keyword_in_headings = len(
            re.findall(fr'<h[23][^>]*>.*?{re.escape(focus_keyword)}.*?</h[23]>', content, re.I)
        )
        if keyword_in_headings < 2:
            issues.append(f"Focus keyword parādās tikai {keyword_in_headings} apakšvirsrakstos (vajag vismaz 2)")

    return issues


# =============================================================================
# Outline + sadaļas + refine
# =============================================================================

def outline_response_format() -> dict:
    if not USE_JSON_SCHEMA:
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "WpOutline",
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string", "minLength": 40, "maxLength": 90},
                    "seoSlug": {"type": "string", "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
                    "excerpt": {"type": "string", "minLength": 80, "maxLength": 420},
                    "introHtml": {"type": "string", "minLength": 400},
                    "h3": {
                        "type": "array",
                        "minItems": 7,
                        "maxItems": 12,
                        "items": {"type": "string", "minLength": 12},
                    },
                },
                "required": ["title", "seoSlug", "excerpt", "introHtml", "h3"],
            },
        },
    }


def generate_draft_outline(meta: dict, target_words: int) -> dict:
    category = meta.get("wpCategory") or "SharePoint"
    focus_keyword = meta.get("focusKeyword", "")

    focus_integration = ""
    if focus_keyword:
        focus_integration = (
            f"\n\nFOCUS KEYWORD INTEGRĀCIJA (OBLIGĀTI):"
            f"\n- TITLE: Jāsākas ar '{focus_keyword}' un jāsatur skaitlis (max 60 rakstzīmes)"
            f"\n- EXCERPT: Jāsākas ar '{focus_keyword}' (max 160 rakstzīmes)"
            f"\n- SEO SLUG: Jāsatur '{slugify(focus_keyword)}'"
            f"\n- INTRO HTML: Pirmajai rindkopai jāsākas ar '{focus_keyword}'"
            f"\n- H3 VIRSKRAKSTI: Vismaz 2-3 jāsatur '{focus_keyword}'"
            f"\n- SATURS: Keyword blīvumam jābūt 1-1.5%"
        )

    outline_prompt = (
        f"Koncepts: primārā='{meta.get('primary')}', leņķis='{meta.get('angle')}', auditorija='{meta.get('audience')}'.\n"
        f"Titula hints: {meta.get('titleHint')}\n"
        f"Ieteiktais seoSlug: {meta.get('seoSlugHint')}\n"
        f"Kategorija: {category}\n"
        f"{focus_integration}\n"
        "Uzdevums: Atgriez TIKAI JSON ar atslēgām: title, seoSlug, excerpt, introHtml, h3 (7-12 virsraksti). \n"
        "OBLIGĀTI ievērot focus keyword prasības augstāk.\n"
        "introHtml: viens <h2> 'Ievads' + 1-2 <p> ar tēmu un biznesa kontekstu (bez linkiem). \n"
        "h3: latviski, konkrēti un darbīgi nosaukumi. Neiekļauj CTA, FAQ vai kopsavilkumu.\n"
        "Tonis: profesionāls; bez clickbait; bez miglaina satura.\n"
    )

    payload = {
        "messages": [
            {"role": "system", "content": ARTICLE_SYSTEM_PROMPT},
            {"role": "user", "content": outline_prompt},
        ],
        "max_tokens": min(MAX_TOKENS_MAIN, 3000),
        "temperature": 0.2,
        "response_format": outline_response_format(),
    }

    data = chat_json(payload)

    data["category"] = category
    data["tags"] = []
    data["tagSlugs"] = []
    data["focusKeyword"] = focus_keyword

    data["seoSlug"] = slugify(data.get("seoSlug") or meta.get("seoSlugHint") or data.get("title"))
    data["introHtml"] = sanitize_html(normalize_lv_headings(data.get("introHtml", "")))

    if not isinstance(data.get("h3"), list):
        data["h3"] = []
    if len(data["h3"]) < 7:
        raise RuntimeError("Outline returned too few h3 headings")
    return data


def section_response_format(min_len: int = 1200) -> dict:
    if not USE_JSON_SCHEMA:
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "WpSection",
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "sectionHtml": {"type": "string", "minLength": min_len},
                },
                "required": ["sectionHtml"],
            },
        },
    }


def _chars_min_for_words(words: int) -> int:
    return max(1500, int(words * 5.2))


def generate_section_html(meta: dict, h3_title: str, target_words: int, previous_sections: list[str] | None = None) -> str:
    words = max(380, int(target_words))
    min_len = _chars_min_for_words(words)

    prev_ctx = ", ".join(previous_sections) if previous_sections else "šī ir pirmā sadaļa"
    focus_kw = meta.get("focusKeyword", "") or ""
    wp_cat = meta.get("wpCategory", "") or "SharePoint"

    system = SECTION_SYSTEM_PROMPT.format(
        wpCategory=wp_cat,
        focusKeyword=focus_kw or "(nav norādīts)",
        previousSections=prev_ctx,
    )

    focus_hint = ""
    if focus_kw:
        focus_hint = f"\nFocus keyword '{focus_kw}' — iekļaut organiski 1-2 reizes šajā sadaļā."

    user = (
        f"Sadaļas virsraksts (H3): {h3_title}\n"
        f"Mērķa garums: ~{target_words} vārdi.\n"
        f"Konteksts: {meta.get('primary')}, {meta.get('angle')}, {meta.get('audience')}"
        f"{focus_hint}"
    )
    payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 3500,
        "temperature": 0.7,
        "presence_penalty": 0.3,
        "frequency_penalty": 0.2,
        "response_format": section_response_format(min_len),
    }
    data = chat_json(payload)
    html = sanitize_html(normalize_lv_headings(data.get("sectionHtml", "")))
    return html


def topup_section_html(meta: dict, h3_title: str, deficit_words: int) -> str:
    deficit = max(120, int(deficit_words))
    min_len = _chars_min_for_words(deficit)

    user = (
        f"Sadaļas virsraksts (H3): {h3_title}\n"
        f"Papildini esošo sadaļu ar JAUNU materiālu (neatkārto iepriekš teikto): "
        "1-2 rindkopas ar praktisku padziļinājumu + jaunu mini-piemēru; vajadzības gadījumā 1 <ul> (4-6 punkti). "
        "Bez <h3>, bez <a>, tikai atļautās birkas.\n"
        f"Aptuvenais papildinājums: {deficit} vārdi."
    )
    payload = {
        "messages": [
            {"role": "system", "content": "Tu papildini esošo sadaļu ar jaunu saturu; neesi repetitīvs."},
            {"role": "user", "content": user},
        ],
        "max_tokens": 1800,
        "temperature": 0.7,
        "presence_penalty": 0.4,
        "frequency_penalty": 0.2,
        "response_format": section_response_format(min_len),
    }
    data = chat_json(payload)
    return sanitize_html(normalize_lv_headings(data.get("sectionHtml", "")))


def refine_response_format(target_words: int) -> dict:
    return response_format_for_model(target_words)


def refine_full_article(
    meta: dict,
    title: str,
    seo_slug: str,
    excerpt: str,
    category: str,
    tags: List[str],
    tag_slugs: List[str],
    content_html: str,
    target_words: int,
) -> dict:
    current_words = count_words_from_html(content_html)
    needs_expansion = current_words < int(target_words * 0.9)
    focus_keyword = meta.get("focusKeyword", "")

    focus_keyword_quality = ""
    if focus_keyword:
        keyword_count = content_html.lower().count(focus_keyword.lower())
        keyword_density = (keyword_count / max(1, current_words)) * 100
        focus_keyword_quality = (
            f"\nFOCUS KEYWORD KVALITĀTE: '{focus_keyword}'"
            f"\n- Pašreizējais blīvums: {keyword_density:.2f}% (mērķis: 1-1.5%)"
            f"\n- Nepieciešams: {max(1, int(current_words * 0.01) - keyword_count)} papildu parādīšanās"
            f"\n- Pārliecinies, ka keywords parādās:"
            f"\n  * Pirmajā rindkopā"
            f"\n  * Vismaz 2-3 apakšvirsrakstos (h2/h3)"
            f"\n  * Vienmērīgi visā tekstā"
        )

    user = (
        "Tev ir pilns HTML saturs (zemāk). Pārraksti TIKAI tik, cik vajadzīgs, lai uzlabotu plūdumu starp sadaļām, "
        "precizētu terminus, pievienotu īsas pārejas teikumus un saglabātu konsekventu toni. "
        f"{'PAPILDINI saturu ar detalizētākiem piemēriem, papildu scenārijiem un padziļinātiem skaidrojumiem, lai sasniegtu aptuveni ' + str(target_words) + ' vārdu garumu.' if needs_expansion else ''}"
        f"{focus_keyword_quality}\n"
        "Struktūra: ievads <h2>, pēc tam 7-12 <h3> sadaļas. Saglabā ROI rindkopas un sarakstu skaitu (≥2). "
        "Nekādus linkus, neskarti <code>/<pre> saturu, saglabā atļauto birku komplektu.\n\n"
        f"KONTEKSTS: primārā='{meta.get('primary')}', leņķis='{meta.get('angle')}', auditorija='{meta.get('audience')}'.\n"
        f"MĒRĶIS: ~{target_words} vārdi (±15%).\n\n"
        f"IEEJA_CONTENT_HTML:\n{content_html}"
    )

    payload = {
        "messages": [
            {"role": "system", "content": ARTICLE_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "max_tokens": MAX_TOKENS_MAIN,
        "temperature": 0.2,
        "response_format": refine_response_format(target_words),
    }

    pre = {
        "title": title,
        "seoSlug": seo_slug,
        "excerpt": excerpt,
        "category": category,
        "tags": tags,
        "tagSlugs": tag_slugs,
        "contentHtml": content_html,
        "focusKeyword": focus_keyword,
    }

    try:
        data = chat_json(payload)
    except Exception:
        data = pre

    data["seoSlug"] = slugify(data.get("seoSlug") or seo_slug or title)
    data["contentHtml"] = sanitize_html(normalize_lv_headings(data.get("contentHtml", content_html)))
    if not data.get("category"):
        data["category"] = category or meta.get("wpCategory") or "SharePoint"
    if not data.get("focusKeyword"):
        data["focusKeyword"] = focus_keyword

    return data


# =============================================================================
# WP tag helperi (normalize_tags + ensure_wp_tag_ids)
# =============================================================================

def _strip_level_suffix(slug: str) -> str:
    if not slug:
        return slug
    return re.sub(r"-l[1-5]$", "", slug.strip().lower())


def normalize_tags(data: dict, meta: dict) -> dict:
    import json

    def _load_tag_whitelist() -> dict[str, str]:
        path = os.getenv("TAG_WHITELIST_PATH", os.path.join(os.getcwd(), "cfg_dir", "tags.json"))
        try:
            with open(path, "r", encoding="utf-8") as f:
                items = json.load(f)
            return {i["slug"]: i["name"] for i in items if i.get("slug") and i.get("name")}
        except Exception as e:
            logging.warning(f"[tags.json] load failed: {e}")
            return {}

    def _load_anchor_map() -> dict[str, list[str]]:
        path = os.getenv("ANCHOR_MAP_PATH", os.path.join(os.getcwd(), "cfg_dir", "anchor_map.json"))
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            return {k.strip().lower(): (v if isinstance(v, list) else [v]) for k, v in obj.items()}
        except Exception as e:
            logging.warning(f"[anchor_map.json] load failed: {e}")
            return {}

    limit = int(os.getenv("PER_POST_TAG_LIMIT", "3"))
    strict = os.getenv("ANCHOR_STRICT", "1") == "1"

    wl = _load_tag_whitelist()
    amap = _load_anchor_map()

    anchor_raw = (meta.get("seoSlugHint") or meta.get("SeoSlug") or "").strip().lower()
    anchor = anchor_raw
    if anchor not in amap:
        anchor = _strip_level_suffix(anchor_raw)

    picked_slugs: list[str] = []
    if anchor and anchor in amap:
        for s in amap[anchor]:
            if s in wl and s not in picked_slugs:
                picked_slugs.append(s)
            if len(picked_slugs) >= limit:
                break

    if not picked_slugs and strict:
        data["tagSlugs"] = []
        data["tags"] = []
        return data

    data["tagSlugs"] = picked_slugs
    data["tags"] = [wl[s] for s in picked_slugs]
    return data


import requests


def _wp_auth_headers() -> dict:
    import base64

    scheme = (os.getenv("WP_AUTH_SCHEME", "jwt") or "jwt").lower()
    if scheme == "basic":
        b64 = os.getenv("WP_BASIC_AUTH_B64")
        if not b64:
            user = os.environ["WP_USER"]
            app_pw = os.environ["WP_APP_PASSWORD"]
            b64 = base64.b64encode(f"{user}:{app_pw}".encode("utf-8")).decode("utf-8")
        return {"Authorization": f"Basic {b64}", "Content-Type": "application/json"}
    token = os.environ["WP_TOKEN"]
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def create_or_get_wp_tag(api_base: str, *, name: str, slug: str) -> int:
    headers = _wp_auth_headers()
    r = requests.get(f"{api_base}/wp/v2/tags", params={"slug": slug}, headers=headers, timeout=15)
    r.raise_for_status()
    arr = r.json()
    if arr:
        tag = arr[0]
        if tag.get("name") != name:
            try:
                requests.post(f"{api_base}/wp/v2/tags/{tag['id']}", json={"name": name}, headers=headers, timeout=15)
            except Exception:
                pass
        return tag["id"]

    r = requests.post(f"{api_base}/wp/v2/tags", json={"name": name, "slug": slug}, headers=headers, timeout=15)
    if r.ok:
        return r.json()["id"]

    try:
        err = r.json()
        if err.get("code") == "term_exists":
            return err["data"]["term_id"]
    except Exception:
        pass
    raise RuntimeError(f"WP tag create failed: {r.status_code} {r.text}")


def ensure_wp_tag_ids(api_base: str, token: str, *, names: list[str], slugs: list[str]) -> list[int]:
    ids: list[int] = []
    for name, slug in zip(names, slugs):
        try:
            tag_id = create_or_get_wp_tag(api_base, name=name, slug=slug)
            ids.append(tag_id)
        except Exception as e:
            logging.warning(f"[WP Tag] '{name}' ({slug}) failed: {e}")
    return ids


# =============================================================================
# Core builder (synhronais raksta ģenerators)
# =============================================================================

def calculate_section_words(total_words: int, num_sections: int) -> int:
    intro_share = 0.08
    remaining = total_words * (1 - intro_share)
    buffer = 1.15
    return max(600, int((remaining * buffer) / max(6, num_sections)))


def get_dynamic_max_tokens(target_words: int) -> int:
    base_tokens = 12000
    additional_per_thousand = 2000
    extra_tokens = (target_words // 1000) * additional_per_thousand
    # Azure OpenAI GPT-4o max output is 16384 tokens
    return min(16384, base_tokens + extra_tokens)


def ensure_length_progress(current_html: str, target_words: int, phase: str) -> str:
    current_words = count_words_from_html(current_html)
    progress_ratio = current_words / target_words

    if progress_ratio < 0.7 and phase == "mid_process":
        additional_target = target_words - current_words
        logging.info(f"Garuma progress tikai {progress_ratio:.1%}, nepieciešami papildus {additional_target} vārdi")

    return current_html


def needs_aggressive_topup(html: str, h3_title: str) -> bool:
    word_count = count_words_from_html(html)
    has_list = count_tag(html, "ul") + count_tag(html, "ol") > 0
    has_roi = bool(re.search(r"(\d+\s?%|€|\bstund|\bmin|\beiro|\bEUR)", html, flags=re.I))

    if word_count < 120:
        return True
    if not has_list and word_count < 200:
        return True
    if not has_roi and any(word in h3_title.lower() for word in ['ieguvums', 'roi', 'ietaupījums', 'efektivitāte']):
        return True

    return False


def validate_section_structure(html: str, h3_title: str) -> List[str]:
    issues = []
    if count_words_from_html(html) < 150:
        issues.append("Sadaļa pārāk īsa (<150 vārdi)")
    if count_tag(html, "p") < 3:
        issues.append("Vajag vismaz 3 rindkopas")
    if (count_tag(html, "ul") + count_tag(html, "ol")) < 1:
        issues.append("Trūkst saraksta (ul/ol)")
    if not re.search(r"\b(piemēr|scenārij|solis|darbīb|iestatījum|konfigurē)\w*\b", html, re.I):
        issues.append("Trūkst konkrētu piemēru/soļu")
    if not re.search(r"(\d+\s?%|\d+\s?€|\d+\s?(stund|minūt|mēnes|gad))", html):
        issues.append("Trūkst kvantitatīvu datu (%, €, laiks)")
    return issues


def generate_aggressive_section(meta: dict, h3_title: str, target_words: int, previous_issues: List[str], previous_sections: list[str] | None = None) -> str:
    prev_ctx = ", ".join(previous_sections) if previous_sections else "šī ir pirmā sadaļa"
    focus_kw = meta.get("focusKeyword", "") or ""
    wp_cat = meta.get("wpCategory", "") or "SharePoint"

    system = (
        "Tu esi Kaspars Jurjāns — SharePoint konsultants. "
        "Iepriekšējais mēģinājums NEIZDEVĀS. Tagad raksti PILNĀ STRUKTŪRĀ — bez saīsinājumiem!\n"
        f"Kategorija: {wp_cat}. Focus keyword: {focus_kw or '(nav)'}.\n"
        f"Iepriekšējās sadaļas: {prev_ctx}\n"
        "NEATKĀRTO jau aplūkotās tēmas. Atgriez TIKAI JSON: {\"sectionHtml\": \"<p>...</p>\"}"
    )

    user = (
        f"KĻŪDA IEPRIEKŠ: {', '.join(previous_issues)}\n"
        f"Sadaļas virsraksts: {h3_title}\n"
        f"OBLIGĀTI IZKĀRTOT VISAS KĻŪDAS!\n\n"
        f"STRUKTŪRA (NEMAINĀMS):\n"
        f"1. 1-2 ievada rindkopas (kas un kāpēc)\n"
        f"2. 2-3 praktiski piemēri ar konkrētiem soļiem\n"
        f"3. 1 saraksts ar 5-7 punktiem (padomi/soļi/priekšrocības)\n"
        f"4. 1 ROI rindkopa ar skaitļiem (piem., 'ietaupa 15-30% laika')\n"
        f"5. 1-2 noslēguma rindkopas\n\n"
        f"Konteksts: {meta.get('primary')} → {meta.get('angle')} → {meta.get('audience')}\n"
        f"Garums: ~{target_words} vārdi\n"
        f"ATBILDI TIKAI AR PILNU STRUKTŪRU!"
    )

    payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 3500,
        "temperature": 0.3,
        "response_format": section_response_format(target_words),
    }

    data = chat_json(payload)
    html = sanitize_html(normalize_lv_headings(data.get("sectionHtml", "")))
    return html


def generate_section_html_with_validation(meta: dict, h3_title: str, target_words: int, max_retries: int = 2, previous_sections: list[str] | None = None) -> str:
    for attempt in range(max_retries):
        html = generate_section_html(meta, h3_title, target_words, previous_sections=previous_sections)
        issues = validate_section_structure(html, h3_title)

        if not issues:
            return html

        logging.warning(f"Sadaļa '{h3_title}' neatbilst prasībām (mēģinājums {attempt+1}): {issues}")

        if attempt == max_retries - 1:
            return generate_aggressive_section(meta, h3_title, target_words, issues, previous_sections=previous_sections)

    return html


ARTICLE_MODE = (os.getenv("ARTICLE_MODE", "multi") or "multi").strip().lower()

# =============================================================================
# MEGA-PROMPT: single-call article generation (Plan C)
# =============================================================================

MEGA_SYSTEM_PROMPT = (
    "Tu esi Kaspars Jurjāns — Latvijas vadošais SharePoint un Microsoft 365 konsultants "
    "(15+ gadu pieredze, 200+ veiksmīgi projekti). Tu raksti vienu padziļinātu B2B tehnisku "
    "rakstu latviešu valodā.\n\n"
    "AUDITORIJA: IT vadītāji un biznesa procesu īpašnieki Latvijā un Baltijā "
    "(uzņēmumi ar 50-500 darbiniekiem).\n\n"
    "RAKSTA STRUKTŪRA (obligāti):\n"
    "• 1 ievads ar <h2>: problēmas definīcija un kāpēc tā ir aktuāla (150-200 vārdi)\n"
    "• 8-10 sadaļas ar <h3>: katra satur problēmu → risinājumu → soļus → ROI (250-350 vārdi katra)\n"
    "• 2+ saraksti (<ul>/<ol>) visā rakstā ar 4-7 punktiem katrā\n"
    "• 1 <blockquote> ar galveno ROI kopsavilkumu\n"
    "• Katra sadaļa beidzas ar pārejas teikumu uz nākamo\n\n"
    "KVALITĀTES STANDARTI:\n"
    "• Katrs apgalvojums pamatos ar scenāriju VAI skaitli\n"
    "• Visi skaitļi ir diapazoni ar kontekstu (piem., '15-30% mazāk laika — uzņēmumā ar 50+ darbiniekiem')\n"
    "• Praktiski soļi izmanto precīzus Microsoft 365 UI terminus "
    "(piem., 'Document Library → Settings → Versioning settings')\n"
    "• Focus keyword: organiski 1-1.5% blīvumā (NEmehāniski ievadīts)\n\n"
    "RAKSTĪŠANAS PRINCIPI:\n"
    "1. Neraksti 'uzlabo produktivitāti' — raksti 'samazina dokumentu meklēšanas laiku no 12 min uz 45 sek'\n"
    "2. Neizmanto vārdus: 'var', 'iespējams', 'varētu' — aizstāj ar konkrētu instrukciju\n"
    "3. Katrai sadaļai struktūra: problēma → risinājums → soļi → rezultāts\n\n"
    "AIZLIEGTS: 'šajā rakstā aplūkosim', angliski heading, clickbait, tukši CTA, <a> birkas, inline stili.\n\n"
    "FORMATĒŠANA: Atļauts tikai <h2>,<h3>,<p>,<ul>,<ol>,<li>,<strong>,<em>,<code>,<pre>,<blockquote>,<br>.\n"
    "TAGI: 3-6 domēna termini latviski. Katram tagam dod ASCII slug.\n\n"
    "ATBILDI TIKAI AR DERĪGU JSON."
)

MEGA_USER_TEMPLATE = (
    "Raksts: {title}\n"
    "Tēma: {primary}\n"
    "Leņķis: {angle}\n"
    "Auditorija: {audience}\n"
    "Focus keyword: {focusKeyword}\n"
    "Kategorija: {wpCategory}\n"
    "Mērķa garums: {targetWords} vārdi\n\n"
    "JSON shēma:\n"
    '{{\n'
    '  "title": "max 60 rakstz., sākas ar focus keyword, satur skaitli",\n'
    '  "seoSlug": "ascii-lowercase-bez-diakritikam",\n'
    '  "excerpt": "max 160 rakstz., sākas ar focus keyword",\n'
    '  "contentHtml": "<h2>Ievads</h2><p>...</p><h3>...</h3><p>...</p>...",\n'
    '  "category": "{wpCategory}",\n'
    '  "tags": ["3-6 latviski termini"],\n'
    '  "tagSlugs": ["ascii-slug"],\n'
    '  "focusKeyword": "{focusKeyword}"\n'
    '}}'
)


def build_wp_article_mega(meta: dict, target_words: int) -> dict:
    """
    Generate a complete article in ONE API call using the mega-prompt approach.
    Returns the same dict structure as build_wp_article_from_item.
    """
    focus_keyword = meta.get("focusKeyword", "") or ""
    title_hint = meta.get("titleHint", "") or ""

    user_msg = MEGA_USER_TEMPLATE.format(
        title=title_hint,
        primary=meta.get("primary", ""),
        angle=meta.get("angle", ""),
        audience=meta.get("audience", ""),
        focusKeyword=focus_keyword,
        wpCategory=meta.get("wpCategory", "SharePoint"),
        targetWords=target_words,
    )

    max_tokens = get_dynamic_max_tokens(target_words)

    # Mega mode uses simpler json_object format to avoid Azure strict schema
    # restrictions with large minLength values on contentHtml
    payload = {
        "messages": [
            {"role": "system", "content": MEGA_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.4,
        "response_format": {"type": "json_object"},
    }

    logging.info(f"[mega] Starting single-call generation, target={target_words} words, max_tokens={max_tokens}")
    data = chat_json(payload)

    # Minimal post-processing (sanitize, don't rewrite)
    data["contentHtml"] = sanitize_html(normalize_lv_headings(data.get("contentHtml", "")))
    data["seoSlug"] = slugify(data.get("seoSlug") or data.get("title", ""))

    if not data.get("category"):
        data["category"] = meta.get("wpCategory") or "SharePoint"
    if not data.get("focusKeyword"):
        data["focusKeyword"] = focus_keyword

    # One quality check + retry with feedback if needed
    issues = quality_issues(data, target_words)
    if issues:
        logging.info(f"[mega] Quality issues found, retrying: {issues}")
        retry_msg = (
            f"Kļūdas iepriekšējā versijā:\n"
            + "\n".join(f"- {i}" for i in issues)
            + "\n\nIZLABO tieši šīs kļūdas, paturot pārējo saturu. Atgriez pilnu labotu JSON."
        )
        retry_payload = {
            "messages": [
                {"role": "system", "content": MEGA_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)[:60000]},
                {"role": "user", "content": retry_msg},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        try:
            data2 = chat_json(retry_payload)
            data2["contentHtml"] = sanitize_html(normalize_lv_headings(data2.get("contentHtml", "")))
            data2["seoSlug"] = slugify(data2.get("seoSlug") or data2.get("title", ""))
            if not data2.get("category"):
                data2["category"] = meta.get("wpCategory") or "SharePoint"
            if not data2.get("focusKeyword"):
                data2["focusKeyword"] = focus_keyword
            data = data2
            logging.info("[mega] Retry completed")
        except Exception as e:
            logging.warning(f"[mega] Retry failed, using first version: {e}")

    final_words = count_words_from_html(data.get("contentHtml", ""))
    logging.info(f"[mega] Done: {final_words} words (target {target_words})")

    return data


def build_wp_article_from_item(item: dict) -> dict:
    incoming = item or {}
    picked = pick_item(incoming)
    meta = extract_meta(picked)

    try:
        tw = int(picked.get("targetWords")) if isinstance(picked, dict) and picked.get("targetWords") else int(
            os.getenv("WP_DEFAULT_WORDS", "5000")
        )
    except Exception:
        tw = int(os.getenv("WP_DEFAULT_WORDS", "5000"))
    TW_MIN = int(os.getenv("WP_MIN_WORDS", "3000"))
    TW_MAX = int(os.getenv("WP_MAX_WORDS", "15000"))
    target_words = max(TW_MIN, min(TW_MAX, tw))

    missing = [k for k in ("primary", "angle", "audience") if not meta.get(k)]
    if missing:
        raise RuntimeError(f"Missing required fields: {', '.join(missing)}")

    # Route to mega-prompt mode if configured
    mode = (picked.get("articleMode") or ARTICLE_MODE).strip().lower()
    if mode == "mega":
        logging.info(f"[build] Using MEGA mode for article generation (target={target_words})")
        data = build_wp_article_mega(meta, target_words)
        # Apply same post-processing as multi-call path
        data = normalize_tags(data, meta)
        try:
            names = data.get("tags") or []
            slugs = data.get("tagSlugs") or []
            if names and slugs and not data.get("wpTagIds"):
                api_base = os.environ.get("WP_API_BASE")
                if api_base:
                    token = os.getenv("WP_TOKEN", "")
                    data["wpTagIds"] = ensure_wp_tag_ids(api_base, token, names=names, slugs=slugs)
                else:
                    data["wpTagIds"] = []
        except Exception as _e:
            logging.warning(f"[wpTagIds] mega mode failed: {_e}")
            data["wpTagIds"] = data.get("wpTagIds") or []
        return data

    dynamic_max_tokens = get_dynamic_max_tokens(target_words)
    global MAX_TOKENS_MAIN
    original_max_tokens = MAX_TOKENS_MAIN
    MAX_TOKENS_MAIN = dynamic_max_tokens

    try:
        try:
            outline = generate_draft_outline(meta, target_words)
        except Exception:
            if not FORCE_SHORT_MODE:
                raise
            title_fallback = (meta.get("titleHint") or "SharePoint risinājumi praksē").strip()
            outline = {
                "title": title_fallback,
                "seoSlug": slugify(meta.get("seoSlugHint") or title_fallback),
                "excerpt": "Ātrs kopsavilkums par praktisku pieeju ar SharePoint/Power Automate.",
                "category": meta.get("wpCategory") or "SharePoint",
                "tags": ["SharePoint", "automatizācija", "datu drošība"],
                "tagSlugs": ["sharepoint", "automatizacija", "datu-drosiba"],
                "focusKeyword": meta.get("focusKeyword", ""),
                "introHtml": "<h2>Ievads</h2><p>Īss ievads, lai turpinātu ģenerēt pa daļām arī avārijas režīmā.</p>",
                "h3": [
                    "Biznesa vajadzības un mērķi",
                    "Informācijas arhitektūra",
                    "Automatizācija ar Power Automate",
                    "Drošība un piekļuves kontrole",
                    "Datu kvalitāte un metadati",
                    "Izvietošana un pārvaldība",
                    "Mērījumi un ROI",
                ],
            }

        title = outline.get("title") or (meta.get("titleHint") or "").strip() or "SharePoint risinājumi praksē"
        seo_slug = slugify(outline.get("seoSlug") or meta.get("seoSlugHint") or title)
        excerpt = outline.get("excerpt", "")
        category = outline.get("category") or meta.get("wpCategory") or "SharePoint"
        tags = outline.get("tags") or []
        tag_slugs = outline.get("tagSlugs") or []
        focus_keyword = outline.get("focusKeyword") or meta.get("focusKeyword", "")
        h3_list = [h.strip() for h in outline.get("h3", []) if isinstance(h, str) and h.strip()]
        intro_html = outline.get("introHtml") or "<h2>Ievads</h2><p>-</p>"

        sections: List[Tuple[str, str]] = []
        h3_worklist = h3_list[:4] if FORCE_SHORT_MODE else h3_list
        per_section = calculate_section_words(target_words, len(h3_worklist))

        for idx, h in enumerate(h3_worklist):
            prev_h3 = [title for title, _ in sections]  # already generated section titles
            try:
                sec_html = generate_section_html_with_validation(meta, h, per_section, previous_sections=prev_h3)
            except Exception as e:
                logging.error(f"Sadaļas '{h}' ģenerēšana neizdevās: {e}")
                sec_html = "<p>Šīs sadaļas ģenerēšanā radās grūtības, bet turpinām ar pārējo saturu.</p>"

            words_now = count_words_from_html(sec_html)
            need = int(per_section * 0.95) - words_now

            if need > 60 or needs_aggressive_topup(sec_html, h):
                try:
                    extra = topup_section_html(meta, h, max(need, 100))
                    sec_html = (sec_html.strip() + "\n\n" + extra.strip()).strip()
                    logging.info(f"Sadaļai '{h}' piemērots top-up: +{count_words_from_html(extra)} vārdi")
                except Exception as topup_error:
                    logging.warning(f"Top-up neizdevās sadaļai '{h}': {topup_error}")

            sections.append((h, f"<h3>{h}</h3>\n{sec_html}"))

            current_content = intro_html + "\n\n" + "\n\n".join([frag for _, frag in sections])
            current_content = ensure_length_progress(current_content, target_words, "mid_process")

            if FORCE_SHORT_MODE and idx >= 2:
                break

        content_html = (intro_html or "").strip()
        has_global_bq = has_blockquote(content_html)
        composed_parts = [content_html] if content_html else []
        for _, frag in sections:
            composed_parts.append(frag)
            if not has_global_bq and has_blockquote(frag):
                has_global_bq = True
        content_html = sanitize_html(normalize_lv_headings("\n\n".join(composed_parts)))

        total_words_now = count_words_from_html(content_html)
        if total_words_now < int(target_words * 0.85):
            filler_target = max(500, target_words - total_words_now + 300)
            filler_h3 = "Papildu praktiskie scenāriji un BUJ"
            try:
                filler_html = generate_section_html(
                    meta,
                    f"{filler_h3}: Scenārijs A, Scenārijs B, BUJ (riski, drošība, uzturēšana)",
                    filler_target,
                )
                now = total_words_now + count_words_from_html(filler_html)
                need = int(target_words * 0.90) - now
                if need > 80:
                    extra = topup_section_html(meta, filler_h3, need)
                    filler_html = (filler_html.strip() + "\n\n" + extra.strip()).strip()
                content_html = content_html + "\n\n" + f"<h3>{filler_h3}</h3>\n{filler_html}"
            except Exception:
                pass

        refined = refine_full_article(
            meta=meta,
            title=title,
            seo_slug=seo_slug,
            excerpt=excerpt,
            category=category,
            tags=tags,
            tag_slugs=tag_slugs,
            content_html=content_html,
            target_words=target_words,
        )
        data = refined
        for _ in range(2):
            issues = quality_issues(data, target_words)
            if not issues:
                break
            try:
                data = refine_full_article(
                    meta=meta,
                    title=data.get("title") or title,
                    seo_slug=data.get("seoSlug") or seo_slug,
                    excerpt=data.get("excerpt") or excerpt,
                    category=data.get("category") or category,
                    tags=data.get("tags") or tags,
                    tag_slugs=data.get("tagSlugs") or tag_slugs,
                    content_html=data.get("contentHtml") or content_html,
                    target_words=target_words,
                )
            except Exception:
                break

        data = normalize_tags(data, meta)

        try:
            names = data.get("tags") or []
            slugs = data.get("tagSlugs") or []
            if names and slugs and not data.get("wpTagIds"):
                api_base = os.environ.get("WP_API_BASE")
                if api_base:
                    token = os.getenv("WP_TOKEN", "")
                    data["wpTagIds"] = ensure_wp_tag_ids(api_base, token, names=names, slugs=slugs)
                else:
                    data["wpTagIds"] = []
        except Exception as _e:
            logging.warning(f"[wpTagIds] generation failed: {_e}")
            data["wpTagIds"] = data.get("wpTagIds") or []

        for key in ("title", "seoSlug", "excerpt", "contentHtml", "category", "tags", "tagSlugs", "focusKeyword"):
            if key not in data:
                if key == "seoSlug":
                    data["seoSlug"] = slugify(data.get("title") or title)
                elif key == "category":
                    data["category"] = category
                elif key == "tags":
                    data["tags"] = tags if tags else ["SharePoint", "automatizācija", "Microsoft 365"]
                elif key == "tagSlugs":
                    data["tagSlugs"] = [slugify(t) for t in data.get("tags", [])]
                elif key == "excerpt":
                    data["excerpt"] = (
                        excerpt
                        or "Rakstā praktiski piemēri un ROI par SharePoint/Power Automate risinājumiem."
                    )
                elif key == "title":
                    data["title"] = title
                elif key == "contentHtml":
                    data["contentHtml"] = content_html
                elif key == "focusKeyword":
                    data["focusKeyword"] = focus_keyword

        data["seoSlug"] = slugify(data.get("seoSlug") or data.get("title"))
        data["contentHtml"] = sanitize_html(normalize_lv_headings(data.get("contentHtml", "")))
        if not data.get("category"):
            data["category"] = category
        if not data.get("focusKeyword"):
            data["focusKeyword"] = focus_keyword

        final_words = count_words_from_html(data.get("contentHtml", ""))
        logging.info(f"Ģenerēts raksts ar {final_words} vārdiem no mērķa {target_words} vārdi")

        return data
    finally:
        MAX_TOKENS_MAIN = original_max_tokens


# Pievienojiet šo funkciju article_gen.py galā (pārņem pilnībā; tai nav ārēju atkarību,
# izņemot opcionalo pytrends/SerpApi un chat_json, ja tie ir pieejami jūsu vidē).
def generate_keywords_from_input(input_obj: dict, limit: int = 10, use_llm: bool = False, enrich: bool = False) -> dict:
    """
    Generate keyword candidates and return top recommendation.
    - input_obj: dict with keys like primary, angle, audience, wpCategory, tagsCsv, SeoSlug, combokey, existing_text_sample, target_words
    - limit: number of candidates to return
    - use_llm: if True, attempt a single LLM re-rank (requires chat_json in scope and API keys)
    - enrich: if True, attempt optional enrichment (SerpApi/pytrends) if keys & libs available
    Returns: {"top_recommendation": str|None, "candidates": [ ... ]}
    """
    import re
    import html
    import os
    import json
    from collections import Counter

    # lightweight Latvian stopwords (extend if needed)
    LV_STOPWORDS = {
        "un","vai","ar","uz","par","kas","no","lai","ka","ir","tas","tā","es","tu","viņš","viņa",
        "mēs","jūs","būt","kā","pie","šeit","tur","šajā","tajā","tad","gan","kuri","kura",
        "kuras","viens","viena","vēl","arī","bez","pēc","savu","savā","mūsu","jau","cits",
        "cita","kuru","kāds","kāda","kādi","kādas"
    }

    CORE_TOKENS = ["teams","copilot","rēķin","apstiprinā","kanāls","finanšu","vadītājs",
                   "automati","status","atskai","plūsma","proces","darbplūsa","apstrād"]

    def normalize_text(s: str) -> str:
        if not s:
            return ""
        s = html.unescape(s)
        s = s.lower()
        s = re.sub(r"[-_/]", " ", s)
        s = re.sub(r"[^\wāčēģīķļņōšūž\s]", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    def tokens(text: str):
        return [t for t in re.findall(r"[a-zāčēģīķļņōšūž0-9]+", (text or "").lower())]

    def ngrams(tokens_list, nmin=1, nmax=4):
        n = len(tokens_list)
        out = []
        for i in range(n):
            for L in range(nmin, nmax + 1):
                if i + L <= n:
                    out.append(" ".join(tokens_list[i:i+L]))
        return out

    def source_candidates(meta: dict):
        pool = []
        fields = ["primary", "SeoSlug", "combokey", "angle", "tagsCsv"]
        for f in fields:
            v = meta.get(f) or meta.get(f.lower()) or ""
            if not v:
                continue
            if f == "SeoSlug" and "-" in v:
                pool.append(normalize_text(v.replace("-", " ")))
            elif f == "combokey":
                for p in v.split("|"):
                    pool.append(normalize_text(p))
            else:
                pool.append(normalize_text(v))
        return [p for p in pool if p]

    def extract_from_text(text: str, max_candidates: int = 300):
        text_n = normalize_text(text)
        toks = tokens(text_n)
        ng = ngrams(toks, 1, 4)
        filt = []
        for g in ng:
            words = g.split()
            if len(g) < 4:
                continue
            if all(w in LV_STOPWORDS for w in words):
                continue
            if all(re.fullmatch(r"\d+", w) for w in words):
                continue
            filt.append(g)
        c = Counter(filt)
        return [t for t, _ in c.most_common(max_candidates)]

    def score_candidate(candidate: str, meta_text: str, primary_text: str) -> float:
        c_norm = normalize_text(candidate)
        toks = c_norm.split()
        freq = meta_text.count(c_norm)
        in_primary = 1 if primary_text and c_norm in primary_text else 0
        core_hits = sum(1 for t in CORE_TOKENS if t in c_norm)
        wlen = len(toks)
        if 2 <= wlen <= 4:
            len_w = 2.0
        elif wlen == 1:
            len_w = 1.0
        else:
            len_w = 1.2
        stop_ratio = sum(1 for t in toks if t in LV_STOPWORDS) / max(1, len(toks))
        spec = max(0.2, 1.0 - stop_ratio)
        biz = 1.0 + (0.5 * core_hits)
        raw = (freq * 1.5) + (in_primary * 4.0) + (core_hits * 2.5)
        return float(raw * len_w * spec * biz)

    def predict_intent(candidate: str) -> str:
        c = candidate.lower()
        if any(w in c for w in ("kā ", "kādu", "kāpan", "guide", "how")):
            return "informational"
        if any(w in c for w in ("apstiprinā", "rēķin", "iegād", "pirkt", "risināj", "implement", "izvieto")):
            return "transactional"
        return "informational"

    def dedupe_keep_best(cands):
        seen = {}
        for cand, data in cands:
            key = re.sub(r"\s+", " ", cand.strip())
            if key in seen:
                if data["score"] > seen[key]["score"]:
                    seen[key] = data
            else:
                seen[key] = data
        return [(k, v) for k, v in seen.items()]

    # build meta text
    meta_parts = []
    for k in ("primary", "angle", "audience", "wpCategory", "tagsCsv", "SeoSlug", "combokey"):
        v = input_obj.get(k) or input_obj.get(k.lower()) or ""
        if v:
            meta_parts.append(normalize_text(v.replace("-", " ") if k == "SeoSlug" else v))
    existing = input_obj.get("existing_text_sample") or ""
    meta_text = " ".join(meta_parts) + " " + normalize_text(existing)
    meta_text = re.sub(r"\s+", " ", meta_text).strip()
    primary = normalize_text(input_obj.get("primary") or "")

    pool = []
    pool.extend(source_candidates(input_obj))
    pool.extend(extract_from_text(meta_text, max_candidates=300))
    pool.extend(ngrams(tokens(primary), 1, 4))
    pool = [p for p in pool if p and len(p) >= 4]
    # preserve order
    seen_order = {}
    pooled = []
    for p in pool:
        if p not in seen_order:
            seen_order[p] = True
            pooled.append(p)
    pool = pooled

    scored = []
    for cand in pool:
        sc = score_candidate(cand, meta_text, primary)
        if sc <= 0.1:
            continue
        intent = predict_intent(cand)
        reason = []
        if primary and cand in primary:
            reason.append("matches primary")
        if any(core in cand for core in CORE_TOKENS):
            reason.append("core term present")
        if meta_text.count(cand) > 0:
            reason.append(f"occurs {meta_text.count(cand)}x in meta")
        scored.append((cand, {"score": sc, "intent": intent, "reason": "; ".join(reason)}))

    deduped = dedupe_keep_best(scored)
    deduped.sort(key=lambda item: (-item[1]["score"], - (1 if 2 <= len(item[0].split()) <= 4 else 0), -len(item[0].split())))
    maxs = max((d[1]["score"] for d in deduped), default=1.0)

    candidates = []
    for cand, data in deduped[: max(3 * limit, 40)]:
        norm = data["score"] / maxs if maxs else 0.0
        candidates.append({
            "phrase": cand,
            "predicted_relevance": round(norm, 3),
            "intent": data["intent"],
            "short_reason": data["reason"],
            "score_raw": round(data["score"], 3)
        })
#==========================================================================================================
# optional enrichment (SerpApi/pytrends) only if requested
    enrichment = {}
    if enrich:
        # decide top_n (use default)
        top_n = min(TOP_N_DEFAULT, len(candidates))
        phrases = [c["phrase"] for c in candidates[:top_n]]

        # compute cached count
        cached = 0
        for p in phrases:
            cache_key = "serp:" + p.replace(" ", "_")[:200]
            if redis_get(cache_key) is not None:
                cached += 1
        estimated_searches = top_n - cached

        # check monthly allowance BEFORE calling
        current_month = redis_get_int("serpapi:month:" + datetime.utcnow().strftime("%Y%m"))
        if current_month + estimated_searches > MONTHLY_GUARD:
            # skip calling SerpApi to protect quota
            logging.warning(f"SerpApi monthly guard exceeded: month={current_month}, need={estimated_searches}, guard={MONTHLY_GUARD}")
            for p in phrases:
                enrichment[p] = {}
        else:
            # call SerpApi only for top_n phrases (use cached where present)
            for p in phrases:
                cache_key = "serp:" + p.replace(" ", "_")[:200]
                cached_j = redis_get(cache_key)
                if cached_j is not None:
                    j = cached_j
                else:
                    j = serp_search_cached(p, ttl=DEFAULT_TTL, max_per_minute=30)
                if j:
                    organic = j.get("organic_results", []) or []
                    enrichment[p] = {
                        "organic_count": len(organic),
                        "has_paa": bool(j.get("related_questions")),
                        "has_featured_snippet": bool(j.get("featured_snippet")),
                    }
                else:
                    enrichment[p] = {}

        # pytrends enrichment for a small chunk (optional)
        try:
            from pytrends.request import TrendReq
            pytrends = TrendReq(hl='lv', tz=360)
            chunk = [c for c in phrases][:5]
            if chunk:
                pytrends.build_payload(chunk, timeframe='now 7-d')
                df = pytrends.interest_over_time()
                for p in chunk:
                    if p in df.columns:
                        vals = df[p].tolist()
                        enrichment.setdefault(p, {})["trend"] = (sum(vals) / (len(vals) or 1)) / (max(vals) or 1) if vals else 0.0
        except Exception:
            pass

#==========================================================================================================
    # optional LLM re-rank (single call)
    top_choice = None
    llm_available = False
    try:
        llm_available = bool(globals().get("chat_json") and (os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("OAI_API_KEY")))
    except Exception:
        llm_available = False

    if use_llm and llm_available:
        try:
            system = ("You are an expert SEO analyst. Choose the single best focus keyword from the candidates. "
                      "Return ONLY JSON: {\"keyword\":string,\"score\":float,\"explanation\":string}.")
            user_obj = {
                "meta": {
                    "primary": input_obj.get("primary"),
                    "angle": input_obj.get("angle"),
                    "audience": input_obj.get("audience"),
                    "SeoSlug": input_obj.get("SeoSlug"),
                    "combokey": input_obj.get("combokey"),
                    "target_words": input_obj.get("target_words"),
                },
                "candidates": []
            }
            for c in candidates:
                p = c["phrase"]
                user_obj["candidates"].append({"phrase": p, "predicted_relevance": c.get("predicted_relevance"), "intent": c.get("intent"), "short_reason": c.get("short_reason"), "enrichment": enrichment.get(p, {})})
            payload = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user_obj, ensure_ascii=False)}], "max_tokens": 600, "temperature": 0.0, "response_format": {"type": "json_object"}}
            resp = chat_json(payload)
            if isinstance(resp, dict) and resp.get("keyword"):
                top_choice = {"keyword": resp.get("keyword"), "score": resp.get("score", 1.0), "explanation": resp.get("explanation", "")}
        except Exception:
            top_choice = None

    # fallback local scoring
    if not top_choice:
        best = None
        for c in candidates:
            phrase = c["phrase"]
            rel = c.get("predicted_relevance", 0.0)
            trend = enrichment.get(phrase, {}).get("trend", 0.0)
            serp_score = 1.0 if enrichment.get(phrase, {}).get("organic_count", 0) > 0 else 0.5
            final_score = 0.6 * rel + 0.25 * trend + 0.15 * serp_score
            c["_final_score"] = round(final_score, 3)
            if not best or final_score > best["_final_score"]:
                best = c
        if best:
            top_choice = {"keyword": best["phrase"], "score": best.get("_final_score", best.get("predicted_relevance", 0.0)), "explanation": "local heuristic + enrichment"}
        else:
            top_choice = None

    return {"top_recommendation": (top_choice["keyword"] if top_choice else None), "candidates": candidates[:limit]}
