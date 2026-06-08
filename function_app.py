# =============================================================================
# Imports
# =============================================================================
import io
import logging
import os
import re
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import parse_qs
from base64 import b64decode, b64encode

import json
import uuid
import datetime

from PIL import Image

import azure.functions as func

# Azure Storage SDK
from azure.data.tables import TableServiceClient
from azure.storage.queue import QueueClient
from azure.storage.blob import (
    BlobClient,
    BlobServiceClient,
    generate_blob_sas,
    BlobSasPermissions,
)
from azure.core.exceptions import ResourceExistsError

# =============================================================================
# Raksta Ä£enerÄ“Å¡anas helpers (iznesti uz atseviÅ¡Ä·u moduÄ¼i)
# =============================================================================
from article_gen import (
    build_wp_article_from_item,
    generate_keywords_from_input,
    is_azure_openai,
    get_url,
    get_headers,
    http_post_json,
)

from config import (
    SOCIAL_HEADER_W,
    SOCIAL_HEADER_H,
    CROP_LOSS_THRESHOLD,
    IMAGE_API_TIMEOUT_SEC,
    IMAGE_B64_MIN_LEN,
    PROMPT_SYNTHESIS_MAX_TOKENS,
    PROMPT_SYNTHESIS_TIMEOUT_SEC,
    ALT_TEXT_MAX_LEN,
    IMG_DESC_MAX_LEN,
    DEFAULT_TARGET_WORDS,
    SAS_HOURS_VALID,
)

from article_worker import tick_once

# =============================================================================
# Azure Functions App
# =============================================================================
app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

# =============================================================================
# Storage helpers
# =============================================================================
def _table():
    """Return TableClient for JobStatus table (creates if missing)."""
    conn = os.getenv("STORAGE")
    svc = TableServiceClient.from_connection_string(conn)
    svc.create_table_if_not_exists("JobStatus")
    return svc.get_table_client("JobStatus")


def _queue():
    """Return QueueClient for wpjobs (creates if missing)."""
    conn = os.getenv("STORAGE")
    qc = QueueClient.from_connection_string(conn, "wpjobs")
    try:
        qc.create_queue()
    except ResourceExistsError:
        pass
    return qc


def _blob_service():
    """BlobServiceClient (for container ops & SAS)."""
    return BlobServiceClient.from_connection_string(os.getenv("STORAGE"))


def _blob_client(op_id: str) -> "BlobClient":
    """BlobClient for results/{op_id}.json (container created if missing)."""
    bsc = _blob_service()
    try:
        bsc.create_container("results")
    except ResourceExistsError:
        pass
    return bsc.get_blob_client(container="results", blob=f"{op_id}.json")


def _ensure_storage_objects():
    _queue()
    _table()
    try:
        _blob_service().create_container("results")
    except ResourceExistsError:
        pass


def _make_sas_url(op_id: str, hours_valid: int = SAS_HOURS_VALID) -> str | None:
    """Generate short-term read-only SAS URL for results/{op_id}.json."""
    try:
        bsc = _blob_service()
        acct = bsc.account_name
        expires = datetime.datetime.utcnow() + datetime.timedelta(hours=hours_valid)
        sas = generate_blob_sas(
            account_name=acct,
            container_name="results",
            blob_name=f"{op_id}.json",
            account_key=getattr(bsc.credential, "account_key", None),
            permission=BlobSasPermissions(read=True),
            expiry=expires,
        )
        return f"https://{acct}.blob.core.windows.net/results/{op_id}.json?{sas}"
    except Exception:
        return None


# ---- Job status helpers -----------------------------------------------------
JOB_TABLE = "JobStatus"
RESULT_CONTAINER = "results"
JOB_PK = "wp"


def _status_upsert(op_id: str, status: str, **extra):
    tc = _table()
    entity = {
        "PartitionKey": JOB_PK,
        "RowKey": op_id,
        "status": status,
        "updatedUtc": datetime.datetime.utcnow().isoformat() + "Z",
        **extra,
    }
    tc.upsert_entity(entity)


