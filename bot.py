"""mercuryradio — multi-tenant radio bot.

One process, one bot token, many Discord servers. Each server's config (voice +
card channel) lives in the DB `guilds` table, not env, so a server is added with
`/setup` — no redeploy. Per-server playback state lives in a GuildRadio; ratings
and the track library are shared, so a listener's taste follows them across
servers. Ratings are five buttons + a live colored sidebar on the now-playing
card; the rating-scored engine composes each block over whoever's present.

Config: DISCORD_TOKEN, DB_PATH, MUSIC_DIR from env (see .env.sample). The legacy
single-guild env (GUILD_ID/VOICE_CHANNEL_ID/NOWPLAYING_CHANNEL_ID) is used only
to seed the guilds table on first boot, for backward compatibility.
"""

import asyncio
import collections
import io
import os
import re
import signal
import threading
import time

import aiohttp  # ships with discord.py
import discord
from discord import app_commands
from dotenv import load_dotenv

import db
import engine
import library

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
MUSIC_DIR = os.environ["MUSIC_DIR"]
# Writable ingest dir for /add uploads (the source library stays read-only). Defaults
# into the already-writable /data volume, so shipping /add needs no extra mount; set
# ADDED_DIR to a dedicated RW mount if you'd rather keep uploads out of the DB volume.
ADDED_DIR = os.environ.get("ADDED_DIR") or os.path.join(os.path.dirname(os.environ.get("DB_PATH", "/data/db")), "added")
ADD_MAX_BYTES = 100 * 1024 * 1024  # sanity guard; Discord's own upload cap is the real limit
MAX_ADD_MINUTES = int(os.environ.get("MAX_ADD_MINUTES") or 20)  # cap on /youtube pulls
# Legacy single-guild env — only seeds the guilds table on first boot.
_SEED_GUILD_ID = int(os.environ.get("GUILD_ID") or 0) or None
_SEED_VOICE_ID = int(os.environ.get("VOICE_CHANNEL_ID") or 0) or None
_SEED_NP_ID = int(os.environ.get("NOWPLAYING_CHANNEL_ID") or 0) or None

# -err_detect ignore_err / +discardcorrupt: drop bad MP3 frames cleanly instead of
# glitching on them (some rips have corrupt frames). Opus is encoded by FFmpeg (in C)
# rather than discord.py's Python encoder — far lighter on the send thread, which
# smooths the frame pacing (Python-side encode lag is what makes the beat "speed up").
FFMPEG_OPTS = {"before_options": "-err_detect ignore_err -fflags +discardcorrupt", "options": "-vn"}
STREAM_BITRATE = 128  # kbps Opus; avoids FFmpegOpusAudio's blocking probe
# Read-ahead / prefetch to kill the speed-up artifact. The player bursts to catch up
# whenever source.read() blocks; it blocks worst at each track's START (ffmpeg spawn,
# 0.3–6.8 s measured) and occasionally mid-track (disk). So: a background thread reads
# ffmpeg ahead into a buffer (read() never blocks the player), we PRE-ROLL before play,
# and we PREFETCH the next track a few seconds before the current ends so the boundary
# is seamless.
BUFFER_AHEAD_FRAMES = 500     # max buffered (20ms each) -> ~10s cushion for mid-track blips
PREROLL_FRAMES = 100         # buffer this much (~2s) before handing to the player
PREFETCH_LEAD_S = 12         # start building the next track this many seconds before the end
MAX_SILENCE_FRAMES = 750     # give up (end the track) after ~15s of continuous underrun
OPUS_SILENCE = b"\xf8\xff\xfe"  # a 20ms Opus silence frame (what discord.py itself sends)
# Diagnostic (set PACE_DEBUG=1): log when playback pacing slips, and split a slow
# read (ffmpeg/disk stall) from a stalled send thread (GIL/scheduler). Off by default.
PACE_DEBUG = bool(os.environ.get("PACE_DEBUG"))


class _TimedOpus(discord.AudioSource):
    """Wraps an Opus source to log pacing slips. discord.py reads one 20ms frame per
    ~20ms; a big GAP before a read means the send thread couldn't run (GIL/scheduler),
    while a slow read() itself means the source (ffmpeg/disk) stalled. Logging both
    tells the two apart. Overhead is a couple of perf_counter calls per frame."""

    def __init__(self, source, label):
        self._src = source
        self._label = label[:60]
        self._last = None
        self._n = 0
        self._slips = 0

    def is_opus(self):
        return True

    def read(self):
        now = time.perf_counter()
        if self._last is not None:
            gap = (now - self._last) * 1000  # ms since the previous read started (~20 normal)
            if gap > 35:
                self._slips += 1
                print(f"[pace] {self._label} f{self._n}: {gap:.0f}ms gap before read "
                      f"— SEND-THREAD stall (GIL/sched); slips={self._slips}")
        t0 = time.perf_counter()
        data = self._src.read()
        dur = (time.perf_counter() - t0) * 1000
        if dur > 15:
            self._slips += 1
            print(f"[pace] {self._label} f{self._n}: read() took {dur:.0f}ms "
                  f"— SOURCE stall (ffmpeg/disk); slips={self._slips}")
        self._last = now  # frame cadence = read-start to read-start (~20ms)
        self._n += 1
        return data

    def cleanup(self):
        self._src.cleanup()


