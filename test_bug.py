"""/bug reports land in the bugs table: timestamped, user-attributed, track-optional."""

import db


def test_add_bug_records_everything():
    c = db.connect(":memory:")
    tid = db.upsert_track(c, "Prof", "Gasoline", "", "/m/g.mp3")
    bug_id = db.add_bug(c, "u1", "g1", "card reverted again", track_id=tid)
    row = c.execute("SELECT * FROM bugs WHERE id = ?", (bug_id,)).fetchone()
    assert (row["user_id"], row["guild_id"], row["track_id"], row["text"]) == (
        "u1", "g1", tid, "card reverted again")
    assert row["reported_at"]  # timestamped


def test_add_bug_without_a_track_or_guild():
    c = db.connect(":memory:")
    bug_id = db.add_bug(c, "u1", None, "bot fell over in DMs")
    row = c.execute("SELECT track_id, guild_id FROM bugs WHERE id = ?", (bug_id,)).fetchone()
    assert row["track_id"] is None and row["guild_id"] is None
