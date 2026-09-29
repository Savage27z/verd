"""SQLite storage: searches, leads (with pipeline status + notes), settings."""
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

LEAD_STATUSES = ("new", "contacted", "replied", "won", "lost")
LEAD_COLUMNS = (
    "place_id", "name", "phone", "intl_phone", "address", "email", "website", "web_presence",
    "category", "rating", "reviews", "maps_url", "score",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY,
    niche TEXT NOT NULL,
    location TEXT NOT NULL,
    requested INTEGER NOT NULL,
    returned INTEGER NOT NULL,
    options TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    place_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    intl_phone TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    web_presence TEXT NOT NULL DEFAULT 'none',
    category TEXT NOT NULL DEFAULT '',
    rating REAL,
    reviews INTEGER NOT NULL DEFAULT 0,
    maps_url TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS leads_search_idx ON leads (search_id, position);
CREATE INDEX IF NOT EXISTS leads_place_idx ON leads (place_id);
CREATE INDEX IF NOT EXISTS leads_status_idx ON leads (status, updated_at);
-- Which Telegram message shows which lead, so replying to a lead card can save a note.
CREATE TABLE IF NOT EXISTS lead_messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    PRIMARY KEY (chat_id, message_id)
);
-- Website previews: an unguessable token per lead, plus who's been looking.
CREATE TABLE IF NOT EXISTS mockups (
    token TEXT PRIMARY KEY,
    lead_id INTEGER NOT NULL UNIQUE REFERENCES leads(id) ON DELETE CASCADE,
    views INTEGER NOT NULL DEFAULT 0,
    last_view_at TEXT,
    notified_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def db_path() -> Path:
    return Path(os.getenv("ODIFY_DB", "data/odify.db"))


@contextmanager
def connect():
    """Short-lived connection per operation — safe from the bot loop and worker threads alike."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:  # commits on success, rolls back on exception
            yield conn
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


# ---- searches + leads ----

def save_search(niche: str, location: str, requested: int, options: dict, leads: list[dict]) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO searches (niche, location, requested, returned, options) VALUES (?,?,?,?,?)",
            (niche, location, requested, len(leads), json.dumps(options)),
        )
        search_id = cur.lastrowid
        conn.executemany(
            f"INSERT INTO leads (search_id, position, {', '.join(LEAD_COLUMNS)})"
            f" VALUES (?, ?, {', '.join('?' * len(LEAD_COLUMNS))})",
            [(search_id, i, *[lead.get(c) for c in LEAD_COLUMNS]) for i, lead in enumerate(leads)],
        )
        return search_id


def _search(row) -> dict:
    d = dict(row)
    d["options"] = json.loads(d.get("options") or "{}")
    return d


def get_search(search_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM searches WHERE id=?", (search_id,)).fetchone()
        return _search(row) if row else None


def list_searches(limit: int = 10) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM searches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_search(r) for r in rows]


def get_leads(search_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM leads WHERE search_id=? ORDER BY position", (search_id,)).fetchall()
        return [dict(r) for r in rows]


def all_leads() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM leads ORDER BY search_id DESC, position").fetchall()]


def get_lead(lead_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        return dict(row) if row else None


def seen_place_ids() -> set[str]:
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT place_id FROM leads WHERE place_id <> ''").fetchall()
        return {r["place_id"] for r in rows}


def set_status(lead_id: int, status: str) -> dict | None:
    if status not in LEAD_STATUSES:
        raise ValueError(f"status must be one of {', '.join(LEAD_STATUSES)}")
    with connect() as conn:
        conn.execute("UPDATE leads SET status=?, updated_at=datetime('now') WHERE id=?", (status, lead_id))
    return get_lead(lead_id)


def set_notes(lead_id: int, notes: str) -> dict | None:
    with connect() as conn:
        conn.execute("UPDATE leads SET notes=?, updated_at=datetime('now') WHERE id=?", (notes[:2000], lead_id))
    return get_lead(lead_id)


def pipeline_counts() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute("SELECT status, count(*) AS n FROM leads GROUP BY status").fetchall()
    counts = dict.fromkeys(LEAD_STATUSES, 0)
    counts.update({r["status"]: r["n"] for r in rows})
    return counts


def leads_by_status(status: str, limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM leads WHERE status=? ORDER BY updated_at DESC, id DESC LIMIT ?", (status, limit)
        ).fetchall()
        return [dict(r) for r in rows]


# ---- telegram message <-> lead ----

def link_message(chat_id: int, message_id: int, lead_id: int):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO lead_messages (chat_id, message_id, lead_id) VALUES (?,?,?)",
            (chat_id, message_id, lead_id),
        )


def lead_for_message(chat_id: int, message_id: int) -> int | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT lead_id FROM lead_messages WHERE chat_id=? AND message_id=?", (chat_id, message_id)
        ).fetchone()
        return row["lead_id"] if row else None


# ---- website previews ----

def mockup_token(lead_id: int) -> str:
    """The lead's preview token, created on first use (stable afterwards)."""
    with connect() as conn:
        row = conn.execute("SELECT token FROM mockups WHERE lead_id=?", (lead_id,)).fetchone()
        if row:
            return row["token"]
        token = secrets.token_urlsafe(9)
        conn.execute("INSERT INTO mockups (token, lead_id) VALUES (?, ?)", (token, lead_id))
        return token


def mockup_by_token(token: str) -> tuple[dict, dict] | None:
    """(lead, search) for a token, or None."""
    with connect() as conn:
        row = conn.execute(
            "SELECT l.*, s.options AS search_options, s.location AS search_location FROM mockups m"
            " JOIN leads l ON l.id = m.lead_id JOIN searches s ON s.id = l.search_id WHERE m.token=?",
            (token,),
        ).fetchone()
    if not row:
        return None
    lead = dict(row)
    search = {"options": json.loads(lead.pop("search_options") or "{}"), "location": lead.pop("search_location")}
    return lead, search


def record_view(token: str, notify_every_minutes: int = 30) -> tuple[int, bool]:
    """Count a view. Returns (total views, whether to notify the owner now — at most once per
    `notify_every_minutes`, so a lead scrolling around doesn't spam you)."""
    with connect() as conn:
        conn.execute("UPDATE mockups SET views = views + 1, last_view_at = datetime('now') WHERE token=?", (token,))
        row = conn.execute(
            "SELECT views, notified_at IS NULL OR notified_at < datetime('now', ?) AS due FROM mockups WHERE token=?",
            (f"-{notify_every_minutes} minutes", token),
        ).fetchone()
        if not row:
            return 0, False
        if row["due"]:
            conn.execute("UPDATE mockups SET notified_at = datetime('now') WHERE token=?", (token,))
        return row["views"], bool(row["due"])


def mockup_views(lead_id: int) -> int:
    with connect() as conn:
        row = conn.execute("SELECT views FROM mockups WHERE lead_id=?", (lead_id,)).fetchone()
        return row["views"] if row else 0


# ---- settings ----

def get_setting(key: str, default: str | None = None) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str | None):
    with connect() as conn:
        if value is None:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
