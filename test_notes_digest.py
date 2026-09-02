"""The boot announce posts headline-per-bullet, not the hard-wrapped wall (9/2)."""

import os

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("MUSIC_DIR", "/tmp")

import bot  # noqa: E402


def test_digest_is_headlines_plus_link():
    notes = ("- **Skips are instant now, not just track endings.** The next song pre-buffers\n"
             "  ten seconds after the current one starts (it used to build only near the\n"
             "  end), so an early skip lands ready.\n"
             "- **The first song after the station ID starts sooner.** Detail\n"
             "  wrapped across lines.\n")
    d = bot._notes_digest(notes)
    assert d.splitlines()[0] == "• Skips are instant now, not just track endings."
    assert d.splitlines()[1] == "• The first song after the station ID starts sooner."
    assert bot.CHANGELOG_URL in d
    assert "pre-buffers" not in d  # detail stays in the changelog


def test_digest_survives_a_boldless_bullet_and_junk():
    d = bot._notes_digest("- plain bullet with detail. And more.\n")
    assert d.startswith("• plain bullet with detail")
    assert bot._notes_digest("no bullets at all") == "no bullets at all"
