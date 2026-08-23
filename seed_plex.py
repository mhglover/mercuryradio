"""One-shot: seed mercuryradio ratings from Plex ★.

Reads a (WAL-safe) copy of the Plex library DB, pulls each rated track's stars
and its artist/title/album, and writes ratings into the mercuryradio DB under one
owner. Positive-only mapping — Plex stars rarely mean "ban this", so 1–2★ are not
imported as vetoes.

    Plex 0–10   →  mercuryradio
    >= 9 (5★)   →  love  (+2)
    7–8  (4★)   →  like  (+1)
    5–6  (3★)   →  shrug ( 0)
    < 5  (1–2★) →  not imported

This is not part of the bot. Run it once against a copy of the Plex DB:
    python seed_plex.py --plex-db /path/to/com.plexapp.plugins.library.db --owner <discord_user_id>

Match strategy: by file path first (the bot and Plex both reference the same
files), then by tags (normalized artist|title|album) so tag-duplicate files whose
path the library scan didn't keep still match. Needs no Plex token — DB read only.
"""

import argparse
import os
import sqlite3

import db

# Plex track ratings live in metadata_item_settings, joined to the track by GUID
# (metadata_items.guid = mis.guid). Most tracks carry a matched 'plex://track/...'
# guid, not 'local://<id>' — measured 5,996 matches via guid vs 575 via local://id.
# Join up to the file, and sideways to album/artist titles via parent_id for the
# tag fallback. DISTINCT because a guid can map to duplicate metadata_items.
PLEX_QUERY = """
SELECT DISTINCT
       mp.file        AS file,
       mis.rating     AS rating,
       track.title    AS title,
       album.title    AS album,
       artist.title   AS artist
FROM metadata_item_settings mis
JOIN metadata_items track   ON track.guid = mis.guid AND track.metadata_type = 10
LEFT JOIN media_items medi  ON medi.metadata_item_id = track.id
LEFT JOIN media_parts mp    ON mp.media_item_id = medi.id
LEFT JOIN metadata_items album  ON track.parent_id = album.id
LEFT JOIN metadata_items artist ON album.parent_id = artist.id
WHERE mis.rating > 0
"""


def map_star(rating: float) -> int | None:
    if rating >= 9:
        return db.LOVE
    if rating >= 7:
        return db.LIKE
    if rating >= 5:
        return db.SHRUG
    return None  # 1–2★: not imported


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plex-db", required=True, help="path to a copy of com.plexapp.plugins.library.db")
    ap.add_argument("--owner", default=os.environ.get("OWNER_DISCORD_ID"), help="Discord user id to attach ratings to")
    args = ap.parse_args()
    if not args.owner:
        ap.error("--owner (or OWNER_DISCORD_ID) is required")

    conn = db.connect()
    db.upsert_user(conn, args.owner, "owner")

    plex = sqlite3.connect(f"file:{args.plex_db}?mode=ro", uri=True)
    plex.row_factory = sqlite3.Row

    applied = skipped_low = unmatched = 0
    for r in plex.execute(PLEX_QUERY):
        value = map_star(r["rating"])
        if value is None:
            skipped_low += 1
            continue
        track_id = None
        if r["file"]:
            track_id = db.track_id_for_path(conn, r["file"])
        if track_id is None and r["artist"] and r["title"]:
            track_id = db.track_id_for_key(conn, db.norm_key(r["artist"], r["title"], r["album"] or ""))
        if track_id is None:
            unmatched += 1
            continue
        db.set_rating(conn, args.owner, track_id, value)
        applied += 1

    conn.commit()
    plex.close()
    conn.close()
    print(f"seeded: applied={applied} unmatched={unmatched} skipped_low(1-2★)={skipped_low}")


if __name__ == "__main__":
    main()
