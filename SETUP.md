# Hosting your own Mercury Radio

A walkthrough for running your own instance on your own server, with your own music
library. No prior experience with this bot needed; some comfort with a terminal helps.

If instead you just want the existing bot to play **in your server** (from the host's
library), you don't need any of this — the host invites the bot and runs `/setup`. This
guide is for running a **separate instance you control**.

## What you'll need

1. **A machine that stays on** — a home server, a NAS that runs Docker, a small VPS, or a
   spare always-on computer. It needs outbound internet (to reach Discord); it needs **no
   open ports**.
2. **Docker** installed on it (Docker Desktop, or Docker Engine on Linux/NAS).
3. **A folder of music** it can read — `.flac .mp3 .m4a .aac .ogg .opus .wav .wma`. The bot
   plays *your* files; it ships none.
4. **A Discord account** with permission to add a bot to the server you want it in (the
   "Manage Server" permission there).

## Step 1 — Create the bot and get its token

1. Go to the **Discord Developer Portal**: https://discord.com/developers/applications
2. **New Application** → give it a name (this is what shows in the member list) → Create.
3. Left sidebar → **Bot** → **Add Bot** → Yes.
4. Under the bot's username, **Reset Token** → **Copy**. This is your `DISCORD_TOKEN`.
   🔒 Treat it like a password — anyone with it controls the bot. Don't paste it in chat or
   commit it to git.
5. Scroll down to **Privileged Gateway Intents** — you can leave all three **off**. This bot
   runs on default intents; it does not need the Message Content or Members intents.

## Step 2 — Invite the bot to your server

1. Left sidebar → **OAuth2** → **URL Generator**.
2. Under **Scopes**, tick **`bot`** and **`applications.commands`**.
3. Under **Bot Permissions**, tick: **View Channels, Send Messages, Embed Links, Connect,
   Speak, Manage Channels**.
4. Copy the generated URL at the bottom, open it in your browser, pick your server, Authorize.

The bot is now in your server but idle — it starts serving once you configure and run it
(Step 5) and run `/setup` (Step 6).

## Step 3 — Get the code

```
git clone https://github.com/mhglover/mercuryradio.git && cd mercuryradio
```

## Step 4 — Configure

Copy the sample env file and fill it in:

```
cp .env.sample .env
```

Set at least:

- `DISCORD_TOKEN` — the token from Step 1.
- `MUSIC_DIR` — the **host path** to your music folder (it mounts read-only inside the
  container). In Docker this is the folder on the machine, e.g. `/srv/music`.
- `DATA_DIR` — a **host folder** for the bot's small SQLite database, e.g. `./data`. This is
  where ratings and per-server config live; keep it and your history survives restarts.

Optional but useful:

- `ADDED_DIR` — where `/add` and `/youtube` save new tracks. Point it at a writable folder
  **inside your library** to make added songs part of the collection (see
  `deploy/compose.nas.yaml` for the mount pattern). Defaults to an `added/` subfolder next to
  the DB if you leave it unset.
- `MAX_ADD_MINUTES` — cap on a `/youtube` pull (default 20).

You can leave `GUILD_ID` / `VOICE_CHANNEL_ID` / `NOWPLAYING_CHANNEL_ID` blank — you'll add
your server with `/setup` in Step 6 instead.

## Step 5 — Run it

```
docker compose up -d --build
```

Watch the logs the first time:

```
docker compose logs -f
```

On a fresh library it scans your files once (this can take a minute for a big library), then
prints `mercuryradio up as <botname> — N tracks`. After that, restarts are quick.

## Step 6 — Point it at your channels

In your Discord server, run **`/setup`** (you need Manage Server):

- pick the **voice channel** the radio should stream in,
- optionally pick a **text channel** for the now-playing card.

The bot joins and starts as soon as a person is in the voice channel — it follows the room,
leaving when the channel empties.

## Using it

- **Join the voice channel** and the radio wakes up. Rate the now-playing card with the five
  buttons — the next block is scored from the ratings of whoever's present.
- **`/request <track>`** — queue a track (autocompletes over your library).
- **`/add <file>`** — upload an audio file into the library.
- **`/youtube <url>`** — pull audio from a link into the library.
- **`/recent`**, **`/rate`**, **`/myratings`** — rate tracks you missed / by name / review yours.
- **`/perms`** (admin) — restrict who can `/add` and `/youtube`, or hide those commands until
  you turn them on. New servers start with adding **off**.
- **`/help`** — the in-Discord version of all this.

### Optional: seed ratings from Plex

If you have a Plex library, you can import your existing star ratings so the room starts with
taste. See the "Seed ratings from Plex" section in `README.md`.

## Sharing one library across servers

One running instance can serve **many** Discord servers from a single process and a single
bot token — add each with `/setup` in that server. Ratings and the library are **shared**
across them (a listener's taste follows them); requests and presence are per-server. If you'd
rather each server have its own bot identity, that's a bigger change — see
`docs/multi-client-tenants.md`.

## Troubleshooting

- **Slash commands don't appear** — they register per-server on join; give it a minute, or
  re-invite with both the `bot` and `applications.commands` scopes (Step 2).
- **Bot won't play / "libopus" error** — the Docker image bundles ffmpeg + libopus, so this
  only bites a non-Docker run; install libopus (`apt install libopus0` / `brew install opus`).
- **Renamed a channel and it stopped** — run `/setup` again to re-point it.
- **A track plays speeding up / slowing down** — usually the machine was busy (e.g. a library
  rescan). If it persists, the file itself may have corrupt frames; re-encode it.
