# Deploying to a second Discord server (Chizat)

Most of the restored shasradio users live on the Chizat Discord. This runs a second
bot there that **shares the ratings DB** with Mercury Base, so their tastes work on
Chizat with no re-import.

## Architecture

- **One image, two stacks.** `mercuryradio` (Mercury Base) and `mercuryradio-chizat`
  run the same `mercuryradio:latest` image with different env.
- **Shared ratings.** Both stacks mount the same host dir at `/data`
  (`DATA_DIR=/volume1/docker/mercuryradio/data`), so there's one `mercuryradio.db`.
  Discord user ids are global, so a user's ratings apply on every server they're in.
- **Per-guild requests.** The `requests` table carries a `guild_id`; each bot only
  serves its own server's queue. Play history / the 10-day timeout are shared (a song
  played on one server won't repeat on the other for 10 days) — acceptable, not a bug.
- **Separate token.** A Discord bot token opens exactly one gateway connection, so the
  two stacks need two different tokens = two Discord applications. (A single-process
  multi-guild bot would avoid the second token but needs a code refactor; separate
  stacks is the lazy path and scales fine to a handful of servers.)

## What you provide (the parts a session can't do)

1. **A second Discord application + bot token** — https://discord.com/developers/applications
   → New Application → Bot → reset/copy token. Enable the **Server Members** and
   **Message Content** intents are NOT required; the bot uses default intents plus
   **voice states** (already coded). No privileged intents needed.
2. **Invite that bot to the Chizat server** with an OAuth2 URL, scopes `bot` +
   `applications.commands`, permissions: View Channels, Connect, Speak, Send Messages,
   Embed Links, Use Slash Commands, and Manage Channels (for the now-playing topic).
3. **Three ids from Chizat** (Developer Mode → right-click → Copy ID):
   - the server (guild) id → `GUILD_ID`
   - the voice channel to stream in → `VOICE_CHANNEL_ID`
   - the text channel for the card → `NOWPLAYING_CHANNEL_ID`

## Deploy steps

1. Image is already on the NAS (same one Mercury Base runs). If it changed, reship:
   `docker build -t mercuryradio:latest . && docker save mercuryradio:latest | ssh nas 'sudo docker load'`
2. In Portainer → Stacks → **Add stack**, name `mercuryradio-chizat`, paste
   `deploy/compose.chizat.yaml`.
3. Set the Environment variables from the two lists above. **`DATA_DIR` must be the
   same** `/volume1/docker/mercuryradio/data` as Mercury Base — that is what shares the ratings.
4. Deploy. Confirm: `ssh nas 'sudo docker logs --tail 5 mercuryradio-chizat'` shows
   `logging in` → `auto-joined … streaming/idle`.

## Verify shared ratings

Once up, a Chizat user who was restored (e.g. Anna/Anarkey, Kurt/Chaosopher) should see
the bot's `top`/`allpos` picks reflect their old tastes the moment they're in the VC —
no seeding needed, because it's the same DB.
