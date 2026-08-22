"""shasradio-discord — Phase 1: a bot that joins a voice channel and streams one
file, so two people hear the same audio at the same position. Proves the
shared-stream premise before anything else gets built.

Config comes from the environment (see .env.sample). No paths or tokens live in
this file.
"""

import os

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
TEST_FILE = os.environ["TEST_FILE"]

# -vn drops the embedded cover-art video stream; audio only.
FFMPEG_OPTS = {"options": "-vn"}

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


intents = discord.Intents.default()
intents.voice_states = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
guild = discord.Object(id=GUILD_ID)


def _play(vc: discord.VoiceClient) -> None:
    """Play TEST_FILE, looping when it ends so late joiners still hear it."""
    source = discord.FFmpegPCMAudio(TEST_FILE, **FFMPEG_OPTS)
    vc.play(source, after=lambda err: _play(vc) if not err and vc.is_connected() else None)


@client.event
async def on_ready() -> None:
    _ensure_opus()
    await tree.sync(guild=guild)
    print(f"shasradio up as {client.user}")


@tree.command(name="join", description="Join your voice channel and start the stream.", guild=guild)
async def join(interaction: discord.Interaction) -> None:
    voice = getattr(interaction.user, "voice", None)
    if not voice or not voice.channel:
        await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        return
    vc = interaction.guild.voice_client
    if vc:
        await vc.move_to(voice.channel)
    else:
        vc = await voice.channel.connect()
    _play(vc)
    await interaction.response.send_message(f"Streaming in {voice.channel.name}.", ephemeral=True)


@tree.command(name="leave", description="Stop the stream and leave.", guild=guild)
async def leave(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("Left.", ephemeral=True)
    else:
        await interaction.response.send_message("Not in a channel.", ephemeral=True)


if __name__ == "__main__":
    client.run(TOKEN)
