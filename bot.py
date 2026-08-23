"""mercuryradio — Phase 4: now-playing card with live ratings.

Continuous radio (Phase 3) plus an interactive now-playing message: album art,
the current track, five rating buttons (hate/dislike/shrug/like/love), and a live
colored sidebar showing each present member's rating in its color. The bot's
presence also reflects the current track. Ratings are stored per-user-per-track;
the rating-scored selection engine replaces the flat shuffle in Phase 5.

Config comes from the environment (see .env.sample). No paths or tokens live in
this file.
"""

import asyncio
import io
import os
import signal

import discord
from discord import app_commands
from dotenv import load_dotenv

import db
import engine
import library

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
MUSIC_DIR = os.environ["MUSIC_DIR"]
VOICE_CHANNEL_ID = int(os.environ.get("VOICE_CHANNEL_ID") or 0) or None
# Where the now-playing card is posted. Unset: the voice channel's own text chat.
NOWPLAYING_CHANNEL_ID = int(os.environ.get("NOWPLAYING_CHANNEL_ID") or 0) or None

FFMPEG_OPTS = {"options": "-vn"}

# A rating within this window counts a user as present (for scoring + the sidebar)
# even if they never joined voice — for listeners sharing one speaker/connection.
PRESENCE_WINDOW_MIN = 30

# On wake (empty VC -> someone joins), start the first song this many seconds in,
# so you drop into a track already playing rather than catching every one cold.
WAKE_SEEK_SECONDS = 30

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
_block: list = []  # remaining picker types in the current shuffled block
current_track: str | None = None
_active = False  # True only while a human is listening; gates streaming
_current_row: dict | None = None  # the track playing now (id/artist/title/album/path)
_np_message: discord.Message | None = None  # the live now-playing card
_recent_artists: list = []  # rolling window for the channel-topic update
_since_topic = 0            # tracks played since the topic was last refreshed
TOPIC_EVERY = 5             # refresh the topic once per this many tracks


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


async def _advance(vc: discord.VoiceClient, seek: int = 0) -> None:
    """Compose and play the next track, scored over whoever is in the VC right
    now (the 'room'). Runs on the loop thread — db access is loop-thread-bound —
    and the after-callback (worker thread) hops back here via run_coroutine_threadsafe.
    `seek` starts the track that many seconds in — used on wake so a joiner drops
    into a song already in progress, like tuning into a live station."""
    global _block, current_track, _current_row
    if not _active or not vc.is_connected():
        return
    if not _listeners(vc.channel):
        return  # streaming gate: at least one human must be in the VC to stream
    present = _present(vc.channel)
    member_ids = [uid for uid, _ in present]  # VC members + recent raters
    gid = str(vc.channel.guild.id)
    # Pop the next type from the shuffled block; when it empties, compose a fresh
    # one sized to this guild's request backlog (shasradio: a 6th request slot at
    # 3+ queued). The request slot is guild-scoped so a shared ratings DB can't
    # bleed one server's requests onto another.
    if not _block:
        _block = engine.new_block(db.pending_request_count(conn, gid))
    want = _block.pop()
    row = picker = None
    if want == "request":
        req = db.next_request(conn, gid)
        if req is not None:
            row, picker = dict(req), "request"
            db.mark_request_played(conn, req["id"], gid)
    if row is None:  # a music slot, or the request queue was empty -> pick music
        r, picker = engine.pick(conn, member_ids, "top" if want == "request" else want)
        if r is None:
            return
        row = dict(r)
    _current_row = row
    current_track = f"{row['artist']} – {row['title']}"
    db.record_play(conn, row["id"], reason=picker)
    opts = dict(FFMPEG_OPTS)
    if seek > 0:
        opts["before_options"] = f"-ss {seek}"  # input seek: start mid-song
    source = discord.FFmpegPCMAudio(row["path"], **opts)
    vc.play(source, after=lambda err: _after(vc, err, row["path"]))
    await _post_nowplaying(vc.channel, row)


