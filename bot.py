# -*- coding: utf-8 -*-
import os
import logging
from pathlib import Path

from dotenv import load_dotenv
import discord
from discord.ext import commands

BASE_DIR = Path(__file__).parent
ENV_PATH = BASE_DIR / ".env"

# Load .env explicitly from this directory
load_dotenv(dotenv_path=ENV_PATH, override=False)

# Discord token
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN or not isinstance(TOKEN, str) or not TOKEN.strip():
    raise RuntimeError(
        "DISCORD_TOKEN missing. Set it in the environment or in a `.env` file "
        "next to bot.py."
    )

# Environment + IDs
ENV = os.getenv("ENV", "production").strip().lower()
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID", "").strip()
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
BOT_NAME = os.getenv("BOT_NAME", "Bot").strip()

# Safe debug prints (no secrets)
print(f"BOT_NAME={BOT_NAME}")
print(f"ENV={ENV}")
print(f"TEST_GUILD_ID set? {bool(TEST_GUILD_ID)}")
print(f"SPREADSHEET_ID set? {bool(SPREADSHEET_ID)}")

# ---- Discord logging (so you can see gateway/connect issues) ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
discord_logger = logging.getLogger("discord")
discord_logger.setLevel(logging.INFO)

# Intents
intents = discord.Intents.default()
intents.members = True  # Requires "Server Members Intent" enabled in Dev Portal
# intents.message_content = True  # Enable only if you actually read message content

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_connect():
    print("[DISCORD] Connected to gateway (on_connect fired)")

@bot.event
async def on_ready():
    print(f"[DISCORD] {bot.user} is online and connected! (on_ready fired)")

    # Sync slash commands safely (test guild only in staging)
    try:
        all_commands = bot.tree.get_commands()
        print(f"Tree commands before sync: {len(all_commands)}")
        if all_commands:
            print(f"Command names: {[cmd.name for cmd in all_commands]}")

        if ENV == "staging" and TEST_GUILD_ID:
            guild = discord.Object(id=int(TEST_GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} command(s) to TEST guild {TEST_GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} command(s) globally")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    # Re-register persistent views for quest buttons
    try:
        quest_tracker = bot.get_cog("QuestTracker")
        if quest_tracker:
            await quest_tracker.register_persistent_views()
            print("Re-registered persistent views")
        else:
            print("QuestTracker cog not loaded (skipping persistent views)")
    except Exception as e:
        print(f"Error re-registering persistent views: {e}")

@bot.command()
async def ping(ctx):
    await ctx.send("Pong 🛰️")

# Load extensions (cogs + commands)
async def load_extensions():
    """
    Loads *.py extensions from:
      - /cogs
      - /commands
    """
    folders = ["cogs", "commands"]

    # Extra safety: skip known non-cogs even if not renamed
    SKIP_FILES = {"bot_identity.py", "bot_identity_simple.py"}

    for folder in folders:
        dir_path = BASE_DIR / folder
        if not dir_path.exists():
            print(f"No {folder} directory found at: {dir_path}")
            continue

        for py_file in dir_path.glob("*.py"):
            if py_file.name.startswith("_"):
                continue
            if py_file.name in SKIP_FILES:
                print(f"Skipping non-cog module: {folder}.{py_file.stem}")
                continue

            ext_name = f"{folder}.{py_file.stem}"
            try:
                if ext_name in bot.extensions:
                    await bot.reload_extension(ext_name)
                    print(f"Reloaded extension: {ext_name}")
                else:
                    await bot.load_extension(ext_name)
                    print(f"Loaded extension: {ext_name}")
            except Exception as e:
                print(f"Failed to load {ext_name}: {e}")

# Load extensions before running the bot
async def main():
    async with bot:
        await load_extensions()

        # Diagnostic: Show what commands are registered
        print("\n=== COMMAND REGISTRATION CHECK ===")
        for cmd in bot.tree.get_commands():
            print(f"  -> Registered command: /{cmd.name}")
        print(f"inventory registered? {any(c.name == 'inventory' for c in bot.tree.get_commands())}")
        print("===================================\n")

        print("[DISCORD] Starting bot.start() (this will block while bot runs)...")
        await bot.start(TOKEN.strip())

if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by user")
    except Exception as e:
        print(f"Bot crashed with error: {e}")
        print("Bot will not auto-restart to prevent infinite loops")