class _BufferedOpus(discord.AudioSource):
    """FFmpeg Opus source with a background read-ahead thread, so the player's read()
    never blocks on ffmpeg. That block is what makes discord.py burst to catch up (the
    speed-up). A slow ffmpeg (startup or a disk blip) drains the buffer instead of
    stalling the player; on a true underrun we return an Opus silence frame (a tiny gap,
    never a speed-up). prebuffer() lets the caller pre-roll before playback starts."""

    def __init__(self, path, opts, bitrate, source=None):
        # `source` is an injection seam for tests; normally we build the ffmpeg source.
        self._src = source if source is not None else discord.FFmpegOpusAudio(path, bitrate=bitrate, **opts)
        self._buf = collections.deque()
        self._cv = threading.Condition()
        self._ended = False     # ffmpeg reached EOF
        self._closed = False
        self._silence_run = 0
        self._thread = threading.Thread(target=self._fill, daemon=True)
        self._thread.start()

    def _fill(self):
        while True:
            with self._cv:
                while len(self._buf) >= BUFFER_AHEAD_FRAMES and not self._closed:
                    self._cv.wait(timeout=1.0)
                if self._closed:
                    return
            data = self._src.read()  # blocks on ffmpeg — fine, this is the bg thread
            with self._cv:
                self._buf.append(data)
                self._cv.notify_all()
                if not data:      # ffmpeg EOF (read() returns b"")
                    self._ended = True
                    return

    def prebuffer(self, frames, timeout=12.0):
        """Block (in a thread executor, never the loop) until `frames` are buffered,
        ffmpeg ends, or timeout — so ffmpeg startup latency lands here, off the player."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while len(self._buf) < frames and not self._ended and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._cv.wait(timeout=remaining)

    def is_opus(self):
        return True

    def read(self):
        with self._cv:
            if self._buf:
                self._silence_run = 0
                data = self._buf.popleft()
                self._cv.notify_all()
                return data
            if self._ended:
                return b""  # real end -> player stops
        # underrun: buffer empty but ffmpeg hasn't ended. Return silence to hold the
        # clock (no burst); bail out if ffmpeg has clearly hung.
        self._silence_run += 1
        if self._silence_run > MAX_SILENCE_FRAMES:
            return b""
        return OPUS_SILENCE

    def cleanup(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        try:
            self._src.cleanup()
        except Exception:
            pass

# A rating within this window counts a user as present (for scoring + the sidebar)
# even without joining voice — for listeners sharing one speaker/connection.
PRESENCE_WINDOW_MIN = 30
# On wake (empty VC -> someone joins), start the first song this many seconds in.
WAKE_SEEK_SECONDS = 30
TOPIC_EVERY = 5  # refresh the channel topic once per this many tracks
CARD_REPOST_AFTER = 5  # chat messages since the card before a track change REPOSTS it at the bottom
TOPIC_MIN_S = 360      # min seconds between topic edits — Discord throttles ~2/10min, and a
                       # track-churn burst (21 tracks/4min, 2026-09-02) blew straight through the
                       # per-track counter into a 429 whose 294s in-library retry wedged the card
CHURN_UNDER_S = 45     # a track ending under this is a "short end" (underrun/kill, not a song)
CHURN_BREAK_N = 5      # this many consecutive short ends trips the breaker
CHURN_COOLDOWN_S = 300 # how long the breaker pauses picking before retrying

# (label, rating value, colored square for the sidebar, button style)
RATINGS = [
    ("Hate", db.HATE, "🟥", discord.ButtonStyle.danger),
    ("Dislike", db.DISLIKE, "🟧", discord.ButtonStyle.secondary),
    ("Shrug", db.SHRUG, "⬜", discord.ButtonStyle.secondary),
    ("Like", db.LIKE, "🟩", discord.ButtonStyle.success),
    ("Love", db.LOVE, "💙", discord.ButtonStyle.primary),
]
_SQUARE = {value: square for _, value, square, _ in RATINGS}
UNRATED = "⬛"

conn = None
_loop = None
_draining = False  # set on shutdown: finish the current song, pick no more, then close
DRAIN_TIMEOUT = 300  # cap the graceful wait (s) so a long/stuck track can't stall the deploy


class GuildRadio:
    """All per-server playback state. One per served guild, keyed by guild id."""

    def __init__(self, guild_id, voice_channel_id, nowplaying_channel_id=None):
        self.guild_id = int(guild_id)
        self.voice_channel_id = int(voice_channel_id) if voice_channel_id else None
        self.nowplaying_channel_id = int(nowplaying_channel_id) if nowplaying_channel_id else None
        self.block = []            # remaining picker types in the current shuffled block
        self.active = False        # True only while a human is in the VC
        self.current_row = None    # the track playing now
        self.current_track = None  # "artist – title" for status
        self.np_message = None     # the live now-playing card (ONE per guild, edited in place)
        self.card_has_cover = False  # whether the card message carries a cover attachment
        self.msgs_since_card = 0   # chat since the card last moved — counted from gateway events (no history perm)
        self.last_topic_edit = None  # time.monotonic() of the last topic edit (time-throttled, not per-track)
        self.short_tracks = 0      # consecutive tracks that ended under CHURN_UNDER_S — the churn breaker's count
        self.track_started = None  # time.monotonic() when the current track began (seek-adjusted), for the time bar
        self.promo_track_id = None # station-ID track for this guild (guilds.promo_track_id), loaded by _radio
        self.promo_pending = False # play the promo before the first track of this wake
        self.card_lock = asyncio.Lock()  # serialize card ops so a track change can't leak a card
        self.next_row = None       # prefetched next track (picked ~PREFETCH_LEAD_S before the end)
        self.next_source = None    # its pre-rolled _BufferedOpus, ready to play seamlessly
        self.next_picker = None    # the picker reason for the prefetched track
        self.prefetch_handle = None  # the loop.call_later timer that runs the prefetch
        self.recent_artists = []   # rolling window for the topic
        self.since_topic = 0       # tracks played since the topic was refreshed


radios: dict[int, GuildRadio] = {}


def _radio(guild_id) -> GuildRadio | None:
    """The GuildRadio for a served guild, loading its config from the DB on first
    use. None if the guild isn't set up."""
    gid = int(guild_id)
    if gid in radios:
        return radios[gid]
    row = db.get_guild(conn, gid)
    if row is None:
        return None
    radios[gid] = GuildRadio(gid, row["voice_channel_id"], row["nowplaying_channel_id"])
    radios[gid].promo_track_id = row["promo_track_id"]
    return radios[gid]


def _ensure_opus() -> None:
    if discord.opus.is_loaded():
        return
    from ctypes.util import find_library

    for path in (find_library("opus"), "/opt/homebrew/lib/libopus.dylib",
                 "/usr/local/lib/libopus.dylib", "libopus.so.0"):
        if not path:
            continue
        try:
            discord.opus.load_opus(path)
            return
        except OSError:
            continue
    raise RuntimeError("libopus not found — install it (brew install opus / apt install libopus0)")


def _listeners(channel) -> list:
    return [m for m in getattr(channel, "members", []) if not m.bot]


def _present(radio: GuildRadio, voice_channel):
    """Present listeners in this guild = VC members + anyone who rated here within
    PRESENCE_WINDOW_MIN. Presence-by-rating is scoped per guild. Returns
    [(user_id_str, display_name)], VC members first, deduped."""
    present = {str(m.id): m.display_name for m in _listeners(voice_channel)}
    for r in db.present_since(conn, radio.guild_id, PRESENCE_WINDOW_MIN):
        present.setdefault(r["user_id"], r["name"])
    return list(present.items())


def _pick_next(radio: GuildRadio, vc: discord.VoiceClient):
    """Compose the next track for this guild, scored over whoever is present RIGHT NOW.
    Pops the block and marks a request played — so call it when the track will actually
    play (live) or at prefetch time (a few seconds early). Returns (row, picker) or None."""
    present = _present(radio, vc.channel)
    member_ids = [uid for uid, _ in present]
    gid = str(radio.guild_id)
    if not radio.block:
        radio.block = engine.new_block(db.pending_request_count(conn, gid))
    want = radio.block.pop()
    if want == "request":
        req = db.next_request(conn, gid)
        if req is not None:
            db.mark_request_played(conn, req["id"], gid)
            return dict(req), "request"
    r, picker = engine.pick(conn, member_ids, "top" if want == "request" else want)
    if r is None:
        return None
    return dict(r), picker


async def _make_source(row: dict, seek: int):
    """Build a pre-rolled buffered source. The blocking part (ffmpeg startup + reading
    the pre-roll) runs in a thread executor so it never freezes the event loop."""
    opts = dict(FFMPEG_OPTS)
    if seek > 0:  # input seek goes first, keep the error-tolerance flags after it
        opts["before_options"] = f"-ss {seek} {opts['before_options']}"

    def _build():
        src = _BufferedOpus(row["path"], opts, STREAM_BITRATE)
        src.prebuffer(PREROLL_FRAMES)  # absorb ffmpeg startup here, off the player's clock
        return src

    src = await _loop.run_in_executor(None, _build)
    if PACE_DEBUG:
        src = _TimedOpus(src, f"{row['artist']} – {row['title']}")
    return src


def _discard_prefetch(radio: GuildRadio) -> None:
    """Drop a prefetched-but-unused source (cleans up its ffmpeg)."""
    if radio.next_source is not None:
        try:
            radio.next_source.cleanup()
        except Exception:
            pass
    radio.next_row = radio.next_source = radio.next_picker = None


def _cancel_prefetch(radio: GuildRadio) -> None:
    """Cancel a pending prefetch timer and discard any prefetched source."""
    if radio.prefetch_handle is not None:
        radio.prefetch_handle.cancel()
        radio.prefetch_handle = None
    _discard_prefetch(radio)


def _schedule_prefetch(radio: GuildRadio, vc: discord.VoiceClient, row: dict, seek: int) -> None:
    """Arrange for the next track to be picked + pre-buffered ~PREFETCH_LEAD_S before
    this one ends, so the boundary is seamless. Skipped when the duration is unknown."""
    if radio.prefetch_handle is not None:
        radio.prefetch_handle.cancel()
        radio.prefetch_handle = None
    dur = row.get("duration")
    if not dur:  # unknown length -> can't time it; next track just pays startup latency
        return
    delay = max(1.0, float(dur) - seek - PREFETCH_LEAD_S)
    radio.prefetch_handle = _loop.call_later(
        delay, lambda: _loop.create_task(_do_prefetch(radio, vc)))


async def _do_prefetch(radio: GuildRadio, vc: discord.VoiceClient) -> None:
    radio.prefetch_handle = None
    if _draining or not radio.active or not vc.is_connected() or not _listeners(vc.channel):
        return
    if radio.next_source is not None:  # already have one
        return
    picked = _pick_next(radio, vc)
    if picked is None:
        return
    row, picker = picked
    try:
        source = await _make_source(row, 0)
    except Exception as e:
        print(f"prefetch build failed: {e}")
        return
    radio.next_row, radio.next_source, radio.next_picker = row, source, picker


