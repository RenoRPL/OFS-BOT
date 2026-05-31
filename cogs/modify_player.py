# cogs/Modify_player.py
# -*- coding: utf-8 -*-
from __future__ import annotations

print("=== LOADED Modify_player.py v3.1 (ADJUSTMENTS + LOG CHANNEL FROM PERMISSIONS SHEET) ===")

import asyncio
import random
import string
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

try:
    import gspread  # noqa: F401
except Exception:
    gspread = None

from utils.google_auth import open_spreadsheet

# ----------------------------
# Sheets / tabs
# ----------------------------
PERMISSIONS_SHEET = "Permissions for slash commands"
MEMBER_LOG_SHEET = "Member Log"

# READ current values from computed totals
TOTALS_SHEET = "Patrols_User_Totals"

# WRITE admin adjustments here (append-only)
ADJUSTMENTS_SHEET = "Patrols_User_Adjustments"

# Columns we allow admins to edit (by header name in Patrols_User_Totals)
EDITABLE_HEADERS = [
    "PatrolCount",
    "TotalLength",
    "FPS_Kills_Total",
    "Ship_Kills_Total",
    "Crusades_Total",
    "Turret_Kills_Total",
    "Quest_Total",
    "Led_Completed_Quests",
    "Led_Completed_Crusades",
]

# Patrols_User_Adjustments sheet headers
ADJ_USER_ID_HEADER = "User ID"
ADJ_META_HEADERS = ["Delta", "Reason", "AdminID", "Timestamp"]

# Permission sheet headers (expected)
PERM_COL_SLASH = "Slash command"
PERM_COL_REVIEW = "Review Channel"
PERM_COL_LOG = "Log Channel ID"  # <-- NEW (what you asked for)
PERM_COL_RUN = "Ranks allowed to run command"
PERM_COL_EDIT = "Ranks allowed to edit request"

# Member Log headers
MEMBER_RANK_HEADERS = ["Rank", "Ranks", "Member Rank"]
MEMBER_ID_HEADERS = ["User ID", "Discord ID", "DiscordId", "Discord ID#", "ID"]

# Totals sheet ID header candidates
TOTALS_ID_HEADERS = ["UserID", "User ID", "Discord ID", "DiscordId", "Discord_ID", "Player ID", "ID"]


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _req_id(n: int = 7) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


def _norm_cmd_name(s: str) -> str:
    if not s:
        return ""
    s = str(s).strip()
    if s.startswith("/"):
        s = s[1:]
    return s.strip().lower()


def _parse_rank_list(raw: str) -> List[str]:
    if not raw:
        return []
    parts = [p.strip() for p in str(raw).replace("\n", ",").split(",")]
    return [p.lower() for p in parts if p]


def _safe_int(value: str, *, allow_blank: bool = True) -> Optional[int]:
    if value is None:
        return None if allow_blank else 0
    s = str(value).strip()
    if s == "":
        return None if allow_blank else 0
    try:
        return int(s)
    except ValueError:
        return None


def _short_num(s: str) -> str:
    s = ("" if s is None else str(s)).strip()
    if s == "":
        return "0"
    if len(s) > 30:
        return s[:27] + "..."
    return s


def _build_totals_embed(target: discord.Member, mode: str, totals: Dict[str, str], request_id: str) -> discord.Embed:
    embed = discord.Embed(
        title="🛠️ Modify Player Totals",
        description=f"**Target:** {target.mention}\n**Mode:** `{mode}`\n**Request ID:** `{request_id}`",
        color=0x2B2D31,
        timestamp=datetime.now(timezone.utc),
    )

    lines = []
    for h in EDITABLE_HEADERS:
        v = totals.get(h, "0")
        lines.append(f"• **{h}**: `{_short_num(v)}`")

    chunk = "\n".join(lines)
    if len(chunk) > 1024:
        chunk = chunk[:1000] + "\n…"

    embed.add_field(name="📊 Current Totals (computed)", value=chunk, inline=False)
    embed.set_footer(text="Select a stat below to create an adjustment entry.")
    return embed


