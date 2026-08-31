# mercuryradio — agent orientation

A private Discord where friends hear the **same stream** and rate the song playing now on a live
colored sidebar; the next block is picked from the ratings of whoever is in the voice channel.
The "room" half of the shasradio / mercury radio projects. See `README.md` for user-facing status,
config, and run/deploy.

## Architecture at a glance
- `bot.py` — discord.py client, slash commands, and per-guild playback (one `GuildRadio` per
  server): the now-playing card with rating buttons and the live colored sidebar.
- `engine.py` — the selection engine. Scores tracks by `SUM(rating)` over whoever is present, over
  a repeating block, with a play-timeout and an artist back-to-back guard.
- `library.py` — filesystem catalog: walk a music dir, read tags with mutagen, upsert into `tracks`.
- `db.py` — SQLite schema and accessors (tracks / ratings / requests / guilds / presence /
  play_history).
- `seed_plex.py`, `shas_import.py` — optional one-shot rating seeders.

**Multi-tenant:** one process and one bot token serve many servers. Per-server config lives in the
DB `guilds` table — `/setup` adds a server, no redeploy. Ratings and the library are **shared**
(a listener's taste follows them across servers); requests and presence are **per-guild**. Track
identity is the normalized `artist|title|album` (the path is playback only). Rating scale is
asymmetric: hate −4 · dislike −1 · shrug 0 · like +1 · love +2.

## Hard rules
- **Ships no music, no secrets.** `media/` and `.env` are gitignored. All config is env
  (`DISCORD_TOKEN`, `MUSIC_DIR`, `DB_PATH`, optional `ADDED_DIR`; per-server channel ids live in the
  DB). Never commit a path or token — the repo may be open-sourced.
- **Commits:** no Claude attribution trailer; DCO `Signed-off-by` is fine. Stage by name, not
  `git add -A`.
- **Verify for real, not by import.** `python -c "import bot"` catches syntax, nothing else. Voice
  bugs only surface on `vc.play` — decode a real file with `ffmpeg` and run a `docker build` before
  claiming it works.
- **Never pipe a verification.** `pytest | tail` reports tail's exit code, not pytest's — a failing
  suite reads as green and `&&` chains commit/push right past it (bit twice on 2026-08-31 alone).
  Run the check bare, or send full output to a file and read the file; branch on the check's own
  exit status.
- **Lean.** No dependency added for what a few lines do.

## Run / test
- Local: `.env` with `MUSIC_DIR` pointed at a folder, then `uv run bot.py`.
- Docker: `docker compose up -d --build` (the image bundles ffmpeg + libopus; no published ports;
  reads the library read-only at `/music`).
- Tests: `uv run --with pytest pytest` (engine scoring, db behavior, the seeders).
