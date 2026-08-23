"""mercuryradio — multi-tenant radio bot.

One process, one bot token, many Discord servers. Each server's config (voice +
card channel) lives in the DB `guilds` table, not env, so a server is added with
`/setup` — no redeploy. Per-server playback state lives in a GuildRadio; ratings
and the track library are shared, so a listener's taste follows them across
servers. Ratings are five buttons + a live colored sidebar on the now-playing
card; the rating-scored engine composes each block over whoever's present.

Config: DISCORD_TOKEN, DB_PATH, MUSIC_DIR from env (see .env.sample). The legacy
single-guild env (GUILD_ID/VOICE_CHANNEL_ID/NOWPLAYING_CHANNEL_ID) is used only
to seed the guilds table on first boot, for backward compatibility.
"""

import asyncio
import io
import os
import signal

import discord
from discord import app_commands
from dotenv import load_dotenv

import db
import engine
import library

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
MUSIC_DIR = os.environ["MUSIC_DIR"]
# Legacy single-guild env — only seeds the guilds table on first boot.
_SEED_GUILD_ID = int(os.environ.get("GUILD_ID") or 0) or None
_SEED_VOICE_ID = int(os.environ.get("VOICE_CHANNEL_ID") or 0) or None
_SEED_NP_ID = int(os.environ.get("NOWPLAYING_CHANNEL_ID") or 0) or None

FFMPEG_OPTS = {"options": "-vn"}

# A rating within this window counts a user as present (for scoring + the sidebar)
# even without joining voice — for listeners sharing one speaker/connection.
PRESENCE_WINDOW_MIN = 30
# On wake (empty VC -> someone joins), start the first song this many seconds in.
WAKE_SEEK_SECONDS = 30
TOPIC_EVERY = 5  # refresh the channel topic once per this many tracks

# (label, rating value, colored square for the sidebar, button style)
RATINGS = [
    ("Hate", db.HATE, "🟥", discord.ButtonStyle.danger),
    ("Dislike", db.DISLIKE, "🟧", discord.ButtonStyle.secondary),
    ("Shrug", db.SHRUG, "⬜", discord.ButtonStyle.secondary),
    ("Like", db.LIKE, "🟩", discord.ButtonStyle.success),
    ("Love", db.LOVE, "💙", discord.ButtonStyle.primary),
]
_SQUARE = {value: square for _, value, square, _ in RATINGS}
UNRATED = "⬛"

conn = None
_loop = None


class GuildRadio:
    """All per-server playback state. One per served guild, keyed by guild id."""

    def __init__(self, guild_id, voice_channel_id, nowplaying_channel_id=None):
        self.guild_id = int(guild_id)
        self.voice_channel_id = int(voice_channel_id) if voice_channel_id else None
        self.nowplaying_channel_id = int(nowplaying_channel_id) if nowplaying_channel_id else None
        self.block = []            # remaining picker types in the current shuffled block
        self.active = False        # True only while a human is in the VC
        self.current_row = None    # the track playing now
        self.current_track = None  # "artist – title" for status
        self.np_message = None     # the live now-playing card
        self.recent_artists = []   # rolling window for the topic
        self.since_topic = 0       # tracks played since the topic was refreshed


radios: dict[int, GuildRadio] = {}


def _radio(guild_id) -> GuildRadio | None:
    """The GuildRadio for a served guild, loading its config from the DB on first
    use. None if the guild isn't set up."""
    gid = int(guild_id)
    if gid in radios:
        return radios[gid]
    row = db.get_guild(conn, gid)
    if row is None:
        return None
    radios[gid] = GuildRadio(gid, row["voice_channel_id"], row["nowplaying_channel_id"])
    return radios[gid]


def _ensure_opus() -> None:
    if discord.opus.is_loaded():
        return
    from ctypes.util import find_library

    for path in (find_library("opus"), "/opt/homebrew/lib/libopus.dylib",
                 "/usr/local/lib/libopus.dylib", "libopus.so.0"):
        if not path:
            continue
        try:
            discord.opus.load_opus(path)
            return
        except OSError:
            continue
    raise RuntimeError("libopus not found — install it (brew install opus / apt install libopus0)")


