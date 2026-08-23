"""Self-check for the Plex ★ seed (run: `python test_seed.py`).

Builds a synthetic Plex DB + a mercuryradio DB and asserts the positive-only
mapping and the path-then-tags matching. No framework.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile


def _fake_plex(path):
    p = sqlite3.connect(path)
    p.executescript("""
        CREATE TABLE metadata_items (id INTEGER, title TEXT, parent_id INTEGER, metadata_type INTEGER, guid TEXT);
        CREATE TABLE media_items (id INTEGER, metadata_item_id INTEGER);
        CREATE TABLE media_parts (id INTEGER, media_item_id INTEGER, file TEXT);
        CREATE TABLE metadata_item_settings (guid TEXT, rating REAL);
    """)
    # artist(1) → album(2) → tracks(10,11,12,13); track 20 is unmatched.
    # Tracks carry a matched plex://track guid, joined to settings by guid.
    def g(tid):
        return f"plex://track/{tid}"
    rows_mi = [
        (1, "The Artist", None, 8, None), (2, "The Album", 1, 9, None),
        (10, "Love Song", 2, 10, g(10)), (11, "Like Song", 2, 10, g(11)),
        (12, "Meh Song", 2, 10, g(12)), (13, "Bad Song", 2, 10, g(13)), (20, "Ghost Song", 2, 10, g(20)),
    ]
    p.executemany("INSERT INTO metadata_items VALUES (?,?,?,?,?)", rows_mi)
    # media parts: track 10 matches by path; 11 by tags only (path not in library); others by tags
    parts = [(10, "/music/love.flac"), (11, "/music/DUP/like.flac"),
             (12, "/music/meh.flac"), (13, "/music/bad.flac"), (20, "/music/ghost.flac")]
    for i, (tid, f) in enumerate(parts):
        p.execute("INSERT INTO media_items VALUES (?,?)", (100 + i, tid))
        p.execute("INSERT INTO media_parts VALUES (?,?,?)", (200 + i, 100 + i, f))
    ratings = [(10, 10.0), (11, 8.0), (12, 6.0), (13, 4.0), (20, 10.0)]  # 5★,4★,3★,2★,5★
    for tid, r in ratings:
        p.execute("INSERT INTO metadata_item_settings VALUES (?,?)", (f"plex://track/{tid}", r))
    p.commit()
    p.close()


def main():
    import db

    tmp = tempfile.mkdtemp()
    mr_db = os.path.join(tmp, "mr.db")
    plex_db = os.path.join(tmp, "plex.db")
    os.environ["DB_PATH"] = mr_db

    conn = db.connect(mr_db)
    # library has love (matched by path), like (tags match, different path), meh, bad — NOT ghost
    db.upsert_track(conn, "The Artist", "Love Song", "The Album", "/music/love.flac")
    db.upsert_track(conn, "The Artist", "Like Song", "The Album", "/music/REAL/like.flac")
    db.upsert_track(conn, "The Artist", "Meh Song", "The Album", "/music/meh.flac")
    db.upsert_track(conn, "The Artist", "Bad Song", "The Album", "/music/bad.flac")
    conn.commit()
    conn.close()

    _fake_plex(plex_db)

    out = subprocess.run(
        [sys.executable, "seed_plex.py", "--plex-db", plex_db, "--owner", "42"],
        capture_output=True, text=True, env={**os.environ},
    )
    print(out.stdout.strip() or out.stderr.strip())
    assert out.returncode == 0, out.stderr

    conn = db.connect(mr_db)
    got = {
        r["title"]: r["value"]
        for r in conn.execute(
            "SELECT t.title, rt.value FROM ratings rt JOIN tracks t ON t.id = rt.track_id WHERE rt.user_id='42'"
        )
    }
    conn.close()

    assert got.get("Love Song") == db.LOVE, got            # 5★ path match → +2
    assert got.get("Like Song") == db.LIKE, got            # 4★ tag match → +1
    assert got.get("Meh Song") == db.SHRUG, got            # 3★ → 0
    assert "Bad Song" not in got, got                      # 2★ → not imported
    assert "Ghost Song" not in got, got                    # unmatched → not imported
    print("PASS:", got)


if __name__ == "__main__":
    main()
