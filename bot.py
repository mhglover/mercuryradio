"""mercuryradio — Phase 2: continuous block radio.

The bot joins a voice channel and streams a shuffled walk of a music library,
chaining track to track so it never stops. `/skip` jumps to the next track.
This is the flat-shuffle foundation the selection engine (Phase 4) will replace
with rating-scored block composition.

Config comes from the environment (see .env.sample). No paths or tokens live in
this file.
"""

import os
import random
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
MUSIC_DIR = os.environ["MUSIC_DIR"]

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma"}

# -vn drops any embedded cover-art video stream; audio only.
FFMPEG_OPTS = {"options": "-vn"}

# Single-guild state (this bot serves one private server).
_playlist: list[str] = []
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


def _load_library() -> list[str]:
    files = [str(p) for p in Path(MUSIC_DIR).rglob("*") if p.suffix.lower() in AUDIO_EXTS]
    random.shuffle(files)
    return files


def _play_next(vc: discord.VoiceClient) -> None:
    """Play the next track; reshuffle and wrap when the list is exhausted.
    Runs both at /join and from the after-callback (a worker thread) — calling
    vc.play() from there is the supported discord.py chaining pattern.
    """
    global _pos, current_track
    if not vc.is_connected() or not _playlist:
        return
    if _pos >= len(_playlist):
        random.shuffle(_playlist)
        _pos = 0
    path = _playlist[_pos]
    _pos += 1
    current_track = Path(path).stem
    source = discord.FFmpegPCMAudio(path, **FFMPEG_OPTS)
    vc.play(source, after=lambda err: _after(vc, err, path))


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
    _ensure_opus()
    await tree.sync(guild=guild)
    print(f"mercuryradio up as {client.user}")


@tree.command(name="join", description="Join your voice channel and start the radio.", guild=guild)
async def join(interaction: discord.Interaction) -> None:
    global _playlist, _pos
    voice = getattr(interaction.user, "voice", None)
    if not voice or not voice.channel:
        await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        return
    vc = interaction.guild.voice_client
    if vc:
        await vc.move_to(voice.channel)
    else:
        vc = await voice.channel.connect()
    if not _playlist:
        _playlist = _load_library()
        _pos = 0
    if not _playlist:
        await interaction.response.send_message(f"No audio files under {MUSIC_DIR}.", ephemeral=True)
        return
    if not vc.is_playing():
        _play_next(vc)
    await interaction.response.send_message(
        f"Radio on in {voice.channel.name} — {len(_playlist)} tracks.", ephemeral=True
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
