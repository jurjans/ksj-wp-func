"""
Image generation helpers: prompt synthesis, API calls, resize/crop, SEO meta.

Handles OpenAI and Azure OpenAI image endpoints, with automatic
provider detection based on environment variables.
"""

import datetime
import io
import os
import re
import unicodedata
import urllib.error
from base64 import b64decode, b64encode

from PIL import Image

from article_gen import (
    is_azure_openai,
    get_url,
    get_headers,
    http_post_json,
)

# =============================================================================
# Constants
# =============================================================================
SOCIAL_HEADER_W = 1200
SOCIAL_HEADER_H = 630
SOCIAL_HEADER_RATIO = SOCIAL_HEADER_W / SOCIAL_HEADER_H

ALLOWED_SIZES = {(1024, 1024), (1536, 1024), (1024, 1536)}

# Threshold: if cropping loses more than 18% of area, use "contain" instead
CROP_LOSS_THRESHOLD = 0.18

# SEO defaults (overridable via env)
SEO_KEYWORD = os.getenv("KSJ_SEO_KEYWORD", "datu sinhronizācija")
SEO_DESC_SUFFIX = os.getenv(
    "KSJ_SEO_DESC_SUFFIX",
    "Bez dublikātiem, uzlabota datu kvalitāte un uzticami atjauninājumi.",
)


# =============================================================================
# Provider URL / headers
# =============================================================================
def get_images_url() -> str:
    """
    Build image generation endpoint URL.
    Priority: FORCE_IMAGE_PROVIDER > AZURE_OPENAI_IMAGE_DEPLOYMENT > OpenAI fallback.
    """
    force = (os.getenv("FORCE_IMAGE_PROVIDER", "") or "").strip().lower()
    dep = (os.getenv("AZURE_OPENAI_IMAGE_DEPLOYMENT", "") or "").strip()

    if force == "openai":
        base = (os.getenv("OAI_BASE_URL", "https://api.openai.com/v1") or "").rstrip("/")
        return f"{base}/images/generations"

    if force == "azure" or dep:
        if not dep:
            raise RuntimeError(
                "FORCE_IMAGE_PROVIDER=azure, bet AZURE_OPENAI_IMAGE_DEPLOYMENT nav iestatīts"
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

    # Fallback: OpenAI
    base = (os.getenv("OAI_BASE_URL", "https://api.openai.com/v1") or "").rstrip("/")
    return f"{base}/images/generations"


def get_images_headers() -> dict:
    """Image auth headers — Bearer for OpenAI, api-key for Azure."""
    force = (os.getenv("FORCE_IMAGE_PROVIDER", "") or "").strip().lower()
    dep = (os.getenv("AZURE_OPENAI_IMAGE_DEPLOYMENT", "") or "").strip()

    if force == "openai" or not dep:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.getenv('OAI_API_KEY', '')}",
        }
    return {
        "Content-Type": "application/json",
        "api-key": os.getenv("AZURE_OPENAI_API_KEY", ""),
    }


def get_provider_info() -> dict:
    """Return current provider config for diagnostics."""
    force = (os.getenv("FORCE_IMAGE_PROVIDER", "") or "").strip().lower()
    dep = (os.getenv("AZURE_OPENAI_IMAGE_DEPLOYMENT", "") or "").strip()
    url = get_images_url()
    provider = "openai" if "api.openai.com" in url else "azure"
    return {
        "provider": provider,
        "force": force,
        "deployment": dep,
        "imagesUrl": url,
        "has_OAI_KEY": bool(os.getenv("OAI_API_KEY")),
        "has_AZURE_TEXT": bool(
            os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_API_KEY")
        ),
    }


# =============================================================================
# Size helpers
# =============================================================================
def coerce_size(w: int, h: int) -> tuple[int, int]:
    """Snap requested dimensions to nearest allowed DALL·E size."""
    if (w, h) in ALLOWED_SIZES:
        return w, h
    return (1536, 1024) if w >= h else (1024, 1536)


def get_model() -> str:
    """Return the text model name for prompt synthesis."""
    return (
        os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
        if is_azure_openai()
        else os.getenv("OAI_MODEL", "gpt-4o-mini")
    )


# =============================================================================
# SEO image meta
# =============================================================================
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _lv_slug(s: str) -> str:
    """Latvian-aware slug: transliterates diacritics, then ASCII-only."""
    s = s.lower().strip()
    mapping = str.maketrans({
        "ā": "a", "č": "c", "ē": "e", "ģ": "g", "ī": "i",
        "ķ": "k", "ļ": "l", "ņ": "n", "š": "s", "ū": "u", "ž": "z",
        "Ā": "a", "Č": "c", "Ē": "e", "Ģ": "g", "Ī": "i",
        "Ķ": "k", "Ļ": "l", "Ņ": "n", "Š": "s", "Ū": "u", "Ž": "z",
    })
    s = s.translate(mapping)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9\-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "attels"