def _topic_due(radio: GuildRadio, now: float) -> bool:
    """Topic refresh gate: enough TRACKS and enough WALL TIME. The per-track counter alone
    assumed ~4-minute tracks; a churn burst fired 4 edits in 4 minutes and earned a 429."""
    return radio.since_topic >= TOPIC_EVERY and (
        radio.last_topic_edit is None or now - radio.last_topic_edit >= TOPIC_MIN_S)


async def _set_topic(channel, topic: str) -> None:
    try:
        await channel.edit(topic=topic)
    except (discord.HTTPException, AttributeError) as e:
        print(f"could not set channel topic: {e}")


def _note_track_end(radio: GuildRadio, now: float) -> bool:
    """The churn breaker's count. Returns True when CHURN_BREAK_N consecutive tracks ended
    under CHURN_UNDER_S — on a starved host the buffered source underruns to end-of-track
    every ~30s (measured 2026-09-02: 21 tracks in 4 minutes of silence), and machine-gun
    advancing just multiplies the damage. A normal-length track resets the count; a promo
    or wake (track_started None) doesn't count either way."""
    if radio.track_started is None:
        return False
    if now - radio.track_started < CHURN_UNDER_S:
        radio.short_tracks += 1
    else:
        radio.short_tracks = 0
    if radio.short_tracks >= CHURN_BREAK_N:
        radio.short_tracks = 0
        return True
    return False


async def _advance(radio: GuildRadio, vc: discord.VoiceClient, seek: int = 0) -> None:
    """Play the next track for this guild. Uses the prefetched, pre-buffered track when
    one is ready (seamless); otherwise picks + builds one now. Runs on the loop thread;
    the after-callback hops back via run_coroutine_threadsafe. `seek` wakes mid-track."""
    if _draining:
        return  # shutting down — let the current song finish, start no new one
    if not radio.active or not vc.is_connected():
        return
    if not _listeners(vc.channel):
        return  # streaming gate: at least one human must be in the VC
    if _note_track_end(radio, time.monotonic()):
        # Churn breaker: stop feeding a player that keeps killing tracks early (a starved
        # host, a dead mount) — say so ONCE, loudly, and retry after the cooldown.
        radio.track_started = None
        print(f"[churn] {CHURN_BREAK_N} consecutive tracks ended under {CHURN_UNDER_S}s — "
              f"pausing {CHURN_COOLDOWN_S}s")
        try:
            await _np_channel(radio, vc.channel).send(
                f"⏸️ Tracks keep dying early — the host is struggling. "
                f"Backing off for {CHURN_COOLDOWN_S // 60} minutes, then trying again.")
        except discord.HTTPException:
            pass
        if _loop:
            _loop.call_later(CHURN_COOLDOWN_S,
                             lambda: _loop.create_task(_advance(radio, vc)))
        return
    # A prefetch timer for the track that just ended is now moot — cancel it, but keep a
    # source that already finished prefetching so we can play it seamlessly.
    if radio.prefetch_handle is not None:
        radio.prefetch_handle.cancel()
        radio.prefetch_handle = None
    if radio.promo_pending:
        # Station ID first (Anarkey's ask): play the configured clip through the same
        # buffered-source path, then _after re-enters _advance for the real first track
        # (which therefore starts fresh — the wake seek is spent on the promo cycle).
        # Not ratable, no card, no play history; a vanished track skips silently into music.
        radio.promo_pending = False
        prow = db.promo_row(conn, radio.guild_id)
        if prow is not None:
            try:
                psource = await _make_source(dict(prow), 0)
            except Exception as e:
                print(f"promo failed to build, skipping to music: {e}")
            else:
                _discard_prefetch(radio)
                radio.current_row = None
                radio.current_track = "station ID"
                radio.track_started = None
                vc.play(psource, after=lambda err: _after(radio, vc, err, prow["path"]))
                # Prefetch the first real track WHILE the promo plays — the two ffmpeg
                # builds overlap instead of running serially, which is what made the
                # wake sound like promo … pause … music (his /bug, 2026-09-02 1:15 PM).
                _loop.create_task(_do_prefetch(radio, vc))
                return
    if seek == 0 and radio.next_source is not None:
        row, source, picker = radio.next_row, radio.next_source, radio.next_picker
        radio.next_row = radio.next_source = radio.next_picker = None
    else:
        _discard_prefetch(radio)  # a wake/seek doesn't use the prefetch
        picked = _pick_next(radio, vc)
        if picked is None:
            return
        row, picker = picked
        try:
            source = await _make_source(row, seek)
        except Exception as e:
            print(f"could not start track: {e}")
            return
    radio.current_row = row
    radio.current_track = f"{row['artist']} – {row['title']}"
    radio.track_started = time.monotonic() - (seek or 0)
    db.record_play(conn, row["id"], reason=picker)
    vc.play(source, after=lambda err: _after(radio, vc, err, row["path"]))
    await _post_nowplaying(radio, vc.channel, row)
    _schedule_prefetch(radio, vc, row, seek)


def _after(radio: GuildRadio, vc: discord.VoiceClient, err, path: str) -> None:
    # Runs on discord's audio worker thread — schedule the next pick onto the loop.
    if err:
        print(f"playback error on {path}: {err}")
    if _loop and not _draining:  # while draining, don't queue another track
        asyncio.run_coroutine_threadsafe(_advance(radio, vc), _loop)


def _sync_playback(radio: GuildRadio, vc: discord.VoiceClient) -> None:
    """Start streaming if a human is in the VC and we're not already playing.
    Leaving an empty VC is handled by _reconcile_voice. Idempotent."""
    if not vc or not vc.is_connected():
        return
    if _listeners(vc.channel) and not radio.active:
        radio.active = True
        radio.short_tracks = 0  # a fresh wake starts the churn count clean
        radio.promo_pending = radio.promo_track_id is not None  # station ID leads this wake
        if _loop and not vc.is_playing():
            _loop.create_task(_advance(radio, vc, seek=WAKE_SEEK_SECONDS))


# ── now-playing card ────────────────────────────────────────────────────────

def _sidebar(radio: GuildRadio, voice_channel, track_id: int) -> str:
    """One line per present listener (VC + recent raters): their rating square."""
    lines = []
    for uid, name in _present(radio, voice_channel):
        val = db.get_rating(conn, uid, track_id)
        square = _SQUARE.get(val, UNRATED) if val is not None else UNRATED
        lines.append(f"{square} {name}")
    return "\n".join(lines) or "_nobody here_"


def _np_channel(radio: GuildRadio, voice_channel):
    if radio.nowplaying_channel_id:
        return client.get_channel(radio.nowplaying_channel_id) or voice_channel
    return voice_channel  # voice channels are Messageable (text-in-voice) in discord.py 2.x


