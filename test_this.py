"""/this anchors chat to the song playing now (9/2)."""

import asyncio
import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("MUSIC_DIR", "/tmp")

import bot  # noqa: E402


class _Resp:
    def __init__(self):
        self.sent = None
        self.kwargs = None

    async def send_message(self, content, **kw):
        self.sent, self.kwargs = content, kw


class _Ix:
    guild_id = 1

    def __init__(self):
        self.response = _Resp()


def test_this_anchors_publicly_with_and_without_comment():
    radio = bot.GuildRadio(guild_id=1, voice_channel_id=2)
    radio.current_row = {"id": 7, "artist": "Prof", "title": "Gasoline", "album": ""}
    bot.radios.clear()
    bot.radios[1] = radio
    ix = _Ix()
    asyncio.run(bot.this.callback(ix, "an all-timer"))
    assert ix.response.sent == "🎶 **Prof – Gasoline** — an all-timer"
    assert "ephemeral" not in ix.response.kwargs  # public: that's the anchor

    radio.current_row = None
    ix2 = _Ix()
    asyncio.run(bot.this.callback(ix2))
    assert ix2.response.kwargs.get("ephemeral") is True  # nothing playing: quiet refusal
