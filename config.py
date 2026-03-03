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

# =============================================================================
# Article generation — section sizing
# =============================================================================
SECTION_INTRO_SHARE = 0.08         # fraction of total words reserved for intro
SECTION_WORD_BUFFER = 1.15         # per-section word multiplier (GPT underdelivers)
SECTION_MIN_WORDS = 600            # minimum words to request per section

# =============================================================================
# Article generation — validation thresholds
# =============================================================================
SECTION_VALIDATE_MIN_WORDS = 150   # minimum words for a valid section
SECTION_VALIDATE_MIN_PARAS = 3     # minimum <p> tags in a valid section
SECTION_TOPUP_MIN_WORDS = 120      # force topup if section has fewer words
SECTION_TOPUP_NO_LIST_MIN = 200    # force topup if no list and fewer words
TOPUP_MIN_DEFICIT_WORDS = 60       # minimum word deficit to trigger topup in worker
TOPUP_SECTION_MIN_DEFICIT = 120    # minimum deficit passed to topup_section_html()
REFINE_EXPANSION_THRESHOLD = 0.9   # refine adds content if < 90% of target words
PROGRESS_MIN_RATIO = 0.7           # log warning if progress < 70% during generation

# =============================================================================
# Article generation — keyword density
# =============================================================================
KW_DENSITY_MIN_PCT = 1.0           # minimum acceptable keyword density %
KW_DENSITY_MAX_PCT = 1.5           # maximum acceptable keyword density %
KW_SAFETY_NET_DENSITY_PCT = 0.3    # safety net fires only below this density %
KW_SAFETY_NET_MAX_INJECTIONS = 3   # max keyword injections by safety net
KW_INJECT_MIN_PARA_CHARS = 80      # min plain-text chars for injection-eligible <p>

# =============================================================================
# Article generation — LLM token budgets
# =============================================================================
OUTLINE_MAX_TOKENS = 3000          # max_tokens for outline generation call
SECTION_MAX_TOKENS = 3500          # max_tokens for individual section generation
TOPUP_MAX_TOKENS = 1800            # max_tokens for section topup call
MEGA_BATCH_MAX_TOKENS = 8000       # max_tokens cap for mega-mode batch calls
DYNAMIC_TOKENS_BASE = 12000        # base token budget for dynamic calculation
DYNAMIC_TOKENS_PER_1K_WORDS = 2000 # additional tokens per 1000 target words
GPT4O_MAX_OUTPUT_TOKENS = 16384    # Azure GPT-4o max output token limit

# =============================================================================
# Mega mode
# =============================================================================
MEGA_WORD_INFLATION = 1.40         # inflate target words (GPT underdelivers ~30-40%)
MEGA_SECTION_WORD_MULTIPLIER = 1.5 # per-section word multiplier in mega mode
MEGA_SECTION_MIN_WORDS = 250       # minimum words per section in mega mode
MEGA_OUTLINE_H3_MIN = 4            # minimum h3 headings; use default list if fewer
SUMMARIZE_PREVIOUS_MAX_CHARS = 2000  # max chars in context summary passed to batches

# =============================================================================
# Image generation
# =============================================================================
IMAGE_API_TIMEOUT_SEC = 120        # timeout for image API calls
IMAGE_B64_MIN_LEN = 1000           # minimum b64 string length for a valid image
PROMPT_SYNTHESIS_MAX_TOKENS = 300  # max_tokens for prompt synthesis LLM call
PROMPT_SYNTHESIS_TIMEOUT_SEC = 45  # timeout for prompt synthesis LLM call
ALT_TEXT_MAX_LEN = 120             # max chars for image alt text
IMG_DESC_MAX_LEN = 220             # max chars for image description

# =============================================================================
# Facebook copy
# =============================================================================
FB_COPY_MAX_TOKENS = 600           # max_tokens for FB copy generation call

# =============================================================================
# Fallback strings
# =============================================================================
DEFAULT_ARTICLE_TITLE = "SharePoint risinājumi praksē"  # fallback when no title