def _mmss(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _time_bar(radio: GuildRadio, row: dict) -> str | None:
    """A STATIC progress bar as of render time — the card only redraws on ratings and
    track changes, so this updates for free then. ⛔ Never a ticking edit loop: a
    per-few-seconds edit gets rate-limited (the note's own warning). No duration on the
    row (old scans) or no start stamp -> no bar, gracefully."""
    dur = row.get("duration")
    if not dur or radio.track_started is None:
        return None
    elapsed = min(max(time.monotonic() - radio.track_started, 0.0), dur)
    filled = round(10 * elapsed / dur)
    return "▰" * filled + "▱" * (10 - filled) + f" {_mmss(elapsed)} / {_mmss(dur)}"


def _build_embed(radio: GuildRadio, row: dict, voice_channel, has_cover: bool) -> discord.Embed:
    desc = row.get("album") or ""
    bar = _time_bar(radio, row)
    if bar:
        desc = f"{desc}\n{bar}" if desc else bar
    embed = discord.Embed(title=f"{row['artist']} – {row['title']}", description=desc)
    embed.add_field(name="Ratings", value=_sidebar(radio, voice_channel, row["id"]), inline=False)
    if has_cover:
        embed.set_thumbnail(url="attachment://cover.png")
    return embed


class RatingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for label, value, square, style in RATINGS:
            self.add_item(_RatingButton(label, value, square, style))


class _RatingButton(discord.ui.Button):
    def __init__(self, label, value, square, style):
        super().__init__(label=label, emoji=square, style=style)
        self.value = value

    async def callback(self, interaction: discord.Interaction) -> None:
        radio = _radio(interaction.guild_id) if interaction.guild_id else None
        if radio is None or radio.current_row is None:
            await interaction.response.send_message("Nothing playing.", ephemeral=True)
            return
        await interaction.response.defer()  # ack first — Discord gives 3s; do the DB work after
        db.upsert_user(conn, interaction.user.id, interaction.user.display_name)
        db.set_rating(conn, str(interaction.user.id), radio.current_row["id"], self.value)
        db.touch_presence(conn, interaction.user.id, interaction.guild_id)  # rating == present here
        await _refresh_sidebar(radio)


class RecentView(discord.ui.View):
    """Ephemeral, per-invocation: a dropdown of recently-played tracks plus the five
    rating buttons, so a listener can rate a song they didn't click while it played.
    Not persistent — it belongs to the one /recent response, so timeout is fine."""

    def __init__(self, plays):
        super().__init__(timeout=300)
        self.plays = plays  # ordered rows, kept so the list can be rebuilt after a rating
        self.labels = {r["id"]: f"{r['artist']} – {r['title']}" for r in plays}
        self.selected = plays[0]["id"]  # default to the newest
        options = [
            discord.SelectOption(label=self.labels[r["id"]][:100], value=str(r["id"]),
                                 default=(r["id"] == self.selected))
            for r in plays
        ]
        self.add_item(_RecentSelect(options))
        for label, value, square, style in RATINGS:
            self.add_item(_RecentRatingButton(label, value, square, style))

    def body_for(self, uid: str) -> str:
        """The numbered list with THIS user's rating square (⬛ unrated) per track."""
        lines = []
        for i, r in enumerate(self.plays):
            val = db.get_rating(conn, uid, r["id"])
            square = _SQUARE.get(val, UNRATED) if val is not None else UNRATED
            lines.append(f"{i + 1}. {square} {r['artist']} – {r['title']}")
        return "**Recently played** — the square is your rating; pick one below to set or change it:\n" + "\n".join(lines)


class _RecentSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="Pick a recent track to rate", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view.selected = int(self.values[0])
        await interaction.response.defer()  # selection stored; buttons act on it


class _RecentRatingButton(discord.ui.Button):
    def __init__(self, label, value, square, style):
        super().__init__(label=label, emoji=square, style=style, row=1)
        self.value = value

    async def callback(self, interaction: discord.Interaction) -> None:
        track_id = self.view.selected
        uid = str(interaction.user.id)
        db.upsert_user(conn, interaction.user.id, interaction.user.display_name)
        db.set_rating(conn, uid, track_id, self.value)
        if interaction.guild_id:
            db.touch_presence(conn, interaction.user.id, interaction.guild_id)
        # Refresh the list in place so the just-set square shows immediately.
        await interaction.response.edit_message(content=self.view.body_for(uid), view=self.view)


async def _delete_card(radio: GuildRadio) -> None:
    """Remove this guild's card. Lock-free — the caller must hold card_lock."""
    if radio.np_message is not None:
        msg, radio.np_message = radio.np_message, None
        try:
            await msg.delete()
        except discord.HTTPException:
            pass


async def _refresh_sidebar(radio: GuildRadio) -> None:
    """Rebuild the sidebar on this guild's card. Reads the VOICE channel for
    presence, not the card's text channel. Serialized by card_lock so it can't
    race a track change replacing the card."""
    async with radio.card_lock:
        if radio.np_message is None or radio.current_row is None:
            return
        guild = radio.np_message.guild
        vc = guild.voice_client if guild else None
        voice_channel = vc.channel if vc else radio.np_message.channel
        # Rebuild the embed from current_row, NEVER from radio.np_message.embeds — that object
        # is a snapshot from whenever the reference was captured (edit() returns the updated
        # message; a discarded return leaves the snapshot stale), and rebuilding a stale
        # snapshot is what made the card revert to an old track on every rating (8/31 bug).
        embed = _build_embed(radio, radio.current_row, voice_channel, has_cover=radio.card_has_cover)
        try:
            radio.np_message = await radio.np_message.edit(embed=embed)
        except discord.HTTPException:
            pass


async def _post_nowplaying(radio: GuildRadio, voice_channel, row: dict) -> None:
    """Show/refresh this guild's ONE now-playing card for a new track. EDITS the
    existing card in place instead of delete-and-repost — that per-track delete is
    what leaked 'sticky' cards: a delete that failed transiently still nulled the
    reference (orphaning the on-screen card forever), and concurrent updates could
    overwrite the reference without deleting. Serialized by card_lock; reposts only
    when there's no card, the target channel changed, or the card was deleted out
    from under us. silent=True so a track change fires no notification."""
    async with radio.card_lock:
        channel = _np_channel(radio, voice_channel)
        cover = library.extract_cover(row["path"])
        msg = radio.np_message
        if msg is not None and msg.channel.id != channel.id:
            await _delete_card(radio)  # card channel changed (e.g. /setup) -> retire it
            msg = None
        if msg is not None and radio.msgs_since_card >= CARD_REPOST_AFTER:
            # The card is buried under chat — repost at the bottom instead of editing in
            # place, so it's visible when the song changes (the old ratebox pop-out want).
            # Bounded by the counter, so a quiet channel keeps the edit-in-place behavior
            # that fixed sticky cards; a repost only happens when people were talking.
            await _delete_card(radio)
            msg = None
        radio.card_has_cover = bool(cover)
        if msg is not None:
            try:
                file = discord.File(io.BytesIO(cover), filename="cover.png") if cover else None
                embed = _build_embed(radio, row, voice_channel, has_cover=bool(cover))
                radio.np_message = await msg.edit(embed=embed, view=RatingView(), attachments=[file] if file else [])
            except discord.NotFound:
                radio.np_message = None  # someone deleted the card -> repost below
            except discord.HTTPException as e:
                print(f"now-playing card edit failed (keeping the card, retry next track): {e}")
        if radio.np_message is None:  # first track, channel changed, or card vanished
            file = discord.File(io.BytesIO(cover), filename="cover.png") if cover else None
            embed = _build_embed(radio, row, voice_channel, has_cover=bool(cover))
            send_kwargs = {"embed": embed, "view": RatingView()}
            if file:
                send_kwargs["file"] = file
            try:
                radio.np_message = await channel.send(**send_kwargs, silent=True)
            except discord.HTTPException as e:
                print(f"could not post now-playing card: {e}")
        radio.msgs_since_card = 0  # the card just moved (edited or reposted) — restart the burial count
        try:  # bot status is global (one per bot); with N guilds it shows the latest track
            await client.change_presence(
                activity=discord.Activity(type=discord.ActivityType.listening, name=f"{row['artist']} – {row['title']}")
            )
        except discord.HTTPException:
            pass
        # Reflect recent artists in the channel topic, every TOPIC_EVERY tracks — Discord
        # throttles channel edits to ~2 per 10 min, so a per-song edit gets 429'd.
        radio.recent_artists.append(row["artist"])
        del radio.recent_artists[:-TOPIC_EVERY]
        radio.since_topic += 1
        now = time.monotonic()
        if _topic_due(radio, now):
            radio.since_topic = 0
            radio.last_topic_edit = now
            recent = list(dict.fromkeys(radio.recent_artists))
            # Fire-and-forget, ⛔ never awaited here: this runs under card_lock, and a 429's
            # in-library retry sleep (observed 294s, 2026-09-02) held the lock and wedged
            # every card operation behind it.
            asyncio.get_running_loop().create_task(
                _set_topic(channel, "🎵 Recent: " + ", ".join(recent)))


def _release_notes() -> tuple[str | None, str | None]:
    """The newest CHANGELOG section: (release name, its markdown body). CHANGELOG.md
    ships in the image and IS the release notes — one source, no second file."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CHANGELOG.md")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None, None
    m = re.search(r"^## (.+?)\s*$", text, re.M)
    if not m:
        return None, None
    nxt = text.find("\n## ", m.end())
    body = text[m.end():nxt if nxt != -1 else len(text)].strip()
    return m.group(1).strip(), body[:3900]  # embed description cap is 4096


async def _announce_release() -> None:
    """On boot, if this build's newest CHANGELOG section hasn't been announced yet,
    post it (silently) to every configured card channel, once."""
    version, notes = _release_notes()
    if not version or not notes or db.get_option(conn, "announced_release") == version:
        return
    for row in db.list_guilds(conn):
        chan_id = row["nowplaying_channel_id"]
        channel = client.get_channel(int(chan_id)) if chan_id else None
        if channel is None:
            continue  # no card channel configured -> that server gets no announce
        embed = discord.Embed(title=f"Mercury Radio updated — {version}", description=notes)
        try:
            await channel.send(embed=embed, silent=True)
        except discord.HTTPException as e:
            print(f"release-notes post failed for guild {row['guild_id']}: {e}")
    # Mark announced even if a post failed — a broken channel must not re-announce
    # to every OTHER server on each reboot; the failure is in the log above.
    db.set_option(conn, "announced_release", version)


async def _clear_nowplaying(radio: GuildRadio, clear_status: bool = True) -> None:
    async with radio.card_lock:
        await _delete_card(radio)
        if clear_status and not any(r.active for r in radios.values()):
            try:  # only clear the global status when no guild is still playing
                await client.change_presence(activity=None)
            except discord.HTTPException:
                pass


# ── discord wiring ──────────────────────────────────────────────────────────


async def _drain_playback() -> None:
    """Graceful shutdown: stop picking new tracks (via _draining) and wait for the
    songs already playing to finish, so a redeploy never cuts a song mid-play.
    Bounded by DRAIN_TIMEOUT; returns at once when nothing is playing (idle)."""
    playing = sum(1 for vc in client.voice_clients if vc.is_playing())
    if playing:
        print(f"graceful shutdown — waiting for {playing} song(s) to finish (up to {DRAIN_TIMEOUT}s)")
    for _ in range(DRAIN_TIMEOUT):
        if not any(vc.is_playing() for vc in client.voice_clients):
            if playing:
                print("graceful shutdown — song finished, closing")
            return
        await asyncio.sleep(1)
    print("drain timeout — closing with a track still playing")


async def _clear_cards_on_shutdown() -> None:
    """Delete the now-playing cards on shutdown so a (seconds-long) restart doesn't
    leave orphaned cards with dead buttons behind. No off-air announcement — that
    just spammed the channel on every quick redeploy."""
    for radio in radios.values():
        _cancel_prefetch(radio)
        await _clear_nowplaying(radio, clear_status=False)


class MercuryClient(discord.Client):
    _shutdown_done = False

    async def setup_hook(self) -> None:
        # docker stop/restart sends SIGTERM; asyncio.run doesn't trap it, so the
        # normal close() path never runs. Bridge SIGTERM -> close() ourselves.
        try:
            self.loop.add_signal_handler(
                signal.SIGTERM, lambda: self.loop.create_task(self.close())
            )
        except (NotImplementedError, RuntimeError):
            pass

    async def close(self) -> None:
        global _draining
        if not self._shutdown_done:
            self._shutdown_done = True
            _draining = True          # stop picking the next track
            await _drain_playback()   # let the song that's playing finish
            await _clear_cards_on_shutdown()
        await super().close()


intents = discord.Intents.default()
intents.voice_states = True
client = MercuryClient(intents=intents)
tree = app_commands.CommandTree(client)


def _scan_library() -> int:
    """Scan the read-only source library plus the writable /add ingest dir. Runs
    in a thread executor. Added tracks are upserted live on /add, but re-walking
    ADDED_DIR here re-catalogs them if the DB is ever rebuilt from empty."""
    n = library.scan(MUSIC_DIR)
    if os.path.isdir(ADDED_DIR):
        n = library.scan(ADDED_DIR)
    return n


async def _rescan_bg() -> None:
    """Refresh the library off the loop, after playback has already started."""
    try:
        n = await _loop.run_in_executor(None, _scan_library)
        print(f"background library rescan complete — {n} tracks")
    except Exception as e:
        print(f"background rescan failed: {e}")


async def _reconcile_voice(radio: GuildRadio) -> None:
    """The bot is in the VC only while a human is: join when someone's there,
    leave when it empties — so it never sits alone in an empty channel."""
    if not radio.voice_channel_id:
        return
    channel = client.get_channel(radio.voice_channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        return
    vc = channel.guild.voice_client
    if _listeners(channel):
        if vc is None or not vc.is_connected():
            try:
                vc = await channel.connect()
            except discord.ClientException:
                vc = channel.guild.voice_client
        elif vc.channel != channel:
            await vc.move_to(channel)
        if vc:
            _sync_playback(radio, vc)
    elif vc and vc.is_connected():  # emptied out -> leave
        radio.active = False
        _cancel_prefetch(radio)
        await _clear_nowplaying(radio, clear_status=False)
        await vc.disconnect()


async def _serve_guild(radio: GuildRadio) -> None:
    """Join iff a listener is already present on boot; otherwise wait for one."""
    await _reconcile_voice(radio)
    ch = client.get_channel(radio.voice_channel_id) if radio.voice_channel_id else None
    where = f"{ch.guild.name}/{ch.name}" if isinstance(ch, discord.VoiceChannel) else str(radio.guild_id)
    print(f"serving {where} — {'streaming' if radio.active else 'waiting for a listener'}")


async def _sync_commands_to(guild) -> None:
    """Copy the global commands into a guild and sync — instant availability
    (a plain global sync can take up to an hour to propagate)."""
    try:
        tree.copy_global_to(guild=guild)
        # Adding off for this guild -> don't register /add + /youtube at all, so they're
        # invisible in the command picker (not just refused). /perms turns them on.
        if conn is not None and not db.add_enabled(conn, guild.id):
            for name in ("add", "youtube"):
                tree.remove_command(name, guild=guild)
        await tree.sync(guild=guild)
    except discord.HTTPException as e:
        print(f"command sync failed for guild {getattr(guild, 'id', '?')}: {e}")


@client.event
async def on_ready() -> None:
    global conn, _loop
    _ensure_opus()
    _loop = asyncio.get_running_loop()
    if conn is None:
        conn = db.connect()
        # Back-compat: seed the guilds table from the legacy single-guild env once.
        if not db.list_guilds(conn) and _SEED_GUILD_ID:
            db.upsert_guild(conn, _SEED_GUILD_ID, _SEED_VOICE_ID, _SEED_NP_ID)
            print(f"seeded guild {_SEED_GUILD_ID} from env")
        # This app was reused from an earlier project (OpenClaw) whose slash commands
        # were registered GLOBALLY and still show. Wipe stale global commands (ours
        # are registered per-guild below, so this leaves them intact).
        try:
            await client.http.bulk_upsert_global_commands(client.application_id, [])
        except discord.HTTPException as e:
            print(f"could not clear stale global commands: {e}")
        for g in client.guilds:  # instant command availability in every joined guild
            await _sync_commands_to(g)
        if db.music_count(conn) > 0:
            print(f"mercuryradio up as {client.user} — {db.music_count(conn)} tracks (rescanning in background)")
            _loop.create_task(_rescan_bg())
        else:
            count = await _loop.run_in_executor(None, _scan_library)
            print(f"mercuryradio up as {client.user} — {count} tracks (first scan)")
    # (re)serve every enabled guild
    for row in db.list_guilds(conn):
        radio = _radio(row["guild_id"])
        if radio:
            await _serve_guild(radio)
    await _announce_release()  # idempotent: keyed on the newest CHANGELOG heading


@client.event
async def on_guild_join(guild) -> None:
    await _sync_commands_to(guild)  # so /setup is available immediately


def _bump_card_burial(message) -> None:
    """Count chat landing in a guild's card channel since its card last moved. The invite
    permission set has no Read Message History, so channel.history() would 403 — the
    gateway already delivers every message, so the bot counts for itself. Our own
    messages (the card, release notes) don't count."""
    if client.user is not None and message.author.id == client.user.id:
        return
    radio = radios.get(message.guild.id) if message.guild else None
    if radio and radio.np_message is not None and message.channel.id == radio.np_message.channel.id:
        radio.msgs_since_card += 1


@client.event
async def on_message(message) -> None:
    _bump_card_burial(message)


@client.event
async def on_voice_state_update(member, before, after) -> None:
    if member.bot:
        return
    radio = _radio(member.guild.id)
    if radio is None:
        return
    await _reconcile_voice(radio)  # join when a listener arrives, leave when empty
    await _refresh_sidebar(radio)


# ── permissions + shared add-to-library ─────────────────────────────────────

def _may(interaction: discord.Interaction, action: str) -> bool:
    """Permission gate for add/mutate commands. Only /add + /youtube are gated;
    everything else is open. A guild with no add-role set is open to all (his
    friend servers); once an admin sets one with /perms, only members with that
    role (server admins always) may add. Role comes from the interaction member,
    so no privileged members intent is needed."""
    if action not in ("add", "youtube", "retag"):
        return True
    if interaction.guild_id is None:
        return True
    # add/youtube are the per-guild toggle (and get hidden when off); retag isn't
    # part of that — it's a correction, gated only by the same role.
    if action in ("add", "youtube") and not db.add_enabled(conn, interaction.guild_id):
        return False  # adding is off here (commands aren't even registered)
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms and perms.manage_guild:  # admins are never locked out
        return True
    role_id = db.get_add_role(conn, interaction.guild_id)
    if not role_id:
        return True  # no role configured -> open
    member_roles = getattr(interaction.user, "roles", [])
    return any(str(r.id) == str(role_id) for r in member_roles)


async def _gate(interaction: discord.Interaction, action: str) -> bool:
    if _may(interaction, action):
        return True
    role_id = db.get_add_role(conn, interaction.guild_id) if interaction.guild_id else None
    where = f" — you need the <@&{role_id}> role" if role_id else ""
    await interaction.response.send_message(
        f"You don't have permission to add music here{where}.", ephemeral=True)
    return False


def _add_to_library(path: str) -> tuple[str, str, bool]:
    """Tag, dedup, and upsert one already-saved audio file into the shared library.
    Shared by /add and /youtube so both add-paths tag and dedup identically. Returns
    (artist, title, created); created is False when the track's tags already matched a
    library row. A redundant fresh copy is removed so ADDED_DIR doesn't collect dupes
    (but a path healed onto a moved track is kept — that's the canonical file now)."""
    artist, title, album, duration = library._read_tags(path)
    key = db.norm_key(artist, title, album)
    created = db.track_id_for_key(conn, key) is None
    db.upsert_track(conn, artist, title, album, path, duration)  # loop-thread conn
    if not created:
        stored = conn.execute("SELECT path FROM tracks WHERE norm_key = ?", (key,)).fetchone()["path"]
        if stored != path and os.path.exists(path):
            try:
                os.remove(path)  # a duplicate of a track we already have
            except OSError:
                pass
    return artist, title, created


def _tail(text: str, n: int = 300) -> str:
    return (((text or "").strip().splitlines() or ["unknown error"])[-1])[:n]


@tree.command(name="help", description="What Mercury Radio is and how to listen and rate.")
async def help_cmd(interaction: discord.Interaction) -> None:
    scale = " · ".join(f"{sq} {label}" for label, _v, sq, _s in RATINGS)
    embed = discord.Embed(
        title="🎵 Mercury Radio",
        description="Everyone in the voice channel hears the **same stream at the same time**. "
                    "The next song isn't random — it's picked from the ratings of whoever's "
                    "listening right now. Rate along and the station bends toward the room's taste.",
    )
    embed.add_field(
        name="Listen",
        value="Join the station **voice channel**. The bot hops in whenever someone's there and "
              "leaves when it's empty — so if you're the first in, give it a moment to wake up.",
        inline=False,
    )
    embed.add_field(
        name="Rate",
        value=f"Click a button under the **now-playing card**:\n{scale}\n"
              "Your pick shows in your color on the card's sidebar and feeds what plays next. "
              "The scale punishes the veto — a Hate counts far more than a Love. "
              "You can also `/rate` the current song without touching the card, or any track by name, even off-air.",
        inline=False,
    )
    embed.add_field(
        name="Commands",
        value="`/request` a track to play next · `/add` a file to the library · `/skip` the current "
              "song · `/myratings` your history · `/join` / `/leave` the radio · "
              "`/retag` fix a track's artist/title · "
              "`/setup` (admin) point it at a channel · `/perms` (admin) restrict who can add.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="setup", description="Register this server's radio (admin).")
@app_commands.describe(voice="Voice channel to stream in", card="Text channel for the now-playing card")
async def setup(interaction: discord.Interaction, voice: discord.VoiceChannel,
                card: discord.TextChannel | None = None) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("Run this in a server.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need Manage Server to set up the radio.", ephemeral=True)
        return
    db.upsert_guild(conn, interaction.guild_id, voice.id, card.id if card else None)
    radios.pop(interaction.guild_id, None)  # reload config
    radio = _radio(interaction.guild_id)
    await interaction.response.send_message(
        f"Radio set up — streaming in **{voice.name}**"
        + (f", card in **#{card.name}**" if card else "") + ". Joining now.",
        ephemeral=True,
    )
    await _serve_guild(radio)


async def _is_bot_owner(user) -> bool:
    app = await client.application_info()
    if app.team:
        return any(m.id == user.id for m in app.team.members)
    return user.id == app.owner.id


@tree.command(name="update", description="Restart the bot on the newest release (bot owner).")
async def update(interaction: discord.Interaction) -> None:
    # Defer IMMEDIATELY — Discord gives 3s to ack, and the owner check below is an HTTP
    # call; on a busy loop it blew the window ("The application did not respond", 9/2 1:12 PM).
    await interaction.response.defer(ephemeral=True)
    if not await _is_bot_owner(interaction.user):
        await interaction.followup.send("Only the bot owner can update the bot.", ephemeral=True)
        return
    url, token = os.environ.get("WATCHTOWER_URL"), os.environ.get("WATCHTOWER_TOKEN")
    if not url or not token:
        await interaction.followup.send(
            "Self-update isn't configured — set WATCHTOWER_URL and WATCHTOWER_TOKEN in the stack "
            "(see deploy/compose.nas.yaml).", ephemeral=True)
        return
    # Reply BEFORE asking watchtower: if there IS a new image, watchtower stops this very
    # process (after the drain), so no code after the update reliably runs. The release-notes
    # announce on the new boot is the visible confirmation.
    await interaction.followup.send(
        "📦 Checking for a new release. If there is one I'll finish the current song, restart, "
        "and post the release notes when I'm back.", ephemeral=True)

    async def _kick() -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers={"Authorization": f"Bearer {token}"},
                                        timeout=aiohttp.ClientTimeout(total=900)) as r:
                    print(f"[update] watchtower answered {r.status}: {(await r.text())[:200]!r} "
                          "(an answer means NO new image — a real update kills this process first)")
        except Exception as e:
            print(f"[update] watchtower call failed: {e}")

    asyncio.get_running_loop().create_task(_kick())


@tree.command(name="promo", description="Set the station-ID clip played when the radio wakes (admin).")
@app_commands.describe(track="A library track to play on wake — /add or /youtube it in first",
                       clear="Remove the promo")
async def promo(interaction: discord.Interaction, track: str | None = None, clear: bool = False) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("Run this in a server.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need Manage Server to set the promo.", ephemeral=True)
        return
    radio = _radio(interaction.guild_id)
    if radio is None:
        await interaction.response.send_message("Run /setup first.", ephemeral=True)
        return
    if clear:
        db.set_guild_promo(conn, interaction.guild_id, None)
        radio.promo_track_id = None  # live update — no radios.pop, playback state survives
        await interaction.response.send_message("Promo cleared — wakes go straight to music.", ephemeral=True)
        return
    if track:
        row = _resolve_track(track)
        if row is None:
            await interaction.response.send_message(f"No track matches “{track}”.", ephemeral=True)
            return
        db.set_guild_promo(conn, interaction.guild_id, row["id"])
        radio.promo_track_id = row["id"]
        await interaction.response.send_message(
            f"📻 Station ID set: **{row['artist']} – {row['title']}** plays when the radio wakes.", ephemeral=True)
        return
    current = db.promo_row(conn, interaction.guild_id)
    msg = (f"Current station ID: **{current['artist']} – {current['title']}**."
           if current else "No station ID set — `/promo track:` to pick one.")
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="perms", description="Control who can add music on this server (admin).")
@app_commands.describe(
    adding="Turn /add + /youtube on or off here — off hides them from the command list entirely",
    add_role="When adding is on, limit it to this role — pass @everyone to open it to all",
)
async def perms(interaction: discord.Interaction, adding: bool | None = None,
                add_role: discord.Role | None = None) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("Run this in a server.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need Manage Server to change permissions.", ephemeral=True)
        return
    if db.get_guild(conn, interaction.guild_id) is None:
        await interaction.response.send_message("This server isn't set up — an admin runs /setup first.", ephemeral=True)
        return
    parts = []
    if adding is not None:
        db.set_add_enabled(conn, interaction.guild_id, adding)
        await _sync_commands_to(interaction.guild)  # register/unregister the commands -> show/hide
        parts.append("🔓 adding is **on**" if adding else "🔒 adding is **off** (commands hidden)")
    if add_role is not None:
        open_all = add_role.id == interaction.guild_id  # the @everyone role's id == guild id
        db.set_add_role(conn, interaction.guild_id, None if open_all else add_role.id)
        parts.append("anyone may add" if open_all else f"limited to **{add_role.name}** (and admins)")
    if not parts:  # no args -> report current state
        on = db.add_enabled(conn, interaction.guild_id)
        rid = db.get_add_role(conn, interaction.guild_id)
        who = f"<@&{rid}> (and admins)" if rid else "everyone"
        parts.append(f"Adding is **{'on' if on else 'off'}**; when on, {who} can add.")
    await interaction.response.send_message(" · ".join(parts), ephemeral=True)


