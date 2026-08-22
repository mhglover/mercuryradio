# shasradio-discord

A small private Discord server where a few friends hear the **same stream at the same time**,
rate the song playing now on a live colored sidebar, and the next block is chosen from the
ratings of whoever is in the voice channel. The "room" half of the shasradio/mercury radio
projects — a voice-channel bot is the shared Icecast mount, reborn; the VC member list is the
listener set.

The code ships **no music** — you point it at your own library. Copyright posture of what you
stream is yours to hold.

## Status: Phase 2 — continuous radio

The bot joins a voice channel and streams a shuffled walk of your library, chaining track to
track so it never stops. Commands: `/join`, `/skip`, `/nowplaying`, `/leave`.

Roadmap: (1) ✅ join + stream one file · (2) ✅ continuous shuffle + `/skip` · (3) ratings + live
sidebar · (4) selection engine over VC members · (5) requests + chat · (6) polish.

## Config

Copy `.env.sample` to `.env` and set:

- `DISCORD_TOKEN` — bot token from the Discord Developer Portal.
- `GUILD_ID` — your server's id.
- `MUSIC_DIR` — path to your music library (scanned recursively for `.flac .mp3 .m4a .aac .ogg
  .opus .wav .wma`).

Invite the bot with the `bot` + `applications.commands` scopes and the **View Channels, Send
Messages, Embed Links, Connect, Speak** permissions.

## Run locally

Needs `ffmpeg` (and libopus) on PATH.

```
cp .env.sample .env      # fill in the three values
uv run bot.py
```

Then `/join` from a voice channel.

## Run in Docker (alongside Plex/Tautulli/Transmission)

No published ports — it only needs Discord egress and a read-only mount of your library. Set the
three values in `.env`, then:

```
docker compose up -d --build
```

Or point Portainer at this repo as a stack (set `DISCORD_TOKEN`, `GUILD_ID`, `MUSIC_DIR` in the
stack environment). The image bundles ffmpeg + libopus; the container mounts `MUSIC_DIR` read-only
at `/music`.
