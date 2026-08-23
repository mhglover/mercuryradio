"""Fast self-check for the selection engine. No discord, no network: seed an
in-memory SQLite library + ratings and assert each picker's contract.

    python test_engine.py
"""

import db
import engine


def _seed():
    conn = db.connect(":memory:")
    t = {}
    for artist, title in [("A", "a1"), ("A", "a2"), ("B", "b1"), ("C", "c1"), ("D", "d1")]:
        t[(artist, title)] = db.upsert_track(conn, artist, title, "", f"/music/{artist}/{title}.mp3")
    ab = db.upsert_track(conn, "Author", "Chapter 1", "", "/music/Audiobooks/Author/01.mp3")
    conn.commit()
    return conn, t, ab


def test_top_scores_over_present_members():
    conn, t, _ab = _seed()
    # present member u1: loves B/b1 (+2), likes A/a1 (+1). absent u2 loves C/c1.
    db.set_rating(conn, "u1", t[("B", "b1")], db.LOVE)
    db.set_rating(conn, "u1", t[("A", "a1")], db.LIKE)
    db.set_rating(conn, "u2", t[("C", "c1")], db.LOVE)
    conn.commit()
    row, name = engine.pick_next(conn, ["u1"], 0, top_k=1)  # slot 0 == 'top'
    assert name == "top", name
    assert (row["artist"], row["title"]) == ("B", "b1"), dict(row)


def test_never_picks_an_audiobook():
    conn, _t, _ab = _seed()  # nothing rated -> wildcard/any territory
    for _ in range(50):
        row, _n = engine.pick_next(conn, [], 4)
        assert row is not None and "/Audiobooks/" not in row["path"], dict(row)


def test_timeout_excludes_recently_played():
    conn, t, _ab = _seed()
    for k in t:
        db.set_rating(conn, "u1", t[k], db.LIKE)
    conn.commit()
    db.record_play(conn, t[("B", "b1")])
    for _ in range(20):
        row, _n = engine.pick_next(conn, ["u1"], 0, timeout_days=3650)
        assert row["id"] != t[("B", "b1")], "played track re-picked inside timeout"


def test_artist_guard_avoids_back_to_back():
    conn, t, _ab = _seed()
    for k in t:
        db.set_rating(conn, "u1", t[k], db.LIKE)
    conn.commit()
    db.record_play(conn, t[("A", "a1")])  # last artist == A
    for _ in range(20):
        row, _n = engine.pick_next(conn, ["u1"], 0, artist_guard=1)
        assert row["artist"] != "A", "same artist picked back-to-back"


def test_always_returns_something_when_library_nonempty():
    conn, _t, _ab = _seed()
    row, name = engine.pick_next(conn, [], 0)  # no members, nothing rated
    assert row is not None and name is not None


if __name__ == "__main__":
    for fn in (
        test_top_scores_over_present_members,
        test_never_picks_an_audiobook,
        test_timeout_excludes_recently_played,
        test_artist_guard_avoids_back_to_back,
        test_always_returns_something_when_library_nonempty,
    ):
        fn()
        print(f"ok {fn.__name__}")
    print("all engine checks passed")