def _after(vc: discord.VoiceClient, err: Exception | None, path: str) -> None:
    # Runs on discord's audio worker thread — schedule the next pick onto the loop.
    if err:
        print(f"playback error on {path}: {err}")
    if _loop:
        asyncio.run_coroutine_threadsafe(_advance(vc), _loop)


def _listeners(channel) -> list:
    return [m for m in getattr(channel, "members", []) if not m.bot]


def _present(voice_channel):
    """Present listeners = everyone in the VC + anyone who rated within
    PRESENCE_WINDOW_MIN. A recent rating signals presence, so a listener sharing
    one speaker (or on the text channel) counts without joining voice. Returns
    [(user_id_str, display_name)], VC members first, deduped."""
    present = {str(m.id): m.display_name for m in _listeners(voice_channel)}
    for r in db.recent_raters(conn, PRESENCE_WINDOW_MIN):
        present.setdefault(r["user_id"], r["name"])
    return list(present.items())


def _sync_playback(vc: discord.VoiceClient) -> None:
    """Stream iff a human is in the channel. Idempotent."""
    global _active, current_track, _current_row
    if not vc or not vc.is_connected():
        return
    if _listeners(vc.channel):
        if not _active:
            _active = True
            if _loop and not vc.is_playing():
                # wake: drop the joiner into a song already in progress
                _loop.create_task(_advance(vc, seek=WAKE_SEEK_SECONDS))
    elif _active:
        _active = False
        current_track = None
        _current_row = None
        if vc.is_playing():
            vc.stop()
        if _loop:
            asyncio.run_coroutine_threadsafe(_clear_nowplaying(), _loop)


# ── now-playing card ────────────────────────────────────────────────────────

def _sidebar(voice_channel, track_id: int) -> str:
    """One line per present listener (VC + recent raters): their rating square."""
    lines = []
    for uid, name in _present(voice_channel):
        val = db.get_rating(conn, uid, track_id)
        square = _SQUARE.get(val, UNRATED) if val is not None else UNRATED
        lines.append(f"{square} {name}")
    return "\n".join(lines) or "_nobody here_"


def _np_channel(voice_channel):
    if NOWPLAYING_CHANNEL_ID:
        return client.get_channel(NOWPLAYING_CHANNEL_ID) or voice_channel
    return voice_channel  # voice channels are Messageable (text-in-voice) in discord.py 2.x


def _build_embed(row: dict, channel, has_cover: bool) -> discord.Embed:
    embed = discord.Embed(title=f"{row['artist']} – {row['title']}", description=row.get("album") or "")
    embed.add_field(name="Ratings", value=_sidebar(channel, row["id"]), inline=False)
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
        if _current_row is None:
            await interaction.response.send_message("Nothing playing.", ephemeral=True)
            return
        db.upsert_user(conn, interaction.user.id, interaction.user.display_name)
        db.set_rating(conn, str(interaction.user.id), _current_row["id"], self.value)
        conn.commit()
        await interaction.response.defer()  # ack, no new message
        await _refresh_sidebar()


async def _refresh_sidebar() -> None:
    """Rebuild the sidebar on the current card (keeps art + buttons). Reads the
    VOICE channel for presence, not the card's text channel."""
    if _np_message is None or _current_row is None:
        return
    guild = _np_message.guild
    vc = guild.voice_client if guild else None
    voice_channel = vc.channel if vc else _np_message.channel
    embed = _np_message.embeds[0]
    embed.set_field_at(0, name="Ratings", value=_sidebar(voice_channel, _current_row["id"]), inline=False)
    try:
        await _np_message.edit(embed=embed)
    except discord.HTTPException:
        pass