@tree.command(name="join", description="Start the radio in the server's voice channel.")
async def join(interaction: discord.Interaction) -> None:
    radio = _radio(interaction.guild_id) if interaction.guild_id else None
    if radio is None:
        await interaction.response.send_message("This server isn't set up — an admin runs /setup first.", ephemeral=True)
        return
    if not db.music_count(conn):
        await interaction.response.send_message(f"No tracks in the library ({MUSIC_DIR}).", ephemeral=True)
        return
    await _reconcile_voice(radio)  # joins iff a listener is in the station channel
    if radio.active:
        await interaction.response.send_message("Radio's on.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "Join the station voice channel and the radio starts automatically.", ephemeral=True
        )


@tree.command(name="skip", description="Skip to the next track.")
async def skip(interaction: discord.Interaction) -> None:
    if not await _gate(interaction, "skip"):
        return
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("Skipped.", ephemeral=True)
    else:
        await interaction.response.send_message("Nothing playing.", ephemeral=True)


def _resolve_track(track: str):
    """The track row for an autocomplete pick (a track id) or free text (first match)."""
    if track.isdigit():
        row = conn.execute("SELECT id, artist, title FROM tracks WHERE id = ?", (int(track),)).fetchone()
        if row:
            return row
    matches = db.search_tracks(conn, track, 1)
    return matches[0] if matches else None


