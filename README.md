# Diablo4DiscordBot
Discord bot that scans users in voice calls, if it finds a user in a call that is signed up, it pings the user(s) about upcoming events.

## Security notes
- Set `DISCORD_BOT_TOKEN` in your environment (or local `bot.env`) and never commit secrets.
- Bot logs are rotated by `run_bot_forever.ps1` to limit unbounded log growth.
- Production logging is minimal by default. Set `BOT_DEBUG_ERRORS=1` only for temporary debugging.