def _status_get(op_id: str) -> dict | None:
    try:
        return _table().get_entity(JOB_PK, op_id)
    except Exception:
        return None


# ---- Cooperative HTTP worker state -----------------------------------------
WORK_CONTAINER = "work"


def _work_blob_client(op_id: str) -> "BlobClient":
    bsc = _blob_service()
    try:
        bsc.create_container(WORK_CONTAINER)
    except ResourceExistsError:
        pass
    return bsc.get_blob_client(container=WORK_CONTAINER, blob=f"{op_id}.json")


def _state_load(op_id: str) -> dict | None:
    try:
        raw = _work_blob_client(op_id).download_blob().readall()
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _state_save(op_id: str, state: dict) -> None:
    _work_blob_client(op_id).upload_blob(
        json.dumps(state, ensure_ascii=False).encode("utf-8"),
        overwrite=True,
    )


def _progress(op_id: str, phase: str, done: int, total: int, **extra):
    pct = int((done / max(1, total)) * 100)
    _status_upsert(op_id, "working", phase=phase, progress=pct, **extra)


# =============================================================================
# Healthcheck
# =============================================================================
@app.function_name(name="ping")
@app.route(route="ping", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def ping(req: func.HttpRequest) -> func.HttpResponse:
    """GET /api/ping — anonymous healthcheck endpoint.

    Returns:
        HTTP 200 with plain-text body "pong".
    """
    return func.HttpResponse("pong", status_code=200)


# =============================================================================
# Image helpers (text + image endpoints use same OpenAI/Azure config as article_gen)
# =============================================================================
def get_images_url() -> str:
    """
    Build image generation endpoint URL based on ENV settings.
    Respects FORCE_IMAGE_PROVIDER and AZURE_OPENAI_IMAGE_DEPLOYMENT.
    """
    force = (os.getenv("FORCE_IMAGE_PROVIDER", "") or "").strip().lower()
    dep = (os.getenv("AZURE_OPENAI_IMAGE_DEPLOYMENT", "") or "").strip()

    # Explicit OpenAI
    if force == "openai":
        base = (os.getenv("OAI_BASE_URL", "https://api.openai.com/v1") or "").rstrip("/")
        return f"{base}/images/generations"

    # Explicit Azure
    if force == "azure":
        if not dep:
            raise RuntimeError(
                "FORCE_IMAGE_PROVIDER=azure, bet AZURE_OPENAI_IMAGE_DEPLOYMENT nav iestatÄ«ts"
            )
        base = (os.getenv("AZURE_OPENAI_ENDPOINT", "") or "").rstrip("/")
        ver = (
            os.getenv(
                "AZURE_OPENAI_API_VERSION_IMAGES",
                os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview"),
            )
            or ""
        ).strip()
        return f"{base}/openai/deployments/{dep}/images/generations?api-version={ver}"

    # Auto: Azure images deployment wins if present
    if dep:
        base = (os.getenv("AZURE_OPENAI_ENDPOINT", "") or "").rstrip("/")
        ver = (
            os.getenv(
                "AZURE_OPENAI_API_VERSION_IMAGES",
                os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview"),
            )
            or ""
        ).strip()
        return f"{base}/openai/deployments/{dep}/images/generations?api-version={ver}"

    # Fallback: OpenAI
    base = (os.getenv("OAI_BASE_URL", "https://api.openai.com/v1") or "").rstrip("/")
    return f"{base}/images/generations"


def get_images_headers() -> dict:
    """
    Image auth headers.
    If using OpenAI, use Bearer; if Azure images deployment is configured, use api-key.
    """
    force = (os.getenv("FORCE_IMAGE_PROVIDER", "") or "").strip().lower()
    dep = (os.getenv("AZURE_OPENAI_IMAGE_DEPLOYMENT", "") or "").strip()

    if force == "openai" or not dep:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.getenv('OAI_API_KEY','')}",
        }
    return {
        "Content-Type": "application/json",
            "api-key": os.getenv("AZURE_OPENAI_API_KEY", ""),
    }


