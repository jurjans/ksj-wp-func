"""
Article generation worker — tick-based incremental processing.

Each call to tick_once() advances the job by one step:
  outline → sections (one per tick) → finalize → done

State is persisted in Blob Storage between ticks.
Status is tracked in Table Storage for polling.
"""

import json
import logging

from storage import (
    get_blob_client,
    get_work_blob_client,
    make_sas_url,
    status_upsert,
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
    TOPUP_MIN_DEFICIT_WORDS,
    DEFAULT_ARTICLE_TITLE,
)

from article_gen import (
    pick_item,
    extract_meta,
    generate_draft_outline,
    generate_section_html_with_validation,
    topup_section_html,
    refine_full_article,
    ensure_wp_tag_ids,
    count_words_from_html,
    calculate_section_words,
    quality_issues,
    slugify,
    sanitize_html,
    normalize_lv_headings,
    normalize_tags,
    build_wp_article_mega,
    ARTICLE_MODE,
)


# =============================================================================
# Content assembly
# =============================================================================
def compose_content(intro_html: str, sections: list[dict]) -> str:
    """Join intro + sections into single sanitized HTML string."""
    parts = [intro_html.strip()] if intro_html else []
    for s in sections:
        parts.append(f"<h3>{s['h3']}</h3>\n{s['html']}")
    return sanitize_html(normalize_lv_headings("\n\n".join(parts)))


# =============================================================================
# Phase handlers
# =============================================================================
def _phase_outline(op_id: str, state: dict, meta: dict, target_words: int) -> dict:
    """Generate article outline with H3 headings and intro."""
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


def _phase_sections(op_id: str, state: dict, meta: dict, target_words: int) -> dict:
    """Generate one section per tick. Advances to finalize when all done."""
    h3 = state["h3"]
    i = int(state.get("sectionIndex", 0))
    total = len(h3)

    if i < total:
        per_sec = calculate_section_words(target_words, total)
        h3_title = h3[i]
        prev_h3 = [s["h3"] for s in state.get("sections", [])]  # already generated
        html = generate_section_html_with_validation(meta, h3_title, per_sec, previous_sections=prev_h3)

        words_now = count_words_from_html(html)
        need = int(per_sec * TOPUP_THRESHOLD) - words_now
        if need > TOPUP_MIN_DEFICIT_WORDS:
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


def _phase_finalize(op_id: str, state: dict, meta: dict, target_words: int) -> dict:
    """Assemble, refine, quality-check, and store the final article."""
    outline = state.get("outline") or {}
    title = outline.get("title") or (
        meta.get("titleHint") or DEFAULT_ARTICLE_TITLE
    )
    seo_slug = slugify(
        outline.get("seoSlug") or meta.get("seoSlugHint") or title
    )
    excerpt = outline.get("excerpt", "")
    category = outline.get("category") or meta.get("wpCategory") or "SharePoint"
    tags = outline.get("tags") or []
    tag_slugs = outline.get("tagSlugs") or [slugify(t) for t in tags]

    content_html = compose_content(
        state.get("introHtml", ""),
        state.get("sections", []),
    )

    # Add filler section if article is too short
    total_words_now = count_words_from_html(content_html)
    if total_words_now < int(target_words * FILLER_THRESHOLD):
        filler_target = max(FILLER_MIN_WORDS, target_words - total_words_now + FILLER_BUFFER_WORDS)
        filler_h3 = "Papildu praktiskie scenāriji un BUJ"
        try:
            filler_html = generate_section_html_with_validation(
                meta,
                f"{filler_h3}: Scenārijs A, Scenārijs B, BUJ (riski, drošība, uzturēšana)",
                filler_target,
            )
            content_html = (
                content_html + "\n\n" + f"<h3>{filler_h3}</h3>\n{filler_html}"
            )
        except Exception:
            pass

    # Refine full article
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

    # One quality-check pass
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

    # Ensure WordPress tag IDs
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

    # Store result blob
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

    # Clean up work blob
    try:
        get_work_blob_client(op_id).delete_blob()
    except Exception:
        pass

    state["phase"] = "done"
    state_save(op_id, state)
    return state


# =============================================================================
# Mega-prompt phase (single-call, completes in one tick)
# =============================================================================
def _phase_mega(op_id: str, state: dict, meta: dict, target_words: int) -> dict:
    """Generate complete article in one API call using mega-prompt."""
    progress(op_id, "mega", 0, 1)

    data = build_wp_article_mega(meta, target_words)

    # Apply tag normalization + WP tag IDs (same as finalize)
    data = normalize_tags(data, meta)
    try:
        names = data.get("tags") or []
        slugs = data.get("tagSlugs") or []
        if names and slugs and not data.get("wpTagIds"):
            if WP_API_BASE:
                data["wpTagIds"] = ensure_wp_tag_ids(
                    WP_API_BASE, WP_TOKEN, names=names, slugs=slugs,
                )
            else:
                data["wpTagIds"] = []
    except Exception as _e:
        logging.warning(f"[wpTagIds] mega mode failed: {_e}")
        data["wpTagIds"] = data.get("wpTagIds") or []

    if "wpTagIds" not in data or data["wpTagIds"] is None:
        data["wpTagIds"] = []

    # Store result blob
    bc = get_blob_client(op_id)
    bc.upload_blob(
        json.dumps(data, ensure_ascii=False).encode("utf-8"),
        overwrite=True,
    )
    blob_path = f"{RESULT_CONTAINER}/{op_id}.json"
    sas = make_sas_url(op_id)
    status_upsert(op_id, "done", blobPath=blob_path, blobUrl=sas or "")

    # Clean up work blob
    try:
        get_work_blob_client(op_id).delete_blob()
    except Exception:
        pass

    state["phase"] = "done"
    state_save(op_id, state)
    return state


# =============================================================================
# Main entry point
# =============================================================================
PHASE_HANDLERS = {
    "outline": _phase_outline,
    "sections": _phase_sections,
    "finalize": _phase_finalize,
    "mega": _phase_mega,
}


def tick_once(op_id: str) -> dict:
    """
    Execute one incremental step of a long-running article generation job.

    Returns the updated state dict.
    Raises RuntimeError if job state is missing.
    """
    state = state_load(op_id)
    if not state:
        raise RuntimeError("Job state missing")

    # Route to mega mode if configured (on first tick, override phase)
    if state["phase"] == "outline":
        item = state["item"]
        picked = pick_item(item) if isinstance(item, dict) else item
        mode = (
            (picked.get("articleMode") if isinstance(picked, dict) else None)
            or ARTICLE_MODE
        ).strip().lower()
        if mode == "mega":
            state["phase"] = "mega"
            logging.info(f"[worker] Routing job {op_id} to MEGA mode")

    status_upsert(op_id, "working", phase=state["phase"])

    item = state["item"]
    meta = state.get("meta") or extract_meta(pick_item(item))
    state["meta"] = meta
    target_words = int(state.get("targetWords", DEFAULT_TARGET_WORDS))

    handler = PHASE_HANDLERS.get(state["phase"])
    if handler:
        return handler(op_id, state, meta, target_words)

    return state
