# =============================================================================
# Imports
# =============================================================================
import logging
import urllib.error
from urllib.parse import parse_qs

import json
import uuid
import datetime

import azure.functions as func

# Storage helpers
from storage import (
    ensure_storage_objects,
    make_sas_url,
    status_upsert,
    status_get,
    state_save,
)

from config import (
    DEFAULT_TARGET_WORDS,
    DEFAULT_FIT_MODE,
)

from docx_html import convert_docx_to_html

# Image generation helpers
from image_gen import (
    get_images_url,
    get_images_headers,
    get_provider_info,
    coerce_size,
    build_image_meta,
    synthesize_image_prompt,
    build_fallback_prompt,
    fit_to_social_header,
    generate_image_b64,
)

# Article generation helpers
from article_gen import (
    build_wp_article_from_item,
    generate_keywords_from_input,
)


# =============================================================================
# Azure Functions App
# =============================================================================
app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

# =============================================================================
# Healthcheck
# =============================================================================
@app.function_name(name="ping")
@app.route(route="ping", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def ping(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("pong", status_code=200)


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
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False),
        status_code=code,
        mimetype="application/json",
    )


def ok(**payload) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False),
        status_code=200,
        mimetype="application/json",
    )

from fb_gen import generate_fb_copy
from article_worker import tick_once


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
# HTTP: FB copy generator
# =============================================================================
@app.function_name(name="generate_fb_copy")
@app.route(
    route="generate-fb-copy",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def fb_copy_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    incoming = read_incoming(req)
    if not incoming:
        return bad(400, error="Invalid JSON body")
    try:
        result = generate_fb_copy(incoming)
    except Exception as e:
        logging.exception("generate_fb_copy failed")
        return bad(502, error="fb_copy_failed", message=str(e)[:400])
    return ok(**result)

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
        kw = out.get("top_recommendation") or (out.get("candidates")[0]["phrase"] if out.get("candidates") else None)

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
    incoming = read_incoming(req)
    if not incoming:
        return bad(400, error="Invalid JSON body")

    ensure_storage_objects()
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
    state_save(op_id, state)
    status_upsert(op_id, "queued", phase="outline", progress=0)

    payload = {
        "opId": op_id,
        "statusUrl": f"/api/wp-job-status/{op_id}",
        "resultUrl": f"/api/wp-job-result/{op_id}",
        "resultBlobSas": make_sas_url(op_id),
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
    """Read-only job status — never triggers work."""
    op_id = req.route_params.get("opId")
    e = status_get(op_id)
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
    """Advance the job by one step. Call repeatedly until status is done/failed."""
    op_id = req.route_params.get("opId")
    e = status_get(op_id)
    if not e:
        return bad(404, error="not_found")

    if (e.get("status") or "") in {"done", "failed"}:
        info = {k: e.get(k) for k in ("error", "blobPath", "blobUrl", "phase", "progress")}
        return ok(opId=op_id, status=e.get("status"), updatedUtc=e.get("updatedUtc"), info=info)

    try:
        tick_once(op_id)
        e = status_get(op_id)
    except Exception as ex:
        logging.exception("tick_once failed")
        status_upsert(op_id, "failed", error=str(ex)[:500])
        e = status_get(op_id)

    info = {k: e.get(k) for k in ("error", "blobPath", "blobUrl", "phase", "progress")}
    return ok(opId=op_id, status=e.get("status"), updatedUtc=e.get("updatedUtc"), info=info)


@app.function_name(name="generate_image")
@app.route(
    route="generate-image",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def generate_image(req: func.HttpRequest) -> func.HttpResponse:
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
            prompt = build_fallback_prompt(ctx)

    prompt_used = (prompt + (f". {style_hint}" if style_hint else "")).strip()
    meta = build_image_meta(ctx, prompt_used, ext=".png")
    fit_mode = (incoming.get("fitMode") or DEFAULT_FIT_MODE).lower()

    try:
        result = generate_image_b64(prompt_used, w, h, fit_mode)
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8") if hasattr(e, "read") else str(e)
        return bad(502, error="images api http", message=txt[:400])
    except RuntimeError as e:
        return bad(502, error="image generation failed", message=str(e)[:400])
    except Exception as e:
        return bad(502, error="images api call failed", message=str(e))

    return ok(
        **result,
        correlationId=cid,
        promptUsed=prompt_used,
        altText=meta["alt_text"],
        caption=meta["caption"],
        description=meta["description"],
        fileName=meta["file_name"],
    )


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
    return ok(**get_provider_info())
