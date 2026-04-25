"""
Persistent Discord UI views for the Support Queue Bot.

The RequestSupportView is re-registered on every bot start so the button
continues to work across restarts (timeout=None + stable custom_id).
"""

import asyncio
import logging
import os

import discord

import database as db

logger = logging.getLogger(__name__)

_user_locks: dict[str, asyncio.Lock] = {}


class RequestSupportView(discord.ui.View):
    """Panel posted in #welcome-read-first.  Survives bot restarts."""

    def __init__(self) -> None:
        super().__init__(timeout=None)  # persistent — never expires

    @discord.ui.button(
        label="Request Support",
        style=discord.ButtonStyle.primary,
        emoji="\U0001f3ab",  # 🎫
        custom_id="support_queue:request_support",
    )
    async def request_support(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        user = interaction.user
        guild = interaction.guild

        user_key = str(user.id)
        lock = _user_locks.setdefault(user_key, asyncio.Lock())
        try:
            async with lock:
                # ── Guard: one active ticket per user ────────────────────────
                existing = await db.get_active_ticket_for_user(user_key)
                if existing:
                    await interaction.followup.send(
                        f"You already have an open ticket **#{existing['number']}** "
                        f"(status: `{existing['status']}`). "
                        "Please wait for it to be resolved before requesting a new one.",
                        ephemeral=True,
                    )
                    return

                # ── Assign Queued role ───────────────────────────────────────
                queued_role_id = os.getenv("QUEUED_ROLE_ID")
                if queued_role_id:
                    queued_role = guild.get_role(int(queued_role_id))
                    if queued_role:
                        try:
                            await user.add_roles(queued_role, reason="Support ticket requested")
                        except discord.Forbidden:
                            logger.error(
                                "Missing permissions to assign Queued role to user %s", user.id
                            )
                        except discord.HTTPException as exc:
                            logger.error("Failed to assign Queued role to %s: %s", user.id, exc)

                # ── Create ticket in DB (atomic under lock) ──────────────────
                ticket_number = await db.create_ticket(user_key)
                position = await db.get_queue_position(user_key)
        finally:
            _user_locks.pop(user_key, None)

        # ── Everything below the lock is non-critical (I/O, notifications) ──
        estimated_wait = position * 5

        # ── DM the user ──────────────────────────────────────────────────────
        dm_sent = True
        try:
            await user.send(
                f"**Support Ticket Created** \U0001f3ab\n"
                f"Your ticket number is **#{ticket_number}**.\n"
                f"Queue position: **#{position}**\n"
                f"Estimated wait: **~{estimated_wait} minute(s)**\n\n"
                "You will be pinged in the server when an operator is ready for you. "
                "Please keep your DMs open."
            )
        except discord.Forbidden:
            dm_sent = False
            logger.warning("Cannot DM user %s — DMs may be disabled.", user.id)
        except discord.HTTPException as exc:
            dm_sent = False
            logger.error("DM to user %s failed: %s", user.id, exc)

        # ── Post embed to queue board ─────────────────────────────────────────
        board_channel_id = os.getenv("QUEUE_BOARD_CHANNEL_ID")
        if board_channel_id:
            channel = guild.get_channel(int(board_channel_id))
            if channel:
                embed = discord.Embed(
                    title=f"\U0001f3ab New Ticket #{ticket_number}",
                    color=discord.Color.blue(),
                    timestamp=discord.utils.utcnow(),
                )
                embed.add_field(name="User", value=user.mention, inline=True)
                embed.add_field(name="Position", value=f"#{position}", inline=True)
                embed.add_field(name="Status", value="`queued`", inline=True)
                if not dm_sent:
                    embed.add_field(
                        name="DM",
                        value="⚠️ Could not DM the user — they may have DMs disabled.",
                        inline=False,
                    )
                try:
                    await channel.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.error("Failed to post to queue board: %s", exc)

        confirmation = (
            f"\U0001f3ab **Ticket #{ticket_number} created!**\n"
            f"You are **#{position}** in the queue (~{estimated_wait} min wait).\n"
        )
        if dm_sent:
            confirmation += (
                "Check your DMs for details. You'll be pinged here when it's your turn."
            )
        else:
            confirmation += (
                "⚠️ I couldn't DM you — please enable DMs from server members so you receive ticket updates."
            )

        await interaction.followup.send(confirmation, ephemeral=True)

        logger.info(
            "Ticket #%d created | user=%s (%s) | position=%d | dm_sent=%s",
            ticket_number,
            user.id,
            user.name,
            position,
            dm_sent,
        )
