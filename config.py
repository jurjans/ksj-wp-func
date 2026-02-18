"""
Centralized configuration — environment variables, defaults, and constants.

All env-var lookups and magic numbers in one place.
Modules import what they need from here instead of calling os.getenv() directly.
"""

import os

# =============================================================================
# Azure Storage
# =============================================================================
STORAGE_CONN_STR = os.getenv("STORAGE", "")

# Table / Queue / Blob names
JOB_TABLE = "JobStatus"
JOB_PK = "wp"
RESULT_CONTAINER = "results"
WORK_CONTAINER = "work"
QUEUE_NAME = "wpjobs"

# =============================================================================
# OpenAI / Azure OpenAI  (text models)
# =============================================================================
OAI_API_KEY = os.getenv("OAI_API_KEY", "")
OAI_BASE_URL = os.getenv("OAI_BASE_URL", "https://api.openai.com/v1")
OAI_MODEL = os.getenv("OAI_MODEL", "gpt-4o-mini")

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")

# =============================================================================
# OpenAI / Azure OpenAI  (image models)
# =============================================================================
FORCE_IMAGE_PROVIDER = os.getenv("FORCE_IMAGE_PROVIDER", "").strip().lower()
AZURE_OPENAI_IMAGE_DEPLOYMENT = os.getenv("AZURE_OPENAI_IMAGE_DEPLOYMENT", "").strip()
AZURE_OPENAI_API_VERSION_IMAGES = os.getenv(
    "AZURE_OPENAI_API_VERSION_IMAGES", AZURE_OPENAI_API_VERSION
)
OAI_IMAGE_MODEL = os.getenv("OAI_IMAGE_MODEL", "gpt-image-1")

# =============================================================================
# Image processing
# =============================================================================
SOCIAL_HEADER_W = 1200
SOCIAL_HEADER_H = 630
SOCIAL_HEADER_RATIO = SOCIAL_HEADER_W / SOCIAL_HEADER_H

ALLOWED_IMAGE_SIZES = {(1024, 1024), (1536, 1024), (1024, 1536)}

# If cropping loses more than this fraction of area, use "contain" instead
CROP_LOSS_THRESHOLD = 0.18

DEFAULT_FIT_MODE = os.getenv("IMAGE_FIT_MODE", "auto").lower()

# =============================================================================
# SEO image meta
# =============================================================================
SEO_KEYWORD = os.getenv("KSJ_SEO_KEYWORD", "datu sinhronizācija")
SEO_DESC_SUFFIX = os.getenv(
    "KSJ_SEO_DESC_SUFFIX",
    "Bez dublikātiem, uzlabota datu kvalitāte un uzticami atjauninājumi.",
)

# =============================================================================
# Article generation
# =============================================================================
DEFAULT_TARGET_WORDS = 5000
TOPUP_THRESHOLD = 0.95       # request top-up if section < 95% of target
FILLER_THRESHOLD = 0.85      # add filler section if total < 85% of target
FILLER_MIN_WORDS = 500
FILLER_BUFFER_WORDS = 300

# =============================================================================
# WordPress
# =============================================================================
WP_API_BASE = os.getenv("WP_API_BASE", "")
WP_TOKEN = os.getenv("WP_TOKEN", "")

# =============================================================================
# Facebook / external links
# =============================================================================
BOOK_LINK = os.getenv("BOOK_LINK", "https://book.jurjans.dev")

# =============================================================================
# SAS URL
# =============================================================================
SAS_HOURS_VALID = 24
