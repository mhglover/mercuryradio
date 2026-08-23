"""Filesystem library provider: scan MUSIC_DIR, read tags with mutagen, and
upsert each file into the tracks table. This is the default (and only) catalog
source; Plex is a rating seed, not a catalog (see seed_plex.py)."""

import os
from pathlib import Path

import mutagen

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma"}


def _first(tags, *keys) -> str | None:
    for k in keys:
        val = tags.get(k)
        if val:
            return str(val[0]) if isinstance(val, list) else str(val)
    return None


def _read_tags(path: str) -> tuple[str, str, str, float | None]:
    """Return (artist, title, album, duration). Falls back to the filename for a
    missing title so an untagged file is still playable and rateable."""
    artist = title = album = None
    duration = None
    try:
        mf = mutagen.File(path, easy=True)
        if mf is not None:
            artist = _first(mf, "artist", "albumartist")
            title = _first(mf, "title")
            album = _first(mf, "album")
            if mf.info and getattr(mf.info, "length", None):
                duration = float(mf.info.length)
    except Exception:
        pass  # unreadable/corrupt tag block — fall back to the filename
    if not title:
        title = Path(path).stem
    return artist or "Unknown Artist", title, album or "", duration


def scan(music_dir: str, db_path: str | None = None) -> int:
    """Walk music_dir, upsert every audio file. Returns the track count.

    Opens its own DB connection so it can run in a thread executor (off the
    asyncio loop) — a 7k-file mutagen walk must not block discord's heartbeat.
    """
    import db

    conn = db.connect(db_path)
    try:
        for p in Path(music_dir).rglob("*"):
            if p.suffix.lower() in AUDIO_EXTS and p.is_file():
                artist, title, album, duration = _read_tags(str(p))
                db.upsert_track(conn, artist, title, album, str(p), duration)
        conn.commit()
        return conn.execute("SELECT COUNT(*) AS n FROM tracks").fetchone()["n"]
    finally:
        conn.close()