async def _post_nowplaying(voice_channel, row: dict) -> None:
    """Replace the now-playing card for a new track, and set presence."""
    global _np_message
    await _clear_nowplaying()
    channel = _np_channel(voice_channel)
    cover = library.extract_cover(row["path"])
    kwargs = {"view": RatingView()}
    if cover:
        kwargs["file"] = discord.File(io.BytesIO(cover), filename="cover.png")
    kwargs["embed"] = _build_embed(row, voice_channel, has_cover=bool(cover))
    try:
        _np_message = await channel.send(**kwargs)
    except discord.HTTPException as e:
        print(f"could not post now-playing card: {e}")
        _np_message = None
    try:
        await client.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name=f"{row['artist']} – {row['title']}")
        )
    except discord.HTTPException:
        pass
    # Reflect recent artists in the channel topic, but only every TOPIC_EVERY tracks
    # — Discord throttles channel edits to ~2 per 10 min, so a per-song edit gets 429'd.
    global _since_topic
    _recent_artists.append(row["artist"])
    del _recent_artists[:-TOPIC_EVERY]  # keep the last TOPIC_EVERY
    _since_topic += 1
    if _since_topic >= TOPIC_EVERY:
        _since_topic = 0
        recent = list(dict.fromkeys(_recent_artists))  # dedup, keep order
        try:
            await channel.edit(topic="🎵 Recent: " + ", ".join(recent))
        except (discord.HTTPException, AttributeError) as e:
            print(f"could not set channel topic: {e}")


async def _clear_nowplaying() -> None:
    global _np_message
    if _np_message is not None:
        try:
            await _np_message.delete()
        except discord.HTTPException:
            pass
        _np_message = None
    try:
        await client.change_presence(activity=None)
    except discord.HTTPException:
        pass


# ── discord wiring ──────────────────────────────────────────────────────────


async def _announce_shutdown() -> None:
    """Post an off-air notice to the card channel before the gateway closes."""
    ch = client.get_channel(NOWPLAYING_CHANNEL_ID) if NOWPLAYING_CHANNEL_ID else None
    if ch is None and VOICE_CHANNEL_ID:
        ch = client.get_channel(VOICE_CHANNEL_ID)
    if ch is None:
        return
    try:
        await _clear_nowplaying()
        await ch.send("📻 Mercury Radio is going off the air — back soon.")
    except discord.HTTPException:
        pass


class MercuryClient(discord.Client):
    _shutdown_announced = False

    async def setup_hook(self) -> None:
        # docker stop/restart sends SIGTERM; asyncio.run doesn't trap it, so the
        # normal close() path never runs. Bridge SIGTERM -> close() ourselves.
        try:
            self.loop.add_signal_handler(
                signal.SIGTERM, lambda: self.loop.create_task(self.close())
            )
        except (NotImplementedError, RuntimeError):
            pass  # no signal support on this platform

    async def close(self) -> None:
        if not self._shutdown_announced:
            self._shutdown_announced = True
            await _announce_shutdown()
        await super().close()


intents = discord.Intents.default()
intents.voice_states = True
client = MercuryClient(intents=intents)
tree = app_commands.CommandTree(client)
guild = discord.Object(id=GUILD_ID)


async def _rescan_bg() -> None:
    """Refresh the library off the loop, after playback has already started.
    library.scan opens its own connection, so it's safe from the executor thread."""
    try:
        n = await _loop.run_in_executor(None, library.scan, MUSIC_DIR)
        print(f"background library rescan complete — {n} tracks")
    except Exception as e:  # a rescan failure must not take the bot down
        print(f"background rescan failed: {e}")


@client.event
async def on_ready() -> None:
    # on_ready fires on every gateway (re)connect — make it idempotent: scan the
    # library once, and connect to voice only if not already connected.
    global conn, _loop
    _ensure_opus()
    _loop = asyncio.get_running_loop()
    if conn is None:
        conn = db.connect()
        await tree.sync(guild=guild)
        # The DB persists across restarts, so on a redeploy we can start playing
        # from it immediately and rescan in the background — turning a ~2-min boot
        # gap into a few-second reconnect. Only block on the scan on a first-ever
        # (empty) DB, when there's nothing to play yet.
        if db.music_count(conn) > 0:
            print(f"mercuryradio up as {client.user} — {db.music_count(conn)} tracks (rescanning in background)")
            _loop.create_task(_rescan_bg())
        else:
            count = await _loop.run_in_executor(None, library.scan, MUSIC_DIR)
            print(f"mercuryradio up as {client.user} — {count} tracks (first scan)")
    if not VOICE_CHANNEL_ID:
        return
    channel = client.get_channel(VOICE_CHANNEL_ID)
    if not isinstance(channel, discord.VoiceChannel):
        print(f"VOICE_CHANNEL_ID {VOICE_CHANNEL_ID} is not a reachable voice channel")
        return
    vc = channel.guild.voice_client
    if vc is None:
        try:
            vc = await channel.connect()
        except discord.ClientException:
            vc = channel.guild.voice_client  # a reconnect raced us; reuse it
    elif vc.channel != channel:
        await vc.move_to(channel)
    if vc:
        _sync_playback(vc)
        print(f"auto-joined {channel.name} — {'streaming' if _active else 'idle (empty)'}")


