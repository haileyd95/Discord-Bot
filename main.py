"""
Support Queue & Voice Call Bot — entry point.

Start with:  python main.py
"""

import asyncio
import logging
import os

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

import database as db
from views import RequestSupportView

# ── Load .env before anything else ──────────────────────────────────────────
load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot")

# ── Intents ───────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True        # Server Members Intent — enable in Dev Portal
intents.guilds = True
intents.voice_states = True

# ── Bot ───────────────────────────────────────────────────────────────────────
bot = commands.Bot(command_prefix="\x00", intents=intents)  # prefix unused; slash-only


# ── Background task: 30-day data purge ───────────────────────────────────────
@tasks.loop(hours=24)
async def daily_purge() -> None:
    count = await db.purge_old_tickets()
    if count:
        logger.info("Daily purge removed %d stale ticket(s) (>30 days closed).", count)


@daily_purge.before_loop
async def before_purge() -> None:
    await bot.wait_until_ready()


# ── Bot events ────────────────────────────────────────────────────────────────
@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)

    # Re-register the persistent view so button interactions still work
    # after a restart (discord.py matches by custom_id).
    bot.add_view(RequestSupportView())

    # Sync slash commands — guild-scoped for instant propagation during dev,
    # or global when GUILD_ID is not set (takes ~1 hour to propagate).
    guild_id = os.getenv("GUILD_ID")
    if guild_id:
        guild_obj = discord.Object(id=int(guild_id))
        bot.tree.copy_global_to(guild=guild_obj)
        synced = await bot.tree.sync(guild=guild_obj)
        logger.info("Synced %d command(s) to guild %s.", len(synced), guild_id)
    else:
        synced = await bot.tree.sync()
        logger.info("Synced %d global command(s).", len(synced))

    if not daily_purge.is_running():
        daily_purge.start()


@bot.event
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    logger.error(
        "Slash command error in /%s by user %s: %s",
        interaction.command.name if interaction.command else "unknown",
        interaction.user.id,
        error,
        exc_info=error,
    )
    msg = "An unexpected error occurred. Please try again or contact an admin."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except (discord.Forbidden, discord.HTTPException):
        pass


# Import needed for the error handler type annotation
from discord import app_commands  # noqa: E402  (kept at bottom to avoid circular at startup)


# ── Main ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.critical(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and fill in your token."
        )
        return

    await db.init_db()

    async with bot:
        await bot.load_extension("cogs.queue")
        logger.info("Loaded cog: cogs.queue")
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
