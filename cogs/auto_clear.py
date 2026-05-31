# cogs/auto_clear.py
# -*- coding: utf-8 -*-
from __future__ import annotations

print("=== LOADED auto_clear.py v1.0 (EPHEMERAL AUTO-DELETE + RESET ON UI UPDATES) ===")

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import discord
from discord.ext import commands

from utils.google_auth import open_spreadsheet, reset_spreadsheet_cache

# ----------------------------
# Sheets
# ----------------------------
PERMISSIONS_SHEET = "Permissions for slash commands"
AUTO_CLEAR_KEY = "/auto_clear"  # matches column A "Slash command"

# Column letters from your sheet:
# M = auto clear ephemeral messages timer (minutes)
# N = Auto clear enable?
TIMER_COL_LETTER = "M"
ENABLED_COL_LETTER = "N"

# Discord interaction tokens are time-limited.
# 10 minutes is safe. 60 minutes will usually FAIL for deleting ephemerals.
# We'll cap (default) to 14 minutes to stay inside typical token windows.
MAX_SAFE_MINUTES = 14

# Cache settings so we don't hammer Sheets
CONFIG_CACHE_SECONDS = 30


def _safe_lower(v) -> str:
    return str(v).strip().lower() if v is not None else ""


def _to_int(v) -> int:
    try:
        s = str(v).strip()
        if not s:
            return 0
        return int(float(s))
    except Exception:
        return 0


def _is_truthy(v) -> bool:
    s = _safe_lower(v)
    return s not in ("", "0", "false", "no", "off", "n")


def _col_letter_to_index0(letter: str) -> int:
    letter = (letter or "").strip().upper()
    if not letter or len(letter) != 1:
        return -1
    return ord(letter) - ord("A")


@dataclass
class AutoClearConfig:
    enabled: bool
    minutes: int


class AutoClearCog(commands.Cog):
    """
    Ephemeral messages are NOT normal channel messages.
    You cannot sweep-delete them later by message_id.

    You MUST schedule deletion at send/edit time while you still have:
      - the Interaction (to delete original response)
      - or the WebhookMessage (for followups)

    Use:
      await self.bot.get_cog("AutoClearCog").bump_interaction(interaction)
      await self.bot.get_cog("AutoClearCog").track_webhook_message(interaction, msg)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sheet = None

        # cache: (config, monotonic_expires)
        self._config_cache: Optional[Tuple[AutoClearConfig, float]] = None
        self._cfg_lock = asyncio.Lock()

        # active scheduled tasks
        # key for original responses: ("orig", interaction.id)
        # key for followups: ("follow", webhook_message.id)
        self._tasks: Dict[Tuple[str, int], asyncio.Task] = {}

    # ----------------------------
    # Sheet helpers
    # ----------------------------
    def _ensure_sheet(self):
        if self.sheet is None:
            self.sheet = open_spreadsheet()
        return self.sheet

    def _ws(self, tab_name: str):
        if not self._ensure_sheet():
            return None
        try:
            return self.sheet.worksheet(tab_name)
        except Exception as e:
            print(f"[AutoClear] worksheet access failed for '{tab_name}': {e} (reopening sheet)")
            try:
                reset_spreadsheet_cache()
                self.sheet = open_spreadsheet()
                if self.sheet:
                    return self.sheet.worksheet(tab_name)
            except Exception as e2:
                print(f"[AutoClear] worksheet reopen failed for '{tab_name}': {e2}")
            return None

    def _get_command_cell_sync(self, slash_command: str, column_letter: str) -> str:
        ws = self._ws(PERMISSIONS_SHEET)
        if not ws:
            return ""
        data = ws.get_all_values()
        if not data or len(data) < 2:
            return ""
        target = (slash_command or "").strip().lower()
        col_idx = _col_letter_to_index0(column_letter)
        if col_idx < 0:
            return ""
        for row in data[1:]:
            cmd = (row[0] if len(row) > 0 else "").strip().lower()
            if cmd == target:
                return (row[col_idx] if len(row) > col_idx else "").strip()
        return ""

    async def _get_config(self) -> AutoClearConfig:
        # cached?
        if self._config_cache and time.monotonic() < self._config_cache[1]:
            return self._config_cache[0]

        async with self._cfg_lock:
            if self._config_cache and time.monotonic() < self._config_cache[1]:
                return self._config_cache[0]

            def read_sync():
                enabled_raw = self._get_command_cell_sync(AUTO_CLEAR_KEY, ENABLED_COL_LETTER)
                minutes_raw = self._get_command_cell_sync(AUTO_CLEAR_KEY, TIMER_COL_LETTER)
                return enabled_raw, minutes_raw

            enabled_raw, minutes_raw = await asyncio.to_thread(read_sync)

            enabled = _is_truthy(enabled_raw)
            minutes = _to_int(minutes_raw)

            # sanitize
            if minutes <= 0:
                minutes = 0  # treat as disabled timer
            if minutes > 60:
                minutes = 60  # your sheet max

            cfg = AutoClearConfig(enabled=enabled, minutes=minutes)
            self._config_cache = (cfg, time.monotonic() + CONFIG_CACHE_SECONDS)
            return cfg

    # ----------------------------
    # Public API for other cogs
    # ----------------------------
    async def bump_interaction(self, interaction: discord.Interaction) -> None:
        """
        Schedule auto-delete for the ORIGINAL interaction response (ephemeral panel).
        Call this after:
          - interaction.response.send_message(... ephemeral=True)
          - interaction.response.edit_message(...)
          - interaction.edit_original_response(...)
        Every call RESETS the timer.
        """
        cfg = await self._get_config()
        if not cfg.enabled or cfg.minutes <= 0:
            return

        # cap to safe window
        minutes = min(cfg.minutes, MAX_SAFE_MINUTES)
        delay = minutes * 60

        key = ("orig", interaction.id)
        self._cancel_task(key)

        async def _delete_later():
            try:
                await asyncio.sleep(delay)
                # delete original response (works for ephemerals when within token window)
                try:
                    await interaction.delete_original_response()
                except Exception as e:
                    # if token expired / already gone, ignore
                    print(f"[AutoClear] delete_original_response failed: {type(e).__name__}: {e}")
            finally:
                self._tasks.pop(key, None)

        self._tasks[key] = asyncio.create_task(_delete_later())

    async def track_webhook_message(self, interaction: discord.Interaction, msg: discord.WebhookMessage) -> None:
        """
        Schedule auto-delete for an ephemeral FOLLOWUP message (returned by followup.send(... wait=True)).
        Call this right after sending the followup. Call again to reset timer.
        """
        cfg = await self._get_config()
        if not cfg.enabled or cfg.minutes <= 0:
            return

        minutes = min(cfg.minutes, MAX_SAFE_MINUTES)
        delay = minutes * 60

        key = ("follow", int(msg.id))
        self._cancel_task(key)

        async def _delete_later():
            try:
                await asyncio.sleep(delay)
                try:
                    await msg.delete()
                except Exception as e:
                    print(f"[AutoClear] followup delete failed: {type(e).__name__}: {e}")
            finally:
                self._tasks.pop(key, None)

        self._tasks[key] = asyncio.create_task(_delete_later())

    def _cancel_task(self, key: Tuple[str, int]) -> None:
        t = self._tasks.get(key)
        if t and not t.done():
            try:
                t.cancel()
            except Exception:
                pass

    # Optional: manual cache bust (if you change sheet often)
    def invalidate_config_cache(self) -> None:
        self._config_cache = None


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoClearCog(bot))