@client.event
async def on_voice_state_update(member, before, after) -> None:
    if member.bot:
        return
    vc = member.guild.voice_client
    if vc and vc.is_connected():
        _sync_playback(vc)
        await _refresh_sidebar()  # a new arrival appears in the sidebar


@tree.command(name="join", description="Start the radio in the station's voice channel.", guild=guild)
async def join(interaction: discord.Interaction) -> None:
    channel = client.get_channel(VOICE_CHANNEL_ID) if VOICE_CHANNEL_ID else None
    if not isinstance(channel, discord.VoiceChannel):
        voice = getattr(interaction.user, "voice", None)
        channel = voice.channel if voice else None
    if channel is None:
        await interaction.response.send_message(
            "No station voice channel configured, and you're not in one.", ephemeral=True
        )
        return
    vc = interaction.guild.voice_client
    if vc:
        await vc.move_to(channel)
    else:
        vc = await channel.connect()
    count = db.music_count(conn)
    if not count:
        await interaction.response.send_message(f"No tracks in the library ({MUSIC_DIR}).", ephemeral=True)
        return
    _sync_playback(vc)
    state = "on" if _active else "idle until someone joins"
    await interaction.response.send_message(
        f"Radio {state} in {channel.name} — {count} tracks.", ephemeral=True
    )


@tree.command(name="skip", description="Skip to the next track.", guild=guild)
async def skip(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("Skipped.", ephemeral=True)
    else:
        await interaction.response.send_message("Nothing playing.", ephemeral=True)


@tree.command(name="request", description="Request a track — it plays next.", guild=guild)
@app_commands.describe(track="Start typing an artist or title, then pick from the list")
async def request(interaction: discord.Interaction, track: str) -> None:
    # `track` is the autocomplete Choice value (a track id). If someone submits
    # free text without picking, fall back to the best substring match.
    row = None
    if track.isdigit():
        row = conn.execute(
            "SELECT id, artist, title FROM tracks WHERE id = ?", (int(track),)
        ).fetchone()
    if row is None:
        matches = db.search_tracks(conn, track, 1)
        row = matches[0] if matches else None
    if row is None:
        await interaction.response.send_message(f"No track matches “{track}”.", ephemeral=True)
        return
    db.add_request(conn, row["id"], str(interaction.guild_id), str(interaction.user.id))
    ahead = db.pending_request_count(conn, str(interaction.guild_id)) - 1
    when = "plays in the next block" if ahead <= 0 else f"{ahead} request(s) ahead of it"
    await interaction.response.send_message(
        f"Queued **{row['artist']} – {row['title']}** — {when}.", ephemeral=True
    )


@request.autocomplete("track")
async def _request_autocomplete(interaction: discord.Interaction, current: str):
    if not current:
        return []
    rows = db.search_tracks(conn, current, 25)
    return [
        app_commands.Choice(name=f"{r['artist']} – {r['title']}"[:100], value=str(r["id"]))
        for r in rows
    ]


@tree.command(name="leave", description="Stop the radio and leave.", guild=guild)
async def leave(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client
    if vc:
        await _clear_nowplaying()
        await vc.disconnect()
        await interaction.response.send_message("Left.", ephemeral=True)
    else:
        await interaction.response.send_message("Not in a channel.", ephemeral=True)


if __name__ == "__main__":
    client.run(TOKEN)
