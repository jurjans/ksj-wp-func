import os
import re
import json
import logging
from typing import List, Tuple

# --- SerpApi / kešēšanas konfigurācija (PASTĪT PĒC IMPORTIEM) -------------
from datetime import datetime
import time

from config import (
    SECTION_INTRO_SHARE,
    SECTION_WORD_BUFFER,
    SECTION_MIN_WORDS,
    SECTION_VALIDATE_MIN_WORDS,
    SECTION_VALIDATE_MIN_PARAS,
    SECTION_TOPUP_MIN_WORDS,
    SECTION_TOPUP_NO_LIST_MIN,
    TOPUP_SECTION_MIN_DEFICIT,
    REFINE_EXPANSION_THRESHOLD,
    PROGRESS_MIN_RATIO,
    KW_DENSITY_MIN_PCT,
    KW_DENSITY_MAX_PCT,
    KW_SAFETY_NET_DENSITY_PCT,
    KW_SAFETY_NET_MAX_INJECTIONS,
    KW_INJECT_MIN_PARA_CHARS,
    OUTLINE_MAX_TOKENS,
    SECTION_MAX_TOKENS,
    TOPUP_MAX_TOKENS,
    MEGA_BATCH_MAX_TOKENS,
    DYNAMIC_TOKENS_BASE,
    DYNAMIC_TOKENS_PER_1K_WORDS,
    GPT4O_MAX_OUTPUT_TOKENS,
    MEGA_WORD_INFLATION,
    MEGA_SECTION_WORD_MULTIPLIER,
    MEGA_SECTION_MIN_WORDS,
    MEGA_OUTLINE_H3_MIN,
    SUMMARIZE_PREVIOUS_MAX_CHARS,
)

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
    """Retrieve a JSON-deserialized value from Redis, or None if unavailable."""
    if not _redis:
        return None
    v = _redis.get(key)
    try:
        return json.loads(v) if v else None
    except Exception:
        return None

def redis_set(key: str, value, ttl: int = DEFAULT_TTL):
    """Serialize value to JSON and store it in Redis with the given TTL (seconds)."""
    if not _redis:
        return
    try:
        _redis.set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
    except Exception:
        return

def redis_get_int(key: str):
    """Return Redis key value as int, or 0 if the key is absent or Redis is unavailable."""
    if not _redis:
        return 0
    try:
        v = _redis.get(key)
        return int(v) if v else 0
    except Exception:
        return 0

def redis_incr_month(count: int = 1):
    """Increment the current-month SerpApi usage counter by count and return the new value."""
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
    """Atomically increment a Redis counter key and set its TTL to window_sec.

    Args:
        key: Redis key to increment.
        window_sec: Expiry window in seconds (reset each call).

    Returns:
        New integer value of the counter, or 0 on error / no Redis.
    """
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
            if r.status_code == 401:
                logging.error(
                    "serp_search_cached: authentication failure (401) — check SERPAPI_KEY. query=%r",
                    q,
                )
                return {}
            if r.status_code == 403:
                logging.warning(
                    "serp_search_cached: access forbidden (403) — plan limit or IP restriction. "
                    "query=%r response=%s",
                    q, r.text[:200],
                )
                return {}
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(wait)
                continue
            logging.warning(
                "serp_search_cached: unexpected HTTP %d for query=%r — %s",
                r.status_code, q, r.text[:200],
            )
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
    """Strip disallowed HTML tags, keeping only the ALLOWED_TAGS set."""
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)

    def _repl(m: re.Match):
        tag = m.group(1).lower()
        return f"<{'' if m.group(0)[1] != '/' else '/'}{tag}>" if tag in ALLOWED_TAGS else ""

    return TAG_RE.sub(_repl, html)


import unicodedata


def slugify(text: str) -> str:
    """Convert text to a URL-friendly ASCII slug (lowercase, hyphens, no diacritics)."""
    if not text:
        return ""
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    # Replace dots with hyphens (matches WordPress sanitize_title behavior)
    s = s.replace(".", "-")
    s = re.sub(r"[^a-zA-Z0-9\-\s]", "", s)
    s = re.sub(r"\s+", "-", s.strip().lower())
    return re.sub(r"-{2,}", "-", s).strip("-")


# ==== LLM HTTP helperi =======================================================

import ssl
import urllib.error
import urllib.request


