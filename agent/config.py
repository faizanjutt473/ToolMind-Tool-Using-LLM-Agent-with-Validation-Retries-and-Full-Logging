"""Central configuration. Values can be overridden through the .env file."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# --- Paths -----------------------------------------------------------------
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
RESULTS_DIR = BASE_DIR / "results"
DB_PATH = DATA_DIR / "company.db"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# --- LLM -------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# gemini-2.5-flash was retired on 16 Oct 2026. Change this in .env if the model
# name below is rejected (run `python main.py --list-models` to see valid names).
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_INCLUDE_THOUGHTS = _env_bool("GEMINI_INCLUDE_THOUGHTS", True)
# None = use the model's default (recommended for Gemini 3 models).
GEMINI_TEMPERATURE = (
    _env_float("GEMINI_TEMPERATURE", 0.0) if os.getenv("GEMINI_TEMPERATURE") else None
)
LLM_MAX_RETRIES = _env_int("LLM_MAX_RETRIES", 4)

# --- Agent loop ------------------------------------------------------------
MAX_STEPS = _env_int("MAX_STEPS", 8)  # one step = one LLM call
TOOL_MAX_ATTEMPTS = _env_int("TOOL_MAX_ATTEMPTS", 3)  # first try + 2 retries
TOOL_RETRY_BASE_DELAY = _env_float("TOOL_RETRY_BASE_DELAY", 1.0)  # seconds, doubles each retry
MAX_IDENTICAL_CALLS = 2  # the 3rd identical tool call in one run is blocked (loop guard)
MAX_TOOL_OUTPUT_CHARS = 6000  # hard cap on what is sent back to the model

# --- Tool limits -----------------------------------------------------------
WEB_TIMEOUT_SECONDS = 10
WEB_MAX_RESULTS = 10
DB_MAX_ROWS = 50
FILE_DEFAULT_MAX_CHARS = 4000
FILE_HARD_MAX_CHARS = 20000
ALLOWED_FILE_EXTENSIONS = (".txt", ".md", ".csv", ".json", ".pdf")
