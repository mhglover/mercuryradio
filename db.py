"""SQLite storage for mercuryradio.

Tracks are keyed on their tags (normalized artist|title|album), not their path,
so ratings survive file moves and can be matched against other rating sources
(e.g. the shasradio study DB) that only carry artist/title/album. Path is stored
only for playback.
"""

import os
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone

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
CREATE TABLE IF NOT EXISTS requests (
    id           INTEGER PRIMARY KEY,
    track_id     INTEGER NOT NULL,
    guild_id     TEXT,
    user_id      TEXT,
    requested_at TEXT NOT NULL,
    played_at    TEXT,
    FOREIGN KEY (track_id) REFERENCES tracks(id)
);
-- Multi-tenant: one process serves every guild in this table. Config that was
-- per-stack env (voice/card channel) lives here so a server is added by data,
-- not a redeploy. Ratings + tracks stay shared (a user's taste follows them).
CREATE TABLE IF NOT EXISTS guilds (
    guild_id              TEXT PRIMARY KEY,
    voice_channel_id      TEXT,
    nowplaying_channel_id TEXT,
    music_dir             TEXT,
    add_role_id           TEXT,   -- role allowed to /add + /youtube; NULL = open to all
    add_enabled           INTEGER NOT NULL DEFAULT 0,  -- 0 = /add + /youtube not registered (hidden)
    enabled               INTEGER NOT NULL DEFAULT 1,
    added                 TEXT NOT NULL
);
-- Per-guild presence: a rating touches (user, guild) so a recent rating counts
-- the user present in THAT server only (scoped, unlike the shared ratings).
CREATE TABLE IF NOT EXISTS presence (
    user_id  TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    seen_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, guild_id)
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
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    # CREATE TABLE IF NOT EXISTS won't add a column to a table that predates it.
    # requests.guild_id was added for multi-server (shared ratings, per-guild queue).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
    if "guild_id" not in cols:
        conn.execute("ALTER TABLE requests ADD COLUMN guild_id TEXT")
        conn.commit()
    # add_role_id gates /add + /youtube per guild (NULL = open); added after guilds shipped.
    gcols = {r["name"] for r in conn.execute("PRAGMA table_info(guilds)")}
    if "add_role_id" not in gcols:
        conn.execute("ALTER TABLE guilds ADD COLUMN add_role_id TEXT")
        conn.commit()
    # add_enabled controls whether /add + /youtube are registered (visible) per guild.
    # New servers default off (hidden); grandfather every server that predates this column
    # to on, so existing rooms keep the adding they already had.
    if "add_enabled" not in gcols:
        conn.execute("ALTER TABLE guilds ADD COLUMN add_enabled INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE guilds SET add_enabled = 1")
        conn.commit()


def upsert_track(conn, artist, title, album, path, duration=None) -> int:
    """Insert a track (or return the existing one for these tags). First path wins —
    EXCEPT a stored path that no longer exists on disk is healed to the path we were
    handed (which does exist, since a scan is looking right at it). This fixes the
    library-reorg drift where files moved and the DB kept the dead scene-folder paths.
    The row id is preserved, so ratings and play_history stay intact."""
    key = norm_key(artist, title, album)
    row = conn.execute("SELECT id, path FROM tracks WHERE norm_key = ?", (key,)).fetchone()
    if row:
        if path and path != row["path"] and not os.path.exists(row["path"]):
            conn.execute("UPDATE tracks SET path = ? WHERE id = ?", (path, row["id"]))
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


_NOT_AUDIOBOOK = "path NOT LIKE '%/Audiobooks/%'"


def all_tracks(conn) -> list[sqlite3.Row]:
    # Exclude audiobooks (they live under a top-level Audiobooks/ dir) — the radio
    # plays music, not chapter-by-chapter narration.
    return conn.execute(
        f"SELECT id, path, artist, title FROM tracks WHERE {_NOT_AUDIOBOOK}"
    ).fetchall()


def music_count(conn) -> int:
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM tracks WHERE {_NOT_AUDIOBOOK}"
    ).fetchone()["n"]