@tree.command(name="request", description="Request a track — it plays next.")
@app_commands.describe(track="Start typing an artist or title, then pick from the list")
async def request(interaction: discord.Interaction, track: str) -> None:
    row = _resolve_track(track)
    if row is None:
        await interaction.response.send_message(f"No track matches “{track}”.", ephemeral=True)
        return
    replaced = db.add_request(conn, row["id"], str(interaction.guild_id), str(interaction.user.id))
    ahead = db.pending_request_count(conn, str(interaction.guild_id)) - 1
    when = "plays in the next block" if ahead <= 0 else f"{ahead} request(s) ahead of it"
    new_label = f"{row['artist']} – {row['title']}"
    if replaced == new_label:
        msg = f"**{new_label}** was already your request — moved to the back of the queue ({when})."
    elif replaced:
        msg = f"Swapped your request: **{replaced}** → **{new_label}** — back of the queue, {when}."
    else:
        msg = f"Queued **{new_label}** — {when}."
    await interaction.response.send_message(msg, ephemeral=True)


@request.autocomplete("track")
async def _request_autocomplete(interaction: discord.Interaction, current: str):
    if not current:
        return []
    rows = db.search_tracks(conn, current, 25)
    return [app_commands.Choice(name=f"{r['artist']} – {r['title']}"[:100], value=str(r["id"])) for r in rows]


