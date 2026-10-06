import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD")
EMAIL_SMTP_HOST = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
EMAIL_SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", "587"))
EMAIL_INGESTION_INTERVAL_MINUTES = int(os.getenv("EMAIL_INGESTION_INTERVAL_MINUTES", "15"))
# Comma-separated senders or domains allowed to trigger ingestion, e.g.
# "csp1@example.com,*.sbi.co.in". Empty (default) means NOT enforced --
# confirm the real policy with your org before setting this, since an
# overly strict allowlist silently drops legitimate agreement emails.
EMAIL_SENDER_ALLOWLIST = [
    s.strip() for s in os.getenv("EMAIL_SENDER_ALLOWLIST", "").split(",") if s.strip()
]

AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

# Free Open-Source / High-Speed LLM Fallback (Groq / NVIDIA)
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# OCR Engine configuration: 'tesseract' (default, eng+hin) or 'easyocr' (PyTorch)
OCR_ENGINE = os.getenv("OCR_ENGINE", "tesseract")

# Feature flags for the extraction pipeline. Both default to the cheap,
# already-working path; flip to the richer path only after comparing
# output quality on real (sanitized) sample documents -- see README.
#   DOCUMENT_AI_MODE=text        -> extract text with the parser below, send text to the LLM
#   DOCUMENT_AI_MODE=multimodal  -> send page images directly to a vision-capable model (not yet implemented)
#   DOCUMENT_PARSER=pymupdf      -> fast, works well on digital PDFs with selectable text
#   DOCUMENT_PARSER=docling      -> layout-aware, for tables/multi-column forms (not yet implemented)
DOCUMENT_AI_MODE = os.getenv("DOCUMENT_AI_MODE", "text")
DOCUMENT_PARSER = os.getenv("DOCUMENT_PARSER", "pymupdf")

# Default based on ONE real sample (digit + letter + 6 digits, e.g.
# "1A852474" -- no "CSP" prefix, contrary to the earlier unconfirmed
# guess). Still not verified as the universal format across all 500+
# CSPs -- override via env once the real specification is confirmed.
CSP_CODE_REGEX = os.getenv("CSP_CODE_REGEX", r"\b\d[A-Z]\d{6}\b")

# Confidence-based routing thresholds. Tune these after testing against
# real sample documents -- they are starting points, not guarantees.
AI_AUTO_REVIEW_THRESHOLD = float(os.getenv("AI_AUTO_REVIEW_THRESHOLD", "0.90"))
AI_HUMAN_REVIEW_THRESHOLD = float(os.getenv("AI_HUMAN_REVIEW_THRESHOLD", "0.70"))

MAX_UPLOAD_SIZE_BYTES = int(os.getenv("MAX_UPLOAD_SIZE_BYTES", str(10 * 1024 * 1024)))
ALLOWED_MIME_TYPES = {"application/pdf", "image/jpeg", "image/png"}

# STORAGE_BACKEND=local for dev (Render's free tier has EPHEMERAL disk --
# never use "local" there, files vanish on every restart/redeploy).
# STORAGE_BACKEND=supabase for anything deployed -- uses the free 1GB
# Storage bucket in the same Supabase project as the database.
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "csp-documents")

# ---------------------------------------------------------------------------
# Settings added for the autonomous renewal agent. Every one has a safe
# default so the app runs before .env is updated.
# ---------------------------------------------------------------------------
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Kolkata")

# Calling sheet. "local_xlsx" reads ONLY the CALLING_SHEET_TAB tab of the
# local workbook, read-only. "google" reads the live sheet (deployment).
CALLING_SHEET_SOURCE = os.getenv("CALLING_SHEET_SOURCE", "local_xlsx")
CALLING_SHEET_LOCAL_PATH = os.getenv("CALLING_SHEET_LOCAL_PATH", "CSP Details.xlsx")
CALLING_SHEET_TAB = os.getenv("CALLING_SHEET_TAB", "Calling Sheet New")
CALLING_SHEET_SPREADSHEET_ID = os.getenv("CALLING_SHEET_SPREADSHEET_ID", "")

# Outbound. "review": everything is a draft until approved on the dashboard.
# "auto": sent automatically (deployment). Anything else is treated as review.
OUTBOUND_COMMUNICATION_MODE = os.getenv("OUTBOUND_COMMUNICATION_MODE", "review").strip().lower()
if OUTBOUND_COMMUNICATION_MODE not in ("review", "auto"):
    OUTBOUND_COMMUNICATION_MODE = "review"

