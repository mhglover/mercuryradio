# Changelog

Notable changes to Mercury Radio, newest first. Each deployment gets an entry.

## 2026-08-31

- **`/update` — tell the bot to restart on the newest release** (bot owner only). It
  finishes the current song, pulls the new image, and comes back announcing its own
  release notes. Needs the `watchtower` sidecar from `deploy/compose.nas.yaml` and a
  `WATCHTOWER_TOKEN` in the stack env; unconfigured, the command says how to set it up.

- **Station-ID promo on wake.** `/promo track:` (admin) picks a library track — `/add` or
  `/youtube` your clip in first — and the radio plays it once when it wakes, before the
  first song. `/promo` shows the current one, `/promo clear:True` removes it. Per server;
  the clip isn't ratable and doesn't hit play history.
- **Request fairness: one pending request each.** A new `/request` replaces your pending
  one and goes to the back of the queue — changing your mind costs your spot, and nobody
  can stack the queue. The reply says what got swapped.
- **A time bar on the card.** Under the album: how far into the song the room is and how
  long it runs, as of the card's last redraw — it refreshes whenever someone rates or the
  track changes. (Deliberately not live-ticking; Discord rate-limits per-second edits.)
  Tracks scanned before durations were recorded just don't show one.
- **The card resurfaces when it gets buried.** If five or more chat messages have landed
  since the now-playing card last moved, the next track change reposts it at the bottom of
  the channel instead of editing it in place — no more scrolling back up to find the
  buttons after a conversation. Quiet channels keep the edit-in-place behavior.
- **Release notes on update.** When the bot boots on a new version, it posts that
  release's notes (this file's newest section) to each server's card channel — silently,
  once per release. So the room finds out what changed without anyone saying so.
- **`/rate` now defaults to the song playing.** Leave the track blank and your rating lands
  on the current song — no card required. Naming a track still works exactly as before.
- **`/bug` — file a bug report from inside Discord.** It lands in the database stamped with
  the time, the reporter, and the track playing at that moment, and prints a timestamped
  `[bug]` marker into the server log so a report made during a glitch lines up with the
  pacing instrumentation. The ack is private to you.

- **Fixed the card reverting to an earlier track when someone rates.** The rating refresh
  rebuilt the card from a snapshot of the message taken when the card was first posted, so
  after a track change (or a skip) any rating snapped the card back to the old song — and
  made the rating look like it landed on the wrong track. The card is now rebuilt from the
  actual now-playing state on every refresh. (Ratings themselves always went to the current
  track; only the display lied.)
- **CI + published images + release tags.** Every push runs the test suite on GitHub
  Actions; pushes to main publish a multi-arch image to
  `ghcr.io/mhglover/mercuryradio:latest`, and `v*` release tags publish a matching
  image tag (roll back by pinning an older tag in the stack). The Dockerfile now
  installs from `uv.lock`, so the image and a local checkout can't drift.

## 2026-08-30

- **Fixed the speed-up/slow-down artifact.** Playback now reads ahead into a buffer and
  **prefetches the next track** a few seconds before the current one ends, so ffmpeg's
  startup no longer makes the player rush to catch up. Track changes are near-seamless, and
  skips to an already-prefetched track are instant. A rare buffer underrun inserts a tiny
  silence instead of speeding up.
- **The now-playing card no longer pings the channel.** It posts as a silent message,
  so a song change never sends a notification to people who aren't listening.
- **Fixed "sticky" now-playing cards.** The card is now a single message edited in place
  each track (instead of delete-and-repost), so it can't be left orphaned in the channel —
  and it reposts itself if the card gets deleted.
- **New `/retag` command** — fix a track's artist/title (handy when a `/youtube` add picks
  up the channel name as the artist). Keeps the track's ratings and play history.
- **Host your own:** the repository is now public and ships a step-by-step
  [SETUP.md](SETUP.md).
- Internal: an opt-in playback-pacing diagnostic (`PACE_DEBUG`) for chasing the
  speed-up/slow-down artifact.
