import os
import re
import json
from datetime import datetime, timezone, timedelta

import aiohttp
import discord
from discord.ext import tasks, commands

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN is not set. Add it to your environment or bot.env.")

SCHEDULE_API = "https://helltides.com/api/schedule"
SUBSCRIPTIONS_FILE = "subscribers.json"
WARN_MINUTES = 15

intents = discord.Intents.default()
intents.voice_states = True
intents.members = False
intents.message_content = False

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)


def load_subscriptions() -> dict:
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        return {"guilds": {}}

    with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        return {"guilds": {}}

    if not isinstance(data, dict) or "guilds" not in data:
        return {"guilds": {}}

    return data


def save_subscriptions() -> None:
    with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(subscriptions, f, indent=2)


def get_guild_config(guild_id: int, create: bool = False) -> dict | None:
    key = str(guild_id)
    guilds = subscriptions.setdefault("guilds", {})

    if create and key not in guilds:
        guilds[key] = {"channel_id": None, "subscribers": []}

    return guilds.get(key)


subscriptions = load_subscriptions()
alerted_keys: set[tuple[int, int]] = set()


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


def _parse_schedule_html(html: str, source: str) -> list[dict]:
    # Parse event rows from the rendered schedule list with icon + timestamp.
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
    for match in item_pattern.finditer(html):
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
                "name": (match.group("name") or "").strip(),
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
        label = labels.get(event_type, event_type.title())
        for entry in entries:
            start = datetime.fromisoformat(entry["startTime"].replace("Z", "+00:00"))
            events.append(
                {
                    "id": _build_event_id(label, start),
                    "type": label,
                    "name": entry.get("boss") or entry.get("name") or "",
                    "startTime": start,
                    "source": "helltides_api",
                }
            )

    return events


async def _fetch_helltides_schedule_page(session: aiohttp.ClientSession) -> list[dict]:
    async with session.get(HELLTIDES_SCHEDULE_PAGE) as resp:
        resp.raise_for_status()
        html = await resp.text()
    return _parse_schedule_html(html, "helltides_schedule_page")


async def _fetch_d4life_tracker_page(session: aiohttp.ClientSession) -> list[dict]:
    # Best-effort scrape: this site may not always expose structured timestamps.
    async with session.get(D4LIFE_TRACKER_PAGE) as resp:
        resp.raise_for_status()
        html = await resp.text()
    return _parse_schedule_html(html, "d4life_tracker")


def _merge_events_by_consensus(events: list[dict], active_sources: set[str]) -> list[dict]:
    grouped: dict[tuple[str, int], list[dict]] = {}

    for event in events:
        minute_key = int(event["startTime"].timestamp() // 60)
        key = (event["type"], minute_key)
        grouped.setdefault(key, []).append(event)

    merged: list[dict] = []

    for _, candidates in grouped.items():
        # Prefer candidate backed by the most distinct sources; never discard.
        source_set = {c.get("source", "unknown") for c in candidates}

        # Pick the candidate whose source is "helltides_api" if available,
        # otherwise just take the first by start time.
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
                print(f"[fetch_schedule] {source_name} failed: {e}")

    if not source_events:
        return []

    merged = _merge_events_by_consensus(source_events, active_sources)
    return merged if merged else sorted(source_events, key=lambda x: x["startTime"])


@tasks.loop(minutes=1)
async def event_scanner() -> None:
    now = datetime.now(timezone.utc)
    start_window = now + timedelta(minutes=WARN_MINUTES - 1)
    end_window = now + timedelta(minutes=WARN_MINUTES + 1)

    try:
        events = await fetch_schedule()
    except Exception as e:
        print(f"[event_scanner] schedule fetch failed: {e}")
        return

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
                print(f"[event_scanner] failed to send in guild {guild.id}: {e}")


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
        print(f"[events] failed to fetch events: {e}")
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
    guild_summary = ", ".join(f"{g.name}({g.id})" for g in bot.guilds) or "none"
    print(f"{bot.user} is online in {len(bot.guilds)} server(s): {guild_summary}", flush=True)


bot.run(TOKEN)
