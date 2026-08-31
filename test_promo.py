"""Station-ID promo: per-guild library track, played once when the VC wakes
(Anarkey's ask: "I want to hear the emilie autumn promo when I start it up")."""

import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("MUSIC_DIR", "/tmp")

import bot  # noqa: E402
import db  # noqa: E402


class _Human:
    bot = False


class _Chan:
    members = [_Human()]


class _VC:
    channel = _Chan()

    def is_connected(self):
        return True

    def is_playing(self):
        return False


def _radio(promo_track_id):
    r = bot.GuildRadio(guild_id=1, voice_channel_id=2)
    r.promo_track_id = promo_track_id
    return r


def test_wake_arms_the_promo_only_when_configured():
    armed = _radio(promo_track_id=7)
    bot._sync_playback(armed, _VC())
    assert armed.active and armed.promo_pending

    plain = _radio(promo_track_id=None)
    bot._sync_playback(plain, _VC())
    assert plain.active and not plain.promo_pending


def test_promo_row_vanished_track_is_none():
    c = db.connect(":memory:")
    db.upsert_guild(c, 1, 2, 3)
    t = db.upsert_track(c, "Emilie Autumn", "Promo", "", "/m/p.mp3")
    db.set_guild_promo(c, 1, t)
    assert db.promo_row(c, 1)["title"] == "Promo"
    c.execute("DELETE FROM tracks WHERE id = ?", (t,))
    c.commit()
    assert db.promo_row(c, 1) is None  # join drops it -> _advance skips into music
