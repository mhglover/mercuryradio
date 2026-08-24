"""Fast self-check for the selection engine + request queue. No discord, no
network: seed an in-memory SQLite library + ratings and assert each contract.

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
    row, name = engine.pick(conn, ["u1"], "top", top_k=1)
    assert name == "top", name
    assert (row["artist"], row["title"]) == ("B", "b1"), dict(row)


def test_never_picks_an_audiobook():
    conn, _t, _ab = _seed()  # nothing rated -> wildcard/any territory
    for _ in range(50):
        row, _n = engine.pick(conn, [], "wildcard")
        assert row is not None and "/Audiobooks/" not in row["path"], dict(row)


def test_timeout_excludes_recently_played():
    conn, t, _ab = _seed()
    for k in t:
        db.set_rating(conn, "u1", t[k], db.LIKE)
    conn.commit()
    db.record_play(conn, t[("B", "b1")])
    for _ in range(20):
        row, _n = engine.pick(conn, ["u1"], "top", timeout_days=3650)
        assert row["id"] != t[("B", "b1")], "played track re-picked inside timeout"


def test_artist_guard_avoids_back_to_back():
    conn, t, _ab = _seed()
    for k in t:
        db.set_rating(conn, "u1", t[k], db.LIKE)
    conn.commit()
    db.record_play(conn, t[("A", "a1")])  # last artist == A
    for _ in range(20):
        row, _n = engine.pick(conn, ["u1"], "top", artist_guard=1)
        assert row["artist"] != "A", "same artist picked back-to-back"


def test_always_returns_something_when_library_nonempty():
    conn, _t, _ab = _seed()
    row, name = engine.pick(conn, [], "top")  # no members, nothing rated
    assert row is not None and name is not None


def test_new_block_shuffles_and_grows_with_backlog():
    for _ in range(20):
        b = engine.new_block()
        assert sorted(b) == sorted(engine.BLOCK_TYPES)
    grown = engine.new_block(pending_requests=engine.REQUEST_GROW_AT)
    assert len(grown) == len(engine.BLOCK_TYPES) + 1  # shasradio's appended slot
    assert grown.count("request") == 2


def test_request_queue_is_fifo_and_guild_scoped():
    conn, t, _ab = _seed()
    g1, g2 = "guildA", "guildB"
    assert db.next_request(conn, g1) is None
    db.add_request(conn, t[("C", "c1")], g1, "u1")
    db.add_request(conn, t[("D", "d1")], g1, "u1")
    db.add_request(conn, t[("B", "b1")], g2, "u2")  # a request on the OTHER server
    r1 = db.next_request(conn, g1)
    assert (r1["artist"], r1["title"]) == ("C", "c1")  # FIFO within the guild
    assert db.next_request(conn, g2)["title"] == "b1"  # g2 sees only its own
    db.mark_request_played(conn, r1["id"], g1)
    r2 = db.next_request(conn, g1)
    assert (r2["artist"], r2["title"]) == ("D", "d1")
    db.mark_request_played(conn, r2["id"], g1)
    assert db.next_request(conn, g1) is None
    assert db.pending_request_count(conn, g1) == 0
    assert db.pending_request_count(conn, g2) == 1  # the other server's request is untouched


def test_search_excludes_audiobooks():
    conn, _t, _ab = _seed()
    hits = db.search_tracks(conn, "Chapter", 25)  # audiobook title
    assert hits == []


def test_recent_raters_is_presence_window():
    conn, t, _ab = _seed()
    db.upsert_user(conn, "u1", "Ann")
    db.set_rating(conn, "u1", t[("A", "a1")], db.LIKE)  # updated = now
    # u2 rated long ago -> outside the window, not present
    conn.execute(
        "INSERT INTO ratings (user_id, track_id, value, updated) VALUES (?, ?, ?, ?)",
        ("u2", t[("B", "b1")], db.LOVE, "2000-01-01T00:00:00+00:00"),
    )
    conn.commit()
    got = {r["user_id"]: r["name"] for r in db.recent_raters(conn, 30)}
    assert "u1" in got and "u2" not in got
    assert got["u1"] == "Ann"  # display name comes from the users table
    assert db.recent_raters(conn, 0) == []  # a zero window means nobody is recent


def test_presence_is_scoped_per_guild():
    conn = db.connect(":memory:")
    db.upsert_user(conn, "u1", "Ann")
    db.touch_presence(conn, "u1", "gA")
    assert {r["user_id"] for r in db.present_since(conn, "gA", 30)} == {"u1"}
    assert db.present_since(conn, "gB", 30) == []  # present in gA only, not gB
    assert db.present_since(conn, "gA", 0) == []   # zero window -> nobody
    assert db.present_since(conn, "gA", 30)[0]["name"] == "Ann"


def test_guilds_table_crud():
    conn = db.connect(":memory:")
    assert db.list_guilds(conn) == []
    db.upsert_guild(conn, "123", "456", "789")
    g = db.get_guild(conn, "123")
    assert g["voice_channel_id"] == "456" and g["nowplaying_channel_id"] == "789"
    assert len(db.list_guilds(conn)) == 1
    db.disable_guild(conn, "123")
    assert db.list_guilds(conn) == [] and db.get_guild(conn, "123") is None


def test_rating_summary_and_recent():
    conn, t, _ab = _seed()
    # u1 rates three tracks; a2 gets re-rated (upsert, not a second row).
    db.set_rating(conn, "u1", t[("A", "a1")], db.LOVE)
    db.set_rating(conn, "u1", t[("B", "b1")], db.LOVE)
    db.set_rating(conn, "u1", t[("A", "a2")], db.HATE)
    db.set_rating(conn, "u1", t[("A", "a2")], db.LIKE)  # changed hate -> like
    assert db.rating_summary(conn, "u1") == {db.LOVE: 2, db.LIKE: 1}, db.rating_summary(conn, "u1")
    assert db.rating_summary(conn, "nobody") == {}
    recent = db.recent_ratings(conn, "u1", 10)
    assert len(recent) == 3  # upsert didn't add a row
    assert (recent[0]["artist"], recent[0]["title"]) == ("A", "a2")  # newest change first


if __name__ == "__main__":
    for fn in (
        test_rating_summary_and_recent,
        test_top_scores_over_present_members,
        test_never_picks_an_audiobook,
        test_timeout_excludes_recently_played,
        test_artist_guard_avoids_back_to_back,
        test_always_returns_something_when_library_nonempty,
        test_new_block_shuffles_and_grows_with_backlog,
        test_request_queue_is_fifo_and_guild_scoped,
        test_search_excludes_audiobooks,
        test_recent_raters_is_presence_window,
        test_presence_is_scoped_per_guild,
        test_guilds_table_crud,
    ):
        fn()
        print(f"ok {fn.__name__}")
    print("all engine checks passed")