def record_play(conn, track_id: int, reason: str | None = None, user_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO play_history (track_id, user_id, played_at, reason) VALUES (?, ?, ?, ?)",
        (track_id, user_id, _now(), reason),
    )
    conn.commit()


def search_tracks(conn, query: str, limit: int = 25) -> list[sqlite3.Row]:
    """Substring match on artist or title, music only — feeds /request autocomplete."""
    like = f"%{query.strip()}%"
    return conn.execute(
        f"SELECT id, artist, title FROM tracks "
        f"WHERE {_NOT_AUDIOBOOK} AND (artist LIKE ? OR title LIKE ?) "
        f"ORDER BY artist, title LIMIT ?",
        (like, like, limit),
    ).fetchall()


def add_request(conn, track_id: int, guild_id: str, user_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO requests (track_id, guild_id, user_id, requested_at) VALUES (?, ?, ?, ?)",
        (track_id, guild_id, user_id, _now()),
    )
    conn.commit()


def next_request(conn, guild_id: str) -> sqlite3.Row | None:
    """The oldest unplayed request FOR THIS GUILD as a playable track row, or None.
    Scoped per guild so a shared ratings DB doesn't bleed requests across servers."""
    return conn.execute(
        "SELECT t.id, t.path, t.artist, t.title FROM requests req "
        "JOIN tracks t ON t.id = req.track_id "
        "WHERE req.played_at IS NULL AND req.guild_id = ? "
        "ORDER BY req.requested_at ASC LIMIT 1",
        (guild_id,),
    ).fetchone()


def mark_request_played(conn, track_id: int, guild_id: str) -> None:
    """Mark the oldest unplayed request for this track IN THIS GUILD as played."""
    conn.execute(
        "UPDATE requests SET played_at = ? WHERE id = ("
        "SELECT id FROM requests WHERE track_id = ? AND guild_id = ? AND played_at IS NULL "
        "ORDER BY requested_at ASC LIMIT 1)",
        (_now(), track_id, guild_id),
    )
    conn.commit()


def pending_request_count(conn, guild_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM requests WHERE played_at IS NULL AND guild_id = ?",
        (guild_id,),
    ).fetchone()["n"]


def upsert_user(conn, user_id: str, name: str | None = None) -> None:
    conn.execute(
        "INSERT INTO users (id, name) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = COALESCE(excluded.name, users.name)",
        (str(user_id), name),
    )


def list_guilds(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT guild_id, voice_channel_id, nowplaying_channel_id, music_dir "
        "FROM guilds WHERE enabled = 1"
    ).fetchall()


def get_guild(conn, guild_id) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT guild_id, voice_channel_id, nowplaying_channel_id, music_dir "
        "FROM guilds WHERE guild_id = ? AND enabled = 1",
        (str(guild_id),),
    ).fetchone()


def upsert_guild(conn, guild_id, voice_channel_id, nowplaying_channel_id=None, music_dir=None) -> None:
    conn.execute(
        "INSERT INTO guilds (guild_id, voice_channel_id, nowplaying_channel_id, music_dir, enabled, added) "
        "VALUES (?, ?, ?, ?, 1, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET voice_channel_id=excluded.voice_channel_id, "
        "nowplaying_channel_id=excluded.nowplaying_channel_id, music_dir=excluded.music_dir, enabled=1",
        (str(guild_id), str(voice_channel_id) if voice_channel_id else None,
         str(nowplaying_channel_id) if nowplaying_channel_id else None, music_dir, _now()),
    )
    conn.commit()


def disable_guild(conn, guild_id) -> None:
    conn.execute("UPDATE guilds SET enabled = 0 WHERE guild_id = ?", (str(guild_id),))
    conn.commit()


def get_add_role(conn, guild_id) -> str | None:
    """The role id allowed to /add + /youtube in this guild, or None (open to all)."""
    row = conn.execute("SELECT add_role_id FROM guilds WHERE guild_id = ?", (str(guild_id),)).fetchone()
    return row["add_role_id"] if row else None