# WhatsApp agent: "stub" (log only), "push" (we POST to it), "pull" (it polls us).
WHATSAPP_MODE = os.getenv("WHATSAPP_MODE", "stub").strip().lower()
WHATSAPP_AGENT_URL = os.getenv("WHATSAPP_AGENT_URL", "")
WHATSAPP_AGENT_TOKEN = os.getenv("WHATSAPP_AGENT_TOKEN", "")
# WHATSAPP_MODE=wabs: at most this many WhatsApp messages approved per day
# (a careless "approve all" can never become a mass send).
WHATSAPP_DAILY_LIMIT = int(os.getenv("WHATSAPP_DAILY_LIMIT", "5") or 5)

# Upload portal and public links.
PORTAL_SECRET_KEY = os.getenv("PORTAL_SECRET_KEY", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
PORTAL_TOKEN_DAYS = int(os.getenv("PORTAL_TOKEN_DAYS", "7"))

# API keys. Empty ADMIN_API_KEY = dashboard/API open (local testing only;
# a warning is logged at startup). /api/agent/* needs AGENT_API_KEY.
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")

# Local vision model on the rack server (Ollama), free and on-premises.
LOCAL_VLM_URL = os.getenv("LOCAL_VLM_URL", "http://127.0.0.1:11434").rstrip("/")
LOCAL_VLM_MODEL = os.getenv("LOCAL_VLM_MODEL", "qwen2.5vl:7b")
LOCAL_VLM_TIMEOUT_SECONDS = int(os.getenv("LOCAL_VLM_TIMEOUT_SECONDS", "60"))
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")

# OCR
OCR_MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "12"))
OCR_DPI = int(os.getenv("OCR_DPI", "220"))
# Laplacian variance below this = blurry photo (tuned on phone photos).
BLUR_THRESHOLD = float(os.getenv("BLUR_THRESHOLD", "60"))
MIN_IMAGE_SIDE_PX = int(os.getenv("MIN_IMAGE_SIDE_PX", "900"))
# How many Tesseract runs may happen at once (pages and attachments share
# this). Default: one less than the CPU count, at most 3.
OCR_WORKERS = max(1, int(os.getenv("OCR_WORKERS", str(max(1, min(3, (os.cpu_count() or 2) - 1))))))
# Cache OCR text per file (by SHA-256) next to the vault, so re-reading a
# document with improved rules never repeats the OCR.
OCR_CACHE = os.getenv("OCR_CACHE", "on").strip().lower() not in ("0", "off", "false", "no")
# Second OCR engine (RapidOCR = PaddleOCR models on ONNX, CPU) for pages
# Tesseract reads with low confidence: phone photos, stamps, faint print.
# "off" disables it; it is skipped automatically when not installed.
OCR_SECOND_ENGINE = os.getenv("OCR_SECOND_ENGINE", "rapidocr").strip().lower()
# Share of automatically accepted documents sent to the Review queue as a
# spot check, to measure real accuracy (app/accuracy.py). 0 turns it off.
SPOT_CHECK_RATE = float(os.getenv("SPOT_CHECK_RATE", "0.05"))
OCR_SECOND_ENGINE_BELOW = float(os.getenv("OCR_SECOND_ENGINE_BELOW", "80"))

# Gmail
GMAIL_BACKFILL_DAYS = int(os.getenv("GMAIL_BACKFILL_DAYS", "730"))
GMAIL_SCAN_INTERVAL_MINUTES = int(os.getenv("GMAIL_SCAN_INTERVAL_MINUTES", str(EMAIL_INGESTION_INTERVAL_MINUTES)))
INTERNAL_EMAIL_DOMAIN = os.getenv("INTERNAL_EMAIL_DOMAIN", "eko.co.in").lower()

# The document vault (app/vault.py). A relative value is resolved against the
# project folder, never the folder the process was started from. On the rack
# server set an absolute path, e.g. /srv/sbi_kiosk/documents.
STORAGE_ROOT = os.getenv("STORAGE_ROOT", "storage/sbi_kiosk/documents")
# When the vault is a network mount (e.g. the rack server over SSHFS), set
# this to on: nothing is written unless STORAGE_ROOT holds the marker file
# .csp_vault, so a dropped mount can't silently fill an empty local folder.
STORAGE_REQUIRE_MARKER = os.getenv("STORAGE_REQUIRE_MARKER", "off").strip().lower() in ("1", "on", "true", "yes")
# Nightly vault job: refresh expiry, rename files, rebuild INDEX.xlsx.
VAULT_JOB_HOUR = int(os.getenv("VAULT_JOB_HOUR", "0"))
VAULT_JOB_MINUTE = int(os.getenv("VAULT_JOB_MINUTE", "10"))
