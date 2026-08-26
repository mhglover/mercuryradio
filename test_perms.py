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
