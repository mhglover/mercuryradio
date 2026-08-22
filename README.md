# shasradio-discord

A small private Discord server where a few friends hear the **same stream at the same time**,
rate the song playing now on a live colored sidebar, and the next block is chosen from the
ratings of whoever is in the voice channel. The "room" half of the shasradio/mercury radio
projects — a voice-channel bot is the shared Icecast mount, reborn; the VC member list is the
listener set.

The code ships **no music** — you point it at your own library. Copyright posture of what you
stream is yours to hold.

## Status: Phase 1

Bot joins a voice channel and streams one configured file, so two people can confirm they hear
the same audio at the same position. That's the whole premise; the rest builds on it.

Roadmap: (1) join + stream one file · (2) continuous shuffle + `/skip` · (3) ratings + live
sidebar · (4) selection engine over VC members · (5) requests + chat · (6) package/Docker.

## Run

Needs `ffmpeg` on PATH.

```
cp .env.sample .env      # fill in DISCORD_TOKEN, GUILD_ID, TEST_FILE
uv run bot.py
```

The bot needs the **Server Members** and **Voice** privileges and the `applications.commands`
scope when you invite it. Then `/join` from a voice channel; `/leave` to stop.
