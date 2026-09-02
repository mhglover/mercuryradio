"""Self-check for the shasradio importer: the MySQL-dump parser (the risky bit)
and one end-to-end restore into an in-memory mercuryradio db.

    python test_shas_import.py
"""

import db
import shas_import as si


def test_parse_row_handles_strings_commas_escapes_null():
    # fid, filename, artist, directory, min, sec, time, title, notes, uploader, flag
    body = r"96,'ATC/x.mp3','ATC','ATC',4,14,254,'Around The World (Remix)','',0,NULL"
    f = si._parse_row(body)
    assert f[0] == "96" and f[2] == "ATC" and f[7] == "Around The World (Remix)"
    assert f[10] is None  # NULL
    # backslash-escaped apostrophe (as mysqldump 3.23 writes it) + a comma in the title
    body2 = r"7,'d/y.mp3','Guns N\' Roses','d',3,0,180,'Welcome, to the Jungle','',0,NULL"
    g = si._parse_row(body2)
    assert g[2] == "Guns N' Roses", g[2]
    assert g[7] == "Welcome, to the Jungle", g[7]


_DUMP = r"""
CREATE TABLE users ( uid int );
INSERT INTO users VALUES (10,'shas','x','y','e','i','Matthew',2,'score','0','2002-01-01',0,0,1,0);
INSERT INTO users VALUES (25,'deirie','x','y','e','i','D',0,'score','0','2002-01-01',0,0,1,0);
CREATE TABLE files ( fid int );
INSERT INTO files VALUES (1,'a/1.mp3','The Cure','a',3,0,180,'Just Like Heaven','',0,NULL);
INSERT INTO files VALUES (2,'b/2.mp3','Morphine','b',3,0,180,'Whisper','',0,NULL);
INSERT INTO files VALUES (3,'c/3.mp3','Nobody','c',3,0,180,'Not In Library','',0,NULL);
CREATE TABLE ratings ( fid int );
INSERT INTO ratings VALUES (1,10,2);
INSERT INTO ratings VALUES (2,10,-4);
INSERT INTO ratings VALUES (1,25,1);
INSERT INTO ratings VALUES (2,99,2);
"""


def _seed_library():
    conn = db.connect(":memory:")
    db.upsert_track(conn, "The Cure", "Just Like Heaven", "Kiss Me", "/m/cure.mp3")
    db.upsert_track(conn, "Morphine", "Whisper", "Cure for Pain", "/m/whisper.mp3")
    conn.commit()
    return conn


def test_run_dry_then_commit():
    si.SHAS_MAP = {"shas": "9999"}  # inject a fake map; real ids live in a gitignored file
    me = "9999"
    conn = _seed_library()
    # dry run: nothing written
    stats = si.run(_DUMP, conn, commit=False, overwrite=False)
    assert stats["applied"] == 2, stats            # shas: Just Like Heaven + Whisper
    assert stats["no_track"] == 0                   # 'Not In Library' is unrated, never seen
    assert "deirie" in stats["unmapped"]            # deirie not in SHAS_MAP -> skipped
    assert db.get_rating(conn, me, 1) is None       # dry run wrote nothing

    # commit: shas's two ratings land, with the original scores (incl. the -4 veto)
    stats = si.run(_DUMP, conn, commit=True, overwrite=False)
    assert stats["applied"] == 2
    assert db.get_rating(conn, me, 1) == db.LOVE
    assert db.get_rating(conn, me, 2) == db.HATE

    # re-run additive: everything already rated -> skipped, not doubled
    stats = si.run(_DUMP, conn, commit=True, overwrite=False)
    assert stats["applied"] == 0 and stats["skipped_existing"] == 2


if __name__ == "__main__":
    for fn in (
        test_parse_row_handles_strings_commas_escapes_null,
        test_run_dry_then_commit,
    ):
        fn()
        print(f"ok {fn.__name__}")
    print("all shas_import checks passed")


def test_appledouble_rows_are_purged_and_skipped():
    import db as _db
    c = _db.connect(":memory:")
    _db.upsert_track(c, "Unknown Artist", "._junk", "", "/music/A/._junk.mp3")
    from db import _migrate
    _migrate(c)  # the purge runs at every connect
    assert c.execute("SELECT COUNT(*) FROM tracks WHERE path LIKE '%/._%'").fetchone()[0] == 0
