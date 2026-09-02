"""Double-Love flourish: once per person per track, escalating with distinct lovers (9/2)."""

import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("MUSIC_DIR", "/tmp")

import bot  # noqa: E402
import db  # noqa: E402


def _radio():
    return bot.GuildRadio(guild_id=1, voice_channel_id=2)


def test_only_a_double_love_flourishes():
    r = _radio()
    assert bot._love_flourish_emoji(r, "u1", None, db.LOVE) is None       # first Love: quiet
    assert bot._love_flourish_emoji(r, "u1", db.LIKE, db.LOVE) is None    # upgrade: quiet
    assert bot._love_flourish_emoji(r, "u1", db.LOVE, db.LIKE) is None    # downgrade: quiet
    assert bot._love_flourish_emoji(r, "u1", db.LOVE, db.LOVE) == bot.LOVE_TIERS[0]


def test_once_per_person_and_tiers_escalate():
    r = _radio()
    assert bot._love_flourish_emoji(r, "u1", db.LOVE, db.LOVE) == bot.LOVE_TIERS[0]
    assert bot._love_flourish_emoji(r, "u1", db.LOVE, db.LOVE) is None    # same person again: once
    assert bot._love_flourish_emoji(r, "u2", db.LOVE, db.LOVE) == bot.LOVE_TIERS[1]
    assert bot._love_flourish_emoji(r, "u3", db.LOVE, db.LOVE) == bot.LOVE_TIERS[2]
    assert bot._love_flourish_emoji(r, "u4", db.LOVE, db.LOVE) == bot.LOVE_TIERS[2]  # caps at top
    r.love_flourished.clear()  # track change
    assert bot._love_flourish_emoji(r, "u1", db.LOVE, db.LOVE) == bot.LOVE_TIERS[0]
