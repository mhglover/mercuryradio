"""mercuryradio — Phase 3: continuous radio backed by a persistent library.

The bot scans MUSIC_DIR into a SQLite tracks table (tags via mutagen), then
streams a shuffled walk of it, chaining track to track so it never stops. Ratings
live in the same DB (seeded from Plex ★ by seed_plex.py). The rating-scored
selection engine replaces the flat shuffle in Phase 5.

Config comes from the environment (see .env.sample). No paths or tokens live in
this file.
"""

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
# The channel the station broadcasts in. Set it and the bot auto-joins on
# startup and /join targets it — no need for anyone to be in a channel first.
# Unset: /join falls back to the caller's current voice channel.
VOICE_CHANNEL_ID = int(os.environ.get("VOICE_CHANNEL_ID") or 0) or None

# -vn drops any embedded cover-art video stream; audio only.
FFMPEG_OPTS = {"options": "-vn"}

# Single-guild state (this bot serves one private server). The playlist is an
# in-memory shuffle of track rows loaded from the DB on the main thread, so the
# after-callback worker thread never touches the SQLite connection.
conn = None
_playlist: list = []
_pos = 0
current_track: str | None = None


def _ensure_opus() -> None:
    """Load libopus for voice encoding. discord.py auto-loads it on some
    platforms but not macOS, where find_library('opus') comes back empty even
    with Homebrew's copy installed — so try the usual locations explicitly.
    """
    if discord.opus.is_loaded():
        return
    from ctypes.util import find_library

    candidates = [
        find_library("opus"),
        "/opt/homebrew/lib/libopus.dylib",  # macOS, Apple Silicon
        "/usr/local/lib/libopus.dylib",  # macOS, Intel
        "libopus.so.0",  # Linux
    ]
    for path in candidates:
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
    """Play the next track; reshuffle and wrap when the list is exhausted.
    Runs both at /join and from the after-callback (a worker thread) — calling
    vc.play() from there is the supported discord.py chaining pattern. Only
    touches the in-memory _playlist, never the DB.
    """
    global _pos, current_track
    if not vc.is_connected() or not _playlist:
        return
    if _pos >= len(_playlist):
        random.shuffle(_playlist)
        _pos = 0
    row = _playlist[_pos]
    _pos += 1
    current_track = f"{row['artist']} – {row['title']}"
    source = discord.FFmpegPCMAudio(row["path"], **FFMPEG_OPTS)
    vc.play(source, after=lambda err: _after(vc, err, row["path"]))


def _after(vc: discord.VoiceClient, err: Exception | None, path: str) -> None:
    if err:
        print(f"playback error on {path}: {err}")  # skip the bad file, keep going
    _play_next(vc)


intents = discord.Intents.default()
intents.voice_states = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
guild = discord.Object(id=GUILD_ID)


@client.event
async def on_ready() -> None:
    global conn
    _ensure_opus()
    conn = db.connect()
    count = library.scan(conn, MUSIC_DIR)
    await tree.sync(guild=guild)
    print(f"mercuryradio up as {client.user} — {count} tracks in the library")
    if VOICE_CHANNEL_ID:
        channel = client.get_channel(VOICE_CHANNEL_ID)
        if isinstance(channel, discord.VoiceChannel):
            vc = await channel.connect()
            _ensure_playlist()
            if _playlist:
                _play_next(vc)
            print(f"auto-joined {channel.name} — broadcasting")
        else:
            print(f"VOICE_CHANNEL_ID {VOICE_CHANNEL_ID} is not a reachable voice channel")


@tree.command(name="join", description="Start the radio in the station's voice channel.", guild=guild)
async def join(interaction: discord.Interaction) -> None:
    channel = client.get_channel(VOICE_CHANNEL_ID) if VOICE_CHANNEL_ID else None
    if not isinstance(channel, discord.VoiceChannel):
        voice = getattr(interaction.user, "voice", None)  # no configured channel → use the caller's
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
    if not vc.is_playing():
        _play_next(vc)
    await interaction.response.send_message(
        f"Radio on in {channel.name} — {len(_playlist)} tracks.", ephemeral=True
    )


@tree.command(name="skip", description="Skip to the next track.", guild=guild)
async def skip(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()  # fires the after-callback, which advances
        await interaction.response.send_message("Skipped.", ephemeral=True)
    else:
        await interaction.response.send_message("Nothing playing.", ephemeral=True)


@tree.command(name="nowplaying", description="Show the current track.", guild=guild)
async def nowplaying(interaction: discord.Interaction) -> None:
    msg = f"Now playing: {current_track}" if current_track else "Nothing playing."
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="leave", description="Stop the radio and leave.", guild=guild)
async def leave(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("Left.", ephemeral=True)
    else:
        await interaction.response.send_message("Not in a channel.", ephemeral=True)


if __name__ == "__main__":
    client.run(TOKEN)