# Rating picker for /rate — same five values as the now-playing buttons.
_RATING_CHOICES = [app_commands.Choice(name=label, value=value) for label, value, _sq, _st in RATINGS]


@tree.command(name="rate", description="Rate the song playing now, or any track by name.")
@app_commands.describe(rating="Your rating", track="Optional — leave blank to rate the song playing now")
@app_commands.choices(rating=_RATING_CHOICES)
async def rate(interaction: discord.Interaction, rating: app_commands.Choice[int], track: str | None = None) -> None:
    if track:
        row = _resolve_track(track)
        if row is None:
            await interaction.response.send_message(f"No track matches “{track}”.", ephemeral=True)
            return
    else:  # no track named -> the song playing in this server right now
        radio = _radio(interaction.guild_id) if interaction.guild_id else None
        row = radio.current_row if radio else None
        if row is None:
            await interaction.response.send_message(
                "Nothing is playing here — name a track to rate one by hand.", ephemeral=True)
            return
    db.upsert_user(conn, interaction.user.id, interaction.user.display_name)
    db.set_rating(conn, str(interaction.user.id), row["id"], rating.value)
    if interaction.guild_id:
        db.touch_presence(conn, interaction.user.id, interaction.guild_id)  # rating == present here
    square = _SQUARE.get(rating.value, UNRATED)
    await interaction.response.send_message(
        f"{square} Rated **{row['artist']} – {row['title']}** {rating.name}.", ephemeral=True)
    # If it's the track playing here right now, reflect the new rating on the card.
    radio = _radio(interaction.guild_id) if interaction.guild_id else None
    if radio and radio.current_row and radio.current_row["id"] == row["id"]:
        await _refresh_sidebar(radio)


# Same free-text -> track-id autocomplete as /request.
rate.autocomplete("track")(_request_autocomplete)
promo.autocomplete("track")(_request_autocomplete)


@tree.command(name="retag", description="Fix a track's artist/title (e.g. a YouTube add tagged with the channel name).")
@app_commands.describe(track="The track to fix — start typing to pick it",
                       artist="Correct artist (leave empty to keep)",
                       title="Correct title (leave empty to keep)")
async def retag(interaction: discord.Interaction, track: str,
                artist: str | None = None, title: str | None = None) -> None:
    if not await _gate(interaction, "retag"):
        return
    if not artist and not title:
        await interaction.response.send_message("Give a new artist and/or title to set.", ephemeral=True)
        return
    row = _resolve_track(track)
    if row is None:
        await interaction.response.send_message(f"No track matches “{track}”.", ephemeral=True)
        return
    new_artist = (artist or row["artist"]).strip()
    new_title = (title or row["title"]).strip()
    ok, info = db.retag_track(conn, row["id"], new_artist, new_title)
    if not ok:
        await interaction.response.send_message(info, ephemeral=True)
        return
    library.write_tags(info, new_artist, new_title)  # info = path; keeps a rescan from re-drifting
    await interaction.response.send_message(f"🏷️ Retagged to **{new_artist} – {new_title}**.", ephemeral=True)
    # If it's the track playing here right now, fix the card in place.
    radio = _radio(interaction.guild_id) if interaction.guild_id else None
    if radio and radio.current_row and radio.current_row["id"] == row["id"]:
        radio.current_row["artist"], radio.current_row["title"] = new_artist, new_title
        radio.current_track = f"{new_artist} – {new_title}"
        if radio.np_message and radio.np_message.embeds:
            embed = radio.np_message.embeds[0]
            embed.title = f"{new_artist} – {new_title}"
            try:
                await radio.np_message.edit(embed=embed)
            except discord.HTTPException:
                pass


retag.autocomplete("track")(_request_autocomplete)


