"""
Discord bot — keeps a shared SQLite database of every server (guild) and
member the bot can see, so the dashboard's API (main.py) can read from it.

The bot is the ONLY thing that writes to servers/members — the API only
reads them (plus its own small `snapshot` table for diffing on refresh).

SETUP
-----
1. python3 -m venv venv && source venv/bin/activate
2. pip install -r requirements.txt
3. Create a .env file next to this script:
       DISCORD_BOT_TOKEN=your-token-here
4. In the Discord Developer Portal, under your bot's settings, make sure
   "SERVER MEMBERS INTENT" is enabled (unchanged from before — no new
   permissions needed for any of this).
5. python3 bot.py

DATA
----
Writes to data.db (SQLite, WAL mode so the API can read concurrently)
in the same folder. main.py points at the same file.
"""

import os
import csv
import io
import sqlite3
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")
FULL_RESYNC_MINUTES = 10  # safety-net full resync interval

intents = discord.Intents.default()
intents.members = True  # Server Members Intent — must be enabled in dev portal

bot = commands.Bot(command_prefix="!", intents=intents)


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS servers (
            guild_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            member_count INTEGER NOT NULL,
            icon_url TEXT,
            first_seen_at TEXT NOT NULL,
            last_updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS members (
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            username TEXT,
            display_name TEXT,
            is_bot INTEGER NOT NULL DEFAULT 0,
            joined_at TEXT,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS snapshot (
            guild_id TEXT PRIMARY KEY
        );
        """
    )
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def upsert_server(guild: discord.Guild):
    conn = get_conn()
    row = conn.execute(
        "SELECT first_seen_at FROM servers WHERE guild_id = ?", (str(guild.id),)
    ).fetchone()
    first_seen = row[0] if row else now_iso()
    icon_url = guild.icon.url if guild.icon else None
    conn.execute(
        """
        INSERT INTO servers (guild_id, name, member_count, icon_url, first_seen_at, last_updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            name=excluded.name,
            member_count=excluded.member_count,
            icon_url=excluded.icon_url,
            last_updated_at=excluded.last_updated_at
        """,
        (str(guild.id), guild.name, guild.member_count, icon_url, first_seen, now_iso()),
    )
    conn.commit()
    conn.close()


def remove_server(guild_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM servers WHERE guild_id = ?", (str(guild_id),))
    conn.execute("DELETE FROM members WHERE guild_id = ?", (str(guild_id),))
    conn.commit()
    conn.close()


def upsert_member(guild_id: int, member: discord.Member):
    conn = get_conn()
    joined = member.joined_at.isoformat() if member.joined_at else None
    conn.execute(
        """
        INSERT INTO members (guild_id, user_id, username, display_name, is_bot, joined_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            username=excluded.username,
            display_name=excluded.display_name,
            is_bot=excluded.is_bot,
            joined_at=excluded.joined_at
        """,
        (str(guild_id), str(member.id), str(member), member.display_name, int(member.bot), joined),
    )
    conn.commit()
    conn.close()


def remove_member(guild_id: int, user_id: int):
    conn = get_conn()
    conn.execute(
        "DELETE FROM members WHERE guild_id = ? AND user_id = ?",
        (str(guild_id), str(user_id)),
    )
    conn.commit()
    conn.close()


def full_resync_guild(guild: discord.Guild):
    """Rewrite the server row + full member list for one guild from live Discord state."""
    upsert_server(guild)
    conn = get_conn()
    conn.execute("DELETE FROM members WHERE guild_id = ?", (str(guild.id),))
    conn.commit()
    for member in guild.members:
        upsert_member(guild.id, member)
    conn.close()


# --------------------------------------------------------------------------
# Bot events
# --------------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    init_db()
    for guild in bot.guilds:
        full_resync_guild(guild)
    print(f"Synced {len(bot.guilds)} server(s) to {DB_PATH}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Slash command sync failed: {e}")
    if not periodic_resync.is_running():
        periodic_resync.start()


@bot.event
async def on_guild_join(guild: discord.Guild):
    full_resync_guild(guild)


@bot.event
async def on_guild_remove(guild: discord.Guild):
    remove_server(guild.id)


@bot.event
async def on_member_join(member: discord.Member):
    upsert_member(member.guild.id, member)
    upsert_server(member.guild)  # refresh member_count


@bot.event
async def on_member_remove(member: discord.Member):
    remove_member(member.guild.id, member.id)
    upsert_server(member.guild)  # refresh member_count


@tasks.loop(minutes=FULL_RESYNC_MINUTES)
async def periodic_resync():
    for guild in bot.guilds:
        full_resync_guild(guild)


# --------------------------------------------------------------------------
# Slash commands (unchanged behavior from before — still ephemeral,
# still requires Manage Server on the caller)
# --------------------------------------------------------------------------
@bot.tree.command(name="exportmembers", description="Export this server's member list to a CSV file")
@app_commands.describe(include_bots="Include bot accounts in the export (default: off)")
async def exportmembers(interaction: discord.Interaction, include_bots: bool = False):
    if interaction.guild is None:
        await interaction.response.send_message("Run this inside a server, not a DM.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need the 'Manage Server' permission to export the member list here.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    guild = interaction.guild
    members = [m for m in guild.members if include_bots or not m.bot]

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["user_id", "username", "display_name", "is_bot"])
    for m in members:
        writer.writerow([m.id, str(m), m.display_name, m.bot])

    file_bytes = buffer.getvalue().encode("utf-8")
    filename = f"{guild.name.replace(' ', '_')}_members.csv"

    await interaction.followup.send(
        content=f"Exported **{len(members)}** members from `{guild.name}`.",
        file=discord.File(io.BytesIO(file_bytes), filename=filename),
        ephemeral=True,
    )


@bot.tree.command(name="refresh", description="Force a full resync of this server's data to the dashboard")
async def refresh(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Run this inside a server, not a DM.", ephemeral=True)
        return
    full_resync_guild(interaction.guild)
    await interaction.response.send_message(
        f"Resynced `{interaction.guild.name}` ({interaction.guild.member_count} members) to the dashboard.",
        ephemeral=True,
    )


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN not set — add it to your .env file")
    bot.run(BOT_TOKEN)
