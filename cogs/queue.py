"""
Queue cog — all slash commands for the Support Queue Bot.

Commands:
  /queue            – position for users; full list for operators
  /call [number]    – operator calls next/specific ticket
  /complete number  – operator completes a ticket
  /skip number      – operator skips a ticket (no-show logic)
  /drop number reason – operator drops a ticket with reason
  /shift on|off     – operator toggles shift status
  /setup_panel      – admin posts the support button to #welcome-read-first
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

NO_SHOW_TIMEOUT = 600  # seconds (10 minutes)


def _is_operator(interaction: discord.Interaction) -> bool:
    """Return True if the interaction member holds the configured Operator role."""
    role_id = os.getenv("OPERATOR_ROLE_ID")
    if not role_id:
        return False
    role = interaction.guild.get_role(int(role_id))
    return role in interaction.user.roles if role else False


class QueueCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Maps ticket_number -> asyncio.Task for the 10-min no-show timer
        self._no_show_tasks: dict[int, asyncio.Task] = {}

    # ────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ────────────────────────────────────────────────────────────────────────

    async def _ops_log(
        self,
        guild: discord.Guild,
        message: str,
        embed: Optional[discord.Embed] = None,
    ) -> None:
        channel_id = os.getenv("OPS_LOG_CHANNEL_ID")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if channel:
            try:
                await channel.send(content=message, embed=embed)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.error("ops-log send failed: %s", exc)

    async def _delete_voice_channel(
        self, guild: discord.Guild, channel_id: Optional[str], reason: str
    ) -> None:
        if not channel_id:
            return
        vc = guild.get_channel(int(channel_id))
        if not vc:
            return
        # Disconnect everyone first
        for member in list(vc.members):
            try:
                await member.move_to(None)
            except (discord.Forbidden, discord.HTTPException):
                pass
        try:
            await vc.delete(reason=reason)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.error("Could not delete voice channel %s: %s", channel_id, exc)

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
        """Core skip logic: increment no_shows, drop or requeue, clean up VC."""
        ticket = await db.get_ticket(ticket_number)
        if not ticket or ticket["status"] not in ("queued", "active"):
            return

        no_shows = await db.increment_no_shows(ticket_number)
        member = guild.get_member(int(ticket["handle_id"]))
        tag = "[AUTO] " if auto else ""

        if no_shows >= 2:
            await db.update_ticket_status(
                ticket_number, "dropped", closed_at=datetime.now(timezone.utc).isoformat()
            )
            await self._remove_queued_role(
                guild, member, f"Ticket #{ticket_number} dropped after {no_shows} no-shows"
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
                    pass
        else:
            await db.move_ticket_to_back(ticket_number)
            await db.update_ticket_status(ticket_number, "queued")
            result = f"moved to **back of queue** (no-shows: {no_shows}/2)"

        await self._delete_voice_channel(
            guild,
            ticket["voice_channel_id"],
            reason=f"Ticket #{ticket_number} skipped",
        )

        user_ref = member.mention if member else f"user {ticket['handle_id']}"
        await self._ops_log(
            guild,
            f"{tag}Ticket **#{ticket_number}** skipped — {result}. User: {user_ref}",
        )
        logger.info(
            "%sTicket #%d skipped | user=%s | result=%s",
            tag,
            ticket_number,
            ticket["handle_id"],
            result,
        )

    async def _no_show_timer(
        self,
        guild: discord.Guild,
        ticket_number: int,
        member_id: int,
    ) -> None:
        """Wait NO_SHOW_TIMEOUT seconds; auto-skip if user never joined voice."""
        await asyncio.sleep(NO_SHOW_TIMEOUT)

        ticket = await db.get_ticket(ticket_number)
        if not ticket or ticket["status"] != "active":
            return  # already resolved while we waited

        # Check if member is currently in the support voice channel
        member = guild.get_member(member_id)
        vc_id = ticket["voice_channel_id"]
        if vc_id and member and member.voice:
            vc = guild.get_channel(int(vc_id))
            if vc and member.voice.channel and member.voice.channel.id == vc.id:
                return  # user joined in time — do nothing

        logger.info("No-show timer fired for ticket #%d, triggering auto-skip.", ticket_number)
        await self._process_skip(guild, ticket_number, auto=True)

    # ────────────────────────────────────────────────────────────────────────
    # /queue
    # ────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="queue", description="Check your queue position, or view the full queue (operators)."
    )
    async def queue_cmd(self, interaction: discord.Interaction) -> None:
        if _is_operator(interaction):
            tickets = await db.get_all_queued_tickets()
            if not tickets:
                await interaction.response.send_message(
                    "The queue is currently empty.", ephemeral=True
                )
                return

            embed = discord.Embed(
                title="\U0001f4cb Support Queue",
                color=discord.Color.blue(),
                timestamp=discord.utils.utcnow(),
            )
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
                    "You don't have an active ticket. "
                    "Use the **Request Support** button to join the queue.",
                    ephemeral=True,
                )
                return
            position = await db.get_queue_position(str(interaction.user.id))
            estimated = position * 5
            await interaction.response.send_message(
                f"\U0001f3ab **Your Queue Status**\n"
                f"Ticket: **#{ticket['number']}** | Status: `{ticket['status']}`\n"
                f"Position: **#{position}** | Estimated wait: **~{estimated} min**",
                ephemeral=True,
            )

    # ────────────────────────────────────────────────────────────────────────
    # /call
    # ────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="call",
        description="[Operator] Call the next ticket or a specific ticket number.",
    )
    @app_commands.describe(number="Ticket number to call (omit to call the next in queue)")
    async def call_cmd(
        self, interaction: discord.Interaction, number: Optional[int] = None
    ) -> None:
        if not _is_operator(interaction):
            await interaction.response.send_message(
                "Only operators can use this command.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        if number is not None:
            ticket = await db.get_ticket(number)
            if not ticket or ticket["status"] != "queued":
                await interaction.followup.send(
                    f"Ticket #{number} not found or is not in `queued` status.",
                    ephemeral=True,
                )
                return
        else:
            ticket = await db.get_next_queued_ticket()
            if not ticket:
                await interaction.followup.send(
                    "The queue is empty — nothing to call.", ephemeral=True
                )
                return

        ticket_number = ticket["number"]
        member = interaction.guild.get_member(int(ticket["handle_id"]))
        operator = interaction.user
        now = datetime.now(timezone.utc).isoformat()

        await db.update_ticket_status(ticket_number, "active", called_at=now)

        # ── Create private voice channel ─────────────────────────────────────
        category_id = os.getenv("SUPPORT_CATEGORY_ID")
        category = (
            interaction.guild.get_channel(int(category_id))
            if category_id
            else None
        )

        overwrites: dict = {
            interaction.guild.default_role: discord.PermissionOverwrite(
                connect=False, view_channel=False
            ),
            operator: discord.PermissionOverwrite(
                connect=True, view_channel=True, speak=True, move_members=True
            ),
        }
        if member:
            overwrites[member] = discord.PermissionOverwrite(
                connect=True, view_channel=True, speak=True
            )

        voice_channel: Optional[discord.VoiceChannel] = None
        try:
            voice_channel = await interaction.guild.create_voice_channel(
                name=f"Support Room #{ticket_number}",
                category=category,
                overwrites=overwrites,
                reason=f"Support ticket #{ticket_number} called by {operator}",
            )
            await db.set_ticket_voice_channel(ticket_number, str(voice_channel.id))
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.error(
                "Failed to create voice channel for ticket #%d: %s", ticket_number, exc
            )

        # ── Notify user in queue-board / ops-log channel ─────────────────────
        notify_channel_id = os.getenv("QUEUE_BOARD_CHANNEL_ID") or os.getenv(
            "OPS_LOG_CHANNEL_ID"
        )
        if notify_channel_id:
            notify_channel = interaction.guild.get_channel(int(notify_channel_id))
            if notify_channel:
                vc_ref = voice_channel.mention if voice_channel else "a support voice channel"
                ping = member.mention if member else f"<Ticket holder {ticket['handle_id']}>"
                try:
                    await notify_channel.send(
                        f"\U0001f514 {ping} — your ticket **#{ticket_number}** is ready! "
                        f"Please join {vc_ref} now. You have **10 minutes** before your spot is skipped."
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.error("Failed to ping user for ticket #%d: %s", ticket_number, exc)

        # ── Move operator into voice channel (if already in one) ─────────────
        if voice_channel and operator.voice:
            try:
                await operator.move_to(voice_channel)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Could not move operator to VC: %s", exc)

        # ── Start no-show timer ──────────────────────────────────────────────
        if member:
            task = asyncio.create_task(
                self._no_show_timer(interaction.guild, ticket_number, member.id)
            )
            self._no_show_tasks[ticket_number] = task

        vc_info = f"\nVoice channel: {voice_channel.mention}" if voice_channel else ""
        user_ref = member.mention if member else f"user `{ticket['handle_id']}`"
        await interaction.followup.send(
            f"\U0001f4de Called ticket **#{ticket_number}** for {user_ref}.{vc_info}\n"
            "10-minute no-show timer has started.",
            ephemeral=True,
        )
        logger.info(
            "Operator %s called ticket #%d | user=%s",
            operator.id,
            ticket_number,
            ticket["handle_id"],
        )

    # ────────────────────────────────────────────────────────────────────────
    # /complete
    # ────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="complete",
        description="[Operator] Mark a ticket as completed and grant the Verified role.",
    )
    @app_commands.describe(number="Ticket number to complete")
    async def complete_cmd(self, interaction: discord.Interaction, number: int) -> None:
        if not _is_operator(interaction):
            await interaction.response.send_message(
                "Only operators can use this command.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        ticket = await db.get_ticket(number)
        if not ticket or ticket["status"] != "active":
            await interaction.followup.send(
                f"Ticket #{number} not found or is not `active`.", ephemeral=True
            )
            return

        now = datetime.now(timezone.utc).isoformat()
        await db.update_ticket_status(number, "completed", closed_at=now)

        # Cancel no-show timer if it's still running
        if number in self._no_show_tasks:
            self._no_show_tasks.pop(number).cancel()

        member = interaction.guild.get_member(int(ticket["handle_id"]))

        # ── Role management ───────────────────────────────────────────────────
        if member:
            verified_role_id = os.getenv("VERIFIED_ROLE_ID")
            if verified_role_id:
                verified_role = interaction.guild.get_role(int(verified_role_id))
                if verified_role:
                    try:
                        await member.add_roles(
                            verified_role, reason=f"Ticket #{number} completed"
                        )
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        logger.error("Failed to add Verified role to %s: %s", member.id, exc)

            await self._remove_queued_role(
                interaction.guild, member, f"Ticket #{number} completed"
            )

        # ── Clean up voice channel ────────────────────────────────────────────
        await self._delete_voice_channel(
            interaction.guild,
            ticket["voice_channel_id"],
            reason=f"Ticket #{number} completed",
        )

        user_ref = member.mention if member else f"user `{ticket['handle_id']}`"
        await self._ops_log(
            interaction.guild,
            f"✅ Ticket **#{number}** completed by {interaction.user.mention}. "
            f"User: {user_ref}",
        )
        await interaction.followup.send(
            f"✅ Ticket **#{number}** marked as completed. "
            f"{user_ref} has been granted the Verified role.",
            ephemeral=True,
        )
        logger.info(
            "Operator %s completed ticket #%d | user=%s",
            interaction.user.id,
            number,
            ticket["handle_id"],
        )

    # ────────────────────────────────────────────────────────────────────────
    # /skip
    # ────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="skip",
        description="[Operator] Skip a ticket (no-show). Drops after 2nd no-show.",
    )
    @app_commands.describe(number="Ticket number to skip")
    async def skip_cmd(self, interaction: discord.Interaction, number: int) -> None:
        if not _is_operator(interaction):
            await interaction.response.send_message(
                "Only operators can use this command.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        ticket = await db.get_ticket(number)
        if not ticket or ticket["status"] not in ("queued", "active"):
            await interaction.followup.send(
                f"Ticket #{number} not found or cannot be skipped (status: "
                f"`{ticket['status'] if ticket else 'not found'}`).",
                ephemeral=True,
            )
            return

        # Cancel running timer so it doesn't fire again
        if number in self._no_show_tasks:
            self._no_show_tasks.pop(number).cancel()

        await self._process_skip(interaction.guild, number, auto=False)

        # Fetch fresh to report result
        updated = await db.get_ticket(number)
        status_str = updated["status"] if updated else "unknown"
        await interaction.followup.send(
            f"Ticket **#{number}** has been skipped. New status: `{status_str}`.",
            ephemeral=True,
        )
        logger.info("Operator %s skipped ticket #%d", interaction.user.id, number)

    # ────────────────────────────────────────────────────────────────────────
    # /drop
    # ────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="drop",
        description="[Operator] Drop a ticket immediately with a reason.",
    )
    @app_commands.describe(
        number="Ticket number to drop",
        reason="Reason for dropping this ticket",
    )
    async def drop_cmd(
        self, interaction: discord.Interaction, number: int, reason: str
    ) -> None:
        if not _is_operator(interaction):
            await interaction.response.send_message(
                "Only operators can use this command.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        ticket = await db.get_ticket(number)
        if not ticket or ticket["status"] in ("completed", "dropped"):
            await interaction.followup.send(
                f"Ticket #{number} not found or is already closed.",
                ephemeral=True,
            )
            return

        now = datetime.now(timezone.utc).isoformat()
        await db.update_ticket_status(number, "dropped", closed_at=now)

        # Cancel running no-show timer
        if number in self._no_show_tasks:
            self._no_show_tasks.pop(number).cancel()

        member = interaction.guild.get_member(int(ticket["handle_id"]))

        await self._remove_queued_role(
            interaction.guild, member, f"Ticket #{number} dropped: {reason}"
        )

        # DM the user
        if member:
            try:
                await member.send(
                    f"Your support ticket **#{number}** has been dropped.\n"
                    f"**Reason:** {reason}\n\n"
                    "Please create a new ticket if you still need assistance."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        # Clean up any voice channel
        await self._delete_voice_channel(
            interaction.guild,
            ticket["voice_channel_id"],
            reason=f"Ticket #{number} dropped",
        )

        user_ref = member.mention if member else f"user `{ticket['handle_id']}`"
        await self._ops_log(
            interaction.guild,
            f"\U0001f6ab Ticket **#{number}** dropped by {interaction.user.mention}.\n"
            f"**Reason:** {reason} | User: {user_ref}",
        )
        await interaction.followup.send(
            f"Ticket **#{number}** dropped. Reason: {reason}", ephemeral=True
        )
        logger.info(
            "Operator %s dropped ticket #%d | reason=%s",
            interaction.user.id,
            number,
            reason,
        )

    # ────────────────────────────────────────────────────────────────────────
    # /shift
    # ────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="shift",
        description="[Operator] Toggle your shift status on or off.",
    )
    @app_commands.describe(status="on = clock in, off = clock out")
    @app_commands.choices(
        status=[
            app_commands.Choice(name="On", value="on"),
            app_commands.Choice(name="Off", value="off"),
        ]
    )
    async def shift_cmd(
        self, interaction: discord.Interaction, status: str
    ) -> None:
        if not _is_operator(interaction):
            await interaction.response.send_message(
                "Only operators can use this command.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        warning = ""
        if status == "off":
            active_count = await db.get_active_tickets_count()
            if active_count > 0:
                warning = (
                    f"\n⚠️ **Warning:** There are **{active_count}** "
                    "active ticket(s) currently in progress. Make sure they are "
                    "handed off or resolved."
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

    # ────────────────────────────────────────────────────────────────────────
    # /setup_panel  (admin only — posts the button to #welcome-read-first)
    # ────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="setup_panel",
        description="[Admin] Post the Request Support button panel to the welcome channel.",
    )
    async def setup_panel_cmd(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server administrators can use this command.", ephemeral=True
            )
            return

        channel_id = os.getenv("WELCOME_CHANNEL_ID")
        if not channel_id:
            await interaction.response.send_message(
                "WELCOME_CHANNEL_ID is not set in the `.env` file.", ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(int(channel_id))
        if not channel:
            await interaction.response.send_message(
                f"Could not find a channel with ID `{channel_id}`.", ephemeral=True
            )
            return

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
            await interaction.response.send_message(
                f"✅ Support panel posted to {channel.mention}!", ephemeral=True
            )
            logger.info(
                "Admin %s posted support panel to channel %s", interaction.user.id, channel_id
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.response.send_message(
                f"Failed to post panel: {exc}", ephemeral=True
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(QueueCog(bot))