@tree.command(name="myratings", description="Show a summary of your ratings and your most recent ones.")
async def myratings(interaction: discord.Interaction) -> None:
    uid = str(interaction.user.id)
    summary = db.rating_summary(conn, uid)
    total = sum(summary.values())
    if not total:
        await interaction.response.send_message("You haven't rated anything yet.", ephemeral=True)
        return
    counts = "  ".join(f"{sq} {summary.get(value, 0)}" for _label, value, sq, _st in RATINGS)
    lines = [f"{_SQUARE.get(r['value'], UNRATED)} {r['artist']} – {r['title']}"
             for r in db.recent_ratings(conn, uid, 10)]
    body = f"**{total}** ratings   {counts}\n\n**Recent:**\n" + "\n".join(lines)
    await interaction.response.send_message(body, ephemeral=True)


@tree.command(name="add", description="Add a song to the shared library from a file.")
@app_commands.describe(file="An audio file — it's added to the library and becomes requestable.")
async def add(interaction: discord.Interaction, file: discord.Attachment) -> None:
    if not await _gate(interaction, "add"):
        return
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in library.AUDIO_EXTS:
        kinds = ", ".join(sorted(e.lstrip(".") for e in library.AUDIO_EXTS))
        await interaction.response.send_message(f"That's not an audio file. Accepted: {kinds}.", ephemeral=True)
        return
    if file.size > ADD_MAX_BYTES:
        await interaction.response.send_message(f"That file is too big ({file.size // (1024*1024)} MB).", ephemeral=True)
        return
    # Split feedback: the interim/errors are ephemeral (requester-only), the "Added"
    # is a public channel message so the room sees new music land.
    await interaction.response.defer(thinking=True, ephemeral=True)
    os.makedirs(ADDED_DIR, exist_ok=True)
    # basename only — never let an attachment name escape the ingest dir
    dest = os.path.join(ADDED_DIR, os.path.basename(file.filename))
    try:
        await file.save(dest)
    except (discord.HTTPException, OSError) as e:
        await interaction.followup.send(f"Couldn't save that file: {e}", ephemeral=True)
        return
    artist, title, created = _add_to_library(dest)
    if not created:
        await interaction.followup.send(f"**{artist} – {title}** is already in the library — /request it.", ephemeral=True)
        return
    await interaction.channel.send(
        f"🎵 **{interaction.user.display_name}** added **{artist} – {title}** to the library — /request it.")
    await interaction.followup.send(f"Added **{artist} – {title}** ✓", ephemeral=True)


async def _yt(*args):
    """Run yt-dlp as an async child process so a download never blocks the audio
    loop. Returns (returncode, stdout, stderr) as decoded text."""
    p = await asyncio.create_subprocess_exec(
        "yt-dlp", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


@tree.command(name="youtube", description="Pull audio from a link and add it to the library.")
@app_commands.describe(
    url="A YouTube (or other yt-dlp-supported) link to a song.",
    artist="Override the artist tag (optional).",
    title="Override the title tag (optional).",
)
async def youtube(interaction: discord.Interaction, url: str,
                  artist: str | None = None, title: str | None = None) -> None:
    if not await _gate(interaction, "youtube"):
        return
    if not url.lower().startswith(("http://", "https://")):
        await interaction.response.send_message("That doesn't look like a link.", ephemeral=True)
        return
    # Split feedback: the interim + every error is ephemeral (keeps #music clean),
    # the final "Added" is a public channel message. So defer ephemeral.
    await interaction.response.defer(thinking=True, ephemeral=True)
    max_secs = MAX_ADD_MINUTES * 60
    # One fast metadata pre-fetch does triple duty: the interim title, the clean
    # tags for the ladder, and an early duration reject (fail a too-long video in
    # ~2s instead of after a wasted download).
    rc, out, err = await _yt(
        "--skip-download", "--no-playlist",
        "--print", "%(artist|NA)s", "--print", "%(track|NA)s",
        "--print", "%(title)s", "--print", "%(duration|0)s", url)
    if rc != 0:
        await interaction.followup.send(f"Couldn't read that link: {_tail(err)}", ephemeral=True)
        return
    lines = (out.strip().splitlines() + ["NA", "NA", "that video", "0"])[:4]
    meta_artist, meta_track, vtitle, dur_s = lines
    try:
        dur = float(dur_s)
    except ValueError:
        dur = 0.0
    if dur and dur > max_secs:
        await interaction.followup.send(
            f"That's {int(dur // 60)} min — over the {MAX_ADD_MINUTES}-minute limit.", ephemeral=True)
        return
    r_artist, r_title = library.resolve_yt_tags(meta_artist, meta_track, vtitle, artist, title)
    interim = await interaction.followup.send(
        f"🔎 Found **{r_artist} – {r_title}** — pulling the audio…", ephemeral=True, wait=True)

    os.makedirs(ADDED_DIR, exist_ok=True)
    rc, out, err = await _yt(
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "--no-playlist", "--match-filter", f"duration < {max_secs}",
        "--restrict-filenames", "--no-progress",
        "-o", os.path.join(ADDED_DIR, "%(title)s-%(id)s.%(ext)s"),
        "--print", "after_move:filepath", url)
    if rc != 0:
        await interim.edit(content=f"Couldn't fetch the audio: {_tail(err)}")
        return
    path = (out.strip().splitlines() or [""])[-1]
    if not path or not os.path.exists(path):
        await interim.edit(content=f"Nothing downloaded — the video may be over {MAX_ADD_MINUTES} minutes.")
        return
    # Set our resolved tags explicitly — they drive norm_key/dedup, so we don't trust
    # whatever the source embedded — then dedup + upsert through the shared path.
    library.write_tags(path, r_artist, r_title)
    lib_artist, lib_title, created = _add_to_library(path)
    if not created:
        await interim.edit(content=f"**{lib_artist} – {lib_title}** is already in the library — /request it.")
        return
    await interim.edit(content=f"Added **{lib_artist} – {lib_title}** ✓")
    await interaction.channel.send(
        f"🎵 **{interaction.user.display_name}** added **{lib_artist} – {lib_title}** from YouTube — /request it.")


@tree.command(name="bug", description="File a bug report — lands straight in the database.")
@app_commands.describe(text="What happened. The time and the current track are recorded for you.")
async def bug(interaction: discord.Interaction, text: str) -> None:
    # Defer first: the writes below can wait on the library rescan's SQLite write lock,
    # and 3s of loop time is not guaranteed on the starved host (9/2 1:15 PM timeout).
    await interaction.response.defer(ephemeral=True)
    radio = _radio(interaction.guild_id) if interaction.guild_id else None
    row = radio.current_row if radio else None
    db.upsert_user(conn, interaction.user.id, interaction.user.display_name)
    bug_id = db.add_bug(conn, str(interaction.user.id), str(interaction.guild_id) if interaction.guild_id else None,
                        text, row["id"] if row else None)
    now = f" — now playing: {row['artist']} – {row['title']}" if row else ""
    # Also a timestamped marker in the container log, next to the [pace] lines — a /bug
    # during a glitch is exactly the human-flagged marker the pacing work asked for.
    print(f"[bug] #{bug_id} {interaction.user.display_name}: {text}{now}")
    await interaction.followup.send(f"🐛 Bug #{bug_id} filed{now}. Thank you!", ephemeral=True)


@tree.command(name="recent", description="Show recently played tracks and rate any you missed.")
async def recent(interaction: discord.Interaction) -> None:
    plays = db.recent_plays(conn, 10)
    if not plays:
        await interaction.response.send_message("Nothing has played yet.", ephemeral=True)
        return
    view = RecentView(plays)
    await interaction.response.send_message(view.body_for(str(interaction.user.id)), view=view, ephemeral=True)


@tree.command(name="leave", description="Stop this server's radio and leave.")
async def leave(interaction: discord.Interaction) -> None:
    if not await _gate(interaction, "leave"):
        return
    radio = _radio(interaction.guild_id) if interaction.guild_id else None
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc:
        if radio:
            _cancel_prefetch(radio)
            await _clear_nowplaying(radio)
            radio.active = False
        await vc.disconnect()
        await interaction.response.send_message("Left.", ephemeral=True)
    else:
        await interaction.response.send_message("Not in a channel.", ephemeral=True)


if __name__ == "__main__":
    client.run(TOKEN)