LLM_HTTP_TIMEOUT = int(os.getenv("LLM_HTTP_TIMEOUT", "1500"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
LLM_RETRY_BASE_DELAY = float(os.getenv("LLM_RETRY_BASE_DELAY", "1.0"))
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


def is_azure_openai() -> bool:
    """Return True when both AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY are set."""
    return bool(os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_API_KEY"))


def get_url() -> str:
    """Build the chat-completions endpoint URL for whichever LLM provider is configured.

    Returns:
        Full URL string for the Azure OpenAI or OpenAI chat/completions endpoint.
    """
    if is_azure_openai():
        base = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
        ver = os.getenv("AZURE_OPENAI_API_VERSION", "2024-11-20")
        dep = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
        return f"{base}/openai/deployments/{dep}/chat/completions?api-version={ver}"
    else:
        base = os.getenv("OAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        return f"{base}/chat/completions"


def get_headers() -> dict:
    """Return authentication headers for the active LLM provider (api-key for Azure, Bearer for OpenAI)."""
    if is_azure_openai():
        return {"Content-Type": "application/json", "api-key": os.getenv("AZURE_OPENAI_API_KEY", "")}
    return {"Content-Type": "application/json", "Authorization": f"Bearer {os.getenv('OAI_API_KEY','')}"}


def http_post_json(url: str, headers: dict, body: dict, timeout_sec: int = LLM_HTTP_TIMEOUT) -> dict:
    """POST JSON body to url and return parsed JSON response.

    Retries up to LLM_MAX_RETRIES times for transient HTTP errors (429/5xx)
    with exponential backoff, honouring Retry-After when present.

    Args:
        url: Full endpoint URL.
        headers: HTTP request headers (including auth).
        body: Request payload to serialize as JSON.
        timeout_sec: Per-attempt socket timeout in seconds.

    Returns:
        Parsed JSON response dict.

    Raises:
        RuntimeError: On non-retryable HTTP errors or exhausted retries.
    """
    req_body = json.dumps(body).encode("utf-8")
    ctx = ssl.create_default_context()
    last_exc: Exception = RuntimeError("http_post_json: no attempts made")

    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=req_body, headers=headers, method="POST")
            with urllib.request.urlopen(req, context=ctx, timeout=timeout_sec) as resp:
                return json.loads(resp.read().decode("utf-8"))

        except urllib.error.HTTPError as e:
            status = e.code
            try:
                body_text = e.read().decode("utf-8", errors="replace")
            except Exception:
                body_text = "(unreadable)"

            if status not in _TRANSIENT_STATUS_CODES:
                raise RuntimeError(
                    f"LLM API HTTP {status} (non-retryable): {body_text[:400]}"
                ) from e

            last_exc = RuntimeError(f"LLM API HTTP {status}: {body_text[:200]}")
            if attempt >= LLM_MAX_RETRIES:
                break

            try:
                delay = float(e.headers.get("Retry-After") or 0)
            except Exception:
                delay = 0.0
            if not (delay > 0):
                delay = LLM_RETRY_BASE_DELAY * (2 ** attempt)

            logging.warning(
                "http_post_json: HTTP %s on attempt %d/%d, retrying in %.1fs — %s",
                status, attempt + 1, LLM_MAX_RETRIES + 1, delay, body_text[:200],
            )
            time.sleep(delay)

        except urllib.error.URLError as e:
            last_exc = e
            if attempt >= LLM_MAX_RETRIES:
                break

            delay = LLM_RETRY_BASE_DELAY * (2 ** attempt)
            logging.warning(
                "http_post_json: network error on attempt %d/%d, retrying in %.1fs — %s",
                attempt + 1, LLM_MAX_RETRIES + 1, delay, e,
            )
            time.sleep(delay)

    raise RuntimeError(
        f"http_post_json failed after {LLM_MAX_RETRIES + 1} attempt(s)"
    ) from last_exc


def force_json_from_text(text: str):
    """Extract and parse a JSON object from a text string that may contain markdown fences.

    Args:
        text: Raw model output, optionally wrapped in ```json ... ``` fences.

    Returns:
        Parsed Python dict or list.

    Raises:
        ValueError: When no valid JSON object is found or parsing fails.
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t, flags=re.S)
        t = t.strip()

    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j != -1 and j > i:
        candidate = t[i : j + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            logging.error(
                "force_json_from_text: extracted JSON candidate is malformed "
                "(offset %d, line %d, col %d) — snippet: %.200r",
                e.pos, e.lineno, e.colno, candidate,
            )
            raise ValueError(
                f"Model returned malformed JSON (parse error at offset {e.pos}): {candidate[:200]!r}"
            ) from e

    try:
        return json.loads(t)
    except json.JSONDecodeError as e:
        logging.error(
            "force_json_from_text: no JSON object found in model output — "
            "raw text snippet: %.200r",
            t,
        )
        raise ValueError(
            f"Model returned no JSON object (raw response): {t[:200]!r}"
        ) from e


def chat_json(payload: dict) -> dict:
    """Send a chat-completions payload and return the parsed JSON content.

    Handles both Azure OpenAI response shapes and standard OpenAI choices format.

    Args:
        payload: Full request dict including messages, max_tokens, temperature, etc.

    Returns:
        Parsed dict from the model's JSON response content.

    Raises:
        RuntimeError: When the model returns empty content.
        ValueError: When the content cannot be parsed as JSON.
    """
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
    """Return the OpenAI response_format object for a full WpArticle JSON schema.

    Uses a strict json_schema when USE_JSON_SCHEMA is enabled, otherwise
    falls back to the looser json_object type.

    Args:
        target_words: Target article word count; used to compute minLength for contentHtml.

    Returns:
        Dict suitable for the response_format field of a chat-completions request.
    """
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
    """Return the first element of a SharePoint-style value list, or the payload itself."""
    if isinstance(payload, dict) and isinstance(payload.get("value"), list) and payload["value"]:
        return payload["value"][0]
    return payload


def extract_meta(item: dict) -> dict:
    """Normalize a raw SharePoint list item into the canonical article metadata dict."""
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
    """Strip HTML tags and return the whitespace-delimited word count."""
    txt = re.sub(r"<[^>]+>", " ", html or "")
    return len(re.findall(r"\S+", txt))


def count_tag(html: str, tag: str) -> int:
    """Count opening occurrences of a given HTML tag name in html."""
    return len(re.findall(fr"<{tag}\b", html or "", flags=re.I))


def has_blockquote(html: str) -> bool:
    """Return True when html contains a non-trivial <blockquote> element (12-400 chars of text)."""
    return bool(re.search(r"<blockquote>\s*[^<]{12,400}\s*</blockquote>", html or "", flags=re.I | re.S))


def normalize_lv_headings(html: str) -> str:
    """Replace common English heading phrases inside h2/h3 tags with Latvian equivalents."""
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
        (r"\bfaq(s)?\b(\s*:)?", "Biežāk uzdotie jautājumi"),
    ]

    def apply_map(s: str) -> str:
        out = s
        for pat, repl in patterns:
            out = re.sub(pat, repl, out, flags=re.I)
        return re.sub(r"  +", " ", out).strip()

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
    """Validate a generated article dict and return a list of human-readable issue strings.

    Checks word count, heading counts, list counts, blockquote presence, ROI signals,
    excerpt length, and focus keyword placement/density.

    Args:
        data: Article dict with keys title, seoSlug, excerpt, contentHtml, focusKeyword, etc.
        target_words: Expected word count (±15% tolerance applied).

    Returns:
        List of issue strings; empty list means the article passes all checks.
    """
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
        if keyword_density < KW_DENSITY_MIN_PCT:
            issues.append(f"Keyword blīvums pārāk zems ({keyword_density:.2f}% < {KW_DENSITY_MIN_PCT}%)")
        elif keyword_density > KW_DENSITY_MAX_PCT:
            issues.append(f"Keyword blīvums pārāk augsts ({keyword_density:.2f}% > {KW_DENSITY_MAX_PCT}%)")

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
    """Return the response_format object for the WpOutline JSON schema used in outline generation."""
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
    """Generate article outline: title, seoSlug, excerpt, introHtml, and H3 headings.

    Retries up to 4 times if the model returns fewer than 7 H3 headings.

    Args:
        meta: Normalized article metadata (primary, angle, audience, wpCategory, focusKeyword, etc.).
        target_words: Target word count; influences the requested outline depth.

    Returns:
        Dict with keys: title, seoSlug, excerpt, introHtml, h3, category, tags,
        tagSlugs, focusKeyword.

    Raises:
        RuntimeError: When all retry attempts fail or return too few headings.
    """
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
        "max_tokens": min(MAX_TOKENS_MAIN, OUTLINE_MAX_TOKENS),
        "temperature": 0.2,
        "response_format": outline_response_format(),
    }

    _MAX_ATTEMPTS = 4  # 1 initial + 3 retries
    last_exc: Exception = RuntimeError("generate_draft_outline: no attempts made")

    for attempt in range(_MAX_ATTEMPTS):
        try:
            data = chat_json(payload)
        except (ValueError, RuntimeError) as e:
            last_exc = e
            if attempt + 1 < _MAX_ATTEMPTS:
                delay = LLM_RETRY_BASE_DELAY * (2 ** attempt)
                logging.warning(
                    "generate_draft_outline: attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt + 1, _MAX_ATTEMPTS, e, delay,
                )
                time.sleep(delay)
            continue

        data["category"] = category
        data["tags"] = []
        data["tagSlugs"] = []
        data["focusKeyword"] = focus_keyword
        data["seoSlug"] = slugify(data.get("seoSlug") or meta.get("seoSlugHint") or data.get("title"))
        data["introHtml"] = sanitize_html(normalize_lv_headings(data.get("introHtml", "")))

        if not isinstance(data.get("h3"), list):
            data["h3"] = []
        if len(data["h3"]) < 7:
            last_exc = RuntimeError(
                f"Outline returned too few h3 headings ({len(data['h3'])} < 7)"
            )
            if attempt + 1 < _MAX_ATTEMPTS:
                delay = LLM_RETRY_BASE_DELAY * (2 ** attempt)
                logging.warning(
                    "generate_draft_outline: attempt %d/%d — %s, retrying in %.1fs",
                    attempt + 1, _MAX_ATTEMPTS, last_exc, delay,
                )
                time.sleep(delay)
            continue

        return data

    logging.error(
        "generate_draft_outline: all %d attempts failed — %s",
        _MAX_ATTEMPTS, last_exc,
    )
    raise RuntimeError(
        f"generate_draft_outline failed after {_MAX_ATTEMPTS} attempt(s): {last_exc}"
    ) from last_exc


def section_response_format(min_len: int = 1200) -> dict:
    """Return the response_format object for the WpSection JSON schema.

    Args:
        min_len: Minimum character length enforced on sectionHtml by the schema.

    Returns:
        Dict suitable for the response_format field of a chat-completions request.
    """
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
    """Generate sanitized HTML content for a single article section.

    Args:
        meta: Article metadata (primary, angle, audience, wpCategory, focusKeyword).
        h3_title: The H3 heading text for this section.
        target_words: Approximate target word count for the section.
        previous_sections: List of already-generated H3 heading titles (for deduplication).

    Returns:
        Sanitized HTML string for the section body (no enclosing <h3> tag).
    """
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
        "max_tokens": SECTION_MAX_TOKENS,
        "temperature": 0.7,
        "presence_penalty": 0.3,
        "frequency_penalty": 0.2,
        "response_format": section_response_format(min_len),
    }
    data = chat_json(payload)
    html = sanitize_html(normalize_lv_headings(data.get("sectionHtml", "")))
    return html


def topup_section_html(meta: dict, h3_title: str, deficit_words: int) -> str:
    """Generate additional HTML content to top up an under-length section.

    Args:
        meta: Article metadata (used for context but not reprompted).
        h3_title: The H3 heading of the section being extended.
        deficit_words: Approximate number of words still needed to reach the target.

    Returns:
        Sanitized HTML string with new paragraphs/lists to append to the existing section.
    """
    deficit = max(TOPUP_SECTION_MIN_DEFICIT, int(deficit_words))
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
        "max_tokens": TOPUP_MAX_TOKENS,
        "temperature": 0.7,
        "presence_penalty": 0.4,
        "frequency_penalty": 0.2,
        "response_format": section_response_format(min_len),
    }
    data = chat_json(payload)
    return sanitize_html(normalize_lv_headings(data.get("sectionHtml", "")))


def refine_response_format(target_words: int) -> dict:
    """Return the response_format object used during the full-article refinement call."""
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
    """Ask the LLM to refine a fully assembled article for flow, transitions, and keyword density.

    Falls back to the pre-refinement content if the LLM call fails or returns
    unparseable JSON.

    Args:
        meta: Article metadata including focusKeyword.
        title: Current article title.
        seo_slug: Current SEO slug.
        excerpt: Current excerpt.
        category: WordPress category string.
        tags: List of tag name strings.
        tag_slugs: Corresponding tag slug strings.
        content_html: Assembled HTML content to refine.
        target_words: Target word count for the final article.

    Returns:
        Refined article dict with keys: title, seoSlug, excerpt, category, tags,
        tagSlugs, contentHtml, focusKeyword.
    """
    current_words = count_words_from_html(content_html)
    needs_expansion = current_words < int(target_words * REFINE_EXPANSION_THRESHOLD)
    focus_keyword = meta.get("focusKeyword", "")

    focus_keyword_quality = ""
    if focus_keyword:
        keyword_count = content_html.lower().count(focus_keyword.lower())
        keyword_density = (keyword_count / max(1, current_words)) * 100
        ideal_min = int(current_words * KW_DENSITY_MIN_PCT / 100)
        ideal_max = int(current_words * KW_DENSITY_MAX_PCT / 100)

        if keyword_density < KW_DENSITY_MIN_PCT:
            density_action = f"PĀRĀK ZEMS ({keyword_density:.2f}%). Pievieno vēl {ideal_min - keyword_count} atkārtojumus organiski."
        elif keyword_density > KW_DENSITY_MAX_PCT:
            density_action = f"PĀRĀK AUGSTS ({keyword_density:.2f}%). Samazini par {keyword_count - ideal_max} — aizstāj ar sinonīmiem vai pārfrāzē."
        else:
            density_action = f"LABI ({keyword_density:.2f}%). Nemainīt."

        focus_keyword_quality = (
            f"\nFOCUS KEYWORD KVALITĀTE: '{focus_keyword}'"
            f"\n- Pašreizējais: {keyword_count}× ({keyword_density:.2f}%), mērķis: {ideal_min}-{ideal_max}× (1-1.5%)"
            f"\n- DARBĪBA: {density_action}"
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
    except ValueError as e:
        logging.warning(
            "refine_full_article: model returned unparseable JSON, falling back to pre-refinement content — %s",
            e,
        )
        data = pre
    except RuntimeError as e:
        logging.warning(
            "refine_full_article: LLM call failed, falling back to pre-refinement content — %s",
            e,
        )
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
    """Replace article tags with whitelisted tags from the anchor map.

    Loads cfg_dir/tags.json (whitelist) and cfg_dir/anchor_map.json (slug → tag slugs).
    When ANCHOR_STRICT=1 (default) and no anchor match is found, tags are cleared.

    Args:
        data: Article dict to mutate in-place (tags, tagSlugs keys updated).
        meta: Article metadata containing seoSlugHint or SeoSlug for anchor lookup.

    Returns:
        The mutated data dict.
    """
    import json

    def _load_tag_whitelist() -> dict[str, str]:
        path = os.getenv("TAG_WHITELIST_PATH", os.path.join(os.getcwd(), "cfg_dir", "tags.json"))
        try:
            with open(path, "r", encoding="utf-8") as f:
                items = json.load(f)
            return {i["slug"]: i["name"] for i in items if i.get("slug") and i.get("name")}
        except Exception as e:
            logging.debug(f"[tags.json] not found (optional): {e}")
            return {}

    def _load_anchor_map() -> dict[str, list[str]]:
        path = os.getenv("ANCHOR_MAP_PATH", os.path.join(os.getcwd(), "cfg_dir", "anchor_map.json"))
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            return {k.strip().lower(): (v if isinstance(v, list) else [v]) for k, v in obj.items()}
        except Exception as e:
            logging.debug(f"[anchor_map.json] not found (optional): {e}")
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


_WP_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
_WP_MAX_RETRIES = int(os.getenv("WP_MAX_RETRIES", "3"))
_WP_RETRY_BASE_DELAY = float(os.getenv("WP_RETRY_BASE_DELAY", "1.0"))


def _wp_auth_headers() -> dict:
    import base64

    scheme = (os.getenv("WP_AUTH_SCHEME", "jwt") or "jwt").lower()
    if scheme == "basic":
        b64 = os.getenv("WP_BASIC_AUTH_B64")
        if not b64:
            user = os.getenv("WP_USER", "")
            app_pw = os.getenv("WP_APP_PASSWORD", "")
            missing = [name for name, val in (("WP_USER", user), ("WP_APP_PASSWORD", app_pw)) if not val]
            if missing:
                raise RuntimeError(
                    f"WP basic-auth credentials missing: {', '.join(missing)}. "
                    "Set WP_BASIC_AUTH_B64 or both WP_USER and WP_APP_PASSWORD."
                )
            b64 = base64.b64encode(f"{user}:{app_pw}".encode("utf-8")).decode("utf-8")
        return {"Authorization": f"Basic {b64}", "Content-Type": "application/json"}
    token = os.getenv("WP_TOKEN", "")
    if not token:
        raise RuntimeError(
            "WP JWT token missing: set the WP_TOKEN environment variable. "
            "To use basic auth instead, set WP_AUTH_SCHEME=basic."
        )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _wp_call(method: str, url: str, *, headers: dict, timeout: int = 15, **kwargs) -> requests.Response:
    """
    Execute a WP REST API request with exponential backoff for transient failures
    (429, 500, 502, 503, 504). Always returns the Response object — the caller decides
    what to do with non-ok responses. Raises RuntimeError only when a network-level
    error persists across all retries.
    """
    last_exc: Exception = RuntimeError("_wp_call: no attempts made")

    for attempt in range(_WP_MAX_RETRIES + 1):
        try:
            r = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt >= _WP_MAX_RETRIES:
                break
            delay = _WP_RETRY_BASE_DELAY * (2 ** attempt)
            logging.warning(
                "_wp_call: network error on attempt %d/%d, retrying in %.1fs — %s",
                attempt + 1, _WP_MAX_RETRIES + 1, delay, e,
            )
            time.sleep(delay)
            continue

        if r.status_code not in _WP_TRANSIENT_STATUS_CODES:
            return r  # 2xx success or non-retryable error — caller handles it

        # Transient: log and retry
        last_exc = RuntimeError(f"WP API HTTP {r.status_code}")
        if attempt >= _WP_MAX_RETRIES:
            return r  # Exhausted — return final response for caller to log and raise

        try:
            delay = float(r.headers.get("Retry-After") or 0)
        except Exception:
            delay = 0.0
        if not (delay > 0):
            delay = _WP_RETRY_BASE_DELAY * (2 ** attempt)

        logging.warning(
            "_wp_call: HTTP %s on attempt %d/%d, retrying in %.1fs — %s %s",
            r.status_code, attempt + 1, _WP_MAX_RETRIES + 1, delay, method, url,
        )
        time.sleep(delay)

    raise RuntimeError(
        f"_wp_call: network failure after {_WP_MAX_RETRIES + 1} attempt(s)"
    ) from last_exc


def create_or_get_wp_tag(api_base: str, *, name: str, slug: str) -> int:
    """Look up a WordPress tag by slug; create it if it does not exist.

    Handles a race condition where the tag is created between GET and POST
    by catching term_exists errors and returning the existing term ID.

    Args:
        api_base: WordPress REST API base URL (e.g. https://ksj.lv/wp-json).
        name: Human-readable tag name.
        slug: URL slug for the tag.

    Returns:
        WordPress tag ID (integer).

    Raises:
        RuntimeError: When the GET or POST request fails with a non-recoverable error.
    """
    headers = _wp_auth_headers()

    # 1. Look up existing tag by slug
    r = _wp_call("GET", f"{api_base}/wp/v2/tags", headers=headers, params={"slug": slug})
    if not r.ok:
        raise RuntimeError(
            f"WP tag GET failed: HTTP {r.status_code} — {r.text[:400]}"
        )
    arr = r.json()
    if arr:
        tag = arr[0]
        if tag.get("name") != name:
            r_upd = _wp_call(
                "POST", f"{api_base}/wp/v2/tags/{tag['id']}",
                headers=headers, json={"name": name},
            )
            if not r_upd.ok:
                logging.warning(
                    "create_or_get_wp_tag: name update for tag %d ('%s' → '%s') "
                    "failed HTTP %s — %s",
                    tag["id"], tag.get("name"), name, r_upd.status_code, r_upd.text[:200],
                )
        return tag["id"]

    # 2. Create new tag
    r = _wp_call("POST", f"{api_base}/wp/v2/tags", headers=headers, json={"name": name, "slug": slug})
    if r.ok:
        return r.json()["id"]

    # Handle race condition: tag was created between our GET and POST
    try:
        err = r.json()
        if err.get("code") == "term_exists":
            term_id = err["data"]["term_id"]
            logging.info(
                "create_or_get_wp_tag: '%s' (%s) already existed (race condition), term_id=%d",
                name, slug, term_id,
            )
            return term_id
    except Exception:
        pass

    raise RuntimeError(f"WP tag create failed: HTTP {r.status_code} — {r.text[:400]}")


def ensure_wp_tag_ids(api_base: str, token: str, *, names: list[str], slugs: list[str]) -> list[int]:
    """Resolve or create WordPress tags for each name/slug pair and return their IDs.

    Individual tag failures are logged and skipped; the remaining IDs are returned.

    Args:
        api_base: WordPress REST API base URL.
        token: JWT bearer token (currently unused directly; auth via _wp_auth_headers).
        names: List of tag name strings.
        slugs: Corresponding list of tag slug strings.

    Returns:
        List of WordPress tag IDs (may be shorter than names if some tags failed).
    """
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
    """Compute per-section target word count given total target and number of sections.

    Reserves SECTION_INTRO_SHARE of total words for the intro, then distributes
    the remainder across sections with a SECTION_WORD_BUFFER multiplier.

    Args:
        total_words: Overall article target word count.
        num_sections: Number of H3 sections to distribute words across.

    Returns:
        Target word count per section (at least SECTION_MIN_WORDS).
    """
    intro_share = SECTION_INTRO_SHARE
    remaining = total_words * (1 - intro_share)
    buffer = SECTION_WORD_BUFFER
    return max(SECTION_MIN_WORDS, int((remaining * buffer) / max(6, num_sections)))


def get_dynamic_max_tokens(target_words: int) -> int:
    """Compute max_tokens for the LLM call scaled to the target word count.

    Adds DYNAMIC_TOKENS_PER_1K_WORDS extra tokens per 1000 target words
    and caps at GPT4O_MAX_OUTPUT_TOKENS.

    Args:
        target_words: Target article word count.

    Returns:
        Max output tokens to request from the model.
    """
    base_tokens = DYNAMIC_TOKENS_BASE
    additional_per_thousand = DYNAMIC_TOKENS_PER_1K_WORDS
    extra_tokens = (target_words // 1000) * additional_per_thousand
    # Azure OpenAI GPT-4o max output is GPT4O_MAX_OUTPUT_TOKENS tokens
    return min(GPT4O_MAX_OUTPUT_TOKENS, base_tokens + extra_tokens)


def ensure_length_progress(current_html: str, target_words: int, phase: str) -> str:
    """Log a warning when mid-process word count is below the minimum progress ratio.

    Currently a no-op transformation — returns current_html unchanged.

    Args:
        current_html: Assembled HTML so far.
        target_words: Final target word count.
        phase: Pipeline phase label (e.g. "mid_process").

    Returns:
        current_html unchanged.
    """
    current_words = count_words_from_html(current_html)
    progress_ratio = current_words / target_words

    if progress_ratio < PROGRESS_MIN_RATIO and phase == "mid_process":
        additional_target = target_words - current_words
        logging.info(f"Garuma progress tikai {progress_ratio:.1%}, nepieciešami papildus {additional_target} vārdi")

    return current_html


def needs_aggressive_topup(html: str, h3_title: str) -> bool:
    """Return True when a section is structurally too weak to publish as-is.

    Triggers when word count is below SECTION_TOPUP_MIN_WORDS, when a list is
    absent and words are below SECTION_TOPUP_NO_LIST_MIN, or when ROI data is
    missing from a section whose heading implies ROI content.

    Args:
        html: Current section HTML.
        h3_title: Section heading text (used for ROI heuristic).

    Returns:
        True when an aggressive top-up should be attempted.
    """
    word_count = count_words_from_html(html)
    has_list = count_tag(html, "ul") + count_tag(html, "ol") > 0
    has_roi = bool(re.search(r"(\d+\s?%|€|\bstund|\bmin|\beiro|\bEUR)", html, flags=re.I))

    if word_count < SECTION_TOPUP_MIN_WORDS:
        return True
    if not has_list and word_count < SECTION_TOPUP_NO_LIST_MIN:
        return True
    if not has_roi and any(word in h3_title.lower() for word in ['ieguvums', 'roi', 'ietaupījums', 'efektivitāte']):
        return True

    return False


def validate_section_structure(html: str, h3_title: str) -> List[str]:
    """Check a section's HTML against minimum structural requirements.

    Args:
        html: Section HTML content.
        h3_title: Section heading text (for contextual error messages).

    Returns:
        List of issue strings; empty list means the section passes all checks.
    """
    issues = []
    if count_words_from_html(html) < SECTION_VALIDATE_MIN_WORDS:
        issues.append(f"Sadaļa pārāk īsa (<{SECTION_VALIDATE_MIN_WORDS} vārdi)")
    if count_tag(html, "p") < SECTION_VALIDATE_MIN_PARAS:
        issues.append(f"Vajag vismaz {SECTION_VALIDATE_MIN_PARAS} rindkopas")
    if (count_tag(html, "ul") + count_tag(html, "ol")) < 1:
        issues.append("Trūkst saraksta (ul/ol)")
    if not re.search(r"\b(piemēr|scenārij|solis|darbīb|iestatījum|konfigurē)\w*\b", html, re.I):
        issues.append("Trūkst konkrētu piemēru/soļu")
    if not re.search(r"(\d+\s?%|\d+\s?€|\d+\s?(stund|minūt|mēnes|gad))", html):
        issues.append("Trūkst kvantitatīvu datu (%, €, laiks)")
    return issues


def generate_aggressive_section(meta: dict, h3_title: str, target_words: int, previous_issues: List[str], previous_sections: list[str] | None = None) -> str:
    """Retry section generation with an explicit failure list and mandatory structure prompt.

    Used as the final fallback inside generate_section_html_with_validation when
    the normal generator repeatedly fails validation.

    Args:
        meta: Article metadata.
        h3_title: Section heading text.
        target_words: Target word count for the section.
        previous_issues: List of validation issue strings from earlier attempts.
        previous_sections: List of already-generated H3 heading titles for context.

    Returns:
        Sanitized HTML string for the section body.
    """
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
        "max_tokens": SECTION_MAX_TOKENS,
        "temperature": 0.3,
        "response_format": section_response_format(target_words),
    }

    data = chat_json(payload)
    html = sanitize_html(normalize_lv_headings(data.get("sectionHtml", "")))
    return html


def generate_section_html_with_validation(meta: dict, h3_title: str, target_words: int, max_retries: int = 2, previous_sections: list[str] | None = None) -> str:
    """Generate a section, validate it, and retry with aggressive prompting if needed.

    Runs up to max_retries normal attempts; on the final attempt delegates to
    generate_aggressive_section with the accumulated validation issues.

    Args:
        meta: Article metadata.
        h3_title: Section heading text.
        target_words: Target word count for the section.
        max_retries: Number of normal generation attempts before aggressive fallback.
        previous_sections: List of already-generated H3 heading titles for context.

    Returns:
        Best-effort sanitized HTML string for the section body.
    """
    for attempt in range(max_retries):
        html = generate_section_html(meta, h3_title, target_words, previous_sections=previous_sections)
        issues = validate_section_structure(html, h3_title)

        if not issues:
            return html

        logging.warning(f"Sadaļa '{h3_title}' neatbilst prasībām (mēģinājums {attempt+1}): {issues}")

        if attempt == max_retries - 1:
            return generate_aggressive_section(meta, h3_title, target_words, issues, previous_sections=previous_sections)

    return html


ARTICLE_MODE = (os.getenv("ARTICLE_MODE", "mega") or "mega").strip().lower()

# =============================================================================
# Plan B: Research phase — SERP data enrichment before article generation
# =============================================================================

def research_topic(meta: dict) -> dict:
    """
    Research the topic before writing using SerpApi.
    Returns structured research context to inject into prompts.
    Uses 2 SerpApi calls: one English (Microsoft docs), one Latvian (competitors).
    Returns empty dict if SerpApi unavailable or quota exceeded.
    """
    primary = (meta.get("primary") or "").strip()
    angle = (meta.get("angle") or "").strip()
    focus_keyword = (meta.get("focusKeyword") or "").strip()

    if not primary and not focus_keyword:
        return {}

    research = {
        "en_titles": [],        # English competitor article titles
        "en_links": [],         # Microsoft docs links
        "paa": [],              # People Also Ask questions
        "lv_titles": [],        # Latvian competitor titles
        "lv_snippets": [],      # Latvian competitor snippets
        "related_searches": [], # Related Google searches
    }

    # ── Query 1: English search (Microsoft docs + best practices) ─────────
    en_query = f"{primary} {angle} Microsoft 365 best practices".strip()
    if len(en_query) > 100:
        en_query = f"{primary} best practices"

    try:
        en_serp = serp_search_cached(en_query, ttl=DEFAULT_TTL * 2)
        if en_serp:
            for r in en_serp.get("organic_results", [])[:5]:
                title = (r.get("title") or "").strip()
                link = (r.get("link") or "").strip()
                if title:
                    research["en_titles"].append(title)
                if link and "microsoft.com" in link:
                    research["en_links"].append(link)

            for q in en_serp.get("related_questions", [])[:6]:
                question = (q.get("question") or "").strip()
                if question:
                    research["paa"].append(question)

            for rs in en_serp.get("related_searches", [])[:4]:
                query_text = (rs.get("query") or "").strip()
                if query_text:
                    research["related_searches"].append(query_text)

            logging.info(
                f"[research] EN: {len(research['en_titles'])} titles, "
                f"{len(research['paa'])} PAA, {len(research['en_links'])} MS links"
            )
    except Exception as e:
        logging.warning(f"[research] EN search failed: {e}")

    # ── Query 2: Latvian search (local competitors) ───────────────────────
    lv_query = f"{primary} {focus_keyword}".strip()
    if not lv_query or lv_query == primary:
        lv_query = f"{focus_keyword} praktiskie soļi" if focus_keyword else f"{primary} ieviešana"

    try:
        lv_serp = serp_search_cached(lv_query, ttl=DEFAULT_TTL * 2)
        if lv_serp:
            for r in lv_serp.get("organic_results", [])[:5]:
                title = (r.get("title") or "").strip()
                snippet = (r.get("snippet") or "").strip()
                if title:
                    research["lv_titles"].append(title)
                if snippet:
                    research["lv_snippets"].append(f"{title}: {snippet}" if title else snippet)

            # Also grab PAA from Latvian results if any
            for q in lv_serp.get("related_questions", [])[:4]:
                question = (q.get("question") or "").strip()
                if question and question not in research["paa"]:
                    research["paa"].append(question)

            logging.info(
                f"[research] LV: {len(research['lv_titles'])} titles, "
                f"{len(research['lv_snippets'])} snippets"
            )
    except Exception as e:
        logging.warning(f"[research] LV search failed: {e}")

    total_data = sum(len(v) for v in research.values())
    logging.info(f"[research] Total research data points: {total_data}")
    return research


def format_research_for_outline(research: dict) -> str:
    """Format research data as a prompt section for the outline call."""
    if not research or not any(research.values()):
        return ""

    parts = []

    if research.get("paa"):
        parts.append("GOOGLE 'PEOPLE ALSO ASK' JAUTĀJUMI (jāatbild rakstā):")
        for q in research["paa"][:6]:
            parts.append(f"  • {q}")

    if research.get("en_titles"):
        parts.append("\nKONKURENTU RAKSTI ANGLISKI (jāpārspēj ar labāku saturu):")
        for t in research["en_titles"][:5]:
            parts.append(f"  • {t}")

    if research.get("lv_titles"):
        parts.append("\nLATVIESKI KONKURENTI (jāpiedāvā kas unikāls):")
        for t in research["lv_titles"][:3]:
            parts.append(f"  • {t}")

    if research.get("related_searches"):
        parts.append("\nSAISTĪTIE MEKLĒJUMI (var izmantot kā apakštēmas):")
        for s in research["related_searches"][:4]:
            parts.append(f"  • {s}")

    return "\n".join(parts)


def format_research_for_batch(research: dict, batch_h3: list[str]) -> str:
    """Format relevant research data for a specific batch of sections."""
    if not research or not any(research.values()):
        return ""

    parts = []

    # Include PAA questions that might relate to this batch's topics
    if research.get("paa"):
        parts.append("Google jautājumi, uz kuriem šīm sadaļām jāatbild:")
        for q in research["paa"][:4]:
            parts.append(f"  • {q}")

    if research.get("en_links"):
        parts.append(f"\nMicrosoft dokumentācija atsaucēm: {', '.join(research['en_links'][:2])}")

    return "\n".join(parts) if parts else ""


# =============================================================================
# MEGA-PROMPT: hybrid article generation (Plan C v2 + Plan B research)
# Phase 0: research_topic() — 2 SerpApi calls
# Phase 1: outline + meta + intro (1 GPT call)
# Phase 2: sections in batches of 3-4 (2-3 GPT calls)
# Phase 3: assembly + keyword safety net
# Total: 2 SerpApi + 3-5 GPT calls
# =============================================================================

MEGA_SYSTEM_PROMPT = (
    "Tu esi Kaspars Jurjāns — Latvijas vadošais SharePoint un Microsoft 365 konsultants "
    "(15+ gadu pieredze, 200+ veiksmīgi projekti). Tu raksti padziļinātus B2B tehniskus "
    "rakstus latviešu valodā.\n\n"
    "AUDITORIJA: IT vadītāji un biznesa procesu īpašnieki Latvijā un Baltijā "
    "(uzņēmumi ar 50-500 darbiniekiem).\n\n"
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
    "FORMATĒŠANA: Atļauts tikai <h2>,<h3>,<p>,<ul>,<ol>,<li>,<strong>,<em>,<code>,<pre>,<blockquote>,<br>.\n\n"
    "ATBILDI TIKAI AR DERĪGU JSON."
)

MEGA_OUTLINE_USER = (
    "Tēma: {primary}\n"
    "Leņķis: {angle}\n"
    "Auditorija: {audience}\n"
    "Titula hints: {titleHint}\n"
    "Kategorija: {wpCategory}\n"
    "Mērķa garums: {targetWords} vārdi\n\n"
    "⚠️ FOCUS KEYWORD (BURTISKI, NEMAINĪT): '{focusKeyword}'\n"
    "Šī frāze jāizmanto TIEŠI TĀ KĀ RAKSTĪTA — nedrīkst mainīt vārdu secību, "
    "pievienot vai noņemt vārdus, vai aizstāt ar sinonīmiem.\n\n"
    "FOCUS KEYWORD OBLIGĀTĀ INTEGRĀCIJA:\n"
    "• TITLE: JĀSĀKAS ar '{focusKeyword}' (pēc tam var turpināt ar ':' vai skaitli). "
    "Piemērs: '{focusKeyword}: 7 soļi veiksmīgai ieviešanai'\n"
    "• EXCERPT: Pirmajam teikumam JĀSĀKAS ar '{focusKeyword}'\n"
    "• SEO SLUG: JĀSATUR '{focusKeywordSlug}'\n"
    "• INTRO HTML: Pirmās <p> pirmajam teikumam JĀSĀKAS ar '{focusKeyword}'\n"
    "• H3: Vismaz 2 virsrakstiem jāsatur '{focusKeyword}' vai tā daļa\n\n"
    "{researchContext}"
    "UZDEVUMS: Izveido raksta plānu un ievadu.\n"
    "Raksta plānam JĀATBILD uz Google PAA jautājumiem (ja norādīti) un "
    "JĀPIEDĀVĀ unikāls saturs, ko konkurenti neapraksta.\n\n"
    "JSON shēma:\n"
    '{{\n'
    '  "title": "SĀKAS ar \'{focusKeyword}\', max 60 rakstz., satur skaitli",\n'
    '  "seoSlug": "SATUR \'{focusKeywordSlug}\', ascii-lowercase",\n'
    '  "excerpt": "SĀKAS ar \'{focusKeyword}\', max 160 rakstz.",\n'
    '  "introHtml": "<h2>Ievads</h2><p>{focusKeyword} ir... (150-200 vārdi)</p>",\n'
    '  "h3": ["8 konkrēti virsraksti, vismaz 2 satur \'{focusKeyword}\' vai tā daļu"],\n'
    '  "category": "{wpCategory}",\n'
    '  "tags": ["3-6 latviski termini"],\n'
    '  "tagSlugs": ["ascii-slug"],\n'
    '  "focusKeyword": "{focusKeyword}"\n'
    '}}'
)

MEGA_BATCH_USER = (
    "Raksta konteksts:\n"
    "Tēma: {primary} | Leņķis: {angle}\n"
    "Raksta virsraksts: {title}\n\n"
    "⚠️ FOCUS KEYWORD (BURTISKI, NEMAINĪT): '{focusKeyword}'\n"
    "OBLIGĀTI:\n"
    "• Vismaz 1 no šīs grupas <h3> virsrakstiem JĀSATUR frāzi '{focusKeyword}'\n"
    "• Katrā sadaļas HTML tekstā frāze '{focusKeyword}' jāparādās VISMAZ 1 reizi (organiski teikumā)\n"
    "• Kopējais keyword blīvums visā rakstā: 1-1.5%\n\n"
    "{researchContext}"
    "Jau uzrakstītās sadaļas (neatkārto!):\n{previousContent}\n\n"
    "UZDEVUMS: Uzraksti PILNĀ DETALIZĀCIJĀ šīs {batchCount} sadaļas:\n{batchSections}\n\n"
    "⚠️ KATRAS SADAĻAS OBLIGĀTĀ STRUKTŪRA (VISMAZ {wordsPerSection} vārdi):\n"
    "1. Ievada rindkopa: kāpēc šī tēma svarīga — ar konkrētu problēmu (3-4 teikumi)\n"
    "2. Praktiskie soļi: 2-3 rindkopas ar Microsoft 365 UI terminus un konfigurācijas aprakstiem\n"
    "3. Saraksts: <ul> vai <ol> ar 4-6 detalizētiem punktiem (katrs punkts 1-2 teikumi)\n"
    "4. ROI rindkopa: konkrēti skaitļi kā diapazoni ar kontekstu\n"
    "5. Pārejas teikums uz nākamo sadaļu\n\n"
    "NEDRĪKST: saīsināt sadaļu līdz 1-2 rindkopām. Katra sadaļa ir PILNS, detalizēts apraksts.\n\n"
    "Atgriez JSON:\n"
    '{{\n'
    '  "sections": [\n'
    '    {{"h3": "virsraksts (vismaz 1 satur \'{focusKeyword}\')", "html": "<p>...{focusKeyword}...</p><p>soļi...</p><ul><li>...</li></ul><p>ROI...</p>"}}\n'
    '  ]\n'
    '}}'
)


def _summarize_previous(intro_html: str, sections: list[dict], max_chars: int = SUMMARIZE_PREVIOUS_MAX_CHARS) -> str:
    """Build a compact summary of already-written content for context."""
    parts = []
    if intro_html:
        text = re.sub(r'<[^>]+>', ' ', intro_html).strip()
        words = text.split()[:30]
        parts.append(f"Ievads: {' '.join(words)}...")
    for s in sections:
        text = re.sub(r'<[^>]+>', ' ', s.get('html', '')).strip()
        words = text.split()[:20]
        parts.append(f"<{s['h3']}>: {' '.join(words)}...")
    summary = "\n".join(parts)
    return summary[:max_chars] if len(summary) > max_chars else summary


def build_wp_article_mega(meta: dict, target_words: int) -> dict:
    """
    Hybrid article generation: outline (1 call) + sections in batches (2-3 calls).
    Total: 3-5 API calls with full cross-section context.
    """
    MEGA_MAX_WORDS = int(os.getenv("MEGA_MAX_WORDS", "4000"))
    if target_words > MEGA_MAX_WORDS:
        logging.info(f"[mega] Capping target from {target_words} to {MEGA_MAX_WORDS}")
        target_words = MEGA_MAX_WORDS

    # GPT typically produces 70-85% of requested words; inflate by 40% so output
    # lands above the user's actual target (e.g., 2000 → 2800 → output ~2000+)
    original_target = target_words
    target_words = int(target_words * MEGA_WORD_INFLATION)
    if target_words > MEGA_MAX_WORDS:
        target_words = MEGA_MAX_WORDS
    logging.info(f"[mega] Target inflated: {original_target} → {target_words} words")

    focus_keyword = meta.get("focusKeyword", "") or ""
    title_hint = meta.get("titleHint", "") or ""
    max_tokens = get_dynamic_max_tokens(target_words)

    # ── Auto-generate focusKeyword if missing ─────────────────────────────
    if not focus_keyword:
        # Best source: the primary topic field IS the focus keyword in most cases
        primary = (meta.get("primary") or "").strip()
        if primary:
            focus_keyword = primary
            meta["focusKeyword"] = focus_keyword
            logging.info(f"[mega] focusKeyword from primary: '{focus_keyword}'")
        else:
            # Fallback: try KeywordExtractor
            logging.info("[mega] focusKeyword is empty, auto-generating via KeywordExtractor")
            try:
                kw_result = generate_keywords_from_input(
                    {
                        "primary": meta.get("primary", ""),
                        "angle": meta.get("angle", ""),
                        "audience": meta.get("audience", ""),
                        "wpCategory": meta.get("wpCategory", ""),
                        "tagsCsv": meta.get("tagsCsv", ""),
                        "SeoSlug": meta.get("seoSlugHint", ""),
                        "combokey": meta.get("primary", ""),
                    },
                    limit=5,
                    use_llm=True,
                    enrich=True,
                )
                if isinstance(kw_result, dict):
                    focus_keyword = (
                        kw_result.get("top_recommendation")
                        or (kw_result.get("candidates", [{}])[0].get("phrase") if kw_result.get("candidates") else "")
                        or ""
                    )
                if focus_keyword:
                    meta["focusKeyword"] = focus_keyword
                    logging.info(f"[mega] Auto-generated focusKeyword: '{focus_keyword}'")
            except Exception as e:
                logging.warning(f"[mega] Auto keyword generation failed: {e}")

    # ── Phase 0: Research ─────────────────────────────────────────────────
    research = {}
    try:
        research = research_topic(meta)
    except Exception as e:
        logging.warning(f"[mega] Research phase failed (continuing without): {e}")

    research_outline_text = format_research_for_outline(research)
    if research_outline_text:
        research_outline_text += "\n\n"
    research_batch_text = format_research_for_batch(research, [])
    if research_batch_text:
        research_batch_text += "\n\n"

    # ── Phase 1: Outline + intro ──────────────────────────────────────────
    logging.info(f"[mega] Phase 1: Generating outline, target={target_words}, research={bool(research)}")
    outline_msg = MEGA_OUTLINE_USER.format(
        primary=meta.get("primary", ""),
        angle=meta.get("angle", ""),
        audience=meta.get("audience", ""),
        titleHint=title_hint,
        focusKeyword=focus_keyword,
        focusKeywordSlug=slugify(focus_keyword) if focus_keyword else "",
        wpCategory=meta.get("wpCategory", "SharePoint"),
        targetWords=target_words,
        researchContext=research_outline_text,
    )

    outline_data = chat_json({
        "messages": [
            {"role": "system", "content": MEGA_SYSTEM_PROMPT},
            {"role": "user", "content": outline_msg},
        ],
        "max_tokens": OUTLINE_MAX_TOKENS,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    })

    title = outline_data.get("title") or title_hint or "SharePoint risinājumi"
    intro_html = outline_data.get("introHtml") or "<h2>Ievads</h2><p>-</p>"
    h3_list = [h.strip() for h in outline_data.get("h3", []) if isinstance(h, str) and h.strip()]

    if len(h3_list) < MEGA_OUTLINE_H3_MIN:
        h3_list = [
            "Biznesa vajadzības un mērķi",
            "Informācijas arhitektūra",
            "Automatizācija ar Power Automate",
            "Drošība un piekļuves kontrole",
            "Datu kvalitāte un metadati",
            "Izvietošana un pārvaldība",
            "Mērījumi un ROI",
            "Nākamie soļi",
        ]

    logging.info(f"[mega] Outline: title='{title[:50]}...', {len(h3_list)} sections")

    # ── Phase 2: Generate sections in batches ─────────────────────────────
    BATCH_SIZE = int(os.getenv("MEGA_BATCH_SIZE", "4"))
    # Ask for 1.5x words per section — GPT consistently underdelivers in JSON mode
    words_per_section = max(MEGA_SECTION_MIN_WORDS, int((target_words / len(h3_list)) * MEGA_SECTION_WORD_MULTIPLIER))
    all_sections: list[dict] = []

    for batch_start in range(0, len(h3_list), BATCH_SIZE):
        batch_h3 = h3_list[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1

        prev_summary = _summarize_previous(intro_html, all_sections)
        batch_sections_text = "\n".join(
            f"  {i+1}. <h3>{h}</h3>" for i, h in enumerate(batch_h3)
        )

        batch_msg = MEGA_BATCH_USER.format(
            primary=meta.get("primary", ""),
            angle=meta.get("angle", ""),
            focusKeyword=focus_keyword,
            title=title,
            researchContext=research_batch_text,
            previousContent=prev_summary or "Šī ir pirmā sadaļu grupa.",
            batchCount=len(batch_h3),
            batchSections=batch_sections_text,
            wordsPerSection=words_per_section,
        )

        logging.info(f"[mega] Phase 2, batch {batch_num}: generating {len(batch_h3)} sections ({', '.join(h[:30] for h in batch_h3)})")

        batch_data = chat_json({
            "messages": [
                {"role": "system", "content": MEGA_SYSTEM_PROMPT},
                {"role": "user", "content": batch_msg},
            ],
            "max_tokens": min(max_tokens, MEGA_BATCH_MAX_TOKENS),
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
        })

        # Parse batch response
        raw_sections = batch_data.get("sections", [])
        if not raw_sections and batch_data.get("sectionHtml"):
            # Fallback: single section returned
            raw_sections = [{"h3": batch_h3[0], "html": batch_data["sectionHtml"]}]

        if len(raw_sections) > len(batch_h3):
            logging.warning(
                "[mega] batch %d: model returned %d sections but %d were requested — "
                "stripping %d hallucinated extra section(s)",
                batch_num, len(raw_sections), len(batch_h3), len(raw_sections) - len(batch_h3),
            )
            raw_sections = raw_sections[: len(batch_h3)]

        for idx, sec in enumerate(raw_sections):
            h3 = sec.get("h3") or batch_h3[idx]
            html = sec.get("html") or sec.get("sectionHtml") or ""
            if html.strip():
                all_sections.append({"h3": h3, "html": html})
                words = count_words_from_html(html)
                logging.info(f"[mega]   Section '{h3[:40]}': {words} words")

        # If batch returned fewer sections than requested, generate missing ones individually
        if len(raw_sections) < len(batch_h3):
            for missing_idx in range(len(raw_sections), len(batch_h3)):
                missing_h3 = batch_h3[missing_idx]
                logging.warning(f"[mega]   Missing section '{missing_h3}', generating individually")
                try:
                    prev_summary = _summarize_previous(intro_html, all_sections)
                    individual_msg = MEGA_BATCH_USER.format(
                        primary=meta.get("primary", ""),
                        angle=meta.get("angle", ""),
                        focusKeyword=focus_keyword,
                        title=title,
                        researchContext=research_batch_text,
                        previousContent=prev_summary,
                        batchCount=1,
                        batchSections=f"  1. <h3>{missing_h3}</h3>",
                        wordsPerSection=words_per_section,
                    )
                    ind_data = chat_json({
                        "messages": [
                            {"role": "system", "content": MEGA_SYSTEM_PROMPT},
                            {"role": "user", "content": individual_msg},
                        ],
                        "max_tokens": OUTLINE_MAX_TOKENS,
                        "temperature": 0.4,
                        "response_format": {"type": "json_object"},
                    })
                    ind_sections = ind_data.get("sections", [])
                    if ind_sections:
                        html = ind_sections[0].get("html") or ""
                        if html.strip():
                            all_sections.append({"h3": missing_h3, "html": html})
                except Exception as e:
                    logging.warning(f"[mega]   Individual generation failed for '{missing_h3}': {e}")

    # ── Phase 3: Assembly ─────────────────────────────────────────────────
    content_parts = [intro_html.strip()] if intro_html else []
    for s in all_sections:
        content_parts.append(f"<h3>{s['h3']}</h3>\n{s['html']}")
    content_html = sanitize_html(normalize_lv_headings("\n\n".join(content_parts)))

    # ── Keyword safety net ────────────────────────────────────────────────
    # Fires ONLY when density is near-zero (<0.3%) — i.e. GPT completely
    # ignored keyword instructions. Normal 1-1.5% density is handled by
    # prompt-layer instructions (MEGA_OUTLINE_USER, MEGA_BATCH_USER).
    # Maximum 3 injections total to avoid over-stuffing.
    if focus_keyword:
        kw_lower = focus_keyword.lower()
        plain_text = re.sub(r'<[^>]+>', ' ', content_html)
        total_words_est = len(plain_text.split())
        current_count = plain_text.lower().count(kw_lower)
        density = (current_count / max(1, total_words_est)) * 100

        logging.info(
            "[mega] Keyword '%s': %d occurrence(s), %.2f%% density (%d words)",
            focus_keyword, current_count, density, total_words_est,
        )

        if density < KW_SAFETY_NET_DENSITY_PCT:
            logging.warning(
                "[mega] Safety net triggered: density %.2f%% < %.1f%% — applying max %d injections",
                density, KW_SAFETY_NET_DENSITY_PCT, KW_SAFETY_NET_MAX_INJECTIONS,
            )
            injected = 0

            # Injection 1: keyword paragraph immediately after first </h2>
            if injected < KW_SAFETY_NET_MAX_INJECTIONS:
                kw_sentence = (
                    f"<p><strong>{focus_keyword}</strong> ir viena no svarīgākajām tēmām, "
                    f"ko organizācijas risina, lai uzlabotu savu digitālo infrastruktūru.</p>"
                )
                h2_pattern = re.compile(r'(</h2>)', re.IGNORECASE)
                if h2_pattern.search(content_html):
                    content_html = h2_pattern.sub(r'\1\n' + kw_sentence, content_html, count=1)
                else:
                    content_html = kw_sentence + "\n" + content_html
                injected += 1

            # Injections 2-3: append keyword naturally to substantial paragraphs
            # Two distinct phrases so consecutive injections are not identical
            if injected < KW_SAFETY_NET_MAX_INJECTIONS:
                p_pattern = re.compile(r'(<p>)(.*?)(</p>)', re.DOTALL)
                substantial = [
                    m for m in p_pattern.finditer(content_html)
                    if len(re.sub(r'<[^>]+>', '', m.group(2))) > KW_INJECT_MIN_PARA_CHARS
                    and kw_lower not in m.group(2).lower()
                ]
                extra_phrases = [
                    f" {focus_keyword} šajā kontekstā ir būtisks faktors efektīvai ieviešanai.",
                    f" Praksē {focus_keyword} palīdz organizācijām ievērojami samazināt administratīvo slodzi.",
                ]
                for idx, m in enumerate(substantial):
                    if injected >= KW_SAFETY_NET_MAX_INJECTIONS:
                        break
                    phrase = extra_phrases[idx % len(extra_phrases)]
                    new_p = f"{m.group(1)}{m.group(2)}{phrase}{m.group(3)}"
                    content_html = content_html.replace(m.group(0), new_p, 1)
                    injected += 1

            logging.info("[mega] Safety net complete: %d injection(s) applied", injected)

    content_html = sanitize_html(content_html)

    total_words = count_words_from_html(content_html)
    logging.info(f"[mega] Assembly: {total_words} words from {len(all_sections)} sections (target {target_words})")

    # Build final data dict
    data = {
        "title": title,
        "seoSlug": slugify(outline_data.get("seoSlug") or title),
        "excerpt": outline_data.get("excerpt") or "",
        "contentHtml": content_html,
        "category": outline_data.get("category") or meta.get("wpCategory") or "SharePoint",
        "tags": outline_data.get("tags") or [],
        "tagSlugs": outline_data.get("tagSlugs") or [slugify(t) for t in (outline_data.get("tags") or [])],
        "focusKeyword": focus_keyword,
    }

    # Quality check
    issues = quality_issues(data, target_words)
    if issues:
        logging.info(f"[mega] Quality issues: {issues}")

    # ── Phase 4: "Papildu lasāmviela" block ───────────────────────────────
    try:
        reading_block = build_papildu_lasamviela(
            meta=meta,
            research=research,
            focus_keyword=focus_keyword,
            title=title,
        )
        if reading_block:
            data["contentHtml"] = data["contentHtml"].rstrip() + "\n\n" + reading_block
            logging.info("[mega] Appended 'Papildu lasāmviela' block")
    except Exception as e:
        logging.warning(f"[mega] Papildu lasāmviela failed (skipping): {e}")

    # ── Phase 5: Table of Contents ─────────────────────────────────────────
    try:
        data["contentHtml"] = inject_toc(data["contentHtml"])
        logging.info("[mega] Injected Table of Contents")
    except Exception as e:
        logging.warning(f"[mega] ToC injection failed (skipping): {e}")

    logging.info(f"[mega] Done: {total_words} words, {len(all_sections)} sections (target {target_words})")
    return data


# =============================================================================
# Table of Contents generator
# =============================================================================

def _heading_to_anchor(text: str) -> str:
    """Convert heading text to URL-friendly anchor ID, matching Rank Math style."""
    s = text.lower().strip()
    # Latvian diacritics → ASCII
    mapping = str.maketrans({
        "ā": "a", "č": "c", "ē": "e", "ģ": "g", "ī": "i", "ķ": "k",
        "ļ": "l", "ņ": "n", "š": "s", "ū": "u", "ž": "z",
        "Ā": "a", "Č": "c", "Ē": "e", "Ģ": "g", "Ī": "i", "Ķ": "k",
        "Ļ": "l", "Ņ": "n", "Š": "s", "Ū": "u", "Ž": "z",
    })
    s = s.translate(mapping)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "section"


def inject_toc(html: str) -> str:
    """
    Parse headings from contentHtml, add anchor IDs, and prepend a
    Rank Math compatible Table of Contents block.
    """
    if not html:
        return html

    # Find all h2 and h3 headings
    heading_re = re.compile(r"<(h[23])(?:\s[^>]*)?>(.+?)</\1>", re.I | re.S)
    headings = []
    seen_anchors = set()

    for m in heading_re.finditer(html):
        tag = m.group(1).lower()
        text = re.sub(r"<[^>]+>", "", m.group(2)).strip()  # strip inner HTML
        if not text:
            continue
        anchor = _heading_to_anchor(text)
        # Ensure uniqueness
        base_anchor = anchor
        counter = 1
        while anchor in seen_anchors:
            anchor = f"{base_anchor}-{counter}"
            counter += 1
        seen_anchors.add(anchor)
        headings.append({"tag": tag, "text": text, "anchor": anchor, "match": m})

    if len(headings) < 3:
        # Too few headings, skip ToC
        return html

    # Add id attributes to headings in HTML (process from end to avoid offset issues)
    modified = html
    for h in reversed(headings):
        m = h["match"]
        tag = h["tag"]
        old_tag_open = m.group(0)[:m.group(0).index(">") + 1]
        # Replace opening tag with one that has id attribute
        new_tag_open = f'<{tag} id="{h["anchor"]}">'
        modified = modified[:m.start()] + m.group(0).replace(old_tag_open, new_tag_open, 1) + modified[m.end():]

    # Build ToC HTML matching Rank Math format
    toc_items = []
    current_h2_children = []
    current_h2 = None

    for h in headings:
        if h["tag"] == "h2":
            # Close previous h2 group
            if current_h2 is not None:
                toc_items.append((current_h2, current_h2_children))
            current_h2 = h
            current_h2_children = []
        else:  # h3
            current_h2_children.append(h)

    # Close last h2 group
    if current_h2 is not None:
        toc_items.append((current_h2, current_h2_children))

    # Build nested list HTML
    li_parts = []
    for h2, children in toc_items:
        if children:
            child_lis = "\n".join(
                f'<li class=""><a href="#{c["anchor"]}">{c["text"]}</a></li>'
                for c in children
            )
            li_parts.append(
                f'<li class=""><a href="#{h2["anchor"]}">{h2["text"]}</a>'
                f'<ul>{child_lis}</ul></li>'
            )
        else:
            li_parts.append(
                f'<li class=""><a href="#{h2["anchor"]}">{h2["text"]}</a></li>'
            )

    toc_html = (
        '<div class="wp-block-rank-math-toc-block" id="rank-math-toc">'
        '<h2>Saturs</h2>'
        '<nav><ul>'
        + "\n".join(li_parts)
        + '</ul></nav></div>'
    )

    # Insert ToC at the very beginning of content
    return toc_html + "\n\n" + modified


# =============================================================================
# "Papildu lasāmviela" — related links block
# =============================================================================

def _fetch_ksj_related_posts(focus_keyword: str, title: str, limit: int = 4) -> list[dict]:
    """
    Query ksj.lv WP REST API for related published posts.
    Returns list of {"url": ..., "title": ...}.
    """
    api_base = (os.environ.get("WP_API_BASE") or "").rstrip("/")
    if not api_base:
        return []

    results = []
    # Try multiple search queries to find relevant posts
    search_terms = [focus_keyword]
    # Add first meaningful word from keyword (e.g., "SharePoint" from "SharePoint AI FAQ Web Part")
    parts = focus_keyword.split()
    if len(parts) > 1:
        search_terms.append(parts[0])

    seen_ids = set()
    for term in search_terms:
        if len(results) >= limit:
            break
        try:
            resp = requests.get(
                f"{api_base}/wp/v2/posts",
                params={
                    "search": term,
                    "per_page": limit + 2,
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
                    # Skip the article we're currently generating
                    if not link or not ptitle or pid in seen_ids:
                        continue
                    # Skip if title matches current article too closely
                    if ptitle.lower() == title.lower():
                        continue
                    seen_ids.add(pid)
                    results.append({"url": link, "title": ptitle})
                    if len(results) >= limit:
                        break
        except Exception as e:
            logging.debug(f"[papildu] WP search '{term}' failed: {e}")

    logging.info(f"[papildu] Found {len(results)} KSJ related posts")
    return results[:limit]


def _fetch_ms_docs_links(
    research: dict, focus_keyword: str, limit: int = 4
) -> list[dict]:
    """
    Get verified Microsoft docs links. Sources:
    1. Research phase en_links (already found)
    2. Targeted SerpApi search for learn.microsoft.com
    Returns list of {"url": ..., "title": ...}.
    """
    results = []
    seen_urls = set()

    # Source 1: research phase already found MS links
    en_links = research.get("en_links") or []
    en_titles = research.get("en_titles") or []
    for i, link in enumerate(en_links):
        if link in seen_urls:
            continue
        # Try to get title from same-index en_titles or from URL
        title = en_titles[i] if i < len(en_titles) else ""
        if not title:
            # Extract readable title from URL path
            path = link.rstrip("/").split("/")[-1]
            title = path.replace("-", " ").title()
        seen_urls.add(link)
        results.append({"url": link, "title": title})
        if len(results) >= limit:
            break

    # Source 2: targeted SerpApi search if we need more
    if len(results) < limit:
        try:
            ms_query = f"{focus_keyword} site:learn.microsoft.com"
            serp = serp_search_cached(ms_query, ttl=DEFAULT_TTL * 4)
            if serp:
                for r in serp.get("organic_results", [])[:limit * 2]:
                    link = (r.get("link") or "").strip()
                    title = (r.get("title") or "").strip()
                    if not link or link in seen_urls:
                        continue
                    if "learn.microsoft.com" not in link and "microsoft.com" not in link:
                        continue
                    seen_urls.add(link)
                    results.append({"url": link, "title": title})
                    if len(results) >= limit:
                        break
        except Exception as e:
            logging.debug(f"[papildu] MS docs SerpApi search failed: {e}")

    # Verify links are alive (quick HEAD check)
    verified = []
    for item in results[:limit + 2]:
        try:
            resp = requests.head(item["url"], timeout=5, allow_redirects=True)
            if resp.status_code < 400:
                verified.append(item)
                if len(verified) >= limit:
                    break
        except Exception:
            # Skip broken links
            continue

    logging.info(
        f"[papildu] MS docs: {len(results)} found, {len(verified)} verified"
    )
    return verified[:limit]


def _generate_link_descriptions(
    ksj_links: list[dict],
    ms_links: list[dict],
    focus_keyword: str,
    article_title: str,
) -> dict:
    """
    Use GPT to generate Latvian descriptions for KSJ links
    and Latvian titles for MS docs links.
    Returns {"ksj": [{"url","title","desc"},...], "ms": [{"url","title_lv"},...]}
    """
    if not ksj_links and not ms_links:
        return {"ksj": [], "ms": []}

    ksj_items_text = "\n".join(
        f"- {i+1}. \"{item['title']}\" ({item['url']})"
        for i, item in enumerate(ksj_links)
    )
    ms_items_text = "\n".join(
        f"- {i+1}. \"{item['title']}\" ({item['url']})"
        for i, item in enumerate(ms_links)
    )

    system = (
        "Tu ģenerē īsus aprakstus un virsrakstus latviešu valodā. "
        "Atbildi TIKAI ar derīgu JSON."
    )
    user = (
        f"Raksta virsraksts: \"{article_title}\"\n"
        f"Focus keyword: \"{focus_keyword}\"\n\n"
        f"KSJ RAKSTI (vajag 1-2 teikumu aprakstu katram, kā šis raksts saistās ar galveno tēmu):\n"
        f"{ksj_items_text}\n\n"
        f"MICROSOFT RESURSI (vajag virsrakstu latviski 5-10 vārdi + 1 teikuma aprakstu katram):\n"
        f"{ms_items_text}\n\n"
        f"JSON formāts:\n"
        f'{{\n'
        f'  "ksj": [{{"index": 1, "desc": "Īss apraksts latviski..."}}],\n'
        f'  "ms": [{{"index": 1, "title_lv": "Virsraksts latviski", "desc": "Īss apraksts..."}}]\n'
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

        # Merge descriptions back into link data
        ksj_result = []
        ksj_descs = {d["index"]: d.get("desc", "") for d in (outer.get("ksj") or [])}
        for i, item in enumerate(ksj_links):
            desc = ksj_descs.get(i + 1, "")
            ksj_result.append({"url": item["url"], "title": item["title"], "desc": desc})

        ms_result = []
        ms_data = {d["index"]: d for d in (outer.get("ms") or [])}
        for i, item in enumerate(ms_links):
            d = ms_data.get(i + 1, {})
            title_lv = d.get("title_lv", "") or item["title"]
            desc = d.get("desc", "")
            ms_result.append({"url": item["url"], "title_lv": title_lv, "desc": desc})

        return {"ksj": ksj_result, "ms": ms_result}
    except Exception as e:
        logging.warning(f"[papildu] GPT link descriptions failed: {e}")
        # Return raw data without descriptions
        return {
            "ksj": [{"url": l["url"], "title": l["title"], "desc": ""} for l in ksj_links],
            "ms": [{"url": l["url"], "title_lv": l["title"], "desc": ""} for l in ms_links],
        }


def _build_reading_html(
    ksj_links: list[dict],
    ms_links: list[dict],
    focus_keyword: str,
) -> str:
    """Build the HTML block for Papildu lasāmviela."""
    if not ksj_links and not ms_links:
        return ""

    # KSJ column
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

    # MS column
    ms_items_html = ""
    for item in ms_links:
        title = item.get("title_lv") or item.get("title") or ""
        desc_html = ""
        if item.get("desc"):
            desc_html = f'<br>\n          <small style="color:#666;">{item["desc"]}</small>'
        ms_items_html += (
            f'        <li style="margin-bottom:8px;">\n'
            f'          <a href="{item["url"]}" target="_blank" rel="noopener noreferrer">{title}</a>{desc_html}\n'
            f'        </li>\n'
        )

    # CTA text
    cta_topic = focus_keyword
    if len(cta_topic) > 40:
        cta_topic = " ".join(cta_topic.split()[:4])
    cta_text = f"Sazinieties ar KSJ par {cta_topic}"

    # Build two-column layout
    ksj_col = ""
    if ksj_items_html:
        ksj_col = (
            f'    <div style="flex:1 1 320px;min-width:240px;">\n'
            f'      <strong>Saistītie KSJ raksti</strong>\n'
            f'      <ul style="margin:8px 0 14px 18px;padding:0;color:#222;">\n'
            f'{ksj_items_html}'
            f'      </ul>\n'
            f'    </div>\n'
        )

    ms_col = ""
    if ms_items_html:
        ms_col = (
            f'    <div style="flex:1 1 320px;min-width:240px;">\n'
            f'      <strong>Oficiālie resursi</strong>\n'
            f'      <ul style="margin:8px 0 14px 18px;padding:0;color:#222;">\n'
            f'{ms_items_html}'
            f'      </ul>\n'
            f'    </div>\n'
        )

    html = (
        f'<h2>Papildu lasāmviela</h2>\n'
        f'<div style="border:1px solid #e6e6e6;padding:22px;border-radius:8px;'
        f'background:#fbfbfb;font-family:Arial,Helvetica,sans-serif;color:#222;margin-top:28px;">\n'
        f'  <div style="display:flex;flex-wrap:wrap;gap:24px;">\n'
        f'{ksj_col}{ms_col}'
        f'  </div>\n'
        f'  <p style="text-align:center;margin:18px 0 0;">\n'
        f'    <a href="https://ksj.lv/kontakti/" '
        f'style="display:inline-block;background:#2b8a3e;color:#ffffff;'
        f'padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:600;'
        f'font-size:15px;line-height:1.4;">{cta_text}</a>\n'
        f'  </p>\n'
        f'</div>'
    )
    return html


def build_papildu_lasamviela(
    meta: dict,
    research: dict,
    focus_keyword: str,
    title: str,
) -> str:
    """
    Main entry point: fetch links, generate descriptions, build HTML.
    Returns empty string if insufficient data.
    """
    # Fetch KSJ related posts
    ksj_links = _fetch_ksj_related_posts(focus_keyword, title, limit=4)

    # Fetch MS docs links (from research + SerpApi)
    ms_links = _fetch_ms_docs_links(research, focus_keyword, limit=4)

    if not ksj_links and not ms_links:
        logging.info("[papildu] No links found, skipping block")
        return ""

    # Generate Latvian descriptions via GPT
    enriched = _generate_link_descriptions(ksj_links, ms_links, focus_keyword, title)

    # Build HTML
    html = _build_reading_html(
        ksj_links=enriched.get("ksj") or ksj_links,
        ms_links=enriched.get("ms") or ms_links,
        focus_keyword=focus_keyword,
    )

    return html


def build_wp_article_from_item(item: dict) -> dict:
    """Synchronous single-call article generator entry point.

    Picks the first SharePoint list item from a value-list payload, extracts
    metadata, chooses mega vs multi-call mode, generates the article, runs quality
    checks, normalizes tags, and resolves WordPress tag IDs.

    Args:
        item: Raw incoming request dict; may be a SharePoint value-list wrapper or
              a flat metadata dict with keys primary, angle, audience, etc.

    Returns:
        Complete article dict with keys: title, seoSlug, excerpt, contentHtml,
        category, tags, tagSlugs, focusKeyword, wpTagIds.

    Raises:
        RuntimeError: When required fields (primary, angle, audience) are missing.
    """
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
            phrase = c.get("phrase", "")
            rel = c.get("predicted_relevance", 0.0)
            trend = enrichment.get(phrase, {}).get("trend", 0.0)
            serp_score = 1.0 if enrichment.get(phrase, {}).get("organic_count", 0) > 0 else 0.5
            final_score = 0.6 * rel + 0.25 * trend + 0.15 * serp_score
            c["_final_score"] = round(final_score, 3)
            if not best or final_score > best.get("_final_score", 0.0):
                best = c
        if best:
            top_choice = {"keyword": best.get("phrase", ""), "score": best.get("_final_score", best.get("predicted_relevance", 0.0)), "explanation": "local heuristic + enrichment"}
        else:
            top_choice = None

    return {"top_recommendation": (top_choice.get("keyword") if top_choice else None), "candidates": candidates[:limit]}
