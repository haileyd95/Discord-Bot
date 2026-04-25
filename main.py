"""
Support Queue & Voice Call Bot — entry point.

Start with:  python main.py
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import database as db
from views import RequestSupportView

# ── Load .env before anything else ───────────────────────────────────────────
load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot")

# ── Intents ───────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True       # Server Members Intent — must be enabled in Dev Portal
intents.guilds = True
intents.voice_states = True
# MESSAGE_CONTENT intent is intentionally NOT enabled — the bot never reads messages.

# ── Bot ───────────────────────────────────────────────────────────────────────
bot = commands.Bot(command_prefix="\x00", intents=intents)  # prefix unused; slash-only

_commands_synced = False


# ── Background task: 30-day data purge ───────────────────────────────────────
@tasks.loop(hours=24)
async def daily_purge() -> None:
    count = await db.purge_old_tickets()
    if count:
        logger.info("Daily purge removed %d stale ticket(s) (>30 days closed).", count)


@daily_purge.before_loop
async def before_purge() -> None:
    await bot.wait_until_ready()


# ── Background task: stale active-ticket checker (timer persistence) ──────────
@tasks.loop(seconds=60)
async def check_stale_tickets() -> None:
    """Auto-skip active tickets whose called_at is older than 10 minutes.

    This replaces the previous asyncio.create_task() timers, which were lost
    on bot restart.  The DB-stamped called_at column is the authoritative clock.
    """
    stale = await db.get_stale_active_tickets(timeout_minutes=10)
    if not stale:
        return

    guild_id = os.getenv("GUILD_ID")
    if not guild_id:
        logger.warning("GUILD_ID not set; cannot run stale-ticket check.")
        return

    guild = bot.get_guild(int(guild_id))
    if not guild:
        return

    op_room_id = os.getenv("OPERATOR_ROOM_CHANNEL_ID")
    op_room = guild.get_channel(int(op_room_id)) if op_room_id else None

    cog = bot.cogs.get("QueueCog")
    if not cog:
        return

    for ticket in stale:
        member = guild.get_member(int(ticket["handle_id"]))

        # If the user is already inside the operator room they joined in time — leave them.
        if (
            op_room
            and member
            and member.voice
            and member.voice.channel
            and member.voice.channel.id == op_room.id
        ):
            continue

        logger.info(
            "Stale ticket #%d: active >10 min, user not in operator room — auto-skipping.",
            ticket["number"],
        )
        try:
            await cog._process_skip(guild, ticket["number"], auto=True)
        except Exception as exc:
            logger.exception(
                "Auto-skip of stale ticket #%d failed: %s",
                ticket["number"],
                exc,
            )
            continue


@check_stale_tickets.before_loop
async def before_stale_check() -> None:
    await bot.wait_until_ready()


# ── Bot events ────────────────────────────────────────────────────────────────
@bot.event
async def on_ready() -> None:
    global _commands_synced
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)

    # Re-register the persistent view so the button still works after a restart.
    bot.add_view(RequestSupportView())

    # Sync slash commands once per process — guild-scoped for instant
    # propagation during dev, or global when GUILD_ID is not set.
    if not _commands_synced:
        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild_obj = discord.Object(id=int(guild_id))
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            logger.info("Synced %d command(s) to guild %s.", len(synced), guild_id)
        else:
            synced = await bot.tree.sync()
            logger.info("Synced %d global command(s).", len(synced))
        _commands_synced = True

    if not daily_purge.is_running():
        daily_purge.start()
    if not check_stale_tickets.is_running():
        check_stale_tickets.start()


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    """Auto-drop any active or queued ticket when a user leaves the server."""
    try:
        ticket = await db.get_active_ticket_for_user(str(member.id))
    except Exception as exc:
        logger.exception("Failed to look up ticket for departing member %s: %s", member.id, exc)
        return

    if not ticket:
        return

    try:
        await db.update_ticket_status(
            ticket["number"],
            "dropped",
            closed_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        logger.exception(
            "Failed to auto-drop ticket #%d for departed user %s: %s",
            ticket["number"],
            member.id,
            exc,
        )
        return

    logger.info(
        "Auto-dropped ticket #%d for departing user %s (%s).",
        ticket["number"],
        member.id,
        member.name,
    )

    ops_log_id = os.getenv("OPS_LOG_CHANNEL_ID")
    if ops_log_id and member.guild:
        ops_channel = member.guild.get_channel(int(ops_log_id))
        if ops_channel:
            try:
                await ops_channel.send(
                    f"\U0001f6aa Ticket **#{ticket['number']}** auto-dropped — "
                    f"user {member.mention} (`{member.id}`) left the server."
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.error("Failed to log auto-drop to ops-log: %s", exc)


@bot.event
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    cmd_name = interaction.command.name if interaction.command else "unknown"
    logger.error(
        "Slash command error in /%s by user %s: %s",
        cmd_name,
        interaction.user.id,
        error,
        exc_info=error,
    )

    if isinstance(error, app_commands.MissingPermissions):
        msg = "You don't have permission to use this command."
    elif isinstance(error, app_commands.BotMissingPermissions):
        msg = "I lack the required permissions to do that. Please contact an admin."
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Command on cooldown. Try again in **{error.retry_after:.1f}** second(s)."
    elif isinstance(error, app_commands.CheckFailure):
        msg = "You cannot use this command right now."
    else:
        msg = "An unexpected error occurred. Please try again or contact an admin."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except (discord.Forbidden, discord.HTTPException):
        pass


# ── Environment validation ────────────────────────────────────────────────────
def _validate_env() -> None:
    """Validate every required environment variable before the bot starts."""
    required_numeric = (
        "GUILD_ID",
        "WELCOME_CHANNEL_ID",
        "QUEUE_BOARD_CHANNEL_ID",
        "OPS_LOG_CHANNEL_ID",
        "WAITING_ROOM_CHANNEL_ID",
        "OPERATOR_ROOM_CHANNEL_ID",
        "QUEUED_ROLE_ID",
        "VERIFIED_ROLE_ID",
        "OPERATOR_ROLE_ID",
    )

    token = (os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN is missing or empty. Copy .env.example to .env and "
            "fill in every value before starting the bot."
        )

    for name in required_numeric:
        raw = (os.getenv(name) or "").strip()
        if not raw:
            raise SystemExit(
                f"{name} is missing or empty. Copy .env.example to .env and "
                "fill in every required value before starting the bot."
            )
        if not raw.isdigit():
            raise SystemExit(
                f"{name} must be a numeric Discord ID (got {raw!r}). "
                "Right-click the server/channel/role in Discord and choose Copy ID."
            )


# ── Main ──────────────────────────────────────────────────────────────────────
async def main() -> None:
    _validate_env()
    token = os.getenv("DISCORD_TOKEN").strip()

    await db.init_db()

    async with bot:
        await bot.load_extension("cogs.queue")
        logger.info("Loaded cog: cogs.queue")
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
