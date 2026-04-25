"""
Queue cog — all slash commands for the Support Queue Bot.

Commands:
  /queue               – position for users; full list for operators
  /call [number]       – operator calls next/specific ticket
  /complete <number>   – operator completes a ticket
  /skip <number>       – operator skips a ticket (no-show logic)
  /drop <number> <reason> – operator drops a ticket with reason
  /shift on|off        – operator toggles shift status
  /setup_panel         – admin posts the support button to #welcome-read-first

Voice model: two pre-existing static channels (no dynamic create/delete).
  WAITING_ROOM_CHANNEL_ID  – staging area; ticket holder is moved here first.
  OPERATOR_ROOM_CHANNEL_ID – private call room; both parties end up here.

The 10-minute no-show timer is DB-backed: called_at is stamped on /call and
main.py's check_stale_tickets loop fires _process_skip for overdue tickets.
No asyncio.create_task timers are used here.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import database as db

logger = logging.getLogger(__name__)


# ── Embed factory helpers ─────────────────────────────────────────────────────

def _err_embed(description: str) -> discord.Embed:
    """Red embed for errors and access-denied messages."""
    return discord.Embed(description=description, color=discord.Color.red())


def _ok_embed(title: str) -> discord.Embed:
    """Green embed for successful operations."""
    return discord.Embed(
        title=title,
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow(),
    )


def _info_embed(title: str) -> discord.Embed:
    """Blue embed for informational / status messages."""
    return discord.Embed(
        title=title,
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow(),
    )


def _warn_embed(title: str) -> discord.Embed:
    """Orange embed for warnings."""
    return discord.Embed(
        title=title,
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow(),
    )


# ── Operator check ────────────────────────────────────────────────────────────

def _is_operator(interaction: discord.Interaction) -> bool:
    """Return True if the interaction member holds the configured Operator role."""
    role_id = os.getenv("OPERATOR_ROLE_ID")
    if not role_id:
        return False
    role = interaction.guild.get_role(int(role_id))
    return role in interaction.user.roles if role else False


# ── Cog ───────────────────────────────────────────────────────────────────────

class QueueCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._call_lock = asyncio.Lock()
        self._skip_locks: dict[int, asyncio.Lock] = {}

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _ops_log(self, guild: discord.Guild, message: str) -> None:
        """Post a plain-text line to #ops-log."""
        channel_id = os.getenv("OPS_LOG_CHANNEL_ID")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if channel:
            try:
                await channel.send(content=message)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.error("ops-log send failed: %s", exc)

    async def _cleanup_voice(
        self, guild: discord.Guild, member: Optional[discord.Member]
    ) -> None:
        """Disconnect ticket holder from OPERATOR_ROOM_CHANNEL_ID if they are in it.

        With static rooms we never delete a channel — we only move the user out.
        """
        if not member:
            return
        op_room_id = os.getenv("OPERATOR_ROOM_CHANNEL_ID")
        if not op_room_id:
            return
        op_room = guild.get_channel(int(op_room_id))
        if not op_room:
            return
        if (
            member.voice
            and member.voice.channel
            and member.voice.channel.id == op_room.id
        ):
            try:
                await member.move_to(None)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.error(
                    "Failed to disconnect %s from operator room: %s", member.id, exc
                )

    async def _remove_queued_role(
        self, guild: discord.Guild, member: Optional[discord.Member], reason: str
    ) -> None:
        role_id = os.getenv("QUEUED_ROLE_ID")
        if not role_id or not member:
            return
        role = guild.get_role(int(role_id))
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason=reason)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.error("Failed to remove Queued role from %s: %s", member.id, exc)

    async def _process_skip(
        self,
        guild: discord.Guild,
        ticket_number: int,
        auto: bool = False,
    ) -> None:
        """Core skip logic: increment no_shows, drop or requeue, disconnect from VC.

        Called both by /skip (manual) and the stale-ticket background loop (auto).
        Re-fetches the ticket from DB so it is safe to call after a restart.
        """
        lock = self._skip_locks.setdefault(ticket_number, asyncio.Lock())
        try:
            async with lock:
                ticket = await db.get_ticket(ticket_number)
                if not ticket or ticket["status"] not in ("queued", "active"):
                    return

                no_shows = await db.increment_no_shows(ticket_number)
                member = guild.get_member(int(ticket["handle_id"]))
                tag = "[AUTO] " if auto else ""

                dm_sent = True
                if no_shows >= 2:
                    await db.update_ticket_status(
                        ticket_number,
                        "dropped",
                        closed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    await self._remove_queued_role(
                        guild,
                        member,
                        f"Ticket #{ticket_number} dropped after {no_shows} no-shows",
                    )
                    result = f"**dropped** after {no_shows} no-shows"

                    if member:
                        try:
                            await member.send(
                                f"Your support ticket **#{ticket_number}** has been dropped "
                                f"after {no_shows} missed calls. "
                                "Please create a new ticket if you still need help."
                            )
                        except (discord.Forbidden, discord.HTTPException):
                            dm_sent = False
                            logger.warning(
                                "Could not DM user %s about drop of ticket #%d",
                                ticket["handle_id"],
                                ticket_number,
                            )
                else:
                    await db.requeue_ticket(ticket_number)
                    result = f"moved to **back of queue** (no-shows: {no_shows}/2)"

                # Disconnect from static operator room (does not delete the channel)
                await self._cleanup_voice(guild, member)

                user_ref = member.mention if member else f"user {ticket['handle_id']}"
                log_message = (
                    f"{tag}Ticket **#{ticket_number}** skipped — {result}. "
                    f"User: {user_ref}"
                )
                if no_shows >= 2 and member and not dm_sent:
                    log_message += " ⚠️ DM to user failed."
                await self._ops_log(guild, log_message)
                logger.info(
                    "%sTicket #%d skipped | user=%s | result=%s | dm_sent=%s",
                    tag,
                    ticket_number,
                    ticket["handle_id"],
                    result,
                    dm_sent,
                )
        finally:
            self._skip_locks.pop(ticket_number, None)

    # ── /queue ────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="queue",
        description="Check your queue position, or view the full queue (operators).",
    )
    @app_commands.guild_only()
    async def queue_cmd(self, interaction: discord.Interaction) -> None:
        if _is_operator(interaction):
            tickets = await db.get_all_queued_tickets()
            embed = _info_embed("\U0001f4cb Support Queue")
            if not tickets:
                embed.description = "The queue is currently empty."
            else:
                lines = []
                for idx, t in enumerate(tickets, 1):
                    m = interaction.guild.get_member(int(t["handle_id"]))
                    display = m.display_name if m else f"<ID {t['handle_id']}>"
                    lines.append(
                        f"**#{idx}** — Ticket **#{t['number']}** | {display} | "
                        f"No-shows: {t['no_shows']}"
                    )
                embed.description = "\n".join(lines)
                embed.set_footer(text=f"Total queued: {len(tickets)}")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        else:
            ticket = await db.get_active_ticket_for_user(str(interaction.user.id))
            if not ticket:
                await interaction.response.send_message(
                    embed=_err_embed(
                        "You don't have an active ticket. "
                        "Use the **Request Support** button to join the queue."
                    ),
                    ephemeral=True,
                )
                return

            embed = _info_embed("\U0001f3ab Your Queue Status")
            embed.add_field(name="Ticket", value=f"#{ticket['number']}", inline=True)
            embed.add_field(name="Status", value=f"`{ticket['status']}`", inline=True)

            if ticket["status"] == "active":
                embed.description = (
                    "Your ticket is currently **active** — an operator has called "
                    "you. Please join the operator voice room as soon as possible."
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            position = await db.get_queue_position(str(interaction.user.id))
            estimated = position * 5
            embed.add_field(name="Position", value=f"#{position}", inline=True)
            embed.add_field(name="Estimated Wait", value=f"~{estimated} min", inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /call ─────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="call",
        description="[Operator] Call the next ticket or a specific ticket number.",
    )
    @app_commands.guild_only()
    @app_commands.describe(number="Ticket number to call (omit to call the next in queue)")
    async def call_cmd(
        self, interaction: discord.Interaction, number: Optional[int] = None
    ) -> None:
        if not _is_operator(interaction):
            await interaction.response.send_message(
                embed=_err_embed("Only operators can use this command."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        active_count = await db.get_active_tickets_count()
        if active_count > 0:
            await interaction.followup.send(
                embed=_err_embed(
                    "Another support call is currently in progress. "
                    "Use /complete or /skip to resolve the active ticket "
                    "before calling a new one."
                ),
                ephemeral=True,
            )
            return

        async with self._call_lock:
            # ── Fetch ticket ──────────────────────────────────────────────────
            if number is not None:
                ticket = await db.get_ticket(number)
                if not ticket or ticket["status"] != "queued":
                    await interaction.followup.send(
                        embed=_err_embed(
                            f"Ticket #{number} was not found or is not in `queued` status."
                        ),
                        ephemeral=True,
                    )
                    return
            else:
                ticket = await db.get_next_queued_ticket()
                if not ticket:
                    await interaction.followup.send(
                        embed=_info_embed("\U0001f4cb Queue Empty").add_field(
                            name="Status", value="No tickets are currently queued."
                        ),
                        ephemeral=True,
                    )
                    return

            ticket_number = ticket["number"]

            # Stamp called_at so the DB-backed stale-ticket loop can time out no-shows
            await db.update_ticket_status(
                ticket_number,
                "active",
                called_at=datetime.now(timezone.utc).isoformat(),
            )

        member = interaction.guild.get_member(int(ticket["handle_id"]))
        operator = interaction.user

        # ── Resolve static voice channels ─────────────────────────────────────
        # WAITING_ROOM is kept resolvable for potential future use, but the
        # bot no longer stages users through it.
        waiting_room_id = os.getenv("WAITING_ROOM_CHANNEL_ID")
        op_room_id = os.getenv("OPERATOR_ROOM_CHANNEL_ID")
        _ = (
            interaction.guild.get_channel(int(waiting_room_id)) if waiting_room_id else None
        )
        op_room = (
            interaction.guild.get_channel(int(op_room_id)) if op_room_id else None
        )

        # ── Move operator to OPERATOR_ROOM (voice-state guard) ────────────────
        if op_room and operator.voice is not None:
            try:
                await operator.move_to(op_room)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Could not move operator to operator room: %s", exc)

        # ── Handle ticket-holder voice state ──────────────────────────────────
        user_in_voice = member is not None and member.voice is not None
        op_room_ref = op_room.mention if op_room else "`OPERATOR_ROOM_CHANNEL_ID not set`"

        if user_in_voice and op_room:
            try:
                await member.move_to(op_room)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.error(
                    "Failed to move user %s to operator room: %s", member.id, exc
                )

        # ── Notify ticket holder in queue-board / ops-log ─────────────────────
        notify_channel_id = os.getenv("QUEUE_BOARD_CHANNEL_ID") or os.getenv(
            "OPS_LOG_CHANNEL_ID"
        )
        if notify_channel_id:
            notify_ch = interaction.guild.get_channel(int(notify_channel_id))
            if notify_ch:
                ping = member.mention if member else f"<ID {ticket['handle_id']}>"
                if not user_in_voice:
                    # Warning embed: user is not currently in voice
                    warn = _warn_embed("⚠️ User Not in Voice")
                    warn.description = (
                        f"{ping}, your ticket **#{ticket_number}** is ready!\n"
                        f"Please join {op_room_ref} within **10 minutes** "
                        "or your spot will be skipped."
                    )
                    try:
                        await notify_ch.send(embed=warn)
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        logger.error(
                            "Failed to send voice warning for ticket #%d: %s",
                            ticket_number,
                            exc,
                        )
                else:
                    try:
                        await notify_ch.send(
                            f"\U0001f514 {ping} — ticket **#{ticket_number}** is active! "
                            f"You have been moved to {op_room_ref}."
                        )
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        logger.error(
                            "Failed to notify user for ticket #%d: %s", ticket_number, exc
                        )

        # ── Reply to operator ─────────────────────────────────────────────────
        user_ref = member.mention if member else f"user `{ticket['handle_id']}`"
        voice_status = (
            "Moved to voice ✓"
            if user_in_voice
            else "⚠️ Not in voice — 10-min timer started"
        )
        embed = _ok_embed("\U0001f4de Ticket Called")
        embed.add_field(name="Ticket", value=f"#{ticket_number}", inline=True)
        embed.add_field(name="User", value=user_ref, inline=True)
        embed.add_field(name="Operator Room", value=op_room_ref, inline=True)
        embed.add_field(name="Voice Status", value=voice_status, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

        logger.info(
            "Operator %s called ticket #%d | user=%s | user_in_voice=%s",
            operator.id,
            ticket_number,
            ticket["handle_id"],
            user_in_voice,
        )

    # ── /complete ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="complete",
        description="[Operator] Mark a ticket as completed and grant the Verified role.",
    )
    @app_commands.guild_only()
    @app_commands.describe(number="Ticket number to complete")
    async def complete_cmd(self, interaction: discord.Interaction, number: int) -> None:
        if not _is_operator(interaction):
            await interaction.response.send_message(
                embed=_err_embed("Only operators can use this command."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        ticket = await db.get_ticket(number)
        if not ticket or ticket["status"] != "active":
            await interaction.followup.send(
                embed=_err_embed(
                    f"Ticket #{number} was not found or is not in `active` status."
                ),
                ephemeral=True,
            )
            return

        await db.update_ticket_status(
            number,
            "completed",
            closed_at=datetime.now(timezone.utc).isoformat(),
        )

        member = interaction.guild.get_member(int(ticket["handle_id"]))

        # ── Role management ───────────────────────────────────────────────────
        verified_granted = False
        if member:
            verified_role_id = os.getenv("VERIFIED_ROLE_ID")
            if verified_role_id:
                verified_role = interaction.guild.get_role(int(verified_role_id))
                if verified_role:
                    try:
                        await member.add_roles(
                            verified_role, reason=f"Ticket #{number} completed"
                        )
                        verified_granted = True
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        logger.error(
                            "Failed to add Verified role to %s: %s", member.id, exc
                        )
            await self._remove_queued_role(
                interaction.guild, member, f"Ticket #{number} completed"
            )

        # ── Disconnect ticket holder from operator room ───────────────────────
        await self._cleanup_voice(interaction.guild, member)

        user_ref = member.mention if member else f"user `{ticket['handle_id']}`"
        await self._ops_log(
            interaction.guild,
            f"✅ Ticket **#{number}** completed by {interaction.user.mention}. "
            f"User: {user_ref}",
        )

        verified_status = (
            "Granted ✓" if verified_granted else "⚠️ Failed — check bot permissions"
        )
        embed = _ok_embed("✅ Ticket Completed")
        embed.add_field(name="Ticket", value=f"#{number}", inline=True)
        embed.add_field(name="User", value=user_ref, inline=True)
        embed.add_field(name="Verified Role", value=verified_status, inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

        logger.info(
            "Operator %s completed ticket #%d | user=%s",
            interaction.user.id,
            number,
            ticket["handle_id"],
        )

    # ── /skip ─────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="skip",
        description="[Operator] Skip a ticket (no-show). Drops after 2nd no-show.",
    )
    @app_commands.guild_only()
    @app_commands.describe(number="Ticket number to skip")
    async def skip_cmd(self, interaction: discord.Interaction, number: int) -> None:
        if not _is_operator(interaction):
            await interaction.response.send_message(
                embed=_err_embed("Only operators can use this command."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        ticket = await db.get_ticket(number)
        if not ticket or ticket["status"] not in ("queued", "active"):
            current = ticket["status"] if ticket else "not found"
            await interaction.followup.send(
                embed=_err_embed(
                    f"Ticket #{number} cannot be skipped (current status: `{current}`)."
                ),
                ephemeral=True,
            )
            return

        await self._process_skip(interaction.guild, number, auto=False)

        updated = await db.get_ticket(number)
        new_status = updated["status"] if updated else "unknown"

        embed = _warn_embed("⏭️ Ticket Skipped")
        embed.add_field(name="Ticket", value=f"#{number}", inline=True)
        embed.add_field(name="New Status", value=f"`{new_status}`", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

        logger.info("Operator %s skipped ticket #%d", interaction.user.id, number)

    # ── /drop ─────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="drop",
        description="[Operator] Drop a ticket immediately with a reason.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        number="Ticket number to drop",
        reason="Reason for dropping this ticket",
    )
    async def drop_cmd(
        self, interaction: discord.Interaction, number: int, reason: str
    ) -> None:
        if not _is_operator(interaction):
            await interaction.response.send_message(
                embed=_err_embed("Only operators can use this command."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        ticket = await db.get_ticket(number)
        if not ticket or ticket["status"] in ("completed", "dropped"):
            await interaction.followup.send(
                embed=_err_embed(
                    f"Ticket #{number} was not found or is already closed."
                ),
                ephemeral=True,
            )
            return

        await db.update_ticket_status(
            number,
            "dropped",
            closed_at=datetime.now(timezone.utc).isoformat(),
        )

        member = interaction.guild.get_member(int(ticket["handle_id"]))

        await self._remove_queued_role(
            interaction.guild, member, f"Ticket #{number} dropped: {reason}"
        )

        # DM the user with drop reason
        dm_sent = True
        if member:
            try:
                await member.send(
                    f"Your support ticket **#{number}** has been dropped.\n"
                    f"**Reason:** {reason}\n\n"
                    "Please create a new ticket if you still need assistance."
                )
            except (discord.Forbidden, discord.HTTPException):
                dm_sent = False
                logger.warning(
                    "Could not DM user %s about drop of ticket #%d",
                    member.id,
                    number,
                )

        # Disconnect ticket holder from operator room
        await self._cleanup_voice(interaction.guild, member)

        user_ref = member.mention if member else f"user `{ticket['handle_id']}`"
        ops_message = (
            f"\U0001f6ab Ticket **#{number}** dropped by {interaction.user.mention}.\n"
            f"**Reason:** {reason} | User: {user_ref}"
        )
        if member and not dm_sent:
            ops_message += " ⚠️ DM to user failed."
        await self._ops_log(interaction.guild, ops_message)

        embed = discord.Embed(
            title="\U0001f6ab Ticket Dropped",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Ticket", value=f"#{number}", inline=True)
        embed.add_field(name="User", value=user_ref, inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        if member and not dm_sent:
            embed.add_field(
                name="DM",
                value="⚠️ Could not DM the user — they may have DMs disabled.",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

        logger.info(
            "Operator %s dropped ticket #%d | reason=%s",
            interaction.user.id,
            number,
            reason,
        )

    # ── /shift ────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="shift",
        description="[Operator] Toggle your shift status on or off.",
    )
    @app_commands.guild_only()
    @app_commands.describe(status="on = clock in, off = clock out")
    @app_commands.choices(
        status=[
            app_commands.Choice(name="On", value="on"),
            app_commands.Choice(name="Off", value="off"),
        ]
    )
    async def shift_cmd(self, interaction: discord.Interaction, status: str) -> None:
        if not _is_operator(interaction):
            await interaction.response.send_message(
                embed=_err_embed("Only operators can use this command."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        warning = ""
        if status == "off":
            active_count = await db.get_active_tickets_count()
            if active_count > 0:
                warning = (
                    f"\n⚠️ **Warning:** There are **{active_count}** "
                    "active ticket(s) in progress. Make sure they are handed off or resolved."
                )

        await db.set_operator_status(str(interaction.user.id), status)

        emoji = "\U0001f7e2" if status == "on" else "\U0001f534"
        await self._ops_log(
            interaction.guild,
            f"{emoji} {interaction.user.mention} is now **{status.upper()}** shift.",
        )
        await interaction.followup.send(
            f"Your shift is now **{status.upper()}**.{warning}", ephemeral=True
        )
        logger.info("Operator %s set shift to %s", interaction.user.id, status)

    # ── /setup_panel ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="setup_panel",
        description="[Admin] Post the Request Support button panel to the welcome channel.",
    )
    @app_commands.guild_only()
    async def setup_panel_cmd(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=_err_embed("Only server administrators can use this command."),
                ephemeral=True,
            )
            return

        channel_id = os.getenv("WELCOME_CHANNEL_ID")
        if not channel_id:
            await interaction.response.send_message(
                embed=_err_embed("WELCOME_CHANNEL_ID is not set in the `.env` file."),
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(int(channel_id))
        if not channel:
            await interaction.response.send_message(
                embed=_err_embed(f"Could not find a channel with ID `{channel_id}`."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        from views import RequestSupportView

        embed = discord.Embed(
            title="\U0001f3ab Support Queue",
            description=(
                "Need help from our team? Click the button below to join the support queue.\n\n"
                "• You will receive a **DM** with your ticket number.\n"
                "• An operator will **ping you** when it is your turn.\n"
                "• Make sure your DMs are open and you are watching for pings.\n\n"
                "_One active ticket per user._"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(
            text="Support Queue Bot — use /queue to check your position at any time."
        )

        try:
            await channel.send(embed=embed, view=RequestSupportView())
            await interaction.followup.send(
                embed=_ok_embed(f"✅ Panel posted to {channel.mention}!"),
                ephemeral=True,
            )
            logger.info(
                "Admin %s posted support panel to channel %s",
                interaction.user.id,
                channel_id,
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.followup.send(
                embed=_err_embed(f"Failed to post panel: {exc}"),
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(QueueCog(bot))
