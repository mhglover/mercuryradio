"""The card-burial counter: chat in the card channel counts, everything else doesn't.
When the count reaches CARD_REPOST_AFTER, the next track change reposts the card at the
bottom instead of editing it in place (the scroll-off complaint, 2026-08-31 8:20 AM)."""

import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("MUSIC_DIR", "/tmp")

import bot  # noqa: E402


class _O:  # tiny attribute bag
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _radio_with_card(guild_id=1, channel_id=10):
    radio = bot.GuildRadio(guild_id=guild_id, voice_channel_id=2)
    radio.np_message = _O(channel=_O(id=channel_id))
    bot.radios.clear()
    bot.radios[guild_id] = radio
    return radio


def _msg(author_id=99, guild_id=1, channel_id=10):
    return _O(author=_O(id=author_id), guild=_O(id=guild_id), channel=_O(id=channel_id))


def test_chat_in_the_card_channel_counts(monkeypatch):
    monkeypatch.setattr(bot, "client", _O(user=_O(id=7)))
    radio = _radio_with_card()
    for _ in range(3):
        bot._bump_card_burial(_msg())
    assert radio.msgs_since_card == 3


def test_own_other_channel_and_dm_messages_do_not_count(monkeypatch):
    monkeypatch.setattr(bot, "client", _O(user=_O(id=7)))
    radio = _radio_with_card()
    bot._bump_card_burial(_msg(author_id=7))            # the bot's own message
    bot._bump_card_burial(_msg(channel_id=11))          # a different channel
    bot._bump_card_burial(_O(author=_O(id=99), guild=None, channel=_O(id=10)))  # a DM
    assert radio.msgs_since_card == 0


def test_no_card_means_nothing_to_bury(monkeypatch):
    monkeypatch.setattr(bot, "client", _O(user=_O(id=7)))
    radio = _radio_with_card()
    radio.np_message = None
    bot._bump_card_burial(_msg())
    assert radio.msgs_since_card == 0