def _listeners(channel) -> list:
    return [m for m in getattr(channel, "members", []) if not m.bot]


def _present(radio: GuildRadio, voice_channel):
    """Present listeners in this guild = VC members + anyone who rated here within
    PRESENCE_WINDOW_MIN. Presence-by-rating is scoped per guild. Returns
    [(user_id_str, display_name)], VC members first, deduped."""
    present = {str(m.id): m.display_name for m in _listeners(voice_channel)}
    for r in db.present_since(conn, radio.guild_id, PRESENCE_WINDOW_MIN):
        present.setdefault(r["user_id"], r["name"])
    return list(present.items())


async def _advance(radio: GuildRadio, vc: discord.VoiceClient, seek: int = 0) -> None:
    """Compose and play the next track for this guild, scored over whoever is
    present. Runs on the loop thread; the after-callback hops back via
    run_coroutine_threadsafe. `seek` starts the track that many seconds in (wake)."""
    if not radio.active or not vc.is_connected():
        return
    if not _listeners(vc.channel):
        return  # streaming gate: at least one human must be in the VC
    present = _present(radio, vc.channel)
    member_ids = [uid for uid, _ in present]
    gid = str(radio.guild_id)
    if not radio.block:
        radio.block = engine.new_block(db.pending_request_count(conn, gid))
    want = radio.block.pop()
    row = picker = None
    if want == "request":
        req = db.next_request(conn, gid)
        if req is not None:
            row, picker = dict(req), "request"
            db.mark_request_played(conn, req["id"], gid)
    if row is None:  # a music slot, or the request queue was empty -> pick music
        r, picker = engine.pick(conn, member_ids, "top" if want == "request" else want)
        if r is None:
            return
        row = dict(r)
    radio.current_row = row
    radio.current_track = f"{row['artist']} – {row['title']}"
    db.record_play(conn, row["id"], reason=picker)
    opts = dict(FFMPEG_OPTS)
    if seek > 0:
        opts["before_options"] = f"-ss {seek}"  # input seek: start mid-song
    source = discord.FFmpegPCMAudio(row["path"], **opts)
    vc.play(source, after=lambda err: _after(radio, vc, err, row["path"]))
    await _post_nowplaying(radio, vc.channel, row)


def _after(radio: GuildRadio, vc: discord.VoiceClient, err, path: str) -> None:
    # Runs on discord's audio worker thread — schedule the next pick onto the loop.
    if err:
        print(f"playback error on {path}: {err}")
    if _loop:
        asyncio.run_coroutine_threadsafe(_advance(radio, vc), _loop)


def _sync_playback(radio: GuildRadio, vc: discord.VoiceClient) -> None:
    """Stream iff a human is in this guild's VC. Idempotent."""
    if not vc or not vc.is_connected():
        return
    if _listeners(vc.channel):
        if not radio.active:
            radio.active = True
            if _loop and not vc.is_playing():
                _loop.create_task(_advance(radio, vc, seek=WAKE_SEEK_SECONDS))
    elif radio.active:
        radio.active = False
        radio.current_track = None
        radio.current_row = None
        if vc.is_playing():
            vc.stop()
        if _loop:
            asyncio.run_coroutine_threadsafe(_clear_nowplaying(radio), _loop)


# ── now-playing card ────────────────────────────────────────────────────────

def _sidebar(radio: GuildRadio, voice_channel, track_id: int) -> str:
    """One line per present listener (VC + recent raters): their rating square."""
    lines = []
    for uid, name in _present(radio, voice_channel):
        val = db.get_rating(conn, uid, track_id)
        square = _SQUARE.get(val, UNRATED) if val is not None else UNRATED
        lines.append(f"{square} {name}")
    return "\n".join(lines) or "_nobody here_"


def _np_channel(radio: GuildRadio, voice_channel):
    if radio.nowplaying_channel_id:
        return client.get_channel(radio.nowplaying_channel_id) or voice_channel
    return voice_channel  # voice channels are Messageable (text-in-voice) in discord.py 2.x


