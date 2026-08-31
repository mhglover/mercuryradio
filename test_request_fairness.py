"""Request fairness: one pending request per user per guild — a new request replaces
yours and goes to the back of the queue (his rule, 2026-08-31)."""

import db


def _setup():
    c = db.connect(":memory:")
    a = db.upsert_track(c, "A", "One", "", "/m/a.mp3")
    b = db.upsert_track(c, "B", "Two", "", "/m/b.mp3")
    return c, a, b


def test_new_request_replaces_and_goes_to_the_back():
    c, a, b = _setup()
    assert db.add_request(c, a, "g", "alice") is None  # first request, nothing replaced
    db.add_request(c, b, "g", "bob")
    replaced = db.add_request(c, b, "g", "alice")      # alice changes her mind
    assert replaced == "A – One"
    # bob's older request now leads; alice's replacement sorted to the back
    assert db.next_request(c, "g")["artist"] == "B"
    assert db.pending_request_count(c, "g") == 2       # one each, never stacked


def test_same_track_re_request_just_moves_to_the_back():
    c, a, b = _setup()
    db.add_request(c, a, "g", "alice")
    db.add_request(c, b, "g", "bob")
    assert db.add_request(c, a, "g", "alice") == "A – One"  # replaced by itself
    assert db.next_request(c, "g")["artist"] == "B"


def test_played_requests_are_not_touched_and_guilds_are_scoped():
    c, a, b = _setup()
    db.add_request(c, a, "g", "alice")
    db.mark_request_played(c, a, "g")                  # history row
    db.add_request(c, a, "other-guild", "alice")       # pending elsewhere
    db.add_request(c, b, "g", "alice")
    assert db.pending_request_count(c, "g") == 1
    assert db.pending_request_count(c, "other-guild") == 1  # other guild untouched
    played = c.execute("SELECT COUNT(*) AS n FROM requests WHERE played_at IS NOT NULL").fetchone()["n"]
    assert played == 1                                 # history survived the replace
