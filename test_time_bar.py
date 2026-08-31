"""The card's static time bar: drawn from track_started + duration at render time,
absent gracefully when either is missing (Anarkey's ask, was a ratebox feature)."""

import os
import time

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("MUSIC_DIR", "/tmp")

import bot  # noqa: E402


def _radio(started_ago):
    r = bot.GuildRadio(guild_id=1, voice_channel_id=2)
    r.track_started = time.monotonic() - started_ago if started_ago is not None else None
    return r


def test_halfway_bar():
    bar = bot._time_bar(_radio(90), {"duration": 180.0})
    assert bar == "▰▰▰▰▰▱▱▱▱▱ 1:30 / 3:00"


def test_start_and_clamped_past_end():
    assert bot._time_bar(_radio(0), {"duration": 180.0}).startswith("▱" * 0 + "▱")  # 0 filled
    late = bot._time_bar(_radio(500), {"duration": 180.0})
    assert late == "▰▰▰▰▰▰▰▰▰▰ 3:00 / 3:00"  # clamped, never past the end


def test_absent_without_duration_or_start():
    assert bot._time_bar(_radio(10), {"duration": None}) is None
    assert bot._time_bar(_radio(10), {}) is None
    assert bot._time_bar(_radio(None), {"duration": 180.0}) is None


def test_embed_carries_the_bar_under_the_album():
    radio = _radio(90)
    row = {"id": 1, "artist": "Prof", "title": "Gasoline", "album": "King", "duration": 180.0}
    monkey_sidebar = bot._sidebar
    bot._sidebar = lambda r, vc, tid: "x"
    try:
        embed = bot._build_embed(radio, row, None, has_cover=False)
    finally:
        bot._sidebar = monkey_sidebar
    assert embed.description.startswith("King\n▰")
