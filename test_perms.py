"""Per-guild add-permission policy (the DB half of _may). No discord needed."""

import db


def _conn():
    return db.connect(":memory:")


def test_defaults_open():
    c = _conn()
    db.upsert_guild(c, 1, 10, 11)
    assert db.get_add_role(c, 1) is None  # no role set -> open to all


def test_set_and_clear():
    c = _conn()
    db.upsert_guild(c, 1, 10, 11)
    db.set_add_role(c, 1, 999)
    assert db.get_add_role(c, 1) == "999"
    db.set_add_role(c, 1, None)  # clearing re-opens
    assert db.get_add_role(c, 1) is None


def test_unknown_guild_is_open():
    assert db.get_add_role(_conn(), 42) is None


def test_adding_off_by_default_for_new_guild():
    c = _conn()
    db.upsert_guild(c, 1, 10, 11)
    assert db.add_enabled(c, 1) is False  # a fresh server hides /add + /youtube


def test_toggle_adding():
    c = _conn()
    db.upsert_guild(c, 1, 10, 11)
    db.set_add_enabled(c, 1, True)
    assert db.add_enabled(c, 1) is True
    db.set_add_enabled(c, 1, False)
    assert db.add_enabled(c, 1) is False


def test_unknown_guild_adding_off():
    assert db.add_enabled(_conn(), 42) is False
