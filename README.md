# mercuryradio

A small private Discord server where a few friends hear the **same stream at the same time**,
rate the song playing now on a live colored sidebar, and the next block is chosen from the
ratings of whoever is in the voice channel. The "room" half of the shasradio/mercury radio
projects — a voice-channel bot is the shared Icecast mount, reborn; the VC member list is the
listener set.

The code ships **no music** — you point it at your own library. Copyright posture of what you
stream is yours to hold.

## Status: Phase 4 — now-playing card with live ratings

On startup the bot scans `MUSIC_DIR` into a SQLite library (tags via mutagen) and auto-joins its
configured voice channel. It streams a shuffled walk of the library **only while a human is in the
channel**. For each track it posts a **now-playing card** — album art, the track, five rating
buttons (hate/dislike/shrug/like/love) and a **live colored sidebar** showing each present member's
rating in its color — and sets its Discord presence to the track. Ratings live in the DB, seeded
from Plex ★. Commands: `/join`, `/skip`, `/leave`.

Roadmap: (1) ✅ stream one file · (2) ✅ continuous shuffle · (3) ✅ persistence + ratings ·
(4) ✅ now-playing card + rating buttons · (5) selection engine over VC members · (6) chat +
requests · someday: package for open source.

Ratings are per-user-per-track on an asymmetric scale that punishes the veto: hate −4, dislike −1,
shrug 0, like +1, love +2.

## Config

Copy `.env.sample` to `.env` and set:

- `DISCORD_TOKEN` — bot token from the Discord Developer Portal.
- `GUILD_ID` — your server's id.
- `VOICE_CHANNEL_ID` — the channel the station broadcasts in; the bot auto-joins it on startup.
  Leave blank to make `/join` use the caller's current channel instead.
- `MUSIC_DIR` — path to your music library (scanned recursively for `.flac .mp3 .m4a .aac .ogg
  .opus .wav .wma`).
- `DB_PATH` / `DATA_DIR` — where the SQLite DB lives (local default `./data`).

Invite the bot with the `bot` + `applications.commands` scopes and the **View Channels, Send
Messages, Embed Links, Connect, Speak** permissions.

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

## Run in Docker (alongside Plex/Tautulli/Transmission)

No published ports — it only needs Discord egress, a read-only mount of your library, and a
writable `/data` dir for the SQLite DB. Set the values in `.env`, then:

```
docker compose up -d --build
```

Or point Portainer at this repo as a stack (set `DISCORD_TOKEN`, `GUILD_ID`, `VOICE_CHANNEL_ID`,
`MUSIC_DIR`, `DATA_DIR` in the stack environment; `deploy/compose.nas.yaml` is the image-based
variant). The image bundles ffmpeg + libopus; `MUSIC_DIR` mounts read-only at `/music`, `DATA_DIR`
at `/data`.
