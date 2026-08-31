"""/rate with no track names rates the song playing now (his 8/31 ask, born from the
card-revert bug: the card was gone and there was no way to rate the current song)."""

import asyncio
import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("MUSIC_DIR", "/tmp")

from discord import app_commands  # noqa: E402

import bot  # noqa: E402
import db  # noqa: E402


class _FakeResponse:
    def __init__(self):
        self.sent = None

    async def send_message(self, content, **kw):
        self.sent = content


class _FakeUser:
    id = 42
    display_name = "tester"


class _FakeInteraction:
    guild_id = 1
    user = _FakeUser()

    def __init__(self):
        self.response = _FakeResponse()


def _setup():
    bot.conn = db.connect(":memory:")
    radio = bot.GuildRadio(guild_id=1, voice_channel_id=2)
    bot.radios[1] = radio
    return radio


def test_rate_defaults_to_the_current_track():
    radio = _setup()
    tid = db.upsert_track(bot.conn, "Prof", "Gasoline", "", "/m/g.mp3")
    radio.current_row = {"id": tid, "artist": "Prof", "title": "Gasoline", "album": ""}
    ix = _FakeInteraction()
    love = app_commands.Choice(name="Love", value=db.LOVE)
    asyncio.run(bot.rate.callback(ix, love))  # no track argument
    assert db.get_rating(bot.conn, "42", tid) == db.LOVE
    assert "Gasoline" in ix.response.sent


def test_rate_with_nothing_playing_says_so():
    radio = _setup()
    radio.current_row = None
    ix = _FakeInteraction()
    love = app_commands.Choice(name="Love", value=db.LOVE)
    asyncio.run(bot.rate.callback(ix, love))
    assert "Nothing is playing" in ix.response.sent
