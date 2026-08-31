"""Boot-time release notes: newest CHANGELOG section, posted to card channels once per release."""

import asyncio
import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("MUSIC_DIR", "/tmp")

import bot  # noqa: E402
import db  # noqa: E402


def test_release_notes_parses_the_newest_section():
    version, body = bot._release_notes()
    assert version and body
    assert "\n## " not in body  # one section only, not the whole file


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, **kw):
        self.sent.append(kw)


class _FakeClient:
    def __init__(self, channel):
        self._c = channel

    def get_channel(self, _id):
        return self._c


def test_announce_posts_once_per_release(monkeypatch):
    bot.conn = db.connect(":memory:")
    db.upsert_guild(bot.conn, 1, 2, 3)  # guild with a card channel
    chan = _FakeChannel()
    monkeypatch.setattr(bot, "client", _FakeClient(chan))
    monkeypatch.setattr(bot, "_release_notes", lambda: ("v-test", "- notes"))

    asyncio.run(bot._announce_release())
    asyncio.run(bot._announce_release())  # same release again — reboot, not an update

    assert len(chan.sent) == 1  # announced exactly once
    assert chan.sent[0]["silent"] is True
    assert db.get_option(bot.conn, "announced_release") == "v-test"

    monkeypatch.setattr(bot, "_release_notes", lambda: ("v-next", "- more"))
    asyncio.run(bot._announce_release())
    assert len(chan.sent) == 2  # a NEW release announces again