def _build_embed(radio: GuildRadio, row: dict, voice_channel, has_cover: bool) -> discord.Embed:
    embed = discord.Embed(title=f"{row['artist']} – {row['title']}", description=row.get("album") or "")
    embed.add_field(name="Ratings", value=_sidebar(radio, voice_channel, row["id"]), inline=False)
    if has_cover:
        embed.set_thumbnail(url="attachment://cover.png")
    return embed


class RatingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for label, value, square, style in RATINGS:
            self.add_item(_RatingButton(label, value, square, style))


class _RatingButton(discord.ui.Button):
    def __init__(self, label, value, square, style):
        super().__init__(label=label, emoji=square, style=style)
        self.value = value

    async def callback(self, interaction: discord.Interaction) -> None:
        radio = _radio(interaction.guild_id) if interaction.guild_id else None
        if radio is None or radio.current_row is None:
            await interaction.response.send_message("Nothing playing.", ephemeral=True)
            return
        db.upsert_user(conn, interaction.user.id, interaction.user.display_name)
        db.set_rating(conn, str(interaction.user.id), radio.current_row["id"], self.value)
        db.touch_presence(conn, interaction.user.id, interaction.guild_id)  # rating == present here
        await interaction.response.defer()  # ack, no new message
        await _refresh_sidebar(radio)


async def _refresh_sidebar(radio: GuildRadio) -> None:
    """Rebuild the sidebar on this guild's card. Reads the VOICE channel for
    presence, not the card's text channel."""
    if radio.np_message is None or radio.current_row is None:
        return
    guild = radio.np_message.guild
    vc = guild.voice_client if guild else None
    voice_channel = vc.channel if vc else radio.np_message.channel
    embed = radio.np_message.embeds[0]
    embed.set_field_at(0, name="Ratings", value=_sidebar(radio, voice_channel, radio.current_row["id"]), inline=False)
    try:
        await radio.np_message.edit(embed=embed)
    except discord.HTTPException:
        pass


async def _post_nowplaying(radio: GuildRadio, voice_channel, row: dict) -> None:
    """Replace this guild's now-playing card for a new track."""
    await _clear_nowplaying(radio, clear_status=False)
    channel = _np_channel(radio, voice_channel)
    cover = library.extract_cover(row["path"])
    kwargs = {"view": RatingView()}
    if cover:
        kwargs["file"] = discord.File(io.BytesIO(cover), filename="cover.png")
    kwargs["embed"] = _build_embed(radio, row, voice_channel, has_cover=bool(cover))
    try:
        radio.np_message = await channel.send(**kwargs)
    except discord.HTTPException as e:
        print(f"could not post now-playing card: {e}")
        radio.np_message = None
    try:  # bot status is global (one per bot); with N guilds it shows the latest track
        await client.change_presence(
            activity=discord.Activity(type=discord.ActivityType.listening, name=f"{row['artist']} – {row['title']}")
        )
    except discord.HTTPException:
        pass
    # Reflect recent artists in the channel topic, every TOPIC_EVERY tracks — Discord
    # throttles channel edits to ~2 per 10 min, so a per-song edit gets 429'd.
    radio.recent_artists.append(row["artist"])
    del radio.recent_artists[:-TOPIC_EVERY]
    radio.since_topic += 1
    if radio.since_topic >= TOPIC_EVERY:
        radio.since_topic = 0
        recent = list(dict.fromkeys(radio.recent_artists))
        try:
            await channel.edit(topic="🎵 Recent: " + ", ".join(recent))
        except (discord.HTTPException, AttributeError) as e:
            print(f"could not set channel topic: {e}")


async def _clear_nowplaying(radio: GuildRadio, clear_status: bool = True) -> None:
    if radio.np_message is not None:
        try:
            await radio.np_message.delete()
        except discord.HTTPException:
            pass
        radio.np_message = None
    if clear_status and not any(r.active for r in radios.values()):
        try:  # only clear the global status when no guild is still playing
            await client.change_presence(activity=None)
        except discord.HTTPException:
            pass


