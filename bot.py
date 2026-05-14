import os
import re
import json
import asyncio
import threading
import html as _html
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone, timedelta

import aiohttp
import discord
from discord.ext import tasks, commands

try:
    import psycopg2
except ImportError:
    psycopg2 = None

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN is not set. Add it to your environment or bot.env.")
DEBUG_ERRORS = os.getenv("BOT_DEBUG_ERRORS", "").strip().lower() in {"1", "true", "yes", "on"}

SCHEDULE_API = "https://helltides.com/api/schedule"
SUBSCRIPTIONS_FILE = os.getenv("SUBSCRIPTIONS_FILE", "subscribers.json")
WARN_MINUTES = 15

_raw_db_url = os.getenv("DATABASE_URL", "").strip()
# psycopg2 requires 'postgresql://' scheme; Railway/Render may emit 'postgres://'
DATABASE_URL = _raw_db_url.replace("postgres://", "postgresql://", 1) if _raw_db_url.startswith("postgres://") else _raw_db_url

intents = discord.Intents.default()
intents.voice_states = True
intents.members = False
intents.message_content = False

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)


def _db_connect():
    """Open a new psycopg2 connection using DATABASE_URL."""
    return psycopg2.connect(DATABASE_URL)


def _db_ensure_table(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS guild_configs (
            guild_id   TEXT PRIMARY KEY,
            channel_id BIGINT,
            subscribers BIGINT[]
        )
    """)


def _db_load_subscriptions() -> dict:
    try:
        conn = _db_connect()
        try:
            with conn.cursor() as cur:
                _db_ensure_table(cur)
                conn.commit()
                cur.execute("SELECT guild_id, channel_id, subscribers FROM guild_configs")
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        print(f"[load_subscriptions:db] {type(e).__name__}: {e}", flush=True)
        return {"guilds": {}}

    normalized: dict[str, dict] = {"guilds": {}}
    for guild_id, channel_id, subscribers in rows:
        normalized["guilds"][guild_id] = {
            "channel_id": channel_id,
            "subscribers": sorted(subscribers or []),
        }
    return normalized


def _db_save_subscriptions() -> None:
    conn = _db_connect()
    try:
        with conn.cursor() as cur:
            _db_ensure_table(cur)
            for guild_id, cfg in subscriptions.get("guilds", {}).items():
                cur.execute("""
                    INSERT INTO guild_configs (guild_id, channel_id, subscribers)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (guild_id) DO UPDATE
                        SET channel_id  = EXCLUDED.channel_id,
                            subscribers = EXCLUDED.subscribers
                """, (guild_id, cfg.get("channel_id"), cfg.get("subscribers", [])))
        conn.commit()
    finally:
        conn.close()


def _normalize_json_subscriptions(data) -> dict:
    if not isinstance(data, dict) or "guilds" not in data:
        return {"guilds": {}}
    guilds = data.get("guilds")
    if not isinstance(guilds, dict):
        return {"guilds": {}}
    normalized: dict[str, dict] = {"guilds": {}}
    for guild_id, cfg in guilds.items():
        if not isinstance(guild_id, str) or not isinstance(cfg, dict):
            continue
        channel_id = cfg.get("channel_id")
        if not isinstance(channel_id, int):
            channel_id = None
        subscribers = cfg.get("subscribers", [])
        if not isinstance(subscribers, list):
            subscribers = []
        clean_subscribers = sorted(
            {uid for uid in subscribers if isinstance(uid, int) and uid > 0}
        )
        normalized["guilds"][guild_id] = {
            "channel_id": channel_id,
            "subscribers": clean_subscribers,
        }
    return normalized


def load_subscriptions() -> dict:
    if DATABASE_URL and psycopg2:
        return _db_load_subscriptions()

    if not os.path.exists(SUBSCRIPTIONS_FILE):
        return {"guilds": {}}
    try:
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"guilds": {}}
    if isinstance(data, list):
        return {"guilds": {}}
    return _normalize_json_subscriptions(data)


def save_subscriptions() -> None:
    if DATABASE_URL and psycopg2:
        _db_save_subscriptions()
        return

    target_dir = os.path.abspath(os.path.dirname(SUBSCRIPTIONS_FILE) or ".")
    fd, temp_path = tempfile.mkstemp(dir=target_dir)
    try:
        os.chmod(temp_path, 0o600)
    except OSError:
        pass
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            json.dump(subscriptions, temp_file, indent=2)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    os.replace(temp_path, SUBSCRIPTIONS_FILE)
    try:
        os.chmod(SUBSCRIPTIONS_FILE, 0o600)
    except OSError:
        pass


def get_guild_config(guild_id: int, create: bool = False) -> dict | None:
    key = str(guild_id)
    guilds = subscriptions.setdefault("guilds", {})

    if create and key not in guilds:
        guilds[key] = {"channel_id": None, "subscribers": []}

    return guilds.get(key)


subscriptions = load_subscriptions()
alerted_keys: set[tuple[int, int]] = set()
_cached_events: list = []  # updated by event_scanner every minute; read by status page


HELLTIDES_SCHEDULE_PAGE = "https://helltides.com/schedule"
D4LIFE_TRACKER_PAGE = "https://diablo4.life/trackers/helltide"
EVENT_TYPE_ORDER = ["World Boss", "Helltide", "Legion Event"]


def _event_type_offset(event_type: str) -> int:
    return {
        "Helltide": 1,
        "World Boss": 2,
        "Legion Event": 3,
    }.get(event_type, 9)


def _build_event_id(event_type: str, start_time: datetime) -> int:
    return int(start_time.timestamp()) * 10 + _event_type_offset(event_type)


def _safe_parse_us_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.strptime(value.strip(), "%m/%d/%Y %I:%M %p")
        return parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _sanitize_external_text(value: str, max_len: int = 80) -> str:
    if max_len <= 0:
        return ""

    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", value or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = discord.utils.escape_mentions(cleaned)
    if len(cleaned) > max_len:
        if max_len == 1:
            return "…"
        cleaned = cleaned[:max_len].rstrip()
        cleaned = cleaned[:-1].rstrip() + "…"
    return cleaned


def _log_error(context: str, err: Exception) -> None:
    if DEBUG_ERRORS:
        print(f"[{context}] {type(err).__name__}: {err}", flush=True)
    else:
        print(f"[{context}] {type(err).__name__}", flush=True)


def _parse_schedule_html(html_text: str, source: str) -> list[dict]:
    item_pattern = re.compile(
        r"<li[^>]*>\s*<img[^>]*?/images/icons/(?P<icon>[a-z_]+)\.png[^>]*>\s*"
        r"<span>(?P<dt>\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s+[AP]M)</span>"
        r"(?:<span[^>]*>(?P<name>[^<]+)</span>)?",
        re.IGNORECASE,
    )
    icon_map = {
        "world_boss": "World Boss",
        "helltide": "Helltide",
        "legion": "Legion Event",
    }

    events: list[dict] = []
    for match in item_pattern.finditer(html_text):
        event_type = icon_map.get(match.group("icon").lower())
        if not event_type:
            continue

        start = _safe_parse_us_datetime(match.group("dt"))
        if not start:
            continue

        events.append(
            {
                "id": _build_event_id(event_type, start),
                "type": event_type,
                "name": _sanitize_external_text(match.group("name") or ""),
                "startTime": start,
                "source": source,
            }
        )

    return events


async def _fetch_helltides_api(session: aiohttp.ClientSession) -> list[dict]:
    async with session.get(SCHEDULE_API) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)

    labels = {
        "world_boss": "World Boss",
        "helltide": "Helltide",
        "legion": "Legion Event",
    }

    events: list[dict] = []
    for event_type, entries in data.items():
        label = _sanitize_external_text(labels.get(event_type, event_type.title()), max_len=40)
        for entry in entries:
            start = datetime.fromisoformat(entry["startTime"].replace("Z", "+00:00"))
            events.append(
                {
                    "id": _build_event_id(label, start),
                    "type": label,
                    "name": _sanitize_external_text(entry.get("boss") or entry.get("name") or ""),
                    "startTime": start,
                    "source": "helltides_api",
                }
            )

    return events


async def _fetch_helltides_schedule_page(session: aiohttp.ClientSession) -> list[dict]:
    async with session.get(HELLTIDES_SCHEDULE_PAGE) as resp:
        resp.raise_for_status()
        html_text = await resp.text()
    return _parse_schedule_html(html_text, "helltides_schedule_page")


async def _fetch_d4life_tracker_page(session: aiohttp.ClientSession) -> list[dict]:
    async with session.get(D4LIFE_TRACKER_PAGE) as resp:
        resp.raise_for_status()
        html_text = await resp.text()
    return _parse_schedule_html(html_text, "d4life_tracker")


def _merge_events_by_consensus(events: list[dict], active_sources: set[str]) -> list[dict]:
    grouped: dict[tuple[str, int], list[dict]] = {}

    for event in events:
        minute_key = int(event["startTime"].timestamp() // 60)
        key = (event["type"], minute_key)
        grouped.setdefault(key, []).append(event)

    merged: list[dict] = []

    for _, candidates in grouped.items():
        source_set = {c.get("source", "unknown") for c in candidates}
        api_candidates = [c for c in candidates if c.get("source") == "helltides_api"]
        best = (api_candidates or sorted(candidates, key=lambda c: c["startTime"]))[0]

        merged.append(
            {
                "id": best["id"],
                "type": best["type"],
                "name": best["name"],
                "startTime": best["startTime"],
                "source_count": len(source_set),
                "sources": sorted(source_set),
            }
        )

    return sorted(merged, key=lambda x: x["startTime"])


async def fetch_schedule() -> list[dict]:
    timeout = aiohttp.ClientTimeout(total=20)
    source_events: list[dict] = []
    active_sources: set[str] = set()

    async with aiohttp.ClientSession(timeout=timeout) as session:
        fetchers = [
            ("helltides_api", _fetch_helltides_api),
            ("helltides_schedule_page", _fetch_helltides_schedule_page),
            ("d4life_tracker", _fetch_d4life_tracker_page),
        ]

        for source_name, fetcher in fetchers:
            try:
                events = await fetcher(session)
                if events:
                    active_sources.add(source_name)
                    source_events.extend(events)
            except Exception as e:
                _log_error(f"fetch_schedule:{source_name}", e)

    if not source_events:
        return []

    merged = _merge_events_by_consensus(source_events, active_sources)
    return merged if merged else sorted(source_events, key=lambda x: x["startTime"])


@tasks.loop(minutes=1)
async def event_scanner() -> None:
    global _cached_events
    now = datetime.now(timezone.utc)
    start_window = now + timedelta(minutes=WARN_MINUTES - 1)
    end_window = now + timedelta(minutes=WARN_MINUTES + 1)

    # Prune alert keys for events more than 2 hours old.
    cutoff_ts = (now - timedelta(hours=2)).timestamp()
    stale = {key for key in alerted_keys if key[0] // 10 < cutoff_ts}
    alerted_keys.difference_update(stale)

    try:
        events = await fetch_schedule()
    except Exception as e:
        _log_error("event_scanner:schedule_fetch", e)
        return

    # Cache upcoming events for the status page (safe: simple reference reassignment).
    _cached_events = sorted(
        [e for e in events if e["startTime"] > now],
        key=lambda e: e["startTime"],
    )

    for event in events:
        if not (start_window <= event["startTime"] <= end_window):
            continue

        for guild in bot.guilds:
            guild_cfg = get_guild_config(guild.id)
            if not guild_cfg:
                continue

            channel_id = guild_cfg.get("channel_id")
            user_ids = guild_cfg.get("subscribers", [])
            if not channel_id or not user_ids:
                continue

            key = (event["id"], guild.id)
            if key in alerted_keys:
                continue

            channel = guild.get_channel(channel_id)
            if channel is None:
                continue

            mentions: list[str] = []
            for uid in user_ids:
                member = guild.get_member(uid)
                if member is None:
                    try:
                        member = await guild.fetch_member(uid)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        member = None

                if member and member.voice:
                    mentions.append(member.mention)

            name = f" - {event['name']}" if event["name"] else ""
            time_text = discord.utils.format_dt(event["startTime"], style="t")

            if mentions:
                msg = (
                    f"{event['type']}{name} starts at {time_text}. "
                    f"15-minute warning: {' '.join(mentions)}"
                )
            else:
                msg = f"{event['type']}{name} starts at {time_text}. (No subscribers in VC)"

            try:
                await channel.send(msg)
                alerted_keys.add(key)
            except discord.HTTPException as e:
                _log_error(f"event_scanner:send:guild_{guild.id}", e)


@bot.tree.command(name="signup", description="Subscribe to event pings in this server")
async def signup(interaction: discord.Interaction) -> None:
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    guild_cfg = get_guild_config(interaction.guild.id, create=True)
    if guild_cfg.get("channel_id") is None:
        guild_cfg["channel_id"] = interaction.channel.id

    subscribers = guild_cfg.setdefault("subscribers", [])
    if interaction.user.id in subscribers:
        await interaction.response.send_message("You are already signed up in this server.", ephemeral=True)
        return

    subscribers.append(interaction.user.id)
    save_subscriptions()
    await interaction.response.send_message("You are signed up for this server.", ephemeral=True)


@bot.tree.command(name="signout", description="Unsubscribe from event pings in this server")
async def signout(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    guild_cfg = get_guild_config(interaction.guild.id)
    if not guild_cfg:
        await interaction.response.send_message("You were not signed up in this server.", ephemeral=True)
        return

    subscribers = guild_cfg.get("subscribers", [])
    if interaction.user.id not in subscribers:
        await interaction.response.send_message("You were not signed up in this server.", ephemeral=True)
        return

    subscribers.remove(interaction.user.id)
    save_subscriptions()
    await interaction.response.send_message("You are now signed out for this server.", ephemeral=True)


@bot.tree.command(name="setalertchannel", description="Set this channel for server event alerts")
async def setalertchannel(interaction: discord.Interaction) -> None:
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return

    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if member is None or not member.guild_permissions.manage_guild:
        await interaction.response.send_message("You need Manage Server to use this.", ephemeral=True)
        return

    guild_cfg = get_guild_config(interaction.guild.id, create=True)
    guild_cfg["channel_id"] = interaction.channel.id
    save_subscriptions()
    await interaction.response.send_message("Alert channel updated.", ephemeral=True)


@bot.tree.command(name="events", description="Show the next upcoming event for each type")
async def events(interaction: discord.Interaction) -> None:
    try:
        all_events = await fetch_schedule()
    except Exception as e:
        _log_error("events:fetch_schedule", e)
        await interaction.response.send_message(
            "Failed to fetch events right now. Please try again shortly.",
            ephemeral=True,
        )
        return

    now = datetime.now(timezone.utc)
    upcoming = sorted((e for e in all_events if e["startTime"] > now), key=lambda x: x["startTime"])

    if not upcoming:
        await interaction.response.send_message("No upcoming events found.", ephemeral=True)
        return

    first_per_type: dict[str, dict] = {}
    for event_type in EVENT_TYPE_ORDER:
        first = next((e for e in upcoming if e["type"] == event_type), None)
        if first:
            first_per_type[event_type] = first

    if not first_per_type:
        await interaction.response.send_message("No upcoming events found.", ephemeral=True)
        return

    lines = []
    for event_type in EVENT_TYPE_ORDER:
        event = first_per_type.get(event_type)
        if not event:
            continue

        name = f" - {event['name']}" if event["name"] else ""
        confidence = event.get("source_count", 1)
        lines.append(
            f"{event_type}{name}: {discord.utils.format_dt(event['startTime'], style='R')} "
            f"(sources matched: {confidence})"
        )

    await interaction.response.send_message("Upcoming D4 events (best-match per type):\n" + "\n".join(lines), ephemeral=True)


@bot.event
async def on_ready() -> None:
    await bot.tree.sync()
    if not event_scanner.is_running():
        event_scanner.start()
    print(f"{bot.user} is online in {len(bot.guilds)} server(s).", flush=True)


# ---------------------------------------------------------------------------
# Health / status web server
# Runs in a background thread — stays up even if the Discord bot crashes,
# never blocks the asyncio event loop, and is reachable by UptimeRobot.
# ---------------------------------------------------------------------------

_last_error: str = "Starting..."


def _rel_time(dt: datetime) -> str:
    """Return a human-readable relative time string like 'in 14m 30s'."""
    secs = int((dt - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "starting now"
    if secs < 60:
        return f"in {secs}s"
    if secs < 3600:
        m, s = divmod(secs, 60)
        return f"in {m}m {s}s" if s else f"in {m}m"
    h, rem = divmod(secs, 3600)
    m = rem // 60
    return f"in {h}h {m}m" if m else f"in {h}h"


def _build_events_html() -> str:
    now = datetime.now(timezone.utc)
    upcoming = [e for e in _cached_events if e["startTime"] > now]

    first_per_type: dict[str, dict] = {}
    for e in upcoming:
        if e["type"] not in first_per_type:
            first_per_type[e["type"]] = e

    if not first_per_type:
        return '<p class="no-events">No events cached yet &mdash; scanner updates every minute</p>'

    icons = {"World Boss": "&#x1F479;", "Helltide": "&#x1F525;", "Legion Event": "&#x2694;&#xFE0F;"}
    rows = []
    for t in EVENT_TYPE_ORDER:
        ev = first_per_type.get(t)
        if not ev:
            continue
        rel = _rel_time(ev["startTime"])
        abs_t = ev["startTime"].strftime("%H:%M UTC")
        name_html = (
            f' <span class="ev-name">&middot; {_html.escape(ev["name"])}</span>'
            if ev.get("name") else ""
        )
        secs_away = int((ev["startTime"] - now).total_seconds())
        row_cls = "ev-row ev-urgent" if secs_away < 900 else "ev-row"
        rows.append(
            f'<div class="{row_cls}">'
            f'<span class="ev-type">{icons.get(t, "&#x1F4C5;")} {_html.escape(t)}{name_html}</span>'
            f'<span class="ev-time">{rel} &nbsp;&bull;&nbsp; {abs_t}</span>'
            f'</div>'
        )
    return "\n".join(rows)


def _build_status_html() -> str:
    is_ready = not bot.is_closed() and bot.user is not None
    bot_name = _html.escape(str(bot.user) if bot.user else "Not connected")
    guild_count = len(bot.guilds)
    db_status = "PostgreSQL" if DATABASE_URL else "Local JSON"
    status_text = "Online" if is_ready else "Offline / Starting"
    scanner_text = "Running" if event_scanner.is_running() else "Stopped"
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    status_cls = "online" if is_ready else "offline"
    error_row = "" if is_ready else f"""
    <div class="row">
      <span class="lbl">Last error</span>
      <span class="val err">{_html.escape(_last_error)}</span>
    </div>"""
    events_html = _build_events_html()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="30">
  <title>Diablo 4 Bot</title>
  <style>
    :root{{
      --bg:#0a0a12;--card:#11111e;--border:#1c1c30;
      --red:#c0392b;--green:#27ae60;--orange:#e67e22;
      --text:#e6e4f0;--muted:#52506a;--blue:#7ec8e3;
    }}
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
      background:var(--bg);color:var(--text);min-height:100vh;
      display:flex;flex-direction:column;align-items:center;
      padding:36px 16px 52px;gap:16px;
    }}
    header{{text-align:center}}
    header h1{{
      font-size:1.55rem;color:var(--red);letter-spacing:-.02em;
      text-shadow:0 0 40px rgba(192,57,43,.4);
    }}
    header p{{color:var(--muted);font-size:.78rem;margin-top:5px}}
    .card{{
      background:var(--card);border:1px solid var(--border);
      border-radius:10px;padding:20px 24px;width:100%;max-width:460px;
    }}
    .card-label{{
      font-size:.62rem;text-transform:uppercase;letter-spacing:.14em;
      color:var(--muted);padding-bottom:12px;margin-bottom:12px;
      border-bottom:1px solid var(--border);
    }}
    .row{{
      display:flex;justify-content:space-between;align-items:center;
      padding:8px 0;border-bottom:1px solid var(--border);
      font-size:.875rem;gap:8px;
    }}
    .row:last-child{{border-bottom:none}}
    .lbl{{color:var(--muted);white-space:nowrap;flex-shrink:0}}
    .val{{font-weight:600;text-align:right;word-break:break-word}}
    .val.err{{
      color:#e74c3c;font-size:.77rem;font-weight:400;
      max-width:240px;line-height:1.4;
    }}
    .val.ts{{color:var(--muted);font-size:.78rem;font-weight:400}}
    .badge{{
      display:inline-flex;align-items:center;gap:7px;
      padding:3px 12px;border-radius:20px;font-size:.77rem;font-weight:700;
    }}
    .badge::before{{
      content:'';width:7px;height:7px;border-radius:50%;flex-shrink:0;
    }}
    .badge.online{{
      background:rgba(39,174,96,.12);color:var(--green);
      border:1px solid rgba(39,174,96,.22);
    }}
    .badge.online::before{{background:var(--green);box-shadow:0 0 6px var(--green)}}
    .badge.offline{{
      background:rgba(192,57,43,.12);color:#e74c3c;
      border:1px solid rgba(192,57,43,.22);
    }}
    .badge.offline::before{{background:#e74c3c}}
    .ev-row{{
      display:flex;justify-content:space-between;align-items:center;
      padding:9px 10px;border-radius:7px;font-size:.85rem;
      gap:10px;transition:background .15s;
    }}
    .ev-row+.ev-row{{margin-top:3px}}
    .ev-row:hover{{background:rgba(255,255,255,.03)}}
    .ev-type{{font-weight:600}}
    .ev-name{{font-weight:400;color:var(--muted);font-size:.8rem}}
    .ev-time{{
      color:var(--muted);font-size:.8rem;
      white-space:nowrap;text-align:right;
    }}
    .ev-urgent .ev-time{{color:var(--orange);font-weight:700}}
    .no-events{{color:var(--muted);font-size:.85rem;padding:4px 0}}
    footer{{font-size:.7rem;color:#2a2a3a;text-align:center}}
    code{{
      background:var(--border);padding:2px 7px;
      border-radius:4px;color:var(--blue);font-size:.9em;
    }}
  </style>
</head>
<body>
  <header>
    <h1>&#x2694;&#xFE0F;&nbsp;Diablo&nbsp;4 Discord Bot</h1>
    <p>Status dashboard &bull; auto-refreshes every 30&thinsp;s</p>
  </header>

  <div class="card">
    <div class="card-label">Bot Status</div>
    <div class="row">
      <span class="lbl">Connection</span>
      <span class="badge {status_cls}">{status_text}</span>
    </div>
    <div class="row">
      <span class="lbl">Account</span>
      <span class="val">{bot_name}</span>
    </div>
    <div class="row">
      <span class="lbl">Servers</span>
      <span class="val">{guild_count}</span>
    </div>
    <div class="row">
      <span class="lbl">Event scanner</span>
      <span class="val">{scanner_text}</span>
    </div>
    <div class="row">
      <span class="lbl">Database</span>
      <span class="val">{db_status}</span>
    </div>
    {error_row}
    <div class="row">
      <span class="lbl">Generated</span>
      <span class="val ts">{now_utc}</span>
    </div>
  </div>

  <div class="card">
    <div class="card-label">Upcoming D4 Events</div>
    {events_html}
  </div>

  <footer>
    UptimeRobot: point monitor at <code>/health</code> &mdash; returns 200&nbsp;OK every ping
  </footer>
</body>
</html>"""


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            body = b"ok"
            content_type = "text/plain; charset=utf-8"
        else:
            body = _build_status_html().encode("utf-8")
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        if self.path == "/health":
            body = b"ok"
            content_type = "text/plain; charset=utf-8"
        else:
            body = _build_status_html().encode("utf-8")
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        # HEAD: no body

    def log_message(self, *args) -> None:
        pass  # suppress access logs


def _start_health_thread() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    print(f"Health server listening on port {port}", flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()


_start_health_thread()


async def _bot_loop() -> None:
    global _last_error
    delay = 30
    while True:
        connect_time = asyncio.get_event_loop().time()
        try:
            _last_error = "Connecting to Discord..."
            print("[bot] Connecting to Discord...", flush=True)
            # Reset internal state so a fresh aiohttp session is created each attempt.
            # bot.run() closes the connector on exit; bot.start() won't reopen a closed one
            # unless we clear it here.
            bot._closed = False
            bot.http.connector = discord.utils.MISSING
            await bot.start(TOKEN)
            break  # clean shutdown
        except discord.LoginFailure:
            _last_error = "LoginFailure: bad token — update DISCORD_BOT_TOKEN in bot.env"
            print(f"[bot] {_last_error}", flush=True)
            try:
                await bot.close()
            except Exception:
                pass
            await asyncio.sleep(300)
        except Exception as e:
            # 429 from Cloudflare/Discord means the IP is rate-limited — back off hard.
            if "429" in str(e) or "Too Many Requests" in str(e):
                wait = 1800  # 30 minutes
                _last_error = f"Rate-limited by Discord/Cloudflare — waiting {wait // 60} min"
                print(f"[bot] {_last_error}", flush=True)
                try:
                    await bot.close()
                except Exception:
                    pass
                await asyncio.sleep(wait)
                delay = 30  # reset backoff after long rate-limit pause
                continue
            # If we stayed connected for at least 60 s, reset the backoff counter.
            if asyncio.get_event_loop().time() - connect_time > 60:
                delay = 30
            _last_error = f"{type(e).__name__}: {e} (retrying in {delay}s)"
            print(f"[bot] {_last_error}", flush=True)
            try:
                await bot.close()
            except Exception:
                pass
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


asyncio.run(_bot_loop())
