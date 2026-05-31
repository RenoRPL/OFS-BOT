# -*- coding: utf-8 -*-
print("=== LOADED banner_member_sync.py v1.0 (DISCORD BANNER ROLE -> MEMBER LOG SYNC) ===")

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from utils.google_auth import open_spreadsheet, safe_call

BANNERS_SHEET = "Banners"
MEMBER_LOG_SHEET = "Member Log"
CACHE_TTL_SECONDS = 300


@dataclass(frozen=True)
class BannerRoleMap:
    names_by_key: Dict[str, str]
    ordered_names: Tuple[str, ...]


def _norm(value: object) -> str:
    return str(value or "").strip()


def _key(value: object) -> str:
    return _norm(value).lower()


def _header_map(row: Iterable[str]) -> Dict[str, int]:
    return {_key(v): i for i, v in enumerate(row or []) if _norm(v)}


def _truthy_admin(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return bool(perms.administrator or perms.manage_guild or perms.manage_roles)


class BannerMemberSync(commands.Cog):
    """Keeps Member Log Banner synchronized when Discord banner roles are added or removed."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._banner_cache: Optional[BannerRoleMap] = None
        self._banner_cache_ts = 0.0
        self._member_locks: Dict[int, asyncio.Lock] = {}
        print("[BannerMemberSync] Event-driven banner member sync enabled")

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        lock = self._member_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._member_locks[user_id] = lock
        return lock

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        before_ids = {r.id for r in before.roles}
        after_ids = {r.id for r in after.roles}
        before_names = {r.name for r in before.roles}
        after_names = {r.name for r in after.roles}
        if before_ids == after_ids and before_names == after_names:
            return

        # Only do Sheet work when one of the changed roles is a known banner role.
        try:
            role_map = await asyncio.to_thread(self._get_banner_role_map)
        except Exception as exc:
            print(f"[BannerMemberSync] Unable to load banner role map for member update: {exc}")
            return

        changed_keys = {str(rid) for rid in (before_ids ^ after_ids)} | {_key(n) for n in (before_names ^ after_names)}
        if not any(k in role_map.names_by_key for k in changed_keys):
            return

        async with self._lock_for(after.id):
            try:
                result = await asyncio.to_thread(self._sync_member_banner_from_roles, after, role_map, "role_update")
                if result:
                    print(result)
            except Exception as exc:
                print(f"[BannerMemberSync] Error syncing banner for {after.id}: {exc}")

    @app_commands.command(
        name="sync_banner_role",
        description="Admin: resync a member's active banner from their current Discord banner role."
    )
    @app_commands.default_permissions(manage_roles=True)
    async def sync_banner_role(self, interaction: discord.Interaction, member: discord.Member):
        if not isinstance(interaction.user, discord.Member) or not _truthy_admin(interaction.user):
            await interaction.response.send_message("❌ You do not have permission to use this sync command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            role_map = await asyncio.to_thread(self._get_banner_role_map, True)
            async with self._lock_for(member.id):
                result = await asyncio.to_thread(self._sync_member_banner_from_roles, member, role_map, f"manual:{interaction.user.id}")
        except Exception as exc:
            await interaction.edit_original_response(content=f"❌ Banner sync failed: {exc}")
            return

        await interaction.edit_original_response(content=result or f"✅ {member.display_name} already had the correct active banner.")

    def _get_banner_role_map(self, force_refresh: bool = False) -> BannerRoleMap:
        now = time.time()
        if not force_refresh and self._banner_cache and now - self._banner_cache_ts < CACHE_TTL_SECONDS:
            return self._banner_cache

        ss = open_spreadsheet()
        if not ss:
            raise RuntimeError("Google spreadsheet unavailable")

        ws = safe_call(lambda: ss.worksheet(BANNERS_SHEET), label="banner_member_sync_open_banners")
        values = safe_call(lambda: ws.get_all_values(), label="banner_member_sync_get_banners")
        if not values:
            raise RuntimeError("Banners sheet is empty")

        # Banners sheet has two header/reference rows, then one row per banner.
        # Some deployments also append a Role ID column; use it when present, but
        # still support exact role-name matching so older sheets keep working.
        headers = values[1] if len(values) > 1 else values[0]
        hmap = _header_map(headers)
        role_id_idx = hmap.get("role id")
        name_idx = hmap.get("banner name", 0)

        names_by_key: Dict[str, str] = {}
        ordered_names: List[str] = []
        for row in values[2:] if len(values) > 2 else values[1:]:
            if not row:
                continue
            name = _norm(row[name_idx] if name_idx < len(row) else row[0])
            if not name or name.lower().startswith("points needed") or name.lower() == "banner name":
                continue
            ordered_names.append(name)
            names_by_key[_key(name)] = name
            # Also recognize names without a leading "The " when Discord roles omit it.
            if name.lower().startswith("the "):
                names_by_key[_key(name[4:])] = name
            if role_id_idx is not None and role_id_idx < len(row):
                role_id = _norm(row[role_id_idx])
                if role_id:
                    names_by_key[role_id] = name

        if not ordered_names:
            raise RuntimeError("No banner names found in Banners sheet")

        self._banner_cache = BannerRoleMap(names_by_key=names_by_key, ordered_names=tuple(ordered_names))
        self._banner_cache_ts = now
        return self._banner_cache

    def _banner_for_member(self, member: discord.Member, role_map: BannerRoleMap) -> Tuple[str, List[str]]:
        matched: Set[str] = set()
        for role in member.roles:
            by_id = role_map.names_by_key.get(str(role.id))
            by_name = role_map.names_by_key.get(_key(role.name))
            if by_id:
                matched.add(by_id)
            if by_name:
                matched.add(by_name)

        if not matched:
            return "", []

        ordered_matches = [name for name in role_map.ordered_names if name in matched]
        if ordered_matches:
            return ordered_matches[0], ordered_matches
        # Defensive fallback if a mapped name is somehow absent from ordered_names.
        fallback = sorted(matched)
        return fallback[0], fallback

    def _sync_member_banner_from_roles(self, member: discord.Member, role_map: BannerRoleMap, source: str) -> str:
        target_banner, matches = self._banner_for_member(member, role_map)

        ss = open_spreadsheet()
        if not ss:
            raise RuntimeError("Google spreadsheet unavailable")
        ws = safe_call(lambda: ss.worksheet(MEMBER_LOG_SHEET), label="banner_member_sync_open_member_log")
        values = safe_call(lambda: ws.get_all_values(), label="banner_member_sync_get_member_log")
        if not values:
            raise RuntimeError("Member Log is empty")

        headers = values[0]
        hmap = _header_map(headers)
        user_idx = hmap.get("user id")
        banner_idx = hmap.get("banner")
        if user_idx is None or banner_idx is None:
            raise RuntimeError("Member Log missing required headers: User ID and/or Banner")

        target_uid = str(member.id)
        row_num = None
        current_banner = ""
        for i, row in enumerate(values[1:], start=2):
            uid = _norm(row[user_idx] if user_idx < len(row) else "")
            if uid == target_uid:
                row_num = i
                current_banner = _norm(row[banner_idx] if banner_idx < len(row) else "")
                break

        if row_num is None:
            return f"[BannerMemberSync] No Member Log row found for {member.display_name} ({target_uid}); source={source}"

        if current_banner == target_banner:
            if len(matches) > 1:
                return f"[BannerMemberSync] {member.display_name} has multiple banner roles {matches}; kept {target_banner}; source={source}"
            return ""

        safe_call(
            lambda: ws.update_cell(row_num, banner_idx + 1, target_banner),
            label=f"banner_member_sync_update_member_log_{target_uid}",
        )

        if not target_banner:
            return f"[BannerMemberSync] Cleared active banner for {member.display_name} ({target_uid}); old={current_banner or 'blank'}; source={source}"
        if len(matches) > 1:
            return f"[BannerMemberSync] Updated active banner for {member.display_name} ({target_uid}): {current_banner or 'blank'} -> {target_banner}; multiple roles={matches}; source={source}"
        return f"[BannerMemberSync] Updated active banner for {member.display_name} ({target_uid}): {current_banner or 'blank'} -> {target_banner}; source={source}"


async def setup(bot: commands.Bot):
    await bot.add_cog(BannerMemberSync(bot))
