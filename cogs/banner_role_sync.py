# -*- coding: utf-8 -*-
print("=== LOADED banner_role_sync.py v1.0 (BANNER RENAME ROLE SYNC) ===")

import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands, tasks

from utils.google_auth import open_worksheet, safe_call

REQUEST_SHEET = "Banner Rename Requests"
POLL_SECONDS = 60

EXPECTED_HEADERS = [
    "Timestamp UTC",
    "Old Name",
    "New Name",
    "Role ID",
    "Active Banner Updates",
    "Medal Updates",
    "Fleet Updates",
    "Status",
    "Source",
    "Applied At",
    "Error",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(value: object) -> str:
    return str(value or "").strip()


def _header_map(row: List[str]) -> Dict[str, int]:
    return {_norm(v).lower(): i for i, v in enumerate(row or []) if _norm(v)}


class BannerRoleSync(commands.Cog):
    """Processes site-created banner rename requests and renames Discord roles by Role ID."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._processing = False
        self.banner_role_sync_loop.start()

    def cog_unload(self):
        self.banner_role_sync_loop.cancel()

    @tasks.loop(seconds=POLL_SECONDS)
    async def banner_role_sync_loop(self):
        if self._processing:
            return
        self._processing = True
        try:
            await asyncio.to_thread(self._process_pending_sync)
        except Exception as exc:
            print(f"[BannerRoleSync] loop error: {exc}")
        finally:
            self._processing = False

    @banner_role_sync_loop.before_loop
    async def before_banner_role_sync_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(10)

    def _open_request_sheet(self):
        ws = open_worksheet(REQUEST_SHEET)
        if not ws:
            print(f"[BannerRoleSync] Optional sheet '{REQUEST_SHEET}' not available; skipping.")
            return None
        return ws

    def _ensure_headers(self, ws, values: List[List[str]]) -> Tuple[List[List[str]], Dict[str, int]]:
        if not values:
            safe_call(lambda: ws.append_row(EXPECTED_HEADERS, value_input_option="USER_ENTERED"), label="banner_role_sync_append_headers")
            values = [EXPECTED_HEADERS]
        first = values[0] if values else []
        hmap = _header_map(first)
        if "old name" not in hmap or "new name" not in hmap or "role id" not in hmap:
            # Do not rewrite unknown user data. Log the expected header and skip.
            print(f"[BannerRoleSync] '{REQUEST_SHEET}' has unexpected headers. Expected: {EXPECTED_HEADERS}")
        return values, hmap

    def _process_pending_sync(self):
        ws = self._open_request_sheet()
        if not ws:
            return
        values = safe_call(lambda: ws.get_all_values(), label="banner_role_sync_get_all_values")
        values, hmap = self._ensure_headers(ws, values)
        if not values or "old name" not in hmap or "new name" not in hmap or "role id" not in hmap:
            return

        status_idx = hmap.get("status", 7)
        applied_idx = hmap.get("applied at", 9)
        error_idx = hmap.get("error", 10)

        for row_num, row in enumerate(values[1:], start=2):
            status = _norm(row[status_idx] if status_idx < len(row) else "")
            if status and not status.lower().startswith("pending"):
                continue

            old_name = _norm(row[hmap["old name"]] if hmap["old name"] < len(row) else "")
            new_name = _norm(row[hmap["new name"]] if hmap["new name"] < len(row) else "")
            role_id = _norm(row[hmap["role id"]] if hmap["role id"] < len(row) else "")
            if not old_name or not new_name:
                self._mark(ws, row_num, status_idx, applied_idx, error_idx, "Failed", f"Missing old/new name at row {row_num}")
                continue
            if not role_id:
                self._mark(ws, row_num, status_idx, applied_idx, error_idx, "Skipped", "No Role ID set for this banner")
                continue

            ok, message = self._rename_role_by_id(role_id, old_name, new_name)
            self._mark(ws, row_num, status_idx, applied_idx, error_idx, "Complete" if ok else "Failed", message)

    def _rename_role_by_id(self, role_id: str, old_name: str, new_name: str) -> Tuple[bool, str]:
        try:
            rid = int(role_id)
        except ValueError:
            return False, f"Invalid Role ID: {role_id}"

        role: Optional[discord.Role] = None
        for guild in self.bot.guilds:
            found = guild.get_role(rid)
            if found:
                role = found
                break
        if not role:
            return False, f"Role ID not found in connected guilds: {role_id}"
        guild = role.guild
        me = guild.me
        if not me or not me.guild_permissions.manage_roles:
            return False, f"Bot lacks Manage Roles in {guild.name}"
        if role.managed:
            return False, f"Role is managed/integration-owned: {role.name}"
        if (role.name or "").strip().lower() == new_name.lower():
            return True, f"Role already named target: {new_name}"
        if role >= me.top_role:
            return False, f"Role hierarchy blocked: bot top role must be above {role.name}"
        existing = discord.utils.find(lambda r: (r.name or "").strip().lower() == new_name.lower() and r.id != role.id, guild.roles)
        if existing:
            return False, f"Target role name already exists in {guild.name}: {new_name}"

        # This runs inside a worker thread, so schedule the Discord coroutine onto the bot loop.
        fut = asyncio.run_coroutine_threadsafe(
            role.edit(name=new_name, reason=f"OFS banner rename sync: {old_name} -> {new_name}"),
            self.bot.loop,
        )
        try:
            fut.result(timeout=30)
        except discord.Forbidden:
            return False, f"Discord forbids role rename; check Manage Roles and hierarchy for {role.name}"
        except discord.NotFound:
            return False, f"Role disappeared before rename: {role_id}"
        except discord.HTTPException as exc:
            return False, f"Discord API error while renaming {role.name}: {exc}"
        except Exception as exc:
            return False, f"Unexpected role rename error for {role.name}: {exc}"
        return True, f"Renamed role {role_id}: {old_name} -> {new_name}"

    def _mark(self, ws, row_num: int, status_idx: int, applied_idx: int, error_idx: int, status: str, message: str):
        # gspread is 1-indexed. Pad to expected columns with direct cell updates.
        updates = [
            (row_num, status_idx + 1, status),
            (row_num, applied_idx + 1, _utc_now()),
            (row_num, error_idx + 1, message),
        ]
        for r, c, value in updates:
            safe_call(lambda r=r, c=c, value=value: ws.update_cell(r, c, value), label=f"banner_role_sync_update_cell_{r}_{c}")
        print(f"[BannerRoleSync] Row {row_num}: {status} - {message}")


async def setup(bot: commands.Bot):
    await bot.add_cog(BannerRoleSync(bot))
