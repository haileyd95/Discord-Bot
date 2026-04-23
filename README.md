# Support Queue & Voice Call Bot

A Discord bot that manages a ticketed support queue with private voice rooms.

## Features

| Command | Who | Description |
|---|---|---|
| Button: **Request Support** | Everyone | Creates a ticket, assigns Queued role, DMs ticket number |
| `/queue` | Everyone | Shows your position; operators see the full queue |
| `/call [number]` | Operator | Calls the next (or a specific) ticket, creates private voice room |
| `/complete <number>` | Operator | Closes ticket, grants Verified role, cleans up voice room |
| `/skip <number>` | Operator | Marks no-show; drops after 2nd skip |
| `/drop <number> <reason>` | Operator | Drops a ticket immediately, DMs the user |
| `/shift on\|off` | Operator | Toggles shift status |
| `/setup_panel` | Admin | Posts the Request Support button to the welcome channel |

---

## Setup

### 1. Prerequisites

- Python 3.10 or newer
- A Discord application with a Bot user ([create one here](https://discord.com/developers/applications))

### 2. Enable / Verify Privileged Intents

In the Discord Developer Portal, go to your application → **Bot** tab:

- ✅ **Server Members Intent** — required (the bot resolves members by ID)
- ✅ **Presence Intent** — optional, not required
- ❌ **Message Content Intent** — must remain **OFF**. The bot never reads message content, and leaving this off guarantees it cannot scrape chat history.

### 3. Invite the Bot

Use the OAuth2 URL Generator (**OAuth2 → URL Generator**):

**Scopes:** `bot`, `applications.commands`

**Bot Permissions:**
- Manage Roles
- Manage Channels
- Move Members
- Send Messages
- Embed Links
- Use Slash Commands
- Read Message History

### 4. Create Required Server Resources

Before running the bot, create the following in your Discord server:

**Roles (copy their IDs):**
- `Queued` — assigned when a user opens a ticket
- `Verified` — granted when a ticket is completed
- `Operator` (or any name) — gives access to staff commands

**Text Channels (copy their IDs):**
- `#welcome-read-first` — where the Request Support button goes
- `#queue-board` — where new ticket embeds appear and users are pinged on `/call`
- `#ops-log` — where all operator actions are logged

**Voice Channels — Static Model (copy their IDs):**

The bot uses two **pre-existing** voice channels instead of creating and deleting them dynamically. Create these once in your server:

- **Waiting Room** — ticket holders who are already in voice are staged here when their ticket is called, then immediately moved into the Operator Room.
- **Operator Room** — the private call channel where the operator and ticket holder meet.

> **Permission setup for Operator Room (do this once in Discord):**
> - `@everyone` → deny **Connect**, deny **View Channel**
> - `Operator` role → allow **Connect**, allow **View Channel**, allow **Speak**
>
> The bot moves users in and out of this channel; it never creates or deletes it.
> This avoids Discord's rate limits on channel creation during high ticket volume.

### 5. Configure Environment Variables

```bash
cp .env.example .env
```

Open `.env` and fill in every value:

```
DISCORD_TOKEN=your-bot-token
GUILD_ID=your-server-id
WELCOME_CHANNEL_ID=...
QUEUE_BOARD_CHANNEL_ID=...
OPS_LOG_CHANNEL_ID=...
WAITING_ROOM_CHANNEL_ID=...
OPERATOR_ROOM_CHANNEL_ID=...
QUEUED_ROLE_ID=...
VERIFIED_ROLE_ID=...
OPERATOR_ROLE_ID=...
```

> **Finding IDs:** Enable Developer Mode in Discord settings (Advanced → Developer Mode), then right-click any server, channel, or role and choose **Copy ID**.

### 6. Install Dependencies

```bash
pip install -r requirements.txt
```

### 7. Run the Bot

```bash
python main.py
```

On first start the bot will:
1. Create `support_queue.db` (SQLite database)
2. Register all slash commands to your guild
3. Log `Logged in as BotName` when ready

### 8. Post the Support Panel

In your Discord server, run:

```
/setup_panel
```

This posts the **Request Support** button embed to your `WELCOME_CHANNEL_ID` channel. **Run this once** — the button persists across bot restarts.

---

## Project Structure

```
Discord-Bot/
├── main.py          # Bot initialisation, events, daily purge task
├── database.py      # Async SQLite layer (all parameterised queries)
├── views.py         # Persistent UI button (RequestSupportView)
├── cogs/
│   ├── __init__.py
│   └── queue.py     # All slash commands (QueueCog)
├── .env.example     # Environment variable template
├── requirements.txt
└── README.md
```

---

## Data & Privacy

- Only Discord **user IDs** and **ticket metadata** (timestamps, status, no-show count) are stored.
- No IP addresses, emails, or message content are ever saved.
- All database queries use **parameterised statements** — no string concatenation.
- Tickets with status `completed` or `dropped` are automatically **purged after 30 days**.
- The bot token is read exclusively from the `.env` file — never hardcoded.

---

## How the Queue Works

1. A user clicks **Request Support** → ticket is created under a concurrency lock (no race conditions), the Queued role is assigned, and a DM is sent with their ticket number.
2. An operator runs `/call` → the oldest queued ticket is fetched, its status becomes `active`, and `called_at` is stamped in the database.
   - If the ticket holder is in a voice channel, the bot moves them through **Waiting Room** → **Operator Room**.
   - If they are not in voice, a warning embed is posted and the 10-minute timer begins (DB-backed — survives bot restarts).
3. A background task runs every **60 seconds** and auto-skips any `active` ticket whose `called_at` is older than 10 minutes and whose user is not in the Operator Room.
4. `/skip` increments the no-show counter. At 2 no-shows the ticket is dropped; otherwise it returns to the back of the queue.
5. On `/complete`, the Verified role is granted, the Queued role is removed, and the ticket holder is disconnected from the Operator Room. The channel itself is never deleted.
