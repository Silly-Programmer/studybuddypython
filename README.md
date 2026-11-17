# 📚 StudyBuddy (Python)

Upload a PDF (typed or scanned), get easy notes with page references, then test yourself with a quiz.
Same features as the Next.js version, but in plain Python: no login, no cloud database, nothing to build.

**Made to run on your own computer.** There is no login, so anyone who can open the page can see your files.
Keep it on `127.0.0.1` (the default) and don't put it on the internet as it is.

## Set up (5 minutes)

1. **Python 3.10 or newer** ([python.org](https://www.python.org/downloads/)).
2. **Install the libraries**
   ```bash
   pip install -r requirements.txt
   ```
3. **Install Tesseract** (only needed for *scanned* PDFs; typed PDFs work without it)
   - Windows: install from <https://github.com/UB-Mannheim/tesseract/wiki>. If the app says it can't find it, set `TESSERACT_CMD` in `.env` (see below).
   - Mac: `brew install tesseract`
   - Linux: `sudo apt install tesseract-ocr`
4. **Add your AI key**: copy `.env.example` to `.env`, get a free key at <https://aistudio.google.com> (Get API key), paste it as `GEMINI_API_KEY`. Check the model name in AI Studio and put it in `GEMINI_MODEL` (names change over time).
   ```bash
   cp .env.example .env          # Windows: copy .env.example .env
   ```
5. **Run**
   ```bash
   python app.py
   ```
   Open <http://127.0.0.1:5000>.

## Backups when the AI is busy (optional)

Set any of these in `.env`. They are tried in this order when the main model is rate limited or down:

| Setting | What it does |
|---|---|
| `GEMINI_FALLBACK_MODELS` | Other models on the same key. Each model has its own limits. |
| `GEMINI_EXTRA_KEYS` | Keys from **other Google projects**. Keys inside one project share one quota, so extra keys from the same project don't help. |
| `FALLBACK_API_URL` / `_KEY` / `_MODEL` | Any OpenAI-compatible service, e.g. Groq or OpenRouter. |

## Where things are

| File | What it does |
|---|---|
| `app.py` | The web pages and the small API behind them |
| `pdf_reader.py` | Reads a PDF, OCRs scanned pages only, cleans text, splits it into page-tagged chunks |
| `ai.py` | Talks to the AI, with retries and the backup list |
| `summaries.py` | The four summary types (one AI call per chunk, then merged for "Quick") |
| `quizzes.py` | Plans questions per chunk, validates the AI's JSON, removes duplicates, tops up |
| `jobs.py` | Runs the PDF job in the background so the page stays responsive |
| `db.py` | SQLite (a single file in `data/`) |
| `config.py` | Every limit in one place (pages, chunk size, questions per page of text, ...) |
| `templates/`, `static/` | The web pages (HTML + Tailwind from a CDN) and a little JavaScript |

Your files and results live in `data/` (the PDFs in `data/uploads/`, everything else in `data/studybuddy.db`).
Delete that folder to start fresh.

## Good to know

- **Scanned PDFs are slow** because OCR runs on your own CPU. Typed PDFs take seconds.
- **Free AI tier:** limits are strict, so big documents take a few minutes, and Google may use free-tier content to improve its products. Avoid confidential PDFs.
- **If the app restarts mid-job**, that job is marked failed. Use "Try again" for documents, or delete and recreate a summary or quiz.
- **The page loads Tailwind and the Markdown renderer from a CDN**, so the browser needs internet (the PDF work itself does not).
- Quiz size: one question per about 800 characters of text (change `CHARS_PER_QUESTION` in `config.py`).
