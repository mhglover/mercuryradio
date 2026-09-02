"""The 2026-09-02 starved-host hardening: time-throttled topic edits and the churn breaker."""

import os
import time

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("MUSIC_DIR", "/tmp")

import bot  # noqa: E402


def _radio():
    return bot.GuildRadio(guild_id=1, voice_channel_id=2)


def test_topic_needs_both_tracks_and_wall_time():
    r = _radio()
    now = 1000.0
    r.since_topic = bot.TOPIC_EVERY  # enough tracks, never edited -> due
    assert bot._topic_due(r, now)
    r.last_topic_edit = now - 10  # edited 10s ago (the churn shape) -> NOT due
    assert not bot._topic_due(r, now)
    r.last_topic_edit = now - bot.TOPIC_MIN_S  # long enough ago -> due again
    assert bot._topic_due(r, now)
    r.since_topic = bot.TOPIC_EVERY - 1  # time ok but not enough tracks -> not due
    assert not bot._topic_due(r, now)


def test_churn_breaker_trips_on_consecutive_short_ends():
    r = _radio()
    now = time.monotonic()
    for i in range(bot.CHURN_BREAK_N - 1):
        r.track_started = now - 30  # a ~30s end, the measured churn cadence
        assert not bot._note_track_end(r, now), f"tripped early at {i + 1}"
    r.track_started = now - 30
    assert bot._note_track_end(r, now)  # the Nth short end trips it
    assert r.short_tracks == 0  # and the count resets with the trip


def test_a_legitimately_short_track_is_not_churn():
    r = _radio()
    import time as _t
    now = _t.monotonic()
    for _ in range(bot.CHURN_BREAK_N + 2):  # a run of interludes must never trip the breaker
        r.track_started = now - 30
        r.current_row = {"id": 1, "duration": 28.0}  # ended on time for its length
        assert not bot._note_track_end(r, now)
    r.current_row = {"id": 2, "duration": 240.0}  # a real song dying at 30s IS churn
    r.track_started = now - 30
    bot._note_track_end(r, now)
    assert r.short_tracks == 1


def test_normal_track_resets_and_promo_does_not_count():
    r = _radio()
    now = time.monotonic()
    for _ in range(bot.CHURN_BREAK_N - 1):
        r.track_started = now - 30
        bot._note_track_end(r, now)
    r.track_started = now - 240  # a full song played -> reset
    assert not bot._note_track_end(r, now)
    assert r.short_tracks == 0
    r.track_started = None  # promo/wake -> no effect either way
    r.short_tracks = bot.CHURN_BREAK_N - 1
    assert not bot._note_track_end(r, now)
    assert r.short_tracks == bot.CHURN_BREAK_N - 1
