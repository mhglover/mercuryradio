# mercuryradio

[![ci](https://github.com/mhglover/mercuryradio/actions/workflows/ci.yml/badge.svg)](https://github.com/mhglover/mercuryradio/actions/workflows/ci.yml)

A small private Discord server where a few friends hear the **same stream at the same time**,
rate the song playing now on a live colored sidebar, and the next block is chosen from the
ratings of whoever is in the voice channel. The "room" half of the shasradio/mercury radio
projects — a voice-channel bot is the shared Icecast mount, reborn; the VC member list is the
listener set.

The code ships **no music** — you point it at your own library. Copyright posture of what you
stream is yours to hold.

**Want to run your own instance, with your own library?** See **[SETUP.md](SETUP.md)** — a
step-by-step host-your-own walkthrough (create the bot, invite it, configure, run, `/setup`).

## How it works

On startup the bot loads its SQLite library (or scans `MUSIC_DIR` into it on first run, tags via
mutagen) and, for each configured server, joins the voice channel **only while a human is in it** —
it follows the room, leaving when the channel empties. While it plays, it posts a **now-playing
card** in the server's card channel: album art, the track, five rating buttons
(hate/dislike/shrug/like/love), and a **live colored sidebar** showing each present member's rating
in its color.

The next track is not a shuffle. A **selection engine** (`engine.py`) composes each block by
scoring tracks on `SUM(rating)` over whoever is present right now — mixing loved tracks, net
positives, unrated-by-present, and a random-from-unrated wildcard — with a play-timeout and an
artist back-to-back guard. Requests jump the queue. Ratings are per-user-per-track on an asymmetric
scale that punishes the veto: **hate −4 · dislike −1 · shrug 0 · like +1 · love +2.**

## Where it comes from

This is the third build of one idea, over twenty years.

**shasradio** (~2000–2004) was a Perl streaming-radio site — an Icecast mount a handful of friends
tuned into at once. A CGI page (`ratebox.pl`) gave each song five buttons and a live colored
sidebar, and every half hour the station rebuilt its next block **from the ratings of whoever was in
the room at that moment**: a request, a song nobody present had heard, a few the room rated highly.
The scale already punished the veto — love +2, **hate −4**, one hate cancelling two loves — so a
single person could keep the room off a song they couldn't stand.

**mercury** (2024) rebuilt the engine in Python for Spotify. It kept the collective heart — the next
queue is still `SUM(rating)` over everyone active, the same query shasradio ran in 2002 — but each
listener heard it through their own Spotify player. What it dropped was the *room*: no shared audio,
no chat, no live sidebar, no seeing what everyone else thought of the song playing right now.

**mercuryradio** (2026) puts the room back, on Discord. A bot in a voice channel **is** the shared
stream — the Icecast mount reborn — so everyone hears the same song at the same second. The voice
channel's member list is the listener set; the now-playing card carries the five buttons and the
live colored sidebar; the next block is scored over whoever's present. Two decades of ratings came
along: the original shasradio ratings were restored, so a listener's twenty-year-old taste can greet
them the first time they walk in.

The invariant across all three: **selection is collective, computed from the room. Take the
listeners away and the score doesn't exist.**

## Commands

- `/join` — start the radio in this server's voice channel (or just join the channel; it follows you in).
- `/skip` — skip the current track.
- `/request <track>` — queue a track to play next; autocompletes over the library. One pending
  request per person — a new one replaces yours and goes to the back of the queue.
- `/add <file>` — add an audio file to the shared library. It's tagged, saved, and immediately
  requestable. `MUSIC_DIR` stays read-only; uploads land in a separate writable ingest dir
  (`ADDED_DIR`, defaulting inside `/data` so no extra mount is needed).
- `/youtube <url> [artist:] [title:]` — pull audio from a YouTube (or other yt-dlp-supported) link
  into the library. A fast metadata pre-fetch names the track and rejects an over-long video before
  downloading; tags come from the source's own artist/track, else the video title split on `" - "`,
  and the optional `artist:`/`title:` params override either. Downloaded off the audio loop, capped
  at `MAX_ADD_MINUTES` (default 20), lands in `ADDED_DIR` like `/add`. The "Added" lands publicly in
  the channel; the interim and any errors are shown only to you.
- `/this [comment]` — say something about the song playing now; it posts anchored to the track.
- `/recent` — show the last few played tracks and rate one you didn't click while it played.
- `/bug <text>` — file a bug report straight into the database, stamped with the time, you, and
  the track playing right now.
- `/update` — **(bot owner)** restart onto the newest published release: finishes the song,
  pulls, comes back and posts its release notes. Needs the watchtower sidecar — see
  `deploy/compose.nas.yaml`.
- `/promo <track>` — **(admin)** set a station-ID clip played once when the radio wakes;
  `/promo` shows it, `/promo clear:True` removes it.
- `/setup` — **(admin)** register this server's voice + card channel — see multi-tenant below.
- `/leave` — stop this server's radio.

## Status

Streaming, the rating card, the selection engine, requests, listener uploads, multi-tenant (one
process serves many servers), and Plex/shasradio rating import are all in and running daily.
The repo is public: CI runs the test suite on every push, releases are tagged with notes in
`CHANGELOG.md`, and every release publishes a ready-to-run multi-arch image to
`ghcr.io/mhglover/mercuryradio` — see [SETUP.md](SETUP.md) to host your own.

## Config

Copy `.env.sample` to `.env` and set:

- `DISCORD_TOKEN` — bot token from the Discord Developer Portal.
- `MUSIC_DIR` — path to your music library (scanned recursively for `.flac .mp3 .m4a .aac .ogg
  .opus .wav .wma`).
- `DB_PATH` / `DATA_DIR` — where the SQLite DB lives (local default `./data`).
- `ADDED_DIR` — **optional**, writable ingest dir for `/add` and `/youtube` files; defaults to an
  `added/` subdir next to the DB, so it works with no extra mount. Point it at a writable folder
  **inside your library** (mount that subfolder rw and set `ADDED_DIR` to it — see
  `deploy/compose.nas.yaml`) to make added tracks part of the collection on one canonical path.
- `MAX_ADD_MINUTES` — **optional**, cap on a `/youtube` pull (default 20). Longer videos are rejected
  at the pre-fetch, before any download.
- `GUILD_ID` / `VOICE_CHANNEL_ID` / `NOWPLAYING_CHANNEL_ID` — **optional**, seed-only (see below).

Invite the bot with the `bot` + `applications.commands` scopes and the **View Channels, Send
Messages, Embed Links, Add Reactions, Connect, Speak, Manage Channels** permissions.

## Multiple servers (multi-tenant)

One process, one bot token, many servers. Per-server config (voice + card channel) lives in the
DB, not env — **add a server by running `/setup` in it** (needs Manage Server): pick the voice
channel to stream in and, optionally, a text channel for the now-playing card. The bot joins and
starts serving that server immediately — no redeploy, no second token.

**Ratings and the track library are shared** across every server, so a listener's taste follows
them wherever they are. Requests and presence-by-rating are scoped per server.

For a simple single-server deploy you can skip `/setup` and set `GUILD_ID` / `VOICE_CHANNEL_ID` /
`NOWPLAYING_CHANNEL_ID` in env instead — they seed the first server row on a fresh DB.

## Seed ratings from Plex ★ (optional, one-shot)

Import your existing Plex star ratings into the library. Positive-only mapping (Plex 0–10):
`≥9`→love, `7–8`→like, `5–6`→shrug, `<5` skipped. Reads a copy of the Plex DB only (no token):

```
python seed_plex.py --plex-db /path/to/com.plexapp.plugins.library.db --owner <your_discord_user_id>
```

Run the bot at least once first so the library table is populated. `python test_seed.py` checks the
mapping.

## Run locally

Needs `ffmpeg` (and libopus) on PATH.

```
cp .env.sample .env      # fill it in
uv run bot.py
```

With `VOICE_CHANNEL_ID` set the bot auto-joins and starts; otherwise `/join` from a voice channel.

## Run in Docker

No published ports — it only needs Discord egress, a read-only mount of your library, and a
writable `/data` dir for the SQLite DB.

**Prebuilt image** (published by CI, multi-arch, anonymous pull): `deploy/compose.nas.yaml`
pulls `ghcr.io/mhglover/mercuryradio:latest` — deploy it with compose or as a Portainer stack
with the env values set in the stack's Environment. Pin `MERCURYRADIO_TAG` to a release tag to
hold or roll back a version.

**Or build from the checkout** — set the values in `.env`, then:

```
docker compose up -d --build
```

The image bundles ffmpeg + libopus and installs Python dependencies from `uv.lock` (one source
of truth with the repo); `MUSIC_DIR` mounts read-only at `/music`, `DATA_DIR` at `/data`.