class ModifyStatSelect(discord.ui.Select):
    def __init__(self, *, parent_view: "ModifyPlayerView", mode: str, totals: Dict[str, str]):
        self.parent_view = parent_view
        self.mode = mode
        self.totals = totals

        options: List[discord.SelectOption] = []
        for header in EDITABLE_HEADERS:
            cur = _short_num(self.totals.get(header, "0"))
            options.append(
                discord.SelectOption(
                    label=header[:100],
                    value=header[:100],
                    description=f"Current: {cur}"[:100],
                )
            )

        super().__init__(
            placeholder="Select a stat to modify…",
            min_values=1,
            max_values=1,
            options=options[:25],
        )

    async def callback(self, interaction: discord.Interaction):
        stat_name = self.values[0]
        current_val = self.totals.get(stat_name, "0")

        modal = ModifySingleStatModal(
            parent=self.parent_view.cog,
            actor=interaction.user,
            target=self.parent_view.target,
            mode=self.mode,
            stat_name=stat_name,
            current_value=current_val,
            review_channel_id=self.parent_view.review_channel_id,
            log_channel_id=self.parent_view.log_channel_id,  # NEW
            actor_rank=self.parent_view.actor_rank,
            request_id=self.parent_view.request_id,
        )
        await interaction.response.send_modal(modal)


class ModifyPlayerView(discord.ui.View):
    def __init__(
        self,
        *,
        cog: "ModifyPlayerCog",
        target: discord.Member,
        mode: str,
        totals: Dict[str, str],
        review_channel_id: Optional[int],
        log_channel_id: Optional[int],  # NEW
        actor_rank: str,
        request_id: str,
    ):
        super().__init__(timeout=120)
        self.cog = cog
        self.target = target
        self.mode = mode
        self.totals = totals
        self.review_channel_id = review_channel_id
        self.log_channel_id = log_channel_id
        self.actor_rank = actor_rank
        self.request_id = request_id

        self.add_item(ModifyStatSelect(parent_view=self, mode=mode, totals=totals))

    async def on_timeout(self):
        for item in self.children:
            try:
                item.disabled = True
            except Exception:
                pass


