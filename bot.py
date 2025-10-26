# -*- coding: utf-8 -*-
import os
from pathlib import Path
from dotenv import load_dotenv, dotenv_values
import discord
from discord.ext import commands

BASE_DIR = Path(__file__).parent
ENV_PATH = BASE_DIR / ".env"

# Debug: show what we see (won't print your token)
print(f".env exists? {ENV_PATH.exists()}  size={ENV_PATH.stat().st_size if ENV_PATH.exists() else 0}")
print(f".env keys: {list(dotenv_values(ENV_PATH).keys())}")

# Load .env explicitly from this directory
load_dotenv(dotenv_path=ENV_PATH, override=False)

# Fallback to OS env (in case you set $env:DISCORD_TOKEN in the shell)
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN or not isinstance(TOKEN, str) or not TOKEN.strip():
    raise RuntimeError(
        "DISCORD_TOKEN missing. Ensure a file named `.env` sits next to bot.py "
        "with a line: DISCORD_TOKEN=your_token_here"
    )

intents = discord.Intents.default()
# Enable required intents for member tracking
intents.members = True  # ✅ SERVER MEMBERS INTENT enabled in Discord Developer Portal
# Go to https://discord.com/developers/applications -> Your App -> Bot -> Privileged Gateway Intents
# "Server Members Intent" is now enabled in the Discord Developer Portal
# intents.message_content = True  # Keep this disabled unless needed

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"{bot.user} is online and connected!")
    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    
    # Re-register persistent views for quest buttons
    try:
        quest_tracker = bot.get_cog('QuestTracker')
        if quest_tracker:
            await quest_tracker.register_persistent_views()
    except Exception as e:
        print(f"Error re-registering persistent views: {e}")

@bot.command()
async def ping(ctx):
    await ctx.send("Pong 🛰️")

# Load cogs
async def load_cogs():
    """Load all cogs from the cogs directory"""
    cogs_dir = Path("cogs")
    if cogs_dir.exists():
        for cog_file in cogs_dir.glob("*.py"):
            if cog_file.name.startswith("_"):
                continue  # Skip files starting with underscore
            cog_name = f"cogs.{cog_file.stem}"
            try:
                # Check if cog is already loaded
                if cog_name in bot.extensions:
                    await bot.reload_extension(cog_name)
                    print(f"Reloaded cog: {cog_name}")
                else:
                    await bot.load_extension(cog_name)
                    print(f"Loaded cog: {cog_name}")
            except Exception as e:
                print(f"Failed to load cog {cog_name}: {e}")

# Load cogs before running the bot
async def main():
    async with bot:
        await load_cogs()
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
