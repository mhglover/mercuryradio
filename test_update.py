"""/update: bot-owner-only, degrades to a clear message when watchtower isn't configured."""

import asyncio
import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("MUSIC_DIR", "/tmp")

import bot  # noqa: E402


class _Resp:
    def __init__(self):
        self.deferred = False

    async def defer(self, **kw):
        self.deferred = True


class _Followup:
    def __init__(self):
        self.sent = None

    async def send(self, content, **kw):
        self.sent = content


class _User:
    id = 42


class _Ix:
    user = _User()
    guild_id = 1

    def __init__(self):
        self.response = _Resp()
        self.followup = _Followup()


class _App:
    team = None

    class owner:
        id = 42


class _Client:
    async def application_info(self):
        return _App()


def test_non_owner_is_refused(monkeypatch):
    monkeypatch.setattr(bot, "client", _Client())
    ix = _Ix()
    ix.user = type("U", (), {"id": 999})()  # not the owner
    asyncio.run(bot.update.callback(ix))
    assert ix.response.deferred and "owner" in ix.followup.sent


def test_owner_without_config_gets_setup_pointer(monkeypatch):
    monkeypatch.setattr(bot, "client", _Client())
    monkeypatch.delenv("WATCHTOWER_URL", raising=False)
    monkeypatch.delenv("WATCHTOWER_TOKEN", raising=False)
    ix = _Ix()
    asyncio.run(bot.update.callback(ix))
    assert ix.response.deferred and "isn't configured" in ix.followup.sent