class ModifySingleStatModal(discord.ui.Modal):
    """
    SINGLE STAT MODAL so we never exceed Discord's Modal child limit (5).
    This writes an adjustment row to Patrols_User_Adjustments (append-only).
    """

    def __init__(
        self,
        *,
        parent: "ModifyPlayerCog",
        actor: discord.Member,
        target: discord.Member,
        mode: str,
        stat_name: str,
        current_value: str,
        review_channel_id: Optional[int],
        log_channel_id: Optional[int],  # NEW
        actor_rank: str,
        request_id: str,
    ):
        super().__init__(title=f"Edit {stat_name} ({mode.upper()})")
        self.parent = parent
        self.actor = actor
        self.target = target
        self.mode = mode
        self.stat_name = stat_name
        self.current_value = current_value
        self.review_channel_id = review_channel_id
        self.log_channel_id = log_channel_id
        self.actor_rank = actor_rank
        self.request_id = request_id

        placeholder = "Example: 5" if mode == "set" else "Example: +5 or -2"
        self.new_value = discord.ui.TextInput(
            label=f"{stat_name}",
            placeholder=placeholder,
            default="",
            required=True,
            max_length=15,
        )
        self.reason = discord.ui.TextInput(
            label="Reason (optional)",
            placeholder="Why are you changing this stat?",
            required=False,
            style=discord.TextStyle.short,
            max_length=120,
        )

        self.add_item(self.new_value)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        v = _safe_int(self.new_value.value, allow_blank=False)
        if v is None:
            await interaction.followup.send(
                "❌ Invalid number. Use whole integers only (examples: `5`, `0`, `-2`, `+7`).",
                ephemeral=True,
            )
            return

        result = await self.parent.write_adjustment_for_user(
            target_user_id=str(self.target.id),
            stat_name=self.stat_name,
            input_value=v,
            mode=self.mode,  # "set" or "add"
            reason=(self.reason.value or "").strip(),
            admin_id=str(self.actor.id),
        )

        if not result["ok"]:
            await interaction.followup.send(f"❌ {result['error']}", ephemeral=True)
            return

        old_int: int = result["old"]
        new_int: int = result["new"]
        delta_int: int = result["delta"]

        # Ephemeral confirmation to the admin
        embed = discord.Embed(
            title="✅ Adjustment Logged",
            description=(
                f"**Target:** {self.target.mention}\n"
                f"**Mode:** `{self.mode}` (stored as delta)\n"
                f"**Request ID:** `{self.request_id}`"
            ),
            color=0x28A745,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Stat", value=f"**{self.stat_name}**", inline=True)
        embed.add_field(name="Computed", value=f"`{old_int}` → `{new_int}`", inline=True)
        embed.add_field(name="Delta written", value=f"`{delta_int:+d}`", inline=True)

        if self.reason.value and self.reason.value.strip():
            embed.add_field(name="Reason", value=self.reason.value.strip()[:1024], inline=False)

        embed.add_field(
            name="Performed by",
            value=f"{self.actor.mention}\nRank: **{self.actor_rank or 'Unknown'}**",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        # ----------------------------
        # NEW: Send the log embed to Log Channel ID (or fallback to where command was run)
        # ----------------------------
        if interaction.guild:
            log_target = None

            # Preferred: permissions sheet Log Channel ID
            if self.log_channel_id:
                ch = interaction.guild.get_channel(self.log_channel_id)
                if isinstance(ch, (discord.TextChannel, discord.Thread)):
                    log_target = ch

            # Fallback: channel where /modify_player was run
            if log_target is None and isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                log_target = interaction.channel

            if log_target:
                log = discord.Embed(
                    title="🧾 Modify Player Totals Log (Adjustment)",
                    description=(
                        f"**Target:** {self.target.mention}\n"
                        f"**Mode:** `{self.mode}` (stored as delta)\n"
                        f"**Request ID:** `{self.request_id}`"
                    ),
                    color=0x3498DB,
                    timestamp=datetime.now(timezone.utc),
                )
                log.add_field(name="Stat", value=self.stat_name, inline=True)
                log.add_field(name="Delta", value=f"{delta_int:+d}", inline=True)
                log.add_field(name="Computed", value=f"{old_int} → {new_int}", inline=True)
                log.add_field(name="When", value=f"<t:{_now_ts()}:F>", inline=True)
                log.add_field(
                    name="Performed by",
                    value=f"{self.actor.mention} (Rank: {self.actor_rank or 'Unknown'})",
                    inline=False,
                )
                if self.reason.value and self.reason.value.strip():
                    log.add_field(name="Reason", value=self.reason.value.strip()[:1024], inline=False)

                await log_target.send(embed=log)

        # (Optional) Keep existing review channel behavior if you still want it:
        # If you DON'T want a separate review channel anymore, delete this block.
        if self.review_channel_id and interaction.guild:
            ch = interaction.guild.get_channel(self.review_channel_id)
            if ch and isinstance(ch, (discord.TextChannel, discord.Thread)):
                review = discord.Embed(
                    title="🧾 Modify Player Totals Review (Adjustment)",
                    description=(
                        f"**Target:** {self.target.mention}\n"
                        f"**Mode:** `{self.mode}` (stored as delta)\n"
                        f"**Request ID:** `{self.request_id}`"
                    ),
                    color=0x8E44AD,
                    timestamp=datetime.now(timezone.utc),
                )
                review.add_field(name="Stat", value=self.stat_name, inline=True)
                review.add_field(name="Delta", value=f"{delta_int:+d}", inline=True)
                review.add_field(name="Computed", value=f"{old_int} → {new_int}", inline=True)
                review.add_field(name="When", value=f"<t:{_now_ts()}:F>", inline=True)
                review.add_field(
                    name="Performed by",
                    value=f"{self.actor.mention} (Rank: {self.actor_rank or 'Unknown'})",
                    inline=False,
                )
                if self.reason.value and self.reason.value.strip():
                    review.add_field(name="Reason", value=self.reason.value.strip()[:1024], inline=False)
                await ch.send(embed=review)


class ModifyPlayerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # LAZY: sheet is opened on first use (in a thread), not at startup
        self.gc = None
        self.sheet = None

        self.cache_duration = 60
        self._cache: Dict[str, object] = {}
        self._cache_ts: Dict[str, float] = {}

        self.last_api_call = 0.0
        self.api_call_delay = 0.5
        self.retry_count = 3
        self.retry_base_delay = 1.5

        print("[OK] ModifyPlayerCog ready")

    # ----------------------------
    # Google setup / helpers
    # ----------------------------
    def _ensure_sheet(self):
        """Lazy-open spreadsheet on first use (runs in a thread, never at startup)."""
        if self.sheet is None:
            self.sheet = open_spreadsheet()
        return self.sheet

    def setup_google_sheets(self):
        """DEPRECATED: kept for compat. Use _ensure_sheet() instead."""
        self._ensure_sheet()

    async def rate_limited_api_call(self, func, *args, **kwargs):
        for attempt in range(1, self.retry_count + 1):
            current_time = time.time()
            since = current_time - self.last_api_call
            if since < self.api_call_delay:
                await asyncio.sleep(self.api_call_delay - since)

            try:
                result = func(*args, **kwargs)
                self.last_api_call = time.time()
                return result
            except Exception as e:
                print(f"❌ Sheets API error (attempt {attempt}/{self.retry_count}): {e}")
                if attempt >= self.retry_count:
                    raise
                await asyncio.sleep(self.retry_base_delay * attempt)

    def _cache_get(self, key: str):
        ts = self._cache_ts.get(key)
        if ts and (time.time() - ts) < self.cache_duration:
            return self._cache.get(key)
        return None

    def _cache_set(self, key: str, value):
        self._cache[key] = value
        self._cache_ts[key] = time.time()

    def clear_cache(self, contains: Optional[str] = None):
        if not contains:
            self._cache.clear()
            self._cache_ts.clear()
            return
        for k in list(self._cache.keys()):
            if contains in k:
                self._cache.pop(k, None)
                self._cache_ts.pop(k, None)

    async def get_worksheet(self, name: str):
        if not self._ensure_sheet():
            return None
        cache_key = f"ws:{name}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        try:
            ws = await self.rate_limited_api_call(self.sheet.worksheet, name)
            self._cache_set(cache_key, ws)
            return ws
        except Exception as e:
            print(f"❌ Failed to open worksheet {name}: {e}")
            return None

    async def get_values(self, ws_name: str) -> List[List[str]]:
        cache_key = f"vals:{ws_name}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        ws = await self.get_worksheet(ws_name)
        if not ws:
            return []
        try:
            vals = await self.rate_limited_api_call(ws.get_all_values)
            self._cache_set(cache_key, vals)
            return vals
        except Exception as e:
            print(f"❌ Failed to read values from {ws_name}: {e}")
            return []

    # ----------------------------
    # Permissions / Member lookups
    # ----------------------------
    async def get_permission_row(self, command_name: str) -> Optional[dict]:
        rows = await self.get_values(PERMISSIONS_SHEET)
        if not rows or len(rows) < 2:
            return None

        headers = rows[0]
        header_map = {h.strip(): i for i, h in enumerate(headers) if h and h.strip()}

        i_slash = header_map.get(PERM_COL_SLASH)
        i_review = header_map.get(PERM_COL_REVIEW)
        i_log = header_map.get(PERM_COL_LOG)  # NEW
        i_run = header_map.get(PERM_COL_RUN)
        i_edit = header_map.get(PERM_COL_EDIT)

        if i_slash is None:
            return None

        want = _norm_cmd_name(command_name)

        for r in rows[1:]:
            raw = r[i_slash] if i_slash < len(r) else ""
            if _norm_cmd_name(raw) != want:
                continue

            def _to_int_channel(val) -> Optional[int]:
                s = str(val).strip() if val is not None else ""
                if s.isdigit():
                    try:
                        return int(s)
                    except Exception:
                        return None
                return None

            review_id = _to_int_channel(r[i_review] if (i_review is not None and i_review < len(r)) else "")
            log_id = _to_int_channel(r[i_log] if (i_log is not None and i_log < len(r)) else "")  # NEW
            run_raw = r[i_run] if (i_run is not None and i_run < len(r)) else ""
            edit_raw = r[i_edit] if (i_edit is not None and i_edit < len(r)) else ""

            return {
                "review_channel_id": review_id,
                "log_channel_id": log_id,  # NEW
                "run_ranks": _parse_rank_list(run_raw),
                "edit_ranks": _parse_rank_list(edit_raw),
            }

        return None

    async def lookup_member_rank(self, user_id: str) -> str:
        rows = await self.get_values(MEMBER_LOG_SHEET)
        if not rows or len(rows) < 2:
            return "Unknown"

        headers = rows[0]
        header_map = {h.strip(): i for i, h in enumerate(headers) if h and h.strip()}

        id_col = None
        for h in MEMBER_ID_HEADERS:
            if h in header_map:
                id_col = header_map[h]
                break
        if id_col is None:
            id_col = 0

        rank_col = None
        for h in MEMBER_RANK_HEADERS:
            if h in header_map:
                rank_col = header_map[h]
                break
        if rank_col is None:
            rank_col = 2

        for r in rows[1:]:
            rid = r[id_col] if id_col < len(r) else ""
            if str(rid).strip() == str(user_id).strip():
                rank = r[rank_col] if rank_col < len(r) else ""
                return rank.strip() or "Unknown"

        return "Unknown"

    async def can_run(
        self,
        interaction: discord.Interaction,
        perm: Optional[dict],
    ) -> Tuple[bool, str, Optional[int], Optional[int]]:
        """
        Returns: allowed, actor_rank, review_channel_id, log_channel_id
        """
        if interaction.user.guild_permissions.administrator:
            return True, "Administrator", (perm.get("review_channel_id") if perm else None), (perm.get("log_channel_id") if perm else None)

        actor_rank = await self.lookup_member_rank(str(interaction.user.id))
        if not perm:
            return False, actor_rank, None, None

        allowed_ranks = perm.get("run_ranks", [])
        if actor_rank.lower() in allowed_ranks:
            return True, actor_rank, perm.get("review_channel_id"), perm.get("log_channel_id")

        return False, actor_rank, perm.get("review_channel_id"), perm.get("log_channel_id")

    # ----------------------------
    # Totals read logic (computed sheet)
    # ----------------------------
    async def fetch_totals_for_user(self, target_user_id: str) -> dict:
        values = await self.get_values(TOTALS_SHEET)
        if not values or len(values) < 2:
            return {"ok": False, "error": "Patrols_User_Totals has no data.", "totals": {}, "row_index": None}

        headers = values[0]
        header_map = {h.strip(): i for i, h in enumerate(headers) if h and h.strip()}

        id_col = None
        for h in TOTALS_ID_HEADERS:
            if h in header_map:
                id_col = header_map[h]
                break
        if id_col is None:
            id_col = 0

        missing = [h for h in EDITABLE_HEADERS if h not in header_map]
        if missing:
            return {"ok": False, "error": f"Missing columns in Patrols_User_Totals: {', '.join(missing)}", "totals": {}, "row_index": None}

        target_row_idx = None
        for i, row in enumerate(values[1:], start=2):
            rid = row[id_col] if id_col < len(row) else ""
            if str(rid).strip() == str(target_user_id).strip():
                target_row_idx = i
                break

        if not target_row_idx:
            return {"ok": False, "error": f"Player not found in Patrols_User_Totals for ID `{target_user_id}`.", "totals": {}, "row_index": None}

        row = values[target_row_idx - 1]
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))

        totals: Dict[str, str] = {}
        for h in EDITABLE_HEADERS:
            totals[h] = row[header_map[h]] if header_map[h] < len(row) else "0"

        return {"ok": True, "totals": totals, "row_index": target_row_idx, "header_map": header_map}

    # ----------------------------
    # Adjustments write logic (append-only)
    # ----------------------------
    async def _get_adjustments_header_map(self) -> Tuple[Optional[dict], Optional[str]]:
        rows = await self.get_values(ADJUSTMENTS_SHEET)
        if not rows or len(rows) < 1:
            return None, f"{ADJUSTMENTS_SHEET} is missing headers."

        headers = rows[0]
        header_map = {h.strip(): i for i, h in enumerate(headers) if h and h.strip()}

        if ADJ_USER_ID_HEADER not in header_map:
            return None, f"{ADJUSTMENTS_SHEET} is missing required header '{ADJ_USER_ID_HEADER}'."

        missing_stats = [h for h in EDITABLE_HEADERS if h not in header_map]
        missing_meta = [h for h in ADJ_META_HEADERS if h not in header_map]
        missing = missing_stats + missing_meta
        if missing:
            return None, f"{ADJUSTMENTS_SHEET} is missing columns: {', '.join(missing)}"

        return header_map, None

    async def write_adjustment_for_user(
        self,
        *,
        target_user_id: str,
        stat_name: str,
        input_value: int,
        mode: str,
        reason: str,
        admin_id: str,
    ) -> dict:
        ws = await self.get_worksheet(ADJUSTMENTS_SHEET)
        if not ws:
            return {"ok": False, "error": "Could not access Patrols_User_Adjustments sheet."}

        snap = await self.fetch_totals_for_user(target_user_id)
        if not snap["ok"]:
            return {"ok": False, "error": snap["error"]}

        totals: Dict[str, str] = snap["totals"]
        old_raw = totals.get(stat_name, "0")
        old_int = _safe_int(old_raw, allow_blank=False) or 0

        if mode == "add":
            delta = int(input_value)
            new_int = old_int + delta
        else:
            new_int = int(input_value)
            delta = new_int - old_int

        header_map, err = await self._get_adjustments_header_map()
        if err or not header_map:
            return {"ok": False, "error": err or "Adjustments header map error."}

        width = max(header_map.values()) + 1
        out = [""] * width

        out[header_map[ADJ_USER_ID_HEADER]] = str(target_user_id)
        out[header_map[stat_name]] = str(delta)

        out[header_map["Delta"]] = str(delta)
        out[header_map["Reason"]] = reason[:200] if reason else ""
        out[header_map["AdminID"]] = str(admin_id)
        out[header_map["Timestamp"]] = _iso_now()

        try:
            await self.rate_limited_api_call(ws.append_row, out, value_input_option="USER_ENTERED")
        except Exception as e:
            print(f"❌ Failed appending adjustment row for {target_user_id}: {e}")
            return {"ok": False, "error": "Failed to write adjustment to Google Sheets. Check logs for details."}

        self.clear_cache(f"vals:{ADJUSTMENTS_SHEET}")
        return {"ok": True, "old": old_int, "new": new_int, "delta": delta}

    # ----------------------------
    # Slash command
    # ----------------------------
    @app_commands.command(name="modify_player", description="Admin: create an adjustment for Patrols_User_Totals stats")
    @app_commands.describe(
        user="Select the player to modify",
        mode="Set computes a delta to reach that value. Add writes the delta directly.",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Set (compute delta)", value="set"),
            app_commands.Choice(name="Add (delta)", value="add"),
        ]
    )
    async def modify_player(self, interaction: discord.Interaction, user: discord.Member, mode: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)

        perm = await self.get_permission_row("/Modify_Player")
        allowed, actor_rank, review_channel_id, log_channel_id = await self.can_run(interaction, perm)

        if not allowed:
            await interaction.followup.send("❌ You don't have permission to use this command.", ephemeral=True)
            return

        snap = await self.fetch_totals_for_user(str(user.id))
        if not snap["ok"]:
            await interaction.followup.send(f"❌ {snap['error']}", ephemeral=True)
            return

        rid = _req_id()
        totals: Dict[str, str] = snap["totals"]

        embed = _build_totals_embed(user, mode.value, totals, rid)

        view = ModifyPlayerView(
            cog=self,
            target=user,
            mode=mode.value,
            totals=totals,
            review_channel_id=review_channel_id,
            log_channel_id=log_channel_id,  # NEW
            actor_rank=actor_rank,
            request_id=rid,
        )

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModifyPlayerCog(bot))
