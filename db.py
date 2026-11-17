"""A tiny SQLite layer. SQLite is built into Python, so there is nothing to install or run."""
import json
import sqlite3
import uuid
from datetime import datetime, timezone

import config

SCHEMA = """
create table if not exists documents (
  id text primary key, name text not null, filename text not null, size_bytes integer not null,
  page_count integer, status text not null default 'uploaded', progress integer not null default 0,
  status_detail text, ocr_pages integer not null default 0, error text,
  created_at text not null, updated_at text not null);
create table if not exists chunks (
  document_id text not null references documents(id) on delete cascade,
  idx integer not null, page_start integer not null, page_end integer not null,
  content text not null, used_ocr integer not null default 0, primary key (document_id, idx));
create table if not exists summaries (
  id text primary key, document_id text not null references documents(id) on delete cascade,
  type text not null, title text not null, content text not null default '',
  status text not null default 'pending', progress integer not null default 0, status_detail text,
  error text, saved integer not null default 0, created_at text not null, updated_at text not null);
create table if not exists quizzes (
  id text primary key, document_id text not null references documents(id) on delete cascade,
  title text not null, difficulty text not null, questions text not null default '[]',
  question_count integer not null default 0, status text not null default 'pending',
  progress integer not null default 0, error text, saved integer not null default 0,
  created_at text not null, updated_at text not null);
create table if not exists attempts (
  id text primary key, quiz_id text not null references quizzes(id) on delete cascade,
  answers text not null, score integer not null, total integer not null, created_at text not null);
"""

JSON_COLUMNS = {"questions", "answers"}
IN_PROGRESS = ("uploaded", "extracting", "ocr", "chunking", "pending", "generating")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    conn.execute("pragma journal_mode = wal")
    return conn


def init() -> None:
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        # A restart kills background work, so anything still "in progress" can never finish by itself.
        for table in ("documents", "summaries", "quizzes"):
            marks = ",".join("?" * len(IN_PROGRESS))
            conn.execute(
                f"update {table} set status='failed', error='This was interrupted when the app restarted. Please try again.' "
                f"where status in ({marks})",
                IN_PROGRESS,
            )


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in JSON_COLUMNS & out.keys():
        out[key] = json.loads(out[key])
    return out


def query(sql: str, args: tuple = ()) -> list[dict]:
    with connect() as conn:
        return [_row(r) for r in conn.execute(sql, args).fetchall()]


def one(sql: str, args: tuple = ()) -> dict | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: tuple = ()) -> None:
    with connect() as conn:
        conn.execute(sql, args)


def insert(table: str, values: dict) -> None:
    values = {k: json.dumps(v) if k in JSON_COLUMNS else v for k, v in values.items()}
    cols = ",".join(values)
    marks = ",".join("?" * len(values))
    execute(f"insert into {table} ({cols}) values ({marks})", tuple(values.values()))


def update(table: str, row_id: str, **values) -> None:
    values = {k: json.dumps(v) if k in JSON_COLUMNS else v for k, v in values.items()}
    if table != "attempts":  # attempts are never edited, so they have no updated_at
        values["updated_at"] = now()
    sets = ",".join(f"{k}=?" for k in values)
    execute(f"update {table} set {sets} where id=?", (*values.values(), row_id))


def replace_chunks(document_id: str, chunks: list[dict]) -> None:
    with connect() as conn:
        conn.execute("delete from chunks where document_id=?", (document_id,))
        conn.executemany(
            "insert into chunks (document_id, idx, page_start, page_end, content, used_ocr) values (?,?,?,?,?,?)",
            [(document_id, c["index"], c["page_start"], c["page_end"], c["content"], int(c["used_ocr"])) for c in chunks],
        )


def list_chunks(document_id: str) -> list[dict]:
    return query("select * from chunks where document_id=? order by idx", (document_id,))


def count_active_jobs() -> int:
    marks = ",".join("?" * 2)
    n = 0
    for table in ("summaries", "quizzes"):
        n += one(f"select count(*) as n from {table} where status in ({marks})", ("pending", "generating"))["n"]
    return n
