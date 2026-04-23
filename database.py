"""
Async SQLite database layer for the Support Queue Bot.

All queries use parameterized statements to prevent SQL injection.
No IP addresses, emails, or message content are stored — only Discord IDs,
ticket metadata, and timestamps.
"""

import aiosqlite
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

DB_PATH = "support_queue.db"
logger = logging.getLogger(__name__)


async def init_db() -> None:
    """Create tables if they do not already exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                number           INTEGER PRIMARY KEY AUTOINCREMENT,
                handle_id        TEXT    NOT NULL,
                referrer_id      TEXT    DEFAULT NULL,
                created_at       TIMESTAMP NOT NULL,
                called_at        TIMESTAMP DEFAULT NULL,
                closed_at        TIMESTAMP DEFAULT NULL,
                no_shows         INTEGER NOT NULL DEFAULT 0,
                status           TEXT    NOT NULL DEFAULT 'queued',
                voice_channel_id TEXT    DEFAULT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS operator_status (
                user_id          TEXT    PRIMARY KEY,
                status           TEXT    NOT NULL DEFAULT 'off',
                last_shift_change TIMESTAMP NOT NULL
            )
        """)
        await db.commit()
    logger.info("Database initialised at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Ticket helpers
# ---------------------------------------------------------------------------

async def create_ticket(handle_id: str, referrer_id: Optional[str] = None) -> int:
    """Insert a new queued ticket and return its auto-incremented number."""
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO tickets (handle_id, referrer_id, created_at, status)
            VALUES (?, ?, ?, 'queued')
            """,
            (handle_id, referrer_id, now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_ticket(number: int) -> Optional[aiosqlite.Row]:
    """Return a single ticket row by its number, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tickets WHERE number = ?", (number,)
        ) as cur:
            return await cur.fetchone()


async def get_active_ticket_for_user(handle_id: str) -> Optional[aiosqlite.Row]:
    """Return the user's currently queued or active ticket, or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM tickets
            WHERE handle_id = ? AND status IN ('queued', 'active')
            LIMIT 1
            """,
            (handle_id,),
        ) as cur:
            return await cur.fetchone()


async def get_next_queued_ticket() -> Optional[aiosqlite.Row]:
    """Return the oldest queued ticket (FIFO), or None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tickets WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
        ) as cur:
            return await cur.fetchone()


async def get_all_queued_tickets() -> list:
    """Return all queued tickets ordered oldest-first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tickets WHERE status = 'queued' ORDER BY created_at ASC"
        ) as cur:
            return await cur.fetchall()


async def get_queue_position(handle_id: str) -> int:
    """Return the 1-based queue position for the user, or 0 if not found."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COUNT(*) FROM tickets
            WHERE status = 'queued'
              AND created_at <= (
                  SELECT created_at FROM tickets
                  WHERE handle_id = ? AND status = 'queued'
                  ORDER BY created_at ASC LIMIT 1
              )
            """,
            (handle_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_active_tickets_count() -> int:
    """Return number of currently active tickets."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM tickets WHERE status = 'active'"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_stale_active_tickets(timeout_minutes: int = 10) -> list:
    """Return active tickets whose called_at is older than timeout_minutes.

    Used by the background stale-ticket checker to auto-skip no-shows that
    survived a bot restart (where in-memory timers would have been lost).
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
    ).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM tickets
            WHERE status = 'active'
              AND called_at IS NOT NULL
              AND called_at < ?
            """,
            (cutoff,),
        ) as cur:
            return await cur.fetchall()


async def update_ticket_status(
    number: int,
    status: str,
    called_at: Optional[str] = None,
    closed_at: Optional[str] = None,
) -> None:
    """Update a ticket's status and optional timestamp fields."""
    async with aiosqlite.connect(DB_PATH) as db:
        if called_at and closed_at:
            await db.execute(
                "UPDATE tickets SET status = ?, called_at = ?, closed_at = ? WHERE number = ?",
                (status, called_at, closed_at, number),
            )
        elif called_at:
            await db.execute(
                "UPDATE tickets SET status = ?, called_at = ? WHERE number = ?",
                (status, called_at, number),
            )
        elif closed_at:
            await db.execute(
                "UPDATE tickets SET status = ?, closed_at = ? WHERE number = ?",
                (status, closed_at, number),
            )
        else:
            await db.execute(
                "UPDATE tickets SET status = ? WHERE number = ?",
                (status, number),
            )
        await db.commit()


async def set_ticket_voice_channel(number: int, channel_id: str) -> None:
    """Store the private voice channel ID against a ticket."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tickets SET voice_channel_id = ? WHERE number = ?",
            (channel_id, number),
        )
        await db.commit()


async def increment_no_shows(number: int) -> int:
    """Increment no_shows and return the new count."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tickets SET no_shows = no_shows + 1 WHERE number = ?",
            (number,),
        )
        await db.commit()
        async with db.execute(
            "SELECT no_shows FROM tickets WHERE number = ?", (number,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def move_ticket_to_back(number: int) -> None:
    """Push a ticket to the back of the queue by advancing its created_at."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tickets SET created_at = ? WHERE number = ?",
            (_now(), number),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Operator helpers
# ---------------------------------------------------------------------------

async def set_operator_status(user_id: str, status: str) -> None:
    """Upsert operator shift status."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO operator_status (user_id, status, last_shift_change)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE
                SET status            = excluded.status,
                    last_shift_change = excluded.last_shift_change
            """,
            (user_id, status, _now()),
        )
        await db.commit()


async def get_operator_status(user_id: str) -> str:
    """Return 'on' or 'off' for the given operator (defaults to 'off')."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT status FROM operator_status WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else "off"


# ---------------------------------------------------------------------------
# Data purge (30-day retention for closed/dropped tickets)
# ---------------------------------------------------------------------------

async def purge_old_tickets() -> int:
    """Delete completed/dropped tickets whose closed_at is > 30 days ago.

    Returns the number of rows deleted.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            DELETE FROM tickets
            WHERE status IN ('completed', 'dropped')
              AND closed_at IS NOT NULL
              AND closed_at < ?
            """,
            (cutoff,),
        )
        await db.commit()
        return cursor.rowcount


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