def set_add_role(conn, guild_id, role_id) -> None:
    """Restrict /add + /youtube to a role (role_id), or None to open it back up."""
    conn.execute(
        "UPDATE guilds SET add_role_id = ? WHERE guild_id = ?",
        (str(role_id) if role_id else None, str(guild_id)),
    )
    conn.commit()


def add_enabled(conn, guild_id) -> bool:
    """Whether /add + /youtube are turned on (and thus registered/visible) in this guild.
    Unknown guild -> False (a server the bot just joined shows no add commands until setup)."""
    row = conn.execute("SELECT add_enabled FROM guilds WHERE guild_id = ?", (str(guild_id),)).fetchone()
    return bool(row["add_enabled"]) if row else False


def set_add_enabled(conn, guild_id, on: bool) -> None:
    conn.execute(
        "UPDATE guilds SET add_enabled = ? WHERE guild_id = ?",
        (1 if on else 0, str(guild_id)),
    )
    conn.commit()


def touch_presence(conn, user_id, guild_id) -> None:
    """Mark a user seen in a guild (called on every rating) — a recent touch is
    presence, scoped to that server."""
    conn.execute(
        "INSERT INTO presence (user_id, guild_id, seen_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, guild_id) DO UPDATE SET seen_at = excluded.seen_at",
        (str(user_id), str(guild_id), _now()),
    )
    conn.commit()


def present_since(conn, guild_id, minutes: int) -> list[sqlite3.Row]:
    """(user_id, name) seen in THIS guild within the window — presence-by-rating,
    per server so it doesn't bleed across tenants."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    return conn.execute(
        "SELECT p.user_id AS user_id, COALESCE(u.name, p.user_id) AS name "
        "FROM presence p LEFT JOIN users u ON u.id = p.user_id "
        "WHERE p.guild_id = ? AND p.seen_at > ?",
        (str(guild_id), cutoff),
    ).fetchall()


def recent_raters(conn, minutes: int) -> list[sqlite3.Row]:
    """(user_id, name) for everyone who rated within the last `minutes` — a recent
    rating signals presence, so a listener on a shared speaker counts without
    joining voice. name falls back to the id when we've never stored one."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    return conn.execute(
        "SELECT r.user_id AS user_id, COALESCE(u.name, r.user_id) AS name "
        "FROM (SELECT user_id, MAX(updated) AS last FROM ratings GROUP BY user_id) r "
        "LEFT JOIN users u ON u.id = r.user_id "
        "WHERE r.last > ?",
        (cutoff,),
    ).fetchall()


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
    conn.commit()


def rating_summary(conn, user_id: str) -> dict[int, int]:
    """{rating value: count} over all of one user's ratings. Feeds /myratings."""
    rows = conn.execute(
        "SELECT value, COUNT(*) AS n FROM ratings WHERE user_id = ? GROUP BY value",
        (str(user_id),),
    ).fetchall()
    return {r["value"]: r["n"] for r in rows}


def recent_ratings(conn, user_id: str, limit: int = 10) -> list[sqlite3.Row]:
    """A user's most-recently-changed ratings (artist, title, value), newest first."""
    return conn.execute(
        "SELECT t.artist, t.title, r.value FROM ratings r JOIN tracks t ON t.id = r.track_id "
        "WHERE r.user_id = ? ORDER BY r.updated DESC LIMIT ?",
        (str(user_id), limit),
    ).fetchall()


def recent_plays(conn, limit: int = 10) -> list[sqlite3.Row]:
    """The most-recently-played DISTINCT tracks (id, artist, title), newest first.
    Feeds /recent so a listener can rate a track they didn't click while it played."""
    return conn.execute(
        "SELECT t.id, t.artist, t.title, MAX(p.played_at) AS last_played "
        "FROM play_history p JOIN tracks t ON t.id = p.track_id "
        "GROUP BY t.id ORDER BY last_played DESC LIMIT ?",
        (limit,),
    ).fetchall()
