"""The now-playing card rebuilds from current_row, never from the message's own embed
snapshot — the 8/31 bug: rating a track made the card revert to a stale earlier track,
because _refresh_sidebar mutated radio.np_message.embeds[0], a snapshot from post time."""

import asyncio
import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("MUSIC_DIR", "/tmp")
os.environ.setdefault("DB_PATH", ":memory:")

import discord  # noqa: E402

import bot  # noqa: E402


class _FakeMessage:
    """Just enough Message: a stale embed from post time, and an edit() that returns
    the updated message the way discord.py's does."""

    def __init__(self, stale_embed):
        self.guild = None  # no guild -> _refresh_sidebar falls back to .channel
        self.channel = object()
        self.embeds = [stale_embed]
        self.edited_with = None

    async def edit(self, **kwargs):
        self.edited_with = kwargs
        updated = _FakeMessage(kwargs["embed"])
        updated.edited_with = kwargs
        return updated


def test_refresh_sidebar_builds_from_current_row_not_message_snapshot(monkeypatch):
    monkeypatch.setattr(bot, "_sidebar", lambda radio, vc, tid: "💙 tester")
    radio = bot.GuildRadio(guild_id=1, voice_channel_id=2)
    radio.current_row = {"id": 7, "artist": "Prof", "title": "Gasoline", "album": ""}
    stale = discord.Embed(title="Detox – Mashup")  # what the card said at post time
    msg = _FakeMessage(stale)
    radio.np_message = msg

    asyncio.run(bot._refresh_sidebar(radio))

    edited = msg.edited_with["embed"]
    assert edited.title == "Prof – Gasoline"  # pre-fix this read "Detox – Mashup"
    assert edited.fields[0].value == "💙 tester"
    # and the cached reference now points at the updated message, not the snapshot
    assert radio.np_message is not msg
    assert radio.np_message.embeds[0].title == "Prof – Gasoline"