def _trunc(s: str, n: int) -> str:
    s = _norm(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def build_image_meta(ctx: dict, prompt_used: str, ext: str = ".png") -> dict:
    """
    Build SEO-optimized image metadata (alt, caption, description, filename).
    """
    title = _norm(ctx.get("title") or "")
    if not title:
        words = re.split(r"[,.\s]+", _norm(prompt_used))
        title = " ".join(words[:10]) if words else "Datu sinhronizācija"

    alt = title
    if SEO_KEYWORD.lower() not in alt.lower():
        alt = f"{alt} — {SEO_KEYWORD}"
    alt = _trunc(alt, 120)

    caption = title
    desc = _trunc(f"{title}. {SEO_DESC_SUFFIX}", 220)

    stamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    fname = f"{_lv_slug(title)}-{stamp}{ext or '.png'}"

    return {
        "alt_text": alt,
        "caption": caption,
        "description": desc,
        "file_name": fname,
    }


# =============================================================================
# Prompt synthesis
# =============================================================================
def synthesize_image_prompt(ctx: dict, style_hint: str) -> str:
    """Use LLM to generate an image prompt from article context."""
    system = (
        "You write a single high-quality image prompt for Azure OpenAI Images (DALL·E 3 / gpt-image). "
        "Constraints: 1200x630 social header, modern, clean, metaphorical visual. "
        "No text overlays, no logos or trademarks, no faces or personal data, no political content. "
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
        "max_tokens": 300,
        "temperature": 0.7,
    }

    outer = http_post_json(get_url(), get_headers(), payload, timeout_sec=45)
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


def build_fallback_prompt(ctx: dict) -> str:
    """Simple fallback prompt when LLM synthesis fails."""
    title = (ctx.get("title") or "").strip()
    primary = (ctx.get("primary") or "").strip()
    angle = (ctx.get("angle") or "").strip()
    audience = (ctx.get("audience") or "").strip()
    return " ".join([
        "Create a Facebook header image.",
        f"Topic: {title}.",
        f"Primary: {primary}." if primary else "",
        f"Angle: {angle}." if angle else "",
        f"Audience: {audience}." if audience else "",
        "Clean, minimalist, high-contrast. No text, no logos, no trademarks.",
    ]).strip()


# =============================================================================
# Image resize / crop
# =============================================================================
def _crop_cover(img: Image.Image) -> Image.Image:
    """Crop image to social header aspect ratio (center crop)."""
    w, h = img.width, img.height
    if (w / h) > SOCIAL_HEADER_RATIO:
        new_w = int(h * SOCIAL_HEADER_RATIO)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / SOCIAL_HEADER_RATIO)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    return img


def _pad_contain(img: Image.Image, bg=(248, 248, 248, 255)) -> Image.Image:
    """Scale image to fit within social header, pad remaining area."""
    scale = min(SOCIAL_HEADER_W / img.width, SOCIAL_HEADER_H / img.height)
    new_w, new_h = int(img.width * scale), int(img.height * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGBA", (SOCIAL_HEADER_W, SOCIAL_HEADER_H), bg)
    off = ((SOCIAL_HEADER_W - new_w) // 2, (SOCIAL_HEADER_H - new_h) // 2)
    canvas.paste(img, off, img)
    return canvas


def fit_to_social_header(b64_input: str, fit_mode: str = "auto") -> tuple[str, int, int]:
    """
    Resize/crop base64 PNG to 1200x630 social header.
    Returns (b64_output, width, height).
    """
    raw = b64decode(b64_input)
    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    w0, h0 = img.width, img.height

    mode = fit_mode
    if mode == "auto":
        if (w0 / h0) > SOCIAL_HEADER_RATIO:
            cover_w, cover_h = int(h0 * SOCIAL_HEADER_RATIO), h0
        else:
            cover_w, cover_h = w0, int(w0 / SOCIAL_HEADER_RATIO)
        kept = (cover_w * cover_h) / (w0 * h0)
        mode = "contain" if (1 - kept) > CROP_LOSS_THRESHOLD else "cover"

    if mode == "cover":
        img = _crop_cover(img)
        img = img.resize((SOCIAL_HEADER_W, SOCIAL_HEADER_H), Image.LANCZOS)
    else:
        img = _pad_contain(img)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return b64encode(buf.getvalue()).decode("ascii"), SOCIAL_HEADER_W, SOCIAL_HEADER_H


# =============================================================================
# Full image generation call
# =============================================================================
def generate_image_b64(
    prompt: str,
    w: int,
    h: int,
    fit_mode: str = "auto",
) -> dict:
    """
    Call image API and return processed result dict.
    Raises on API errors.
    """
    url = get_images_url()
    headers = get_images_headers()
    provider = "openai" if "api.openai.com" in url else "azure"

    body = {"prompt": prompt, "size": f"{w}x{h}", "n": 1}
    if provider == "openai":
        body["model"] = os.getenv("OAI_IMAGE_MODEL", "gpt-image-1")

    outer = http_post_json(url, headers, body, timeout_sec=120)

    data = outer.get("data") or []
    b64 = data[0].get("b64_json") if data else None
    if not b64 or not isinstance(b64, str) or not b64.strip():
        raise RuntimeError(f"no image in response: {str(outer)[:400]}")
    if len(b64) < 1000:
        raise RuntimeError(f"image too small: {str(outer)[:400]}")

    # Resize to social header
    try:
        b64, w, h = fit_to_social_header(b64, fit_mode)
    except Exception:
        pass  # return original if resize fails

    return {
        "imageBase64": b64,
        "ext": ".png",
        "width": w,
        "height": h,
        "provider": provider,
        "imagesUrl": url[:120],
    }
