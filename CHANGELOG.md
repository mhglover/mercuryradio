# Changelog

Notable changes to Mercury Radio, newest first. Each deployment gets an entry.

## Unreleased

- **Fixed the speed-up/slow-down artifact.** Playback now reads ahead into a buffer and
  **prefetches the next track** a few seconds before the current one ends, so ffmpeg's
  startup no longer makes the player rush to catch up. Track changes are near-seamless, and
  skips to an already-prefetched track are instant. A rare buffer underrun inserts a tiny
  silence instead of speeding up.

## 2026-08-30

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
