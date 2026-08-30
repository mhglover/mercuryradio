"""Selection engine: pick the next track scored over the members present in the
voice channel right now — the shasradio 'room'. Ported from mercury's block
pickers (blocktypes.py) and its queue loop (queue_manager.py), reduced to SQLite
plus a local library. Stateless: the caller draws a shuffled block, pops a type
per track, and asks for it; recent-play state (timeout + artist guard) is read
from play_history. Explicit /requests preempt the block (handled by the caller).
"""

import random
from datetime import datetime, timedelta, timezone

# shasradio's default block (radiod.pl / config): one request slot, two top-band,
# allpos, fresh — composed then shuffled so the shape isn't audible. The request
# slot falls back to rating-music when the queue is empty, so it costs nothing on
# a quiet night. 'wildcard' (pure discovery) stays in the fallback chain, not the
# base block. (Faithful to the original shasradio radiod.pl block composition.)
BLOCK_TYPES = ["request", "top", "top", "allpos", "fresh"]

# shasradio grew the block by a slot when the request backlog built up, so demand
# drained without starving the ratings music (radiod.pl:183).
REQUEST_GROW_AT = 3


def new_block(pending_requests: int = 0) -> list:
    """A fresh block: BLOCK_TYPES shuffled, plus a second request slot when the
    queue is backed up (>= REQUEST_GROW_AT). The caller pops one type per track
    and calls new_block() again when it empties."""
    block = BLOCK_TYPES[:]
    if pending_requests >= REQUEST_GROW_AT:
        block.append("request")
    random.shuffle(block)
    return block

_COLS = "t.id, t.path, t.artist, t.title"
_NOT_AUDIOBOOK = "t.path NOT LIKE '%/Audiobooks/%'"
# Skip anything played inside the timeout window and the artists of the last few
# plays (no back-to-back artist). Both read straight from play_history.
_NOT_RECENT = "t.id NOT IN (SELECT track_id FROM play_history WHERE played_at > ?)"
_NOT_RECENT_ARTIST = (
    "t.artist NOT IN (SELECT t2.artist FROM play_history ph "
    "JOIN tracks t2 ON t2.id = ph.track_id ORDER BY ph.played_at DESC LIMIT ?)"
)


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def _top(conn, members, cutoff, artist_n, top_k):
    """Highest summed rating over the present members (>0); random among top K."""
    if not members:
        return None
    qs = _placeholders(len(members))
    sql = (
        f"SELECT {_COLS}, SUM(r.value) AS score FROM tracks t "
        f"JOIN ratings r ON r.track_id = t.id "
        f"WHERE r.user_id IN ({qs}) AND {_NOT_AUDIOBOOK} AND {_NOT_RECENT} AND {_NOT_RECENT_ARTIST} "
        f"GROUP BY t.id HAVING score > 0 ORDER BY score DESC LIMIT ?"
    )
    rows = conn.execute(sql, (*members, cutoff, artist_n, top_k)).fetchall()
    return random.choice(rows) if rows else None


def _allpos(conn, members, cutoff, artist_n, top_k):
    """Any track the present room likes on balance (summed rating > 0), random."""
    if not members:
        return None
    qs = _placeholders(len(members))
    sql = (
        f"SELECT {_COLS}, SUM(r.value) AS score FROM tracks t "
        f"JOIN ratings r ON r.track_id = t.id "
        f"WHERE r.user_id IN ({qs}) AND {_NOT_AUDIOBOOK} AND {_NOT_RECENT} AND {_NOT_RECENT_ARTIST} "
        f"GROUP BY t.id HAVING score > 0 ORDER BY RANDOM() LIMIT 1"
    )
    return conn.execute(sql, (*members, cutoff, artist_n)).fetchone()


def _fresh(conn, members, cutoff, artist_n, top_k):
    """A track no present member has rated yet — new to this room."""
    if not members:
        return None
    qs = _placeholders(len(members))
    sql = (
        f"SELECT {_COLS} FROM tracks t "
        f"WHERE {_NOT_AUDIOBOOK} AND {_NOT_RECENT} AND {_NOT_RECENT_ARTIST} "
        f"AND t.id NOT IN (SELECT track_id FROM ratings WHERE user_id IN ({qs})) "
        f"ORDER BY RANDOM() LIMIT 1"
    )
    return conn.execute(sql, (cutoff, artist_n, *members)).fetchone()


def _wildcard(conn, members, cutoff, artist_n, top_k):
    """Discovery: a track nobody has rated at all."""
    sql = (
        f"SELECT {_COLS} FROM tracks t "
        f"WHERE {_NOT_AUDIOBOOK} AND {_NOT_RECENT} AND {_NOT_RECENT_ARTIST} "
        f"AND t.id NOT IN (SELECT track_id FROM ratings) "
        f"ORDER BY RANDOM() LIMIT 1"
    )
    return conn.execute(sql, (cutoff, artist_n)).fetchone()


def _any(conn, artist_n):
    """Last resort so the radio never goes silent: a music track avoiding the
    just-played artist, ignoring the timeout; then truly any music track."""
    row = conn.execute(
        f"SELECT {_COLS} FROM tracks t WHERE {_NOT_AUDIOBOOK} AND {_NOT_RECENT_ARTIST} "
        f"ORDER BY RANDOM() LIMIT 1",
        (artist_n,),
    ).fetchone()
    if row:
        return row
    return conn.execute(
        f"SELECT {_COLS} FROM tracks t WHERE {_NOT_AUDIOBOOK} ORDER BY RANDOM() LIMIT 1"
    ).fetchone()


# 'request' is a block slot too, but it needs guild context and mutates a queue,
# so the caller (bot) resolves it against db.next_request; the engine only scores
# music. A 'request' slot with an empty queue falls back to a music pick.
_PICKERS = {
    "top": _top,
    "allpos": _allpos,
    "fresh": _fresh,
    "wildcard": _wildcard,
}


def pick(conn, members, want, *, timeout_days=10, artist_guard=2, top_k=25):
    """Pick a track of type `want`, scored over `members` (present user ids, as
    strings). Falls through the other pickers, then to any track, so it always
    returns something while the library is non-empty. Returns (row, picker_name),
    or (None, None) only if there are no playable tracks at all."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=timeout_days)).isoformat()
    tried = []
    for name in (want, "top", "allpos", "fresh", "wildcard"):
        if name in tried:
            continue
        tried.append(name)
        row = _PICKERS[name](conn, members, cutoff, artist_guard, top_k)
        if row:
            return row, name
    row = _any(conn, artist_guard)
    return (row, "any") if row else (None, None)
