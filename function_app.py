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

# Storage helpers (Table, Queue, Blob, SAS, job status, work state)
from storage import (
    ensure_storage_objects,
    get_blob_client,
    get_work_blob_client,
    make_sas_url,
    status_upsert,
    status_get,
    state_load,
    state_save,
    progress,
)

from config import (
    RESULT_CONTAINER,
    DEFAULT_TARGET_WORDS,
    TOPUP_THRESHOLD,
    FILLER_THRESHOLD,
    FILLER_MIN_WORDS,
    FILLER_BUFFER_WORDS,
    WP_API_BASE,
    WP_TOKEN,
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
    pick_item,
    extract_meta,
    build_wp_article_from_item,
    generate_draft_outline,
    generate_section_html_with_validation,
    topup_section_html,
    refine_full_article,
    ensure_wp_tag_ids,
    count_words_from_html,
    calculate_section_words,
    quality_issues,
    generate_keywords_from_input,
    slugify,
    sanitize_html,
    normalize_lv_headings,
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

# =============================================================================
# Async worker core ("tick" model for long article generation)
# =============================================================================
def _compose_content(intro_html: str, sections: list[dict]) -> str:
    parts = [intro_html.strip()] if intro_html else []
    for s in sections:
        parts.append(f"<h3>{s['h3']}</h3>\n{s['html']}")
    return sanitize_html(normalize_lv_headings("\n\n".join(parts)))


def _tick_once(op_id: str) -> dict:
    """
    Execute one incremental step of the long-running article generation job.
    State is persisted in WORK_CONTAINER and status in JobStatus table.
    """
    state = state_load(op_id)
    if not state:
        raise RuntimeError("Job state missing")

    status_upsert(op_id, "working", phase=state["phase"])

    item = state["item"]
    meta = state.get("meta") or extract_meta(pick_item(item))
    state["meta"] = meta
    target_words = int(state.get("targetWords", DEFAULT_TARGET_WORDS))

    if state["phase"] == "outline":
        outline = generate_draft_outline(meta, target_words)
        state["outline"] = outline
        state["h3"] = [
            h.strip() for h in outline.get("h3", []) if isinstance(h, str) and h.strip()
        ]
        state["introHtml"] = outline.get("introHtml") or "<h2>Ievads</h2><p>-</p>"
        state["phase"] = "sections"
        progress(op_id, "sections", 0, len(state["h3"]))
        state_save(op_id, state)
        return state

    if state["phase"] == "sections":
        h3 = state["h3"]
        i = int(state.get("sectionIndex", 0))
        total = len(h3)
        if i < total:
            per_sec = calculate_section_words(target_words, total)
            h3_title = h3[i]
            html = generate_section_html_with_validation(meta, h3_title, per_sec)

            words_now = count_words_from_html(html)
            need = int(per_sec * TOPUP_THRESHOLD) - words_now
            if need > 60:
                try:
                    html_extra = topup_section_html(meta, h3_title, need)
                    html = (html.strip() + "\n\n" + html_extra.strip()).strip()
                except Exception:
                    pass

            state["sections"].append({"h3": h3_title, "html": html})
            state["sectionIndex"] = i + 1
            progress(op_id, "sections", i + 1, total)
            if (i + 1) < total:
                state_save(op_id, state)
                return state

        state["phase"] = "finalize"
        state_save(op_id, state)
        return state

    if state["phase"] == "finalize":
        outline = state.get("outline") or {}
        title = outline.get("title") or (
            meta.get("titleHint") or "SharePoint risinÄjumi praksÄ“"
        )
        seo_slug = slugify(
            outline.get("seoSlug") or meta.get("seoSlugHint") or title
        )
        excerpt = outline.get("excerpt", "")
        category = outline.get("category") or meta.get("wpCategory") or "SharePoint"
        tags = outline.get("tags") or []
        tag_slugs = outline.get("tagSlugs") or [slugify(t) for t in tags]

        content_html = _compose_content(
            state.get("introHtml", ""),
            state.get("sections", []),
        )

        total_words_now = count_words_from_html(content_html)
        if total_words_now < int(target_words * FILLER_THRESHOLD):
            filler_target = max(FILLER_MIN_WORDS, target_words - total_words_now + FILLER_BUFFER_WORDS)
            filler_h3 = "Papildu praktiskie scenÄriji un BUJ"
            try:
                filler_html = generate_section_html_with_validation(
                    meta,
                    f"{filler_h3}: ScenÄrijs A, ScenÄrijs B, BUJ (riski, droÅ¡Ä«ba, uzturÄ“Å¡ana)",
                    filler_target,
                )
                content_html = (
                    content_html
                    + "\n\n"
                    + f"<h3>{filler_h3}</h3>\n{filler_html}"
                )
            except Exception:
                pass

        data = refine_full_article(
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

        for _ in range(1):
            issues = quality_issues(data, target_words)
            if not issues:
                break
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

        try:
            if data.get("tags") and data.get("tagSlugs") and not data.get("wpTagIds"):
                if WP_API_BASE:
                    data["wpTagIds"] = ensure_wp_tag_ids(
                        WP_API_BASE,
                        WP_TOKEN,
                        names=data["tags"],
                        slugs=data["tagSlugs"],
                    )
                else:
                    data["wpTagIds"] = []
        except Exception as _e:
            logging.warning(f"[wpTagIds] finalize failed: {_e}")
            data["wpTagIds"] = data.get("wpTagIds") or []

        logging.info(
            "[finalize] tags=%s slugs=%s wpTagIds=%s api_base=%s",
            data.get("tags"),
            data.get("tagSlugs"),
            data.get("wpTagIds"),
            WP_API_BASE,
        )
        status_upsert(
            op_id,
            "working",
            phase="finalize",
            wpTagIdsCount=len(data.get("wpTagIds") or []),
        )

        if "wpTagIds" not in data or data["wpTagIds"] is None:
            data["wpTagIds"] = []

        bc = get_blob_client(op_id)
        bc.upload_blob(
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
            overwrite=True,
        )
        blob_path = f"{RESULT_CONTAINER}/{op_id}.json"
        sas = make_sas_url(op_id)
        status_upsert(
            op_id,
            "done",
            blobPath=blob_path,
            blobUrl=sas or "",
        )

        try:
            _workget_blob_client(op_id).delete_blob()
        except Exception:
            pass

        state["phase"] = "done"
        state_save(op_id, state)
        return state

    return state


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
    op_id = req.route_params.get("opId")
    e = status_get(op_id)
    if not e:
        return bad(404, error="not_found")

    if (e.get("status") or "") not in {"done", "failed"}:
        try:
            _tick_once(op_id)
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
