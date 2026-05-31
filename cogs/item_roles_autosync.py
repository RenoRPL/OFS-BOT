# cogs/item_roles_autosync.py
# -*- coding: utf-8 -*-
from __future__ import annotations

print("=== LOADED item_roles_autosync.py v1.0 (AUTO CREATE ROLES FROM ITEM LIST) ===")

import asyncio
from typing import List, Optional, Set, Tuple

import discord
from discord.ext import commands, tasks

from utils.google_auth import open_spreadsheet

ITEM_LIST_SHEET = "Item List"
COL_ENABLED = "Enabled"
COL_ROLE_TO_ASSIGN = "Role to assign"

# How often to re-check the sheet
SYNC_EVERY_MINUTES = 10

# Optional: restrict role creation to a single guild
# Set to None to run for every guild the bot is in.
TARGET_GUILD_ID: Optional[int] = None


def _norm(s: str) -> str:
    return (s or "").strip().lower()


class ItemRolesAutoSync(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sheet = None  # lazy

        # Start background loop
        self.role_sync_loop.start()

    def cog_unload(self):
        try:
            self.role_sync_loop.cancel()
        except Exception:
            pass

    def _ensure_sheet(self):
        if self.sheet is None:
            self.sheet = open_spreadsheet()
        return self.sheet

    def _ws(self, name: str):
        if not self._ensure_sheet():
            return None
        try:
            return self.sheet.worksheet(name)
        except Exception:
            return None

    def _find_header_index(self, headers: List[str], name: str) -> int:
        target = _norm(name)
        for i, h in enumerate(headers):
            if _norm(str(h)) == target:
                return i
        return -1

    def _read_roles_needed_sync(self) -> List[str]:
        """
        Returns a de-duped list of role names to create from Item List where Enabled == Y.
        """
        ws = self._ws(ITEM_LIST_SHEET)
        if not ws:
            return []

        data = ws.get_all_values()
        if not data or len(data) < 2:
            return []

        headers = data[0]
        idx_enabled = self._find_header_index(headers, COL_ENABLED)
        idx_role = self._find_header_index(headers, COL_ROLE_TO_ASSIGN)

        if idx_enabled == -1 or idx_role == -1:
            print("[ItemRolesAutoSync] Missing required headers: 'Enabled' and/or 'Role to assign'")
            return []

        roles: List[str] = []
        for row in data[1:]:
            enabled = (row[idx_enabled] if idx_enabled < len(row) else "").strip()
            role_name = (row[idx_role] if idx_role < len(row) else "").strip()

            if _norm(enabled) != "y":
                continue
            if not role_name:
                continue

            roles.append(role_name)

        # de-dupe while preserving order
        seen: Set[str] = set()
        out: List[str] = []
        for r in roles:
            key = _norm(r)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(r.strip())
        return out

    async def _create_roles_if_missing(self, guild: discord.Guild, role_names: List[str]) -> Tuple[int, int]:
        """
        Returns (created_count, skipped_existing_count)
        """
        # existing role names map (case-insensitive)
        existing = {_norm(r.name): r for r in guild.roles if r.name}

        created = 0
        skipped = 0

        me = guild.me
        if me is None:
            return 0, 0

        # must have manage roles perm
        if not me.guild_permissions.manage_roles:
            print(f"[ItemRolesAutoSync] Missing Manage Roles in guild: {guild.name} ({guild.id})")
            return 0, 0

        for name in role_names:
            key = _norm(name)
            if not key:
                continue

            if key in existing:
                skipped += 1
                continue

            try:
                new_role = await guild.create_role(
                    name=name.strip(),
                    reason="Item List auto-sync: Role to assign enabled item",
                )
                existing[key] = new_role
                created += 1
                print(f"[ItemRolesAutoSync] Created role '{new_role.name}' in {guild.name} ({guild.id})")
            except discord.Forbidden:
                print(f"[ItemRolesAutoSync] Forbidden creating roles in {guild.name} ({guild.id}). Check role hierarchy.")
                break
            except discord.HTTPException as e:
                print(f"[ItemRolesAutoSync] HTTPException creating role '{name}' in {guild.name}: {e}")
                continue

        return created, skipped

    @tasks.loop(minutes=SYNC_EVERY_MINUTES)
    async def role_sync_loop(self):
        # read roles once per tick (shared for all guilds)
        role_names = await asyncio.to_thread(self._read_roles_needed_sync)
        if not role_names:
            return

        # choose guilds to run against
        guilds = list(self.bot.guilds)
        if TARGET_GUILD_ID is not None:
            guilds = [g for g in guilds if g.id == TARGET_GUILD_ID]

        total_created = 0
        total_skipped = 0

        for g in guilds:
            created, skipped = await self._create_roles_if_missing(g, role_names)
            total_created += created
            total_skipped += skipped

        if total_created or total_skipped:
            print(
                f"[ItemRolesAutoSync] Tick complete. Roles listed: {len(role_names)} | "
                f"Created: {total_created} | Already existed: {total_skipped}"
            )

    @role_sync_loop.before_loop
    async def before_role_sync_loop(self):
        await self.bot.wait_until_ready()
        print(f"[ItemRolesAutoSync] Background sync loop started (every {SYNC_EVERY_MINUTES} min).")


async def setup(bot: commands.Bot):
    await bot.add_cog(ItemRolesAutoSync(bot))
