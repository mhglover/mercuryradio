"""Filesystem library provider: scan MUSIC_DIR, read tags with mutagen, and
upsert each file into the tracks table. This is the default (and only) catalog
source; Plex is a rating seed, not a catalog (see seed_plex.py)."""

import os
import re
from pathlib import Path

import mutagen

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma"}

# Junk a video title picks up that isn't part of the song name: "(Official Video)",
# "[HD]", "(Lyrics)", "(Remastered)" and the like. Stripped before parsing artist/title.
_TITLE_JUNK = re.compile(
    r"\s*[\(\[][^)\]]*\b(official|lyric|lyrics|audio|video|visualizer|hd|hq|4k|"
    r"mv|m/v|explicit|remaster(?:ed)?|full\s+album)\b[^)\]]*[\)\]]",
    re.I,
)
# yt-dlp prints "NA" for a field it couldn't fill.
def _has(v) -> bool:
    return bool(v) and str(v).strip() not in ("", "NA")


def _clean_title(t: str) -> str:
    return _TITLE_JUNK.sub("", t or "").strip(" -–—")


def _parse_title(vtitle: str) -> tuple[str | None, str]:
    """Split a "Artist - Title (Official Video)" video title into (artist, title).
    Returns (None, cleaned-title) when there's no separator to split on."""
    cleaned = _clean_title(vtitle)
    for sep in (" - ", " – ", " — "):
        if sep in cleaned:
            a, t = cleaned.split(sep, 1)
            return a.strip() or None, t.strip() or cleaned
    return None, cleaned or (vtitle or "")


def resolve_yt_tags(meta_artist, meta_track, vtitle, artist_override=None, title_override=None):
    """Decide the artist/title to tag a downloaded video with, cheapest-clean first:
    (a) an explicit /youtube artist:/title: override, then (b) yt-dlp's own artist/track
    fields (populated for YouTube Music / "- Topic" channels), then (c) parse the video
    title on the first " - ". These tags drive norm_key/dedup, so they're set explicitly
    on the file rather than trusting whatever the source embedded."""
    artist = artist_override or (meta_artist.strip() if _has(meta_artist) else None)
    title = title_override or (meta_track.strip() if _has(meta_track) else None)
    if not artist or not title:
        pa, pt = _parse_title(vtitle)
        artist = artist or pa
        title = title or pt
    return artist or "Unknown Artist", title or (vtitle or "Unknown Title")


def write_tags(path: str, artist: str, title: str, album: str | None = None) -> None:
    """Write artist/title (and optional album) onto an audio file so its tags — and
    thus norm_key/dedup — are what we intend. Best-effort: a file that won't take easy
    tags just keeps its filename fallback."""
    try:
        mf = mutagen.File(path, easy=True)
        if mf is None:
            return
        if mf.tags is None:
            mf.add_tags()
        mf["artist"] = artist
        mf["title"] = title
        if album is not None:
            mf["album"] = album
        mf.save()
    except Exception:
        pass


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


def extract_cover(path: str) -> bytes | None:
    """Return embedded cover-art image bytes for a track, or None. Handles FLAC
    picture blocks and ID3 APIC frames; best-effort, never raises."""
    try:
        mf = mutagen.File(path)
        if mf is None:
            return None
        pics = getattr(mf, "pictures", None)  # FLAC / Ogg
        if pics:
            return pics[0].data
        tags = getattr(mf, "tags", None)
        if tags:
            for key in tags.keys():
                if key.startswith("APIC"):  # ID3 embedded picture
                    return tags[key].data
            cov = tags.get("covr")  # MP4/M4A
            if cov:
                return bytes(cov[0])
    except Exception:
        return None
    return None


def scan(music_dir: str, db_path: str | None = None) -> int:
    """Walk music_dir, upsert every audio file. Returns the track count.

    Opens its own DB connection so it can run in a thread executor (off the
    asyncio loop) — a 7k-file mutagen walk must not block discord's heartbeat.
    """
    import db

    conn = db.connect(db_path)
    try:
        for p in Path(music_dir).rglob("*"):
            if p.name.startswith("._"):
                continue  # AppleDouble sidecars — resource forks, not audio; 16 were
                          # indexed as unplayable "Unknown Artist" tracks (found 9/2)
            if p.suffix.lower() in AUDIO_EXTS and p.is_file():
                artist, title, album, duration = _read_tags(str(p))
                db.upsert_track(conn, artist, title, album, str(p), duration)
        conn.commit()
        return conn.execute("SELECT COUNT(*) AS n FROM tracks").fetchone()["n"]
    finally:
        conn.close()