def coerce_size(w: int, h: int) -> tuple[int, int]:
    """Snap requested dimensions to the nearest allowed DALL-E image size.

    Args:
        w: Requested width in pixels.
        h: Requested height in pixels.

    Returns:
        Tuple (width, height) from the allowed set {1024x1024, 1536x1024, 1024x1536}.
    """
    allowed = {(1024, 1024), (1536, 1024), (1024, 1536)}
    if (w, h) in allowed:
        return w, h
    return (1536, 1024) if w >= h else (1024, 1536)


# =============================================================================
# Common HTTP helpers (JSON in/out)
# =============================================================================
def read_incoming(req: func.HttpRequest):
    """
    Accept both JSON body and x-www-form-urlencoded payloads;
    also tolerates raw JSON string bodies (e.g., from Logic Apps).
    """
    try:
        return req.get_json()
    except ValueError:
        pass

    raw = req.get_body() or b""
    s = raw.decode("utf-8", "ignore")
    ct = (req.headers.get("content-type") or "").lower()

    looks_like_form = (
        "application/x-www-form-urlencoded" in ct
        or ("=" in s and "&" in s and not s.strip().startswith("{"))
    )
    if looks_like_form:
        qs = {k: v[0] for k, v in parse_qs(s, keep_blank_values=True).items()}
        for k in list(qs):
            qs[k] = qs[k].replace("+", " ")
        return qs

    t = s.strip()
    if t.startswith('"') and t.endswith('"'):
        try:
            t = json.loads(t)
        except Exception:
            pass
    try:
        return json.loads(t)
    except Exception:
        return None


def bad(code: int, **payload) -> func.HttpResponse:
    """Return a JSON error response with the given HTTP status code.

    Args:
        code: HTTP status code (e.g. 400, 404, 500, 502).
        **payload: Additional key-value pairs serialized as the JSON body.

    Returns:
        HttpResponse with application/json content type.
    """
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False),
        status_code=code,
        mimetype="application/json",
    )


def ok(**payload) -> func.HttpResponse:
    """Return a JSON 200 OK response.

    Args:
        **payload: Key-value pairs serialized as the JSON body.

    Returns:
        HttpResponse with status 200 and application/json content type.
    """
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False),
        status_code=200,
        mimetype="application/json",
    )

from fb_gen import generate_fb_copy
from en_article_gen import generate_en_article
from content_plan import generate_content_plan


