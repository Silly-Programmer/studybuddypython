"""StudyBuddy: upload a PDF, get easy notes and quizzes.   Run:  python app.py   then open http://127.0.0.1:5000"""

import os
import re
from datetime import datetime

from flask import Flask, abort, jsonify, redirect, render_template, request

import ai
import config
import db
import jobs
import quizzes
import summaries

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = (
    config.MAX_PDF_BYTES + 1024 * 1024
)  # a little room for the form around the file

STATUS_LABEL = {
    "uploaded": "Queued",
    "extracting": "Reading text",
    "ocr": "Running OCR",
    "chunking": "Preparing",
    "ready": "Ready",
    "failed": "Failed",
    "pending": "Queued",
    "generating": "Writing",
    "done": "Ready",
}


# ---------- small helpers ----------
@app.template_filter("fdate")
def fdate(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b %Y')}"


@app.template_filter("fsize")
def fsize(n: int) -> str:
    return (
        f"{max(1, round(n / 1024))} KB"
        if n < 1024 * 1024
        else f"{n / 1024 / 1024:.1f} MB"
    )


@app.template_filter("status_label")
def status_label(s: str) -> str:
    return STATUS_LABEL.get(s, s)


def _asset_version() -> int:
    """Changes whenever a file in static/ changes, so phones never keep an old app.js or app.css."""
    return int(
        max(
            (
                f.stat().st_mtime
                for f in (config.BASE_DIR / "static").glob("*")
                if f.is_file()
            ),
            default=0,
        )
    )


app.jinja_env.globals.update(
    TYPE_LABEL=summaries.TYPE_LABEL,
    IN_PROGRESS=db.IN_PROGRESS,
    MAX_MB=config.MAX_PDF_BYTES // 1024 // 1024,
    ASSET_V=_asset_version(),
)


@app.after_request
def never_cache_pages(resp):
    """Pages and API answers always come fresh from the server. Without this the browser's Back button
    shows an old copy of the page (for example a progress bar frozen at 1%)."""
    if not request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def status_payload(row: dict) -> dict:
    """What the browser needs to draw a progress bar or a status badge, the same shape for every kind of job."""
    s = row["status"]
    return {
        "id": row["id"],
        "status": s,
        "label": STATUS_LABEL.get(s, s),
        "tone": (
            "green" if s in ("ready", "done") else "red" if s == "failed" else "amber"
        ),
        "active": s in db.IN_PROGRESS,
        "progress": row.get("progress") or 0,
        "status_detail": row.get("status_detail"),
        "error": row.get("error"),
    }


STATUS_COLUMNS = "id, status, progress, status_detail, error"


def fail(message: str, code: int = 400):
    return jsonify(error=message), code


def get_or_404(table: str, row_id: str) -> dict:
    row = db.one(f"select * from {table} where id=?", (row_id,))
    if not row:
        abort(404)
    return row


def check_can_start_job():
    ai.check_configured()  # clear error if the AI key/model are missing
    if db.count_active_jobs() >= config.MAX_ACTIVE_JOBS:
        raise ai.AIError(
            "Several summaries or quizzes are already being written. Wait for one to finish, then try again."
        )


@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return fail("Not found.", 404)
    return (
        render_template(
            "message.html",
            title="Not found",
            text="That page does not exist, or it was deleted.",
        ),
        404,
    )


@app.errorhandler(413)
def too_big(_):
    return fail(
        f"This file is too large. The limit is {config.MAX_PDF_BYTES // 1024 // 1024} MB.",
        413,
    )


@app.errorhandler(ai.AIError)
def ai_error(e):
    return fail(str(e), 503 if e.code == "ai_not_configured" else 429)


# ---------- pages ----------
@app.get("/")
def landing():
    return app.send_static_file("landing.html")


@app.get("/dashboard")
def dashboard():
    return render_template(
        "dashboard.html",
        docs=db.one("select count(*) n from documents")["n"],
        sums=db.one("select count(*) n from summaries where saved=1")["n"],
        quizzes=db.one("select count(*) n from quizzes where saved=1")["n"],
        recent=db.query("select * from documents order by created_at desc limit 5"),
    )


@app.get("/documents")
def documents_page():
    return render_template(
        "documents.html",
        docs=db.query("select * from documents order by created_at desc"),
    )


@app.get("/documents/<doc_id>")
def document_page(doc_id):
    doc = get_or_404("documents", doc_id)
    ctx = {"doc": doc}
    if doc["status"] == "ready":
        capacity = quizzes.quiz_capacity(db.list_chunks(doc_id))
        ctx.update(
            summaries=db.query(
                "select id,type,title,status,saved,created_at from summaries where document_id=? order by created_at desc",
                (doc_id,),
            ),
            quizzes=db.query(
                "select id,title,difficulty,status,question_count,saved,created_at from quizzes where document_id=? order by created_at desc",
                (doc_id,),
            ),
            capacity=capacity,
            count_options=quizzes.count_options(capacity),
            default_count=config.DEFAULT_QUIZ_COUNT,
            summary_types=summaries.TYPE_LABEL,
        )
    return render_template("document.html", **ctx)


@app.get("/summaries")
def saved_summaries():
    rows = db.query(
        "select s.*, d.name as document_name from summaries s join documents d on d.id=s.document_id "
        "where s.saved=1 order by s.created_at desc"
    )
    return render_template("saved_summaries.html", rows=rows)


@app.get("/summaries/<sid>")
def summary_page(sid):
    s = get_or_404("summaries", sid)
    doc = db.one("select id,name from documents where id=?", (s["document_id"],))
    return render_template("summary.html", s=s, doc=doc)


@app.get("/quizzes")
def saved_quizzes():
    rows = db.query(
        "select q.*, d.name as document_name, "
        "(select count(*) from attempts a where a.quiz_id=q.id) as attempts, "
        "(select max(score*1.0/total) from attempts a where a.quiz_id=q.id) as best "
        "from quizzes q join documents d on d.id=q.document_id where q.saved=1 order by q.created_at desc"
    )
    return render_template("saved_quizzes.html", rows=rows)


@app.get("/quizzes/<qid>")
def quiz_page(qid):
    q = get_or_404("quizzes", qid)
    doc = db.one("select id,name from documents where id=?", (q["document_id"],))
    attempts = db.query(
        "select id,score,total,created_at from attempts where quiz_id=? order by created_at desc",
        (qid,),
    )
    return render_template("quiz.html", q=q, doc=doc, attempts=attempts)


@app.get("/quizzes/<qid>/take")
def take_page(qid):
    q = get_or_404("quizzes", qid)
    if q["status"] != "done" or not q["questions"]:
        return redirect(f"/quizzes/{qid}")
    # The browser only gets the questions and options: no answers or explanations until the quiz is submitted.
    public = [
        {
            "question": x["question"],
            "options": x["options"],
            "sourcePage": x["sourcePage"],
        }
        for x in q["questions"]
    ]
    return render_template("take.html", q=q, questions=public)


@app.get("/quizzes/<qid>/attempts/<aid>")
def attempt_page(qid, aid):
    q = get_or_404("quizzes", qid)
    a = db.one("select * from attempts where id=? and quiz_id=?", (aid, qid))
    if not a:
        abort(404)
    return render_template("attempt.html", q=q, a=a)


# ---------- API: live status of many things at once ----------
@app.get("/api/status")
def batch_status():
    """GET /api/status?doc=<id>&sum=<id>&quiz=<id> (repeat a name for more ids).
    Every page polls this one URL, so a list with five running jobs still costs a single request.
    """
    tables = {"doc": "documents", "sum": "summaries", "quiz": "quizzes"}
    out = {}
    for kind, table in tables.items():
        ids = [
            i for i in request.args.getlist(kind) if re.fullmatch(r"[0-9a-f]{32}", i)
        ][:50]
        if not ids:
            continue
        marks = ",".join("?" * len(ids))
        for row in db.query(
            f"select {STATUS_COLUMNS} from {table} where id in ({marks})", tuple(ids)
        ):
            out[f"{kind}:{row['id']}"] = status_payload(row)
    return jsonify(out)


# ---------- API: documents ----------
@app.post("/api/documents")
def upload_document():
    f = request.files.get("file")
    if not f or not f.filename:
        return fail("Choose a PDF file.")
    if not f.filename.lower().endswith(".pdf"):
        return fail("Please choose a PDF file.", 415)
    doc_id = db.new_id()
    filename = f"{doc_id}.pdf"
    path = config.UPLOAD_DIR / filename
    f.save(path)
    size = path.stat().st_size
    if size == 0 or size > config.MAX_PDF_BYTES:
        path.unlink(missing_ok=True)
        return fail(
            (
                "This file is empty."
                if size == 0
                else f"This file is too large. The limit is {config.MAX_PDF_BYTES // 1024 // 1024} MB."
            ),
            413,
        )
    name = (
        re.sub(r'[\x00-\x1f<>:"/\\|?*]', "", os.path.basename(f.filename))[:150]
        or "Untitled.pdf"
    )
    now = db.now()
    db.insert(
        "documents",
        {
            "id": doc_id,
            "name": name,
            "filename": filename,
            "size_bytes": size,
            "status": "uploaded",
            "created_at": now,
            "updated_at": now,
        },
    )
    jobs.start(jobs.process_document, doc_id)
    return jsonify(id=doc_id), 201


@app.get("/api/documents/<doc_id>")
def document_status(doc_id):
    return jsonify({**status_payload(get_or_404("documents", doc_id))})


@app.get("/api/health")
def health():
    import pytesseract

    try:
        ver = str(pytesseract.get_tesseract_version())
        return jsonify(ok=True, tesseract=ver)
    except Exception as e:
        return jsonify(ok=False, tesseract=None, error=str(e)), 500


@app.post("/api/documents/<doc_id>/retry")
def retry_document(doc_id):
    d = get_or_404("documents", doc_id)
    if d["status"] not in ("failed",):
        return fail("This document is not in a failed state.", 409)
    db.update(
        "documents",
        doc_id,
        status="extracting",
        progress=1,
        status_detail="Opening your PDF",
        error=None,
    )
    jobs.start(jobs.process_document, doc_id)
    return jsonify(id=doc_id)


@app.delete("/api/documents/<doc_id>")
def delete_document(doc_id):
    d = get_or_404("documents", doc_id)
    db.execute(
        "delete from documents where id=?", (doc_id,)
    )  # chunks, summaries, quizzes, attempts go with it
    (config.UPLOAD_DIR / d["filename"]).unlink(missing_ok=True)
    return jsonify(ok=True)


# ---------- API: summaries ----------
@app.post("/api/documents/<doc_id>/summaries")
def create_summary(doc_id):
    d = get_or_404("documents", doc_id)
    kind = (request.get_json(silent=True) or {}).get("type")
    if kind not in summaries.TYPE_LABEL:
        return fail("Choose a summary type.")
    if d["status"] != "ready":
        return fail("This document is still being processed.", 409)
    check_can_start_job()
    existing = db.one(
        "select id from summaries where document_id=? and type=? and status in ('pending','generating')",
        (doc_id, kind),
    )
    if existing:  # e.g. a double click
        return jsonify(id=existing["id"])
    sid, now = db.new_id(), db.now()
    db.insert(
        "summaries",
        {
            "id": sid,
            "document_id": doc_id,
            "type": kind,
            "title": summaries.TYPE_LABEL[kind],
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        },
    )
    jobs.start(summaries.generate_summary, sid)
    return jsonify(id=sid), 201


@app.get("/api/summaries/<sid>")
def summary_status(sid):
    return jsonify(status_payload(get_or_404("summaries", sid)))


# ---------- API: quizzes ----------
@app.post("/api/documents/<doc_id>/quizzes")
def create_quiz(doc_id):
    d = get_or_404("documents", doc_id)
    body = request.get_json(silent=True) or {}
    count, difficulty = body.get("count"), body.get("difficulty")
    if (
        not (
            isinstance(count, int)
            and config.MIN_QUESTIONS <= count <= config.MAX_QUESTIONS
        )
        or difficulty not in quizzes.DIFFICULTIES
    ):
        return fail("Choose the number of questions and a difficulty.")
    if d["status"] != "ready":
        return fail("This document is still being processed.", 409)
    capacity = quizzes.quiz_capacity(db.list_chunks(doc_id))
    if capacity < config.MIN_QUESTIONS:
        return fail("This document is too short to make a quiz. Try a longer PDF.")
    if count > capacity:
        return fail(
            f"This document has enough material for up to {capacity} questions. Choose {capacity} or fewer."
        )
    check_can_start_job()
    existing = db.one(
        "select id from quizzes where document_id=? and difficulty=? and question_count=? and status in ('pending','generating')",
        (doc_id, difficulty, count),
    )
    if existing:
        return jsonify(id=existing["id"])
    qid, now = db.new_id(), db.now()
    base_name = re.sub(r"\.pdf$", "", d["name"], flags=re.I)
    title = f"{base_name} — {difficulty} quiz"
    db.insert(
        "quizzes",
        {
            "id": qid,
            "document_id": doc_id,
            "title": title,
            "difficulty": difficulty,
            "questions": [],
            "question_count": count,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        },
    )
    jobs.start(quizzes.generate_quiz, qid)
    return jsonify(id=qid), 201


@app.get("/api/quizzes/<qid>")
def quiz_status(qid):
    return jsonify(status_payload(get_or_404("quizzes", qid)))


@app.post("/api/quizzes/<qid>/attempts")
def submit_attempt(qid):
    q = get_or_404("quizzes", qid)
    answers = (request.get_json(silent=True) or {}).get("answers")
    questions = q["questions"]
    if (
        q["status"] != "done"
        or not isinstance(answers, list)
        or len(answers) != len(questions)
        or not all(a is None or (isinstance(a, int) and 0 <= a <= 3) for a in answers)
    ):
        return fail("Could not submit these answers.")
    score = sum(1 for a, x in zip(answers, questions) if a == x["correctAnswer"])
    aid = db.new_id()
    db.insert(
        "attempts",
        {
            "id": aid,
            "quiz_id": qid,
            "answers": answers,
            "score": score,
            "total": len(questions),
            "created_at": db.now(),
        },
    )
    return jsonify(id=aid), 201


# ---------- API: rename / save / delete (summaries and quizzes work the same way) ----------
def _patch_and_delete(table: str, base: str):
    def patch(row_id):
        get_or_404(table, row_id)
        body = request.get_json(silent=True) or {}
        values = {}
        if "title" in body:
            title = str(body["title"]).strip()
            if not title:
                return fail("Enter a name.")
            if len(title) > 120:
                return fail("Use at most 120 characters.")
            values["title"] = title
        if "saved" in body:
            values["saved"] = int(bool(body["saved"]))
        if not values:
            return fail("Nothing to change.")
        db.update(table, row_id, **values)
        return jsonify(ok=True)

    def delete(row_id):
        get_or_404(table, row_id)
        db.execute(f"delete from {table} where id=?", (row_id,))
        return jsonify(ok=True)

    app.add_url_rule(
        f"/api/{base}/<row_id>", f"patch_{table}", patch, methods=["PATCH"]
    )
    app.add_url_rule(
        f"/api/{base}/<row_id>", f"delete_{table}", delete, methods=["DELETE"]
    )


_patch_and_delete("summaries", "summaries")
_patch_and_delete("quizzes", "quizzes")


if __name__ == "__main__":
    db.init()
    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT") or 5000)
    try:
        ai.check_configured()
    except ai.AIError as e:
        print(
            f"\n  ! {e}\n    (You can still upload PDFs, but summaries and quizzes need the AI.)"
        )
    print(
        f"\n  StudyBuddy is running:  http://{host}:{port}\n  Press Ctrl+C to stop.\n"
    )
    app.run(host=host, port=port, threaded=True)
