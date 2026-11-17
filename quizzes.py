"""Quizzes: plan how many questions each chunk gives, ask the AI, validate, drop duplicates, top up."""
import json
import threading
import math
import random
import re
from concurrent.futures import ThreadPoolExecutor

import ai
import config
import db

DIFFICULTIES = ("Easy", "Medium", "Hard")

SYSTEM = """You write multiple-choice exam questions for students from textbook text.
Rules:
- Use ONLY the text you are given. Never use outside knowledge. Every question must be answerable from the text.
- Each question has exactly 4 options. Exactly ONE option is correct; the other three are plausible but clearly wrong according to the text.
- Do not write "all of the above", "none of the above" or "both A and B". Keep the options similar in length.
- Test understanding of important ideas, not trivia, page numbers or the layout of the text.
- The text contains markers like [[Page 12]]. For each question, set sourcePage to the page number where the answer is found. Never invent a page number.
- Never mention "the text", "the passage" or the markers inside a question; write questions that make sense on their own.
- Output ONLY a JSON array. No Markdown, no code fences, no commentary."""

DIFFICULTY_HINT = {
    "Easy": "Easy: direct recall of key definitions and facts stated clearly in the text.",
    "Medium": "Medium: understanding and simple application; the student must explain or distinguish ideas, not just recall words.",
    "Hard": "Hard: analysis and reasoning across several points in the text, close distractors, and cases that need careful reading.",
}

FORMAT = """Each array item must look exactly like this:
{"question": "...", "options": ["...", "...", "...", "..."], "correctAnswer": "B", "explanation": "One or two sentences saying why the answer is right.", "sourcePage": 12}
"correctAnswer" is the letter (A, B, C or D) of the correct option. Spread the correct answers across A, B, C and D."""


def quiz_prompt(text: str, count: int, difficulty: str, avoid: list[str]) -> str:
    avoid_block = ""
    if avoid:
        avoid_block = "\nDo NOT repeat or rephrase these questions that already exist:\n" + "\n".join(f"- {q}" for q in avoid) + "\n"
    s = "" if count == 1 else "s"
    return f"Write exactly {count} multiple-choice question{s} from the text below.\nDifficulty: {DIFFICULTY_HINT[difficulty]}\n{FORMAT}\n{avoid_block}\nText:\n{text}"


