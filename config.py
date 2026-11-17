"""All the settings in one place. Change a number here and the whole app follows."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "studybuddy.db"

# ---- PDF reading ----
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PAGES = 2000
OCR_MIN_CHARS = 40      # a page with fewer characters than this is treated as a scan
OCR_SCALE = 2           # render scale for OCR (2 is about 150 dpi)
OCR_WORKERS = 3         # pages OCR'd at the same time
CHUNK_CHARS = 24_000    # about 6k tokens per chunk sent to the AI

# ---- AI ----
AI_CONCURRENCY = int(os.getenv("AI_CONCURRENCY") or 2)
REDUCE_CHARS = 40_000   # max size of notes merged in one "combine" step
MAX_ACTIVE_JOBS = 3     # summaries + quizzes writing at the same time

# ---- Quizzes ----
MIN_QUESTIONS = 3
MAX_QUESTIONS = 30
PER_CHUNK_MAX = 20          # never ask for more than this from one chunk
CHARS_PER_QUESTION = 800    # a chunk can supply about one question per this many characters
MIN_CHUNK_CHARS = 400       # chunks with less text than this are skipped
ATTEMPTS_PER_REQUEST = 3    # tries per chunk when the AI returns bad JSON
TOP_UP_ROUNDS = 2           # extra rounds to replace questions lost to validation
QUIZ_COUNTS = (5, 10, 15, 20, 30)
DEFAULT_QUIZ_COUNT = 10
