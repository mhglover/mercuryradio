# Design (future): per-tenant bot identities in one process

Status: **not built.** Current production is "model A" — one shared bot identity
(one `Client`, one token) serving many servers, config in the `guilds` table,
`/setup` to onboard. This document specifies "model B" for if/when a distinct bot
*per server* is wanted. Adopt it only when the shared identity is the thing that
falls short; model A is simpler and is enough for one friend group across a few
servers.

## When to adopt

Move to model B if any of these become true:

- Each server should see a **distinct bot** (its own name/avatar), not one shared
  "mercurybot" — e.g. servers run by people you don't control, or per-server branding.
- You want **per-server status** — the bot's "listening to X" reflecting *that*
  server's track. (Model A's status is global: one per bot, so it shows whichever
  server updated last. Model B fixes this for free — each `Client` has its own presence.)
- You want **per-tenant token rotation / isolation** — rotating or losing one
  server's token without touching the others; a ban on one identity not taking the rest down.

## The key fact

One process **can** run multiple `discord.py` `Client` objects, each with its own
token and its own gateway connection, on the same event loop:

```python
async def main():
    clients = [make_client(t) for t in tenants]          # one Client per tenant token
    async with contextlib.AsyncExitStack() as stack:
        for c in clients:
            await stack.enter_async_context(c)
        await asyncio.gather(*(c.start(c._token) for c in clients))
```

So "per-tenant token" and "one stack" are compatible. This corrects an earlier
wrong claim that one process needs a single token.

## Data model

Extend the tenant config (today's `guilds` table) with a token, or — preferred for
hygiene — keep tokens in a **separate secrets store**, not the shared ratings DB:

- `tenants(guild_id PK, voice_channel_id, nowplaying_channel_id, music_dir, enabled, added)`
  — same as today's `guilds`.
- Token storage, one of:
  1. **Separate secrets DB** `secrets(guild_id PK, token)` in its own file (path via
     env, gitignored). **Recommended.** Keeps tokens out of every ratings-DB copy
     (backups, dry-run copies, DBs shared to inspect ratings).
  2. A `token` column on `tenants` in the shared DB — simplest, but **every token
     then travels with every copy of the ratings DB.** Only acceptable if no
     untrusted copy of that DB is ever made.

Bootstrapping: read tokens in `__main__` before starting the clients; fall back to
`DISCORD_TOKEN` env for a single default tenant.

## What is shared vs per-tenant

| Shared (one instance, underneath all clients) | Per-tenant (one per `Client`) |
|---|---|
| SQLite DB — **ratings + library + play_history** | the `Client` object + its gateway/token |
| the selection engine (`engine.py`) | its voice connection (`guild.voice_client`) |
| the track library / `MUSIC_DIR` (unless per-tenant `music_dir`) | Discord **presence/status** |
| the event loop | its **command tree** (`app_commands.CommandTree(client)`) |
| `GuildRadio` playback state (still per guild) | event handlers (`on_ready`, `on_voice_state_update`, …) |

Ratings staying shared is the whole point — a listener's taste follows them across
servers regardless of which bot identity fronts each one. Requests and
presence-by-rating are already guild-scoped and stay so.

## The refactor from model A

Model A funnels everything through one module-level `client` and one `tree`. Model B
needs those to be per-tenant:

1. **A `Tenant`/`Radio` owns its `Client`.** Today's `GuildRadio` gains a reference
   to the `Client` serving its guild. Anywhere the code calls `client.get_channel`,
   `client.change_presence`, or `tree.sync`, it uses *that tenant's* client/tree.
2. **Event handlers register per `Client`.** `on_ready`, `on_voice_state_update`,
   `on_guild_join` are attached to each client (a factory that closes over the
   tenant). `on_ready` for a client serves only that client's guild(s).
3. **Command tree per client.** Each `Client` gets its own `CommandTree`; the same
   command callbacks are registered on each. `_radio(interaction.guild_id)` still
   resolves the right state.
4. **Boot** reads the tenants table, creates a `Client` per token, wires handlers,
   and `asyncio.gather`s their `start()`. SIGTERM handling and opus load happen once
   for the process; the off-air announce iterates every client's active radio.
5. **Onboarding** can no longer be pure `/setup`-by-invite: a bot app + token must
   be created in the Discord dev portal (a human step; a bot can't mint its own
   identity). Flow: create app + token → store it (secrets store) + the channel
   config → the process spins up a new `Client` for that tenant (either on restart,
   or hot, by starting a new client task). `/setup` still captures the *channel*
   config inside the new server once its bot is in.

## Concurrency

All clients share one event loop (`asyncio.gather` on the same loop), so the
db-access-on-the-loop-thread discipline is unchanged — every `_advance`,
`record_play`, rating write runs on that single loop. Voice playback still uses a
worker thread per stream whose after-callback hops back with
`run_coroutine_threadsafe`. One shared `conn` used from the single loop thread stays
correct. (If clients were ever split across threads/loops, the DB access model would
have to change — don't.)

## Tradeoffs vs model A

| | Model A (built) | Model B (this doc) |
|---|---|---|
| Bot identity | one shared, every server | distinct per server |
| Status/presence | global (shows latest track) | per server ✓ |
| Tokens | one | one per tenant |
| Onboarding | invite + `/setup` | create app+token, then `/setup` |
| Token rotation | rotate the one | per tenant, isolated |
| Blast radius of a ban/leak | all servers | one server |
| Code | one client/tree | client/tree per tenant |
| Gateway connections | 1 | N |

## Migration path (keep model A working throughout)

1. Add the secrets store + a `token` lookup; **default tenant still reads env** — no
   behavior change.
2. Introduce the `Client` factory and run the *single* current tenant through it
   (one client, same as today) — proves the multi-client harness with N=1.
3. Move `client`/`tree` references behind the per-tenant object.
4. Flip boot to iterate tenants and `gather` their clients.
5. Add the create-app/token onboarding step.

Steps 1–3 are safe refactors that leave model A running; only step 4 changes runtime
behavior.

## Open questions

- **Per-tenant library** (`music_dir` column exists, unused) — do tenants share one
  library or get their own? Shared is current; per-tenant means N scans + N track
  namespaces (ratings keyed by tags still cross-match).
- **Resource ceiling** — N gateway connections + N voice streams in one process;
  fine for a handful, revisit past ~dozens.
- **Secrets store choice** — separate DB file vs Docker secret vs a KMS; the
  separate-file approach is the lightest that keeps tokens out of ratings-DB copies.