# ── discord wiring ──────────────────────────────────────────────────────────


async def _clear_cards_on_shutdown() -> None:
    """Delete the now-playing cards on shutdown so a (seconds-long) restart doesn't
    leave orphaned cards with dead buttons behind. No off-air announcement — that
    just spammed the channel on every quick redeploy."""
    for radio in radios.values():
        await _clear_nowplaying(radio, clear_status=False)


class MercuryClient(discord.Client):
    _shutdown_done = False

    async def setup_hook(self) -> None:
        # docker stop/restart sends SIGTERM; asyncio.run doesn't trap it, so the
        # normal close() path never runs. Bridge SIGTERM -> close() ourselves.
        try:
            self.loop.add_signal_handler(
                signal.SIGTERM, lambda: self.loop.create_task(self.close())
            )
        except (NotImplementedError, RuntimeError):
            pass

    async def close(self) -> None:
        if not self._shutdown_done:
            self._shutdown_done = True
            await _clear_cards_on_shutdown()
        await super().close()


intents = discord.Intents.default()
intents.voice_states = True
client = MercuryClient(intents=intents)
tree = app_commands.CommandTree(client)


async def _rescan_bg() -> None:
    """Refresh the library off the loop, after playback has already started."""
    try:
        n = await _loop.run_in_executor(None, library.scan, MUSIC_DIR)
        print(f"background library rescan complete — {n} tracks")
    except Exception as e:
        print(f"background rescan failed: {e}")


