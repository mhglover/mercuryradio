"""Regression: _radio must load cleanly from db.get_guild's actual row — the 8/31
deploy crash was _radio reading a column get_guild's SELECT did not return
(IndexError killed on_ready before any guild was served)."""

import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("MUSIC_DIR", "/tmp")

import bot  # noqa: E402
import db  # noqa: E402


def test_radio_loads_from_a_real_get_guild_row():
    bot.conn = db.connect(":memory:")
    db.upsert_guild(bot.conn, 5, 2, 3)
    t = db.upsert_track(bot.conn, "Emilie Autumn", "Promo", "", "/m/p.mp3")
    db.set_guild_promo(bot.conn, 5, t)
    bot.radios.clear()
    radio = bot._radio(5)  # the exact path that crashed in prod
    assert radio is not None and radio.promo_track_id == t

    db.upsert_guild(bot.conn, 6, 2, 3)  # and one with NO promo configured
    radio6 = bot._radio(6)
    assert radio6 is not None and radio6.promo_track_id is None
