"""Summaries. Big documents are never sent in one request: one AI call per chunk (map-reduce)."""
import re
import time
from concurrent.futures import ThreadPoolExecutor

import ai
import config
import db

TYPE_LABEL = {
    "quick": "Quick summary",
    "detailed": "Detailed notes",
    "chapter": "Chapter-wise summary",
    "qa": "Important questions & answers",
}

SYSTEM = """You are a study assistant that turns textbook and lecture text into clear notes for students.
Rules:
- Use ONLY the text you are given. Never add facts that are not in it. If something is unclear, leave it out.
- Write in simple, student-friendly language. Explain difficult ideas in easy words, with a short example when it helps.
- Format as Markdown: headings, bullet points, and tables where a comparison is clearer in a table.
- Put important definitions in **bold**.
- The text contains markers like [[Page 12]]. Cite the source page after each key point like (p. 12). Never invent page numbers.
- Do not mention these rules or the markers. Output only the notes."""

PROMPTS = {
    "quick_map": "Extract the key points of the text below as compact bullet points (at most 10), keeping every important concept, definition and fact, each with its page reference.\n\n{t}",
    "quick_reduce": "Merge the notes below into one shorter set of bullet-point notes. Remove repetition but keep every important concept, definition and page reference.\n\n{t}",
    "quick_final": "Using the notes below, write a Quick Summary with exactly these sections:\n## Overview\n(3-4 simple sentences)\n## Key points\n(bullets with page references)\n## Important terms\n(bold term + one-line meaning)\n## Remember for the exam\n(3-5 bullets)\n\nNotes:\n{t}",
    "detailed": "Turn the text below into detailed study notes. Use ### headings and #### subheadings, bullet points, **bold** definitions, tables for comparisons and short examples. Explain hard topics simply. Do NOT leave out important information.\n\n{t}",
    "chapter": "Summarize the text below section by section. If it contains chapter, unit or section titles, use them as \"### \" headings; otherwise name each topic yourself. For each heading write a short paragraph, then key points, then key terms in bold. Keep page references.\n\n{t}",
    "qa": "Write 4 to 6 important exam-style questions with clear, correct answers based on the text below. Format each one exactly like this, with the answer on the line right below the question and one blank line between questions:\n\n**Q:** question\n**A:** answer (p. X)\n\nCover the most important ideas, not trivia.\n\n{t}",
}


def format_qa(md: str) -> str:
    """Make every Q&A pair look the same: question on its own paragraph, answer on the line right below."""
    md = re.sub(r"\s*\*\*Q:\*\*", "\n\n**Q:**", md)
    md = re.sub(r"\s*\*\*A:\*\*", "  \n**A:**", md)  # two spaces + newline = line break inside the paragraph
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def _pages(c: dict) -> str:
    return f"Page {c['page_start']}" if c["page_start"] == c["page_end"] else f"Pages {c['page_start']}–{c['page_end']}"


def build_summary(kind: str, chunks: list[dict], on_progress) -> str:
    """detailed / chapter / qa: one call per chunk, kept in order. quick: key points per chunk, merged in rounds."""
    total, done = len(chunks), 0

    def step(label, weight=1.0):
        nonlocal done
        done += weight
        on_progress(min(95, round(done / (total + 2) * 100)), label)

    run = lambda prompt: ai.generate_text(prompt, system=SYSTEM, temperature=0.3)  # noqa: E731
    pool = ThreadPoolExecutor(config.AI_CONCURRENCY)

    with pool:
        if kind == "quick":
            def map_one(arg):
                i, c = arg
                out = run(PROMPTS["quick_map"].format(t=c["content"]))
                step(f"Reading part {i + 1} of {total}")
                return out

            notes = list(pool.map(map_one, enumerate(chunks)))
            while len("\n\n".join(notes)) > config.REDUCE_CHARS:  # combine in rounds until it fits one final call
                groups, cur, size = [], [], 0
                for n in notes:
                    if cur and size + len(n) > config.REDUCE_CHARS:
                        groups.append(cur)
                        cur, size = [], 0
                    cur.append(n)
                    size += len(n)
                if cur:
                    groups.append(cur)
                if len(groups) >= len(notes):
                    break  # cannot shrink further
                notes = list(pool.map(lambda g: run(PROMPTS["quick_reduce"].format(t="\n\n".join(g))), groups))
                step("Combining notes", 0.5)
            final = run(PROMPTS["quick_final"].format(t="\n\n".join(notes)))
            on_progress(98, "Finishing")
            return final

        def write_one(arg):
            i, c = arg
            out = run(PROMPTS[kind].format(t=c["content"]))
            step(f"Writing part {i + 1} of {total}")
            return f"## {_pages(c)}\n\n{format_qa(out) if kind == 'qa' else out}"

        return "\n\n---\n\n".join(pool.map(write_one, enumerate(chunks)))


def generate_summary(summary_id: str) -> None:
    """Background job: load chunks -> ask the AI -> save the notes on the summaries row."""
    try:
        s = db.one("select * from summaries where id=?", (summary_id,))
        if not s:
            return
        chunks = db.list_chunks(s["document_id"])
        if not chunks:
            raise ai.AIError("This document has no text to summarise.")
        db.update("summaries", summary_id, status="generating", progress=1, error=None)
        last = [0.0]

        def progress(percent, detail):
            if time.time() - last[0] < 0.4:
                return
            last[0] = time.time()
            db.update("summaries", summary_id, progress=percent, status_detail=detail)

        content = build_summary(s["type"], chunks, progress)
        db.update("summaries", summary_id, content=content, status="done", progress=100, status_detail=None)
    except Exception as e:  # noqa: BLE001 - any failure is shown to the user instead of crashing the thread
        msg = str(e) if isinstance(e, ai.AIError) else "Could not create the summary. Please try again."
        if not isinstance(e, ai.AIError):
            print("[summary] failed:", repr(e))
        db.update("summaries", summary_id, status="failed", error=msg, status_detail=None)