# =============================================================================
# HTTP: synchronous article generator (single-call)
# =============================================================================
@app.function_name(name="generate_wp_article")
@app.route(
    route="generate-wp-article",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def generate_wp_article(req: func.HttpRequest) -> func.HttpResponse:
    """POST /api/generate-wp-article — synchronous single-call article generator.

    Body: JSON with article metadata fields (primary, angle, audience, wpCategory,
          targetWords, articleMode, etc.; see build_wp_article_from_item).

    Returns:
        HTTP 200 JSON with full article data: title, seoSlug, excerpt, contentHtml,
        category, tags, tagSlugs, focusKeyword, wpTagIds.
        HTTP 400 on missing/invalid body or generation failure.
    """
    incoming = read_incoming(req)
    if not incoming:
        return bad(400, error="Invalid JSON body")
    try:
        data = build_wp_article_from_item(incoming)
    except Exception as e:
        logging.exception("build_wp_article_from_item failed")
        return bad(400, error="build_failed", message=str(e))
    return ok(**data)
# =============================================================================
# HTTP: synchronous EN article generator
# =============================================================================
@app.function_name(name="generate_en_article")
@app.route(route="generate-en-article", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def generate_en_article_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    incoming = read_incoming(req)
    if not incoming:
        return bad(400, error="Invalid JSON body")
    try:
        data = generate_en_article(incoming)
    except Exception as e:
        logging.exception("generate_en_article failed")
        return bad(500, error="en_generation_failed", message=str(e)[:500])
    return ok(**data)
# =============================================================================
# HTTP: FB copy generator
# =============================================================================
@app.function_name(name="generate_fb_copy")
@app.route(
    route="generate-fb-copy",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def generate_fb_copy_route(req: func.HttpRequest) -> func.HttpResponse:
    """POST /api/generate-fb-copy — Facebook post copy generator.

    Body: JSON with article metadata fields (primary, angle, audience, wpLink,
          bookLink, style, tagsCsv, SeoSlug, etc.).

    Returns:
        HTTP 200 JSON with keys: message, hashtags, wpLink, cta.
        HTTP 400 on missing/invalid body.
        HTTP 500 on generation failure.
    """
    incoming = read_incoming(req)
    if not incoming:
        return bad(400, error="Invalid JSON body")
    try:
        data = generate_fb_copy(incoming)
    except Exception as e:
        logging.exception("generate_fb_copy failed")
        return bad(500, error="fb_copy_failed", message=str(e)[:500])
    return ok(**data)


#==============================================================================
# HTTP: Content plan generator
#==============================================================================
@app.function_name(name="generate_content_plan")
@app.route(
    route="generate-content-plan",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def generate_content_plan_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    incoming = read_incoming(req)
    if not incoming:
        incoming = {}

    target_month = (incoming.get("targetMonth") or "").strip() or None
    existing_titles = incoming.get("existingTitles") or {}
    categories = incoming.get("categories") or None
    try:
        articles_per_day = int(incoming.get("articlesPerDay", 1))
    except Exception:
        articles_per_day = 1

    try:
        result = generate_content_plan(
            target_month=target_month,
            existing_titles=existing_titles,
            categories=categories,
            articles_per_day=articles_per_day,
        )
    except Exception as e:
        logging.exception("generate_content_plan failed")
        return bad(500, error="plan_generation_failed", message=str(e)[:500])

    return func.HttpResponse(
        json.dumps(result, ensure_ascii=False),
        status_code=200,
        mimetype="application/json",
    )


#==============================================================================
# HTTP: KeywordExtractor
#==============================================================================
@app.function_name(name="KeywordExtractor")
@app.route(
    route="KeywordExtractor",
    methods=["POST","GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
def KeywordExtractor(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP endpoint to return a single top keyword.
    Query params (optional):
      - use_llm: true|false (default true)
      - enrich: true|false (default true)
      - limit: int (default 10)
    Body: JSON with primary, angle, audience, wpCategory, tagsCsv, SeoSlug, combokey, target_words...
    Returns: {"keyword":"..."} or 204 if none found.
    """
    try:
        incoming = read_incoming(req) or {}
    except Exception:
        incoming = {}

    # read query params or body flags
    def _bool_from(v, default=True):
        if v is None:
            return default
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1","true","yes","on")

    params = req.params or {}
    use_llm = _bool_from(params.get("use_llm") if params.get("use_llm") is not None else incoming.get("use_llm", True), True)
    enrich = _bool_from(params.get("enrich") if params.get("enrich") is not None else incoming.get("enrich", True), True)
    try:
        limit = int(params.get("limit") or incoming.get("limit", 10))
    except Exception:
        limit = 10

    # validate minimal metadata
    if not (incoming.get("primary") or incoming.get("SeoSlug") or incoming.get("combokey")):
        return bad(400, error="missing_meta", message="Require primary or SeoSlug or combokey in request body")

    try:
        # call the generator (deterministic; generate_keywords_from_input handles LLM/enrichment toggles)
        out = generate_keywords_from_input(incoming, limit=limit, use_llm=use_llm, enrich=enrich)
    except Exception as e:
        logging.exception("generate_keywords_from_input failed")
        return bad(500, error="keyword_generation_failed", message=str(e)[:500])

    kw = None
    if isinstance(out, dict):
        kw = out.get("top_recommendation") or (out.get("candidates", [])[0].get("phrase") if out.get("candidates") else None)

    if not kw:
        return func.HttpResponse(status_code=204)

    return func.HttpResponse(json.dumps({"keyword": kw}, ensure_ascii=False), status_code=200, mimetype="application/json")

# =============================================================================
# HTTP: async enqueue + status/result endpoints
# =============================================================================
@app.function_name(name="enqueue_wp_article")
@app.route(
    route="enqueue-wp-article",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def enqueue_wp_article(req: func.HttpRequest) -> func.HttpResponse:
    """POST /api/enqueue-wp-article — enqueue an async article generation job.

    Initialises job state in Blob Storage and status in Table Storage, then
    returns immediately so the caller can poll using wp-job-tick.

    Body: JSON with article metadata fields (same as generate-wp-article),
          plus optional targetWords.

    Returns:
        HTTP 202 JSON with keys: opId, statusUrl, resultUrl, resultBlobSas.
        HTTP 400 on missing/invalid body.
    """
    incoming = read_incoming(req)
    if not incoming:
        return bad(400, error="Invalid JSON body")

    _ensure_storage_objects()
    op_id = uuid.uuid4().hex

    try:
        target_words = int(incoming.get("targetWords", DEFAULT_TARGET_WORDS))
    except Exception:
        target_words = DEFAULT_TARGET_WORDS

    state = {
        "opId": op_id,
        "phase": "outline",
        "item": incoming,
        "outline": None,
        "h3": [],
        "introHtml": "",
        "sectionIndex": 0,
        "sections": [],
        "targetWords": target_words,
        "meta": None,
        "startedUtc": datetime.datetime.utcnow().isoformat() + "Z",
    }
    _state_save(op_id, state)
    _status_upsert(op_id, "queued", phase="outline", progress=0)

    payload = {
        "opId": op_id,
        "statusUrl": f"/api/wp-job-status/{op_id}",
        "resultUrl": f"/api/wp-job-result/{op_id}",
        "resultBlobSas": _make_sas_url(op_id),
    }
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False),
        status_code=202,
        mimetype="application/json",
    )


@app.function_name(name="wp_job_status")
@app.route(
    route="wp-job-status/{opId}",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def wp_job_status(req: func.HttpRequest) -> func.HttpResponse:
    """Read-only status poll. Does not advance the job. Use wp_job_tick (POST, FUNCTION) to advance."""
    op_id = req.route_params.get("opId")
    e = _status_get(op_id)
    if not e:
        return bad(404, error="not_found")

    info = {k: e.get(k) for k in ("error", "blobPath", "blobUrl", "phase", "progress")}
    return ok(opId=op_id, status=e.get("status"), updatedUtc=e.get("updatedUtc"), info=info)


@app.function_name(name="wp_job_tick")
@app.route(
    route="wp-job-tick/{opId}",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def wp_job_tick(req: func.HttpRequest) -> func.HttpResponse:
    """Advance the job by one tick. Called by Power Automate flow."""
    op_id = req.route_params.get("opId")
    e = _status_get(op_id)
    if not e:
        return bad(404, error="not_found")

    if (e.get("status") or "") not in {"done", "failed"}:
        try:
            tick_once(op_id)
            e = _status_get(op_id)
        except Exception as ex:
            logging.exception("tick_once failed")
            _status_upsert(op_id, "failed", error=str(ex)[:500])
            e = _status_get(op_id)

    info = {k: e.get(k) for k in ("error", "blobPath", "blobUrl", "phase", "progress")}
    return ok(opId=op_id, status=e.get("status"), updatedUtc=e.get("updatedUtc"), info=info)


# =============================================================================
# KSJ: SEO image meta helpers
# =============================================================================
KSJ_KEYWORD = os.getenv("KSJ_SEO_KEYWORD", "datu sinhronizÄcija")
KSJ_DESC_SUFFIX = os.getenv(
    "KSJ_SEO_DESC_SUFFIX",
    "Bez dublikÄtiem, uzlabota datu kvalitÄte un uzticami atjauninÄjumi.",
)


def _ksj_norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _ksj_slug(s: str) -> str:
    s = s.lower().strip()
    mapping = str.maketrans(
        {
            "ā": "a",
            "č": "c",
            "ē": "e",
            "ģ": "g",
            "ī": "i",
            "ķ": "k",
            "ļ": "l",
            "ņ": "n",
            "š": "s",
            "ū": "u",
            "ž": "z",
            "Ā": "a",
            "Č": "c",
            "Ē": "e",
            "Ģ": "g",
            "Ī": "i",
            "Ķ": "k",
            "Ļ": "l",
            "Ņ": "n",
            "Š": "s",
            "Ū": "u",
            "Ž": "z",
        }
    )
    s = s.translate(mapping)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9\-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "attels"


def _ksj_trunc(s: str, n: int) -> str:
    s = _ksj_norm(s)
    return s if len(s) <= n else s[: n - 1] + "â€¦"


def ksj_build_image_meta(ctx: dict, prompt_used: str, ext: str = ".png") -> dict:
    """Build SEO-optimised image metadata (alt text, caption, description, filename).

    Args:
        ctx: Context dict with optional title key.
        prompt_used: Image generation prompt; used to derive a title when ctx lacks one.
        ext: File extension for the generated filename (default ".png").

    Returns:
        Dict with keys: alt_text, caption, description, file_name.
    """
    title = _ksj_norm((ctx.get("title") or ""))
    if not title:
        words = re.split(r"[,\.\s]+", _ksj_norm(prompt_used))
        title = " ".join(words[:10]) if words else "Datu sinhronizÄcija"

    alt = title
    if KSJ_KEYWORD.lower() not in alt.lower():
        alt = f"{alt} â€” {KSJ_KEYWORD}"
    alt = _ksj_trunc(alt, ALT_TEXT_MAX_LEN)

    caption = title
    desc = _ksj_trunc(f"{title}. {KSJ_DESC_SUFFIX}", IMG_DESC_MAX_LEN)

    stamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    fname = f"{_ksj_slug(title)}-{stamp}{ext or '.png'}"

    return {
        "alt_text": alt,
        "caption": caption,
        "description": desc,
        "file_name": fname,
    }


# =============================================================================
# Image generator endpoint
# =============================================================================
def synthesize_image_prompt(ctx: dict, style_hint: str) -> str:
    """Use the LLM to generate a single-line image prompt from article context.

    Args:
        ctx: Context dict with optional keys: title, primary, angle, audience.
        style_hint: Visual style directive appended to the user message.

    Returns:
        Single-line image prompt string.

    Raises:
        RuntimeError: When the LLM returns empty content.
    """
    system = (
        "You write a single image-generation prompt for a 1200x630 blog header. "
        "Describe ONE photorealistic, cinematic 3D scene with a single clear hero subject as the focal point, "
        "shot with shallow depth of field and soft background bokeh. "
        "Lighting is dark, moody and professional; a restrained palette of dark neutral tones plus a single warm "
        "light accent (optionally one cool accent), with generous negative space and quiet, uncluttered areas. "
        "The visual is a clean conceptual metaphor for a B2B Microsoft 365 / SharePoint / AI consulting topic. "
        "STRICTLY AVOID clutter: no busy collages, no many competing objects, no surfaces covered in gears, cogs, "
        "circuit-board patterns or floating UI elements, no 'everything everywhere' tech montage. "
        "No text, letters, words or numbers in the image; no logos or trademarks; no faces or identifiable people; "
        "no political content. "
        "Output a single line, no quotes."
    )
    title = (ctx.get("title") or "").strip()
    primary = (ctx.get("primary") or "").strip()
    angle = (ctx.get("angle") or "").strip()
    audience = (ctx.get("audience") or "").strip()

    user = (
        f"Title: {title}\n"
        f"Primary: {primary}\n"
        f"Angle: {angle}\n"
        f"Audience: {audience}\n"
        f"Desired style hint: {style_hint}"
    )

    payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": PROMPT_SYNTHESIS_MAX_TOKENS,
        "temperature": 0.7,
    }

    outer = http_post_json(get_url(), get_headers(), payload, timeout_sec=PROMPT_SYNTHESIS_TIMEOUT_SEC)
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
        raise RuntimeError("prompt synthesis returned empty")
    return " ".join(text.strip().split())


@app.function_name(name="generate_image")
@app.route(
    route="generate-image",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def generate_image(req: func.HttpRequest) -> func.HttpResponse:
    """POST /api/generate-image — AI image generation endpoint.

    Calls the configured image provider (OpenAI gpt-image-1 or Azure DALL-E),
    resizes the result to 1200x630, and returns base64-encoded PNG with SEO metadata.

    Body JSON fields:
        prompt: Optional explicit image prompt (synthesized from context if absent).
        style: Optional style hint passed to prompt synthesis.
        aspect: Requested dimensions as "WIDTHxHEIGHT" (default "1200x630").
        correlationId: Optional caller-provided correlation identifier.
        context: Optional dict with title, primary, angle, audience for prompt synthesis.
        fitMode: "cover", "contain", or "auto" (default "auto").

    Returns:
        HTTP 200 JSON with keys: imageBase64, ext, width, height, correlationId,
        promptUsed, provider, altText, caption, description, fileName, imagesUrl.
        HTTP 400 on invalid aspect value.
        HTTP 502 on image API errors.
    """
    incoming = read_incoming(req)
    if not incoming:
        return bad(400, error="Invalid JSON body")

    prompt = (incoming.get("prompt") or "").strip()
    style = (incoming.get("style") or "").strip()
    aspect = (incoming.get("aspect") or "1200x630").lower()
    cid = (incoming.get("correlationId") or "").strip()
    ctx = incoming.get("context") or {}

    try:
        w, h = [int(x) for x in aspect.split("x")]
    except Exception:
        return bad(400, error="Invalid aspect, expected WIDTHxHEIGHT", value=aspect)
    w, h = coerce_size(w, h)

    style_hint = style or ""
    if not prompt:
        try:
            prompt = synthesize_image_prompt(ctx, style_hint)
        except Exception:
            title = (ctx.get("title") or "").strip()
            primary = (ctx.get("primary") or "").strip()
            angle = (ctx.get("angle") or "").strip()
            audience = (ctx.get("audience") or "").strip()
            prompt = " ".join(
                [
                    "Create a Facebook header image.",
                    f"Topic: {title}.",
                    f"Primary: {primary}." if primary else "",
                    f"Angle: {angle}." if angle else "",
                    f"Audience: {audience}." if audience else "",
                    "Clean, minimalist, high-contrast. No text, no logos, no trademarks.",
                ]
            ).strip()

    prompt_used = (prompt + (f". {style_hint}" if style_hint else "")).strip()
    meta = ksj_build_image_meta(ctx, prompt_used, ext=".png")

    url = get_images_url()
    headers = get_images_headers()
    provider = "openai" if "api.openai.com" in url else "azure"

    body = {"prompt": prompt_used, "size": f"{w}x{h}", "n": 1}
    if provider == "openai":
        body["model"] = os.getenv("OAI_IMAGE_MODEL", "gpt-image-1")

    fit_mode = (incoming.get("fitMode") or os.getenv("IMAGE_FIT_MODE") or "auto").lower()

    try:
        outer = http_post_json(url, headers, body, timeout_sec=IMAGE_API_TIMEOUT_SEC)
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8") if hasattr(e, "read") else str(e)
        return bad(502, error="images api http", message=txt[:400])
    except Exception as e:
        return bad(502, error="images api call failed", message=str(e))

    try:
        data = outer.get("data") or []
        b64 = data[0].get("b64_json") if data else None
        if not b64 or not isinstance(b64, str) or not b64.strip():
            return bad(502, error="no image in response", raw=str(outer)[:400])
        if len(b64) < IMAGE_B64_MIN_LEN:
            return bad(502, error="image too small", raw=str(outer)[:400])

        try:
            raw = b64decode(b64)
            img = Image.open(io.BytesIO(raw)).convert("RGBA")
            target_w, target_h = SOCIAL_HEADER_W, SOCIAL_HEADER_H
            tr = target_w / target_h
            w0, h0 = img.width, img.height

            def crop_cover(im):
                w_, h_ = im.width, im.height
                cur = w_ / h_
                if cur > tr:
                    new_w = int(h_ * tr)
                    left = (w_ - new_w) // 2
                    im = im.crop((left, 0, left + new_w, h_))
                else:
                    new_h = int(w_ / tr)
                    top = (h_ - new_h) // 2
                    im = im.crop((0, top, w_, top + new_h))
                return im

            def pad_contain(im, bg=(248, 248, 248, 255)):
                scale = min(target_w / im.width, target_h / im.height)
                new_w, new_h = int(im.width * scale), int(im.height * scale)
                im = im.resize((new_w, new_h), Image.LANCZOS)
                canvas = Image.new("RGBA", (target_w, target_h), bg)
                off = ((target_w - new_w) // 2, (target_h - new_h) // 2)
                canvas.paste(im, off, im)
                return canvas

            mode = fit_mode
            if mode == "auto":
                if (w0 / h0) > tr:
                    cover_w, cover_h = int(h0 * tr), h0
                else:
                    cover_w, cover_h = w0, int(w0 / tr)
                kept = (cover_w * cover_h) / (w0 * h0)
                mode = "contain" if (1 - kept) > CROP_LOSS_THRESHOLD else "cover"

            if mode == "cover":
                img = crop_cover(img)
                img = img.resize((target_w, target_h), Image.LANCZOS)
            else:
                img = pad_contain(img)

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = b64encode(buf.getvalue()).decode("ascii")
            w, h = target_w, target_h
        except Exception as _resize_err:
            logging.warning(
                "Image resize failed — fit_mode=%s source_b64_len=%d original_size=%dx%d: %s",
                fit_mode, len(b64), w, h, _resize_err,
            )

        return ok(
            imageBase64=b64,
            ext=".png",
            width=w,
            height=h,
            correlationId=cid,
            promptUsed=prompt_used,
            provider=provider,
            altText=meta["alt_text"],
            caption=meta["caption"],
            description=meta["description"],
            fileName=meta["file_name"],
            imagesUrl=url[:120],
        )
    except Exception as e:
        return bad(502, error="parse images response", message=str(e))


# =============================================================================
# Who am I (images provider)
# =============================================================================
@app.function_name(name="whoami_images")
@app.route(
    route="whoami-images",
    methods=["GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
def whoami_images(req: func.HttpRequest) -> func.HttpResponse:
    """GET /api/whoami-images — debug endpoint reporting the active image provider.

    Returns:
        HTTP 200 JSON with keys: provider, force, deployment, imagesUrl,
        has_OAI_KEY, has_AZURE_TEXT.
    """
    force = (os.getenv("FORCE_IMAGE_PROVIDER", "") or "").strip().lower()
    dep = (os.getenv("AZURE_OPENAI_IMAGE_DEPLOYMENT", "") or "").strip()

    url = get_images_url()
    provider = "openai" if "api.openai.com" in url else "azure"

    return ok(
        provider=provider,
        force=force,
        deployment=dep,
        imagesUrl=url,
        has_OAI_KEY=bool(os.getenv("OAI_API_KEY")),
        has_AZURE_TEXT=bool(
            os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_API_KEY")
        ),
    )
