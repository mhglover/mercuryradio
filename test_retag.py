"""retag_track: fix tags, keep ratings, recompute norm_key, guard collisions."""

import db


def _conn():
    c = db.connect(":memory:")
    return c


def test_retag_changes_tags_and_key_but_keeps_ratings():
    c = _conn()
    tid = db.upsert_track(c, "tart", "anri arcane", "", "/music/_Added/x.mp3")
    db.upsert_user(c, "u1", "u1")
    db.set_rating(c, "u1", tid, db.LOVE)
    ok, info = db.retag_track(c, tid, "Anri Arcane", "Tart")
    assert ok and info == "/music/_Added/x.mp3"  # returns the path to sync file tags
    row = c.execute("SELECT artist, title, norm_key FROM tracks WHERE id = ?", (tid,)).fetchone()
    assert (row["artist"], row["title"]) == ("Anri Arcane", "Tart")
    assert row["norm_key"] == db.norm_key("Anri Arcane", "Tart", "")
    assert db.get_rating(c, "u1", tid) == db.LOVE  # rating survived (keyed on track_id)


def test_retag_rejects_a_collision():
    c = _conn()
    a = db.upsert_track(c, "A", "One", "", "/m/a.mp3")
    b = db.upsert_track(c, "B", "Two", "", "/m/b.mp3")
    ok, msg = db.retag_track(c, b, "A", "One")  # would collide with track a
    assert not ok and "already" in msg.lower()
    # b unchanged
    assert c.execute("SELECT artist FROM tracks WHERE id = ?", (b,)).fetchone()["artist"] == "B"


def test_retag_missing_track():
    ok, msg = db.retag_track(_conn(), 999, "X", "Y")
    assert not ok