# ---------- how many questions can a document give? ----------
def chunk_capacity(length: int) -> int:
    if length < config.MIN_CHUNK_CHARS:
        return 0
    return max(1, min(config.PER_CHUNK_MAX, length // config.CHARS_PER_QUESTION))


def quiz_capacity(chunks: list[dict]) -> int:
    return min(config.MAX_QUESTIONS, sum(chunk_capacity(len(c["content"])) for c in chunks))


def count_options(maximum: int) -> list[int]:
    """The counts to offer: presets the document can really give, plus the maximum itself. Empty = too short."""
    m = int(maximum)
    if m < config.MIN_QUESTIONS:
        return []
    options = [n for n in config.QUIZ_COUNTS if n <= m]
    if m not in options:
        options.append(m)
    return options


def _spread_order(n: int) -> list[int]:
    """0,4,2,6,1,5,3,7 ...: any prefix of this order is spread evenly over 0..n-1."""
    bits = max(1, math.ceil(math.log2(max(2, n))))

    def rev(i):
        r = 0
        for b in range(bits):
            if i & (1 << b):
                r |= 1 << (bits - 1 - b)
        return r

    return sorted(range(n), key=rev)


def allocate(lengths: list[int], target: int) -> list[int]:
    """How many questions to ask from each chunk, so the quiz covers the whole document."""
    caps = [chunk_capacity(n) for n in lengths]
    eligible = [i for i, c in enumerate(caps) if c > 0]
    total = min(target, sum(caps))
    out = [0] * len(lengths)
    if not eligible or total <= 0:
        return out
    order = _spread_order(len(eligible))
    rank = {eligible[pos]: r for r, pos in enumerate(order)}
    if total <= len(eligible):  # fewer questions than chunks: one each from chunks spread over the document
        for pos in order[:total]:
            out[eligible[pos]] = 1
        return out
    for i in eligible:
        out[i] = 1
    for _ in range(total - len(eligible)):  # then give extra questions to the chunks with the most room
        best = -1
        for i in eligible:
            if out[i] >= caps[i]:
                continue
            if best == -1 or (out[i] / caps[i], rank[i]) < (out[best] / caps[best], rank[best]):
                best = i
        if best == -1:
            break
        out[best] += 1
    return out


# ---------- checking what the AI sends back ----------
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def is_duplicate(a: str, b: str) -> bool:
    """True when two questions are the same or nearly the same (70% or more word overlap)."""
    if _norm(a) == _norm(b):
        return True
    wa = {w for w in _norm(a).split() if len(w) > 2}
    wb = {w for w in _norm(b).split() if len(w) > 2}
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.7


def extract_json_array(raw: str) -> list | None:
    """Pull a JSON array out of an AI answer (handles ```json fences, stray text, and answers cut off half way)."""
    text = re.sub(r"```(?:json)?", "", raw, flags=re.I).strip()
    start = text.find("[")
    if start == -1:
        return None

    def try_parse(s):
        try:
            v = json.loads(s)
            return v if isinstance(v, list) else None
        except ValueError:
            return None

    end = text.rfind("]")
    if end > start and (whole := try_parse(text[start:end + 1])) is not None:
        return whole
    last_obj = text.rfind("}")  # cut off mid-way: keep every complete item before the cut
    return try_parse(text[start:last_obj + 1] + "]") if last_obj > start else None


def _clean_question(item, chunk: dict, difficulty: str) -> dict | None:
    """Validate one question from the AI. Returns None if anything is wrong with it."""
    if not isinstance(item, dict):
        return None
    q, options, expl = item.get("question"), item.get("options"), item.get("explanation")
    if not (isinstance(q, str) and 10 <= len(q.strip()) <= 600):
        return None
    if not (isinstance(options, list) and len(options) == 4 and all(isinstance(o, str) and 1 <= len(o.strip()) <= 300 for o in options)):
        return None
    if not (isinstance(expl, str) and 10 <= len(expl.strip()) <= 1000):
        return None
    letter = re.sub(r"[^A-D]", "", str(item.get("correctAnswer", "")).upper())[:1]
    if not letter:
        return None
    options = [o.strip() for o in options]
    if len({_norm(o) for o in options}) != 4:  # repeated options
        return None
    try:
        page = int(item.get("sourcePage"))
        page = page if page > 0 else None
    except (TypeError, ValueError):
        page = None
    if page is not None and not (chunk["page_start"] <= page <= chunk["page_end"]):
        page = None  # never keep a made-up page
    if page is None and chunk["page_start"] == chunk["page_end"]:
        page = chunk["page_start"]

    correct_text = options["ABCD".index(letter)]
    shuffled = random.sample(options, 4)  # so the right answer is not always in the same place
    return {"question": q.strip(), "options": shuffled, "correctAnswer": shuffled.index(correct_text),
            "explanation": expl.strip(), "difficulty": difficulty, "sourcePage": page}


def parse_questions(raw: str, chunk: dict, difficulty: str) -> list[dict]:
    items = extract_json_array(raw) or []
    return [q for q in (_clean_question(i, chunk, difficulty) for i in items) if q]


# ---------- building the quiz ----------
def _questions_for_chunk(chunk: dict, need: int, difficulty: str, existing: list[dict]) -> list[dict]:
    got: list[dict] = []
    for _ in range(config.ATTEMPTS_PER_REQUEST):
        if len(got) >= need:
            break
        avoid = [q["question"] for q in [*existing, *got]][-30:]
        try:
            raw = ai.generate_text(quiz_prompt(chunk["content"], need - len(got), difficulty, avoid),
                                   system=SYSTEM, temperature=0.4, max_tokens=8192)
        except ai.AIError as e:
            if e.code in ("blocked", "empty"):  # refused / nothing for this part: try again, then move on
                continue
            raise
        for q in parse_questions(raw, chunk, difficulty):
            if len(got) >= need:
                break
            if any(is_duplicate(x["question"], q["question"]) for x in [*existing, *got]):
                continue  # duplicate filtering
            got.append(q)
    return got


def build_quiz(chunks: list[dict], count: int, difficulty: str, on_progress) -> list[dict]:
    """on_progress(percent, detail) is called every time one part of the document has been finished."""
    lengths = [len(c["content"]) for c in chunks]
    plan = allocate(lengths, count)
    target = sum(plan)
    if target == 0:
        raise ai.AIError("This document does not have enough text to make a quiz.")

    found: list[tuple[int, dict]] = []  # (chunk index, question)
    first_round_parts = sum(1 for n in plan if n > 0)
    total_steps = first_round_parts + config.TOP_UP_ROUNDS  # top-up rounds only happen when questions were rejected
    done = 0
    lock = threading.Lock()

    def run_round(round_plan: list[int], first: bool):
        nonlocal done
        jobs = [(i, n) for i, n in enumerate(round_plan) if n > 0]
        finished_in_round = 0

        def work(job):
            nonlocal done, finished_in_round
            i, n = job
            existing = [q for ci, q in found if ci == i]
            result = i, _questions_for_chunk(chunks[i], n, difficulty, existing)
            with lock:  # report right away, so the bar moves while the round is still running
                done += 1
                finished_in_round += 1
                detail = (f"Writing questions: part {finished_in_round} of {len(jobs)}" if first
                          else "Replacing questions that did not pass the checks")
                on_progress(min(95, round(done / (total_steps + 1) * 100)), detail)
            return result

        with ThreadPoolExecutor(config.AI_CONCURRENCY) as pool:
            results = list(pool.map(work, jobs))
        for i, qs in results:  # merge after the round so two chunks cannot add near-identical questions
            for q in qs:
                if not any(is_duplicate(f[1]["question"], q["question"]) for f in found):
                    found.append((i, q))

    run_round(plan, True)
    for _ in range(config.TOP_UP_ROUNDS):
        if len(found) >= target:
            break
        run_round(allocate(lengths, target - len(found)), False)

    if not found:
        raise ai.AIError("The AI could not write valid questions for this document. Please try again.")
    on_progress(97, "Checking the questions")
    ordered = sorted(found[:target], key=lambda f: (f[0], f[1]["sourcePage"] or 10**9))
    return [q for _, q in ordered]


def generate_quiz(quiz_id: str) -> None:
    """Background job: load chunks -> ask the AI per chunk -> validate -> save the questions."""
    import time
    try:
        quiz = db.one("select * from quizzes where id=?", (quiz_id,))
        if not quiz:
            return
        chunks = db.list_chunks(quiz["document_id"])
        if not chunks:
            raise ai.AIError("This document has no text to make a quiz from.")
        db.update("quizzes", quiz_id, status="generating", progress=1, error=None)
        last = [0.0]

        def progress(percent, detail=None):
            if time.time() - last[0] < 0.4:
                return
            last[0] = time.time()
            db.update("quizzes", quiz_id, progress=percent, status_detail=detail)

        questions = build_quiz(chunks, quiz["question_count"], quiz["difficulty"], progress)
        db.update("quizzes", quiz_id, questions=questions, question_count=len(questions), status="done", progress=100, status_detail=None)
    except Exception as e:  # noqa: BLE001
        msg = str(e) if isinstance(e, ai.AIError) else "Could not create the quiz. Please try again."
        if not isinstance(e, ai.AIError):
            print("[quiz] failed:", repr(e))
        db.update("quizzes", quiz_id, status="failed", error=msg, status_detail=None)
