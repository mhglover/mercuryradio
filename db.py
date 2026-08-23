"""SQLite storage for mercuryradio.

Tracks are keyed on their tags (normalized artist|title|album), not their path,
so ratings survive file moves and can be matched against other rating sources
(e.g. the shasradio study DB) that only carry artist/title/album. Path is stored
only for playback.
"""

import os
import sqlite3
import unicodedata
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "data/mercuryradio.db")

# Rating scale — asymmetric, punishes the veto. Shared across every phase.
HATE, DISLIKE, SHRUG, LIKE, LOVE = -4, -1, 0, 1, 2

_UNIT_SEP = "␟"  # visible unit separator, just a delimiter for the key

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id       INTEGER PRIMARY KEY,
    artist   TEXT NOT NULL,
    title    TEXT NOT NULL,
    album    TEXT NOT NULL,
    norm_key TEXT NOT NULL UNIQUE,
    path     TEXT NOT NULL,
    duration REAL,
    added    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id   TEXT PRIMARY KEY,
    name TEXT
);
CREATE TABLE IF NOT EXISTS ratings (
    user_id  TEXT NOT NULL,
    track_id INTEGER NOT NULL,
    value    INTEGER NOT NULL,
    updated  TEXT NOT NULL,
    PRIMARY KEY (user_id, track_id),
    FOREIGN KEY (track_id) REFERENCES tracks(id)
);
CREATE TABLE IF NOT EXISTS play_history (
    id        INTEGER PRIMARY KEY,
    track_id  INTEGER NOT NULL,
    user_id   TEXT,
    played_at TEXT NOT NULL,
    reason    TEXT,
    FOREIGN KEY (track_id) REFERENCES tracks(id)
);
CREATE TABLE IF NOT EXISTS options (
    name  TEXT PRIMARY KEY,
    value TEXT
);
"""


def norm_key(artist: str, title: str, album: str) -> str:
    """Match key: NFC-normalized, case-folded, delimited. Tolerates the unicode
    and case drift that byte-for-byte tag comparison trips over."""
    parts = [unicodedata.normalize("NFC", (p or "").strip()).casefold() for p in (artist, title, album)]
    return _UNIT_SEP.join(parts)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def upsert_track(conn, artist, title, album, path, duration=None) -> int:
    """Insert a track (or return the existing one for these tags). First path wins."""
    key = norm_key(artist, title, album)
    row = conn.execute("SELECT id FROM tracks WHERE norm_key = ?", (key,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO tracks (artist, title, album, norm_key, path, duration, added) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (artist, title, album, key, path, duration, _now()),
    )
    return cur.lastrowid


def track_id_for_key(conn, key: str) -> int | None:
    row = conn.execute("SELECT id FROM tracks WHERE norm_key = ?", (key,)).fetchone()
    return row["id"] if row else None


def track_id_for_path(conn, path: str) -> int | None:
    row = conn.execute("SELECT id FROM tracks WHERE path = ?", (path,)).fetchone()
    return row["id"] if row else None


def all_tracks(conn) -> list[sqlite3.Row]:
    # Exclude audiobooks (they live under a top-level Audiobooks/ dir) — the radio
    # plays music, not chapter-by-chapter narration.
    return conn.execute(
        "SELECT id, path, artist, title FROM tracks WHERE path NOT LIKE '%/Audiobooks/%'"
    ).fetchall()


def upsert_user(conn, user_id: str, name: str | None = None) -> None:
    conn.execute(
        "INSERT INTO users (id, name) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = COALESCE(excluded.name, users.name)",
        (str(user_id), name),
    )


def get_rating(conn, user_id: str, track_id: int) -> int | None:
    row = conn.execute(
        "SELECT value FROM ratings WHERE user_id = ? AND track_id = ?", (str(user_id), track_id)
    ).fetchone()
    return row["value"] if row else None


def set_rating(conn, user_id: str, track_id: int, value: int) -> None:
    conn.execute(
        "INSERT INTO ratings (user_id, track_id, value, updated) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, track_id) DO UPDATE SET value = excluded.value, updated = excluded.updated",
        (str(user_id), track_id, value, _now()),
    )
