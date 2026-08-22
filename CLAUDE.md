# mercuryradio — agent orientation

A private Discord where friends hear the **same stream** and rate the song playing now on a live
colored sidebar; the next block is picked from the ratings of whoever is in the voice channel.
The "room" half of the shasradio/mercury radio projects. See `README.md` for user-facing status,
config, and run/deploy.

## Where the rest lives
- **Full build plan:** `~/.claude/plans/mercuryradio.md` — architecture, all six phases, verification.
- **Project record** (status, decisions, log): `~/Personal/Notes/mercuryradio.md`. Update it at
  milestones — this repo doesn't carry that history.
- **Ported-from:** `~/devel/mercury` is reference logic for the Phase 4 engine (port the algorithm,
  don't import it — mercury is Spotify/Postgres-bound). Key lines: `queue_manager.py:17-102` (block
  loop), `blocktypes.py:50,124` (pickers), `raters.py:235` (skip→rating).

## Phase status
1. ✅ join + stream one file · 2. ✅ continuous shuffle + `/skip` · 3. ⬜ ratings (5 buttons + live
colored sidebar, SQLite) · 4. ⬜ selection engine over VC members (artist guard, 10-day timeout,
wildcard = random-from-unrated) · 5. ⬜ requests + chat · 6. ⬜ polish. Rating scale is asymmetric:
hate −4 · dislike −1 · shrug 0 · like +1 · love +2.

## Hard rules
- **Ships no music, no secrets.** `media/` and `.env` are gitignored. Config is env-only
  (`DISCORD_TOKEN`, `GUILD_ID`, `MUSIC_DIR`). Never commit a path or token — the repo may be
  open-sourced.
- **Commits:** personal git identity (`matthew@harrisglover.com`), no Claude attribution trailer,
  DCO `Signed-off-by` fine. Stage by name, not `git add -A`.
- **Verify for real, not by import.** `python -c "import bot"` catches syntax, nothing else. Voice
  bugs only surface on `vc.play` — run a real `ffmpeg` decode of a file and a `docker build` before
  claiming it works. (Both the opus-load and the ffmpeg-flag bugs got through import checks.)
- **Lean.** No dep added for what a few lines do; no mutagen until the engine needs tags.

## Run / test
- Local: `.env` with `MUSIC_DIR` pointed at a folder, then `uv run bot.py`.
- Docker: `docker compose up -d --build` (image bundles ffmpeg + libopus; no published ports; reads
  library read-only at `/music`). Runs alongside Plex/Tautulli/Transmission on the NAS.
