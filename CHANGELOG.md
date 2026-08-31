# Changelog

Notable changes to Mercury Radio, newest first. Each deployment gets an entry.

## 2026-08-31

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