async def _serve_guild(radio: GuildRadio) -> None:
    """Connect to a guild's voice channel and start (or idle) its radio."""
    if not radio.voice_channel_id:
        return
    channel = client.get_channel(radio.voice_channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        print(f"guild {radio.guild_id}: voice channel {radio.voice_channel_id} not reachable")
        return
    vc = channel.guild.voice_client
    if vc is None:
        try:
            vc = await channel.connect()
        except discord.ClientException:
            vc = channel.guild.voice_client
    elif vc.channel != channel:
        await vc.move_to(channel)
    if vc:
        _sync_playback(radio, vc)
        print(f"serving {channel.guild.name}/{channel.name} — {'streaming' if radio.active else 'idle (empty)'}")


async def _sync_commands_to(guild) -> None:
    """Copy the global commands into a guild and sync — instant availability
    (a plain global sync can take up to an hour to propagate)."""
    try:
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
    except discord.HTTPException as e:
        print(f"command sync failed for guild {getattr(guild, 'id', '?')}: {e}")


@client.event
async def on_ready() -> None:
    global conn, _loop
    _ensure_opus()
    _loop = asyncio.get_running_loop()
    if conn is None:
        conn = db.connect()
        # Back-compat: seed the guilds table from the legacy single-guild env once.
        if not db.list_guilds(conn) and _SEED_GUILD_ID:
            db.upsert_guild(conn, _SEED_GUILD_ID, _SEED_VOICE_ID, _SEED_NP_ID)
            print(f"seeded guild {_SEED_GUILD_ID} from env")
        for g in client.guilds:  # instant command availability in every joined guild
            await _sync_commands_to(g)
        if db.music_count(conn) > 0:
            print(f"mercuryradio up as {client.user} — {db.music_count(conn)} tracks (rescanning in background)")
            _loop.create_task(_rescan_bg())
        else:
            count = await _loop.run_in_executor(None, library.scan, MUSIC_DIR)
            print(f"mercuryradio up as {client.user} — {count} tracks (first scan)")
    # (re)serve every enabled guild
    for row in db.list_guilds(conn):
        radio = _radio(row["guild_id"])
        if radio:
            await _serve_guild(radio)


@client.event
async def on_guild_join(guild) -> None:
    await _sync_commands_to(guild)  # so /setup is available immediately


@client.event
async def on_voice_state_update(member, before, after) -> None:
    if member.bot:
        return
    radio = _radio(member.guild.id)
    if radio is None:
        return
    vc = member.guild.voice_client
    if vc and vc.is_connected():
        _sync_playback(radio, vc)
        await _refresh_sidebar(radio)


@tree.command(name="setup", description="Register this server's radio (admin).")
@app_commands.describe(voice="Voice channel to stream in", card="Text channel for the now-playing card")
async def setup(interaction: discord.Interaction, voice: discord.VoiceChannel,
                card: discord.TextChannel | None = None) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("Run this in a server.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need Manage Server to set up the radio.", ephemeral=True)
        return
    db.upsert_guild(conn, interaction.guild_id, voice.id, card.id if card else None)
    radios.pop(interaction.guild_id, None)  # reload config
    radio = _radio(interaction.guild_id)
    await interaction.response.send_message(
        f"Radio set up — streaming in **{voice.name}**"
        + (f", card in **#{card.name}**" if card else "") + ". Joining now.",
        ephemeral=True,
    )
    await _serve_guild(radio)


@tree.command(name="join", description="Start the radio in the server's voice channel.")
async def join(interaction: discord.Interaction) -> None:
    radio = _radio(interaction.guild_id) if interaction.guild_id else None
    if radio is None:
        await interaction.response.send_message("This server isn't set up — an admin runs /setup first.", ephemeral=True)
        return
    channel = client.get_channel(radio.voice_channel_id) if radio.voice_channel_id else None
    if not isinstance(channel, discord.VoiceChannel):
        voice = getattr(interaction.user, "voice", None)
        channel = voice.channel if voice else None
    if channel is None:
        await interaction.response.send_message("No station voice channel, and you're not in one.", ephemeral=True)
        return
    vc = interaction.guild.voice_client
    if vc:
        await vc.move_to(channel)
    else:
        vc = await channel.connect()
    count = db.music_count(conn)
    if not count:
        await interaction.response.send_message(f"No tracks in the library ({MUSIC_DIR}).", ephemeral=True)
        return
    _sync_playback(radio, vc)
    state = "on" if radio.active else "idle until someone joins"
    await interaction.response.send_message(f"Radio {state} in {channel.name} — {count} tracks.", ephemeral=True)


@tree.command(name="skip", description="Skip to the next track.")
async def skip(interaction: discord.Interaction) -> None:
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("Skipped.", ephemeral=True)
    else:
        await interaction.response.send_message("Nothing playing.", ephemeral=True)


@tree.command(name="request", description="Request a track — it plays next.")
@app_commands.describe(track="Start typing an artist or title, then pick from the list")
async def request(interaction: discord.Interaction, track: str) -> None:
    row = None
    if track.isdigit():
        row = conn.execute("SELECT id, artist, title FROM tracks WHERE id = ?", (int(track),)).fetchone()
    if row is None:
        matches = db.search_tracks(conn, track, 1)
        row = matches[0] if matches else None
    if row is None:
        await interaction.response.send_message(f"No track matches “{track}”.", ephemeral=True)
        return
    db.add_request(conn, row["id"], str(interaction.guild_id), str(interaction.user.id))
    ahead = db.pending_request_count(conn, str(interaction.guild_id)) - 1
    when = "plays in the next block" if ahead <= 0 else f"{ahead} request(s) ahead of it"
    await interaction.response.send_message(f"Queued **{row['artist']} – {row['title']}** — {when}.", ephemeral=True)


@request.autocomplete("track")
async def _request_autocomplete(interaction: discord.Interaction, current: str):
    if not current:
        return []
    rows = db.search_tracks(conn, current, 25)
    return [app_commands.Choice(name=f"{r['artist']} – {r['title']}"[:100], value=str(r["id"])) for r in rows]


@tree.command(name="leave", description="Stop this server's radio and leave.")
async def leave(interaction: discord.Interaction) -> None:
    radio = _radio(interaction.guild_id) if interaction.guild_id else None
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc:
        if radio:
            await _clear_nowplaying(radio)
            radio.active = False
        await vc.disconnect()
        await interaction.response.send_message("Left.", ephemeral=True)
    else:
        await interaction.response.send_message("Not in a channel.", ephemeral=True)


if __name__ == "__main__":
    client.run(TOKEN)
