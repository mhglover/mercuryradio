"""Early prefetch (skip warmth) must not cost requests-jump-the-queue (9/2 ask)."""

import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("MUSIC_DIR", "/tmp")

import bot  # noqa: E402
import db  # noqa: E402


class _Loop:
    def __init__(self):
        self.delays = []

    def call_later(self, delay, fn):
        self.delays.append(delay)
        return type("H", (), {"cancel": lambda self: None})()


def test_prefetch_fires_early_even_without_duration(monkeypatch):
    fake = _Loop()
    monkeypatch.setattr(bot, "_loop", fake)
    radio = bot.GuildRadio(guild_id=1, voice_channel_id=2)
    bot._schedule_prefetch(radio, None, {"duration": None}, 0)   # unknown length
    bot._schedule_prefetch(radio, None, {"duration": 240.0}, 0)  # known length
    assert fake.delays == [bot.PREFETCH_EARLY_S, bot.PREFETCH_EARLY_S]


def test_pending_request_discards_a_non_request_prefetch():
    bot.conn = db.connect(":memory:")
    t = db.upsert_track(bot.conn, "A", "One", "", "/m/a.mp3")
    radio = bot.GuildRadio(guild_id=1, voice_channel_id=2)
    radio.next_source = object()
    radio.next_picker = "top"
    assert not bot._prefetch_stale_for_requests(radio)  # no request pending -> keep it
    db.add_request(bot.conn, t, "1", "u1")
    assert bot._prefetch_stale_for_requests(radio)      # request pending -> stale
    radio.next_picker = "request"
    assert not bot._prefetch_stale_for_requests(radio)  # prefetched request stays
