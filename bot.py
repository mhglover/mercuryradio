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
import random

import discord
from discord import app_commands
from dotenv import load_dotenv

import db
import library

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
MUSIC_DIR = os.environ["MUSIC_DIR"]
VOICE_CHANNEL_ID = int(os.environ.get("VOICE_CHANNEL_ID") or 0) or None
# Where the now-playing card is posted. Unset: the voice channel's own text chat.
NOWPLAYING_CHANNEL_ID = int(os.environ.get("NOWPLAYING_CHANNEL_ID") or 0) or None

FFMPEG_OPTS = {"options": "-vn"}

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
_playlist: list = []
_pos = 0
current_track: str | None = None
_active = False  # True only while a human is listening; gates streaming
_current_row: dict | None = None  # the track playing now (id/artist/title/album/path)
_np_message: discord.Message | None = None  # the live now-playing card


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


def _ensure_playlist() -> None:
    global _playlist, _pos
    if not _playlist:
        _playlist = [dict(r) for r in db.all_tracks(conn)]
        random.shuffle(_playlist)
        _pos = 0


def _play_next(vc: discord.VoiceClient) -> None:
    """Play the next track; reshuffle on wrap. Runs on the main thread (at join)
    and from the after-callback (worker thread). Schedules the now-playing card
    onto the loop rather than touching Discord from the worker thread."""
    global _pos, current_track, _current_row
    if not _active or not vc.is_connected() or not _playlist:
        return
    if _pos >= len(_playlist):
        random.shuffle(_playlist)
        _pos = 0
    row = _playlist[_pos]
    _pos += 1
    current_track = f"{row['artist']} – {row['title']}"
    _current_row = row
    source = discord.FFmpegPCMAudio(row["path"], **FFMPEG_OPTS)
    vc.play(source, after=lambda err: _after(vc, err, row["path"]))
    if _loop:
        asyncio.run_coroutine_threadsafe(_post_nowplaying(vc.channel, row), _loop)


def _after(vc: discord.VoiceClient, err: Exception | None, path: str) -> None:
    if err:
        print(f"playback error on {path}: {err}")
    _play_next(vc)


def _listeners(channel) -> list:
    return [m for m in getattr(channel, "members", []) if not m.bot]


def _sync_playback(vc: discord.VoiceClient) -> None:
    """Stream iff a human is in the channel. Idempotent."""
    global _active, current_track, _current_row
    if not vc or not vc.is_connected():
        return
    if _listeners(vc.channel):
        if not _active:
            _active = True
            _ensure_playlist()
            if _playlist and not vc.is_playing():
                _play_next(vc)
    elif _active:
        _active = False
        current_track = None
        _current_row = None
        if vc.is_playing():
            vc.stop()
        if _loop:
            asyncio.run_coroutine_threadsafe(_clear_nowplaying(), _loop)


# ── now-playing card ────────────────────────────────────────────────────────

def _sidebar(channel, track_id: int) -> str:
    """One line per present member: their rating square (or ⬛ if unrated)."""
    lines = []
    for m in _listeners(channel):
        val = db.get_rating(conn, m.id, track_id)
        square = _SQUARE.get(val, UNRATED) if val is not None else UNRATED
        lines.append(f"{square} {m.display_name}")
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
    """Rebuild the sidebar on the current card (keeps art + buttons)."""
    if _np_message is None or _current_row is None:
        return
    embed = _np_message.embeds[0]
    embed.set_field_at(0, name="Ratings", value=_sidebar(_np_message.channel, _current_row["id"]), inline=False)
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

intents = discord.Intents.default()
intents.voice_states = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
guild = discord.Object(id=GUILD_ID)


@client.event
async def on_ready() -> None:
    # on_ready fires on every gateway (re)connect — make it idempotent: scan the
    # library once, and connect to voice only if not already connected.
    global conn, _loop
    _ensure_opus()
    _loop = asyncio.get_running_loop()
    if conn is None:
        conn = db.connect()
        count = await _loop.run_in_executor(None, library.scan, MUSIC_DIR)
        await tree.sync(guild=guild)
        print(f"mercuryradio up as {client.user} — {count} tracks in the library")
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
    _ensure_playlist()
    if not _playlist:
        await interaction.response.send_message(f"No tracks in the library ({MUSIC_DIR}).", ephemeral=True)
        return
    _sync_playback(vc)
    state = "on" if _active else "idle until someone joins"
    await interaction.response.send_message(
        f"Radio {state} in {channel.name} — {len(_playlist)} tracks.", ephemeral=True
    )


@tree.command(name="skip", description="Skip to the next track.", guild=guild)
async def skip(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("Skipped.", ephemeral=True)
    else:
        await interaction.response.send_message("Nothing playing.", ephemeral=True)


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
