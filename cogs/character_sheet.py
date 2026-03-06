# -*- coding: utf-8 -*-
from __future__ import annotations

print("=== LOADED character_sheet.py v3.3.1 (QUESTS STATS UI RENAME) ===")

import re
import asyncio
import time
import random
from typing import Optional, Dict, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from utils.google_auth import open_spreadsheet

try:
    from gspread.exceptions import APIError as GSpreadAPIError
except ImportError:
    GSpreadAPIError = None  # type: ignore[misc,assignment]

# ----------------------------
# Sheets / tabs
# ----------------------------
MEMBER_LOG_SHEET = "Member Log"
BANNERS_POINTS_SHEET = "Banners points per user"
BANNERS_SHEET = "Banners"
PERMISSIONS_SHEET = "Permissions for slash commands"
REPUTATION_SHEET = "Reputation"  # v2.3: Reputation tab for level icons
BANK_SHEET = "Bank"  # v3.2: Bank tab for wallet balances
PATROLS_TOTALS_SHEET = "Patrols_User_Totals"        # v3.3: page 2 stats baseline
PATROLS_ADJUSTMENTS_SHEET = "Patrols_User_Adjustments"  # v3.3: per-user deltas applied on top

USER_ID_HEADER = "User ID"
BANNER_HEADER = "Banner"
REPUTATION_XP_HEADER = "Reputation XP"  # v2.3: Column in Member Log for user's rep XP

# v3.2: Header candidates for Bank sheet columns
BANK_USER_ID_CANDIDATES = ["User ID", "Discord ID", "DiscordId", "ID"]
BANK_GOLD_CANDIDATES = ["Gold", "gold", "Gold Balance", "Gold_Amount", "GoldAmount"]
BANK_SILVER_CANDIDATES = ["Silver", "silver", "Silver Balance", "Silver_Amount", "SilverAmount"]
BANK_COPPER_CANDIDATES = ["Copper", "copper", "Copper Balance", "Copper_Amount", "CopperAmount"]

# v3.3: Patrol totals stat columns to display (exact headers expected)
PATROLS_STATS_COLUMNS = [
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

# ----------------------------
# Command mapping (for permissions sheet lookups)
# ----------------------------
VIEW_PROGRESS_COMMAND = "/view_progress"  # matches column A in Permissions tab

# Thumbnail used in embeds
CHARACTER_SHEET_THUMBNAIL = "https://drive.google.com/uc?export=view&id=1kwVXv1dI_VOWOP0Wtk6T2HWIEukbYabl"

# ----------------------------
# House forum cache
# key: (guild_id, forum_channel_id) -> (timestamp, map)
# map: normalized_banner_name -> house_name
# ----------------------------
HOUSE_CACHE_TTL = 300  # seconds
_HOUSE_CACHE: Dict[Tuple[int, int], Tuple[float, Dict[str, str]]] = {}

# ----------------------------
# Sheets value cache (TTL-based, shared across interactions)
# key: sheet tab name -> (timestamp, data)
# ----------------------------
SHEETS_CACHE_TTL = 20  # seconds
_SHEETS_VALUES_CACHE: Dict[str, Tuple[float, list]] = {}


def _is_429(e: Exception) -> bool:
    """Check if an exception is a Google Sheets 429 rate-limit error."""
    if GSpreadAPIError is not None and isinstance(e, GSpreadAPIError):
        resp = getattr(e, "response", None)
        if resp is not None and getattr(resp, "status_code", 0) == 429:
            return True
    err_str = str(e).lower()
    return "429" in err_str or "quota" in err_str or "rate limit" in err_str


def _get_all_values_cached(ws, cache_key: str, ttl: float = SHEETS_CACHE_TTL) -> list:
    """
    Cached wrapper for ws.get_all_values() with exponential backoff on 429.
    Retries up to 4 times. Re-raises non-429 exceptions immediately.
    Falls back to stale cache if all retries exhausted.
    """
    now = time.time()
    cached = _SHEETS_VALUES_CACHE.get(cache_key)
    if cached and (now - cached[0]) < ttl:
        return cached[1]

    last_exc: Optional[Exception] = None
    for attempt in range(4):
        try:
            data = ws.get_all_values()
            _SHEETS_VALUES_CACHE[cache_key] = (time.time(), data)
            return data
        except Exception as exc:
            if _is_429(exc):
                last_exc = exc
                delay = (2 ** attempt) + random.uniform(0, 1)
                print(f"[CharacterSheet] 429 on '{cache_key}' (attempt {attempt + 1}/4), waiting {delay:.1f}s...")
                time.sleep(delay)
            else:
                raise

    # All retries exhausted — return stale cache if available
    if cached:
        print(f"[CharacterSheet] 429 exhausted retries for '{cache_key}', using stale cache")
        return cached[1]
    raise last_exc  # type: ignore[misc]


# ----------------------------
# Helpers
# ----------------------------
def _normalize_role_key(s: str) -> str:
    """
    Make matching tolerant of emojis/punctuation:
    - lower
    - remove custom emoji tokens <:name:id>
    - keep letters/numbers/spaces only
    """
    if not s:
        return ""
    s = str(s).strip().lower()

    # remove custom emoji markup like <:Explorer:1234567890>
    s = re.sub(r"<a?:\w+:\d+>", "", s)

    # remove non-alnum except spaces
    s = re.sub(r"[^a-z0-9\s]+", "", s)

    # collapse spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _find_banner_role(guild: discord.Guild, banner_name: str) -> Optional[discord.Role]:
    """
    Find a role whose normalized name matches the normalized banner name.
    """
    if not guild or not banner_name:
        return None

    target = _normalize_role_key(banner_name)

    for role in guild.roles:
        if _normalize_role_key(role.name) == target:
            return role
    return None


def _display_banner(guild: discord.Guild, banner_name: str) -> str:
    """
    Return role.mention if found, else fallback to the banner_name.
    """
    role = _find_banner_role(guild, banner_name)
    return role.mention if role else banner_name


def _display_banner_name(guild: discord.Guild, banner_name: str) -> str:
    """
    GOAL 1 helper: Return the actual role.name (with emojis) if found,
    else fallback to the banner_name. Used for monospace code block alignment.
    """
    role = _find_banner_role(guild, banner_name)
    return role.name if role else banner_name


def _to_int(value) -> int:
    try:
        if value is None:
            return 0
        s = str(value).strip()
        if s == "":
            return 0
        return int(float(s))
    except Exception:
        return 0


# v2.3: Google Drive URL conversion helpers
def _extract_drive_file_id(url: str) -> Optional[str]:
    """
    Extracts the file ID from various Google Drive URL formats.
    - https://drive.google.com/file/d/<FILE_ID>/view?usp=drive_link
    - https://drive.google.com/uc?export=view&id=<FILE_ID>
    - https://drive.google.com/open?id=<FILE_ID>
    Returns None if no ID found.
    """
    if not url:
        return None
    url = str(url).strip()

    # Pattern 1: /file/d/<FILE_ID>/
    match = re.search(r"/file/d/([^/]+)", url)
    if match:
        return match.group(1)

    # Pattern 2: id=<FILE_ID> (query param)
    match = re.search(r"[?&]id=([^&]+)", url)
    if match:
        return match.group(1)

    return None


def _normalize_drive_image_url(url: str) -> str:
    """
    Converts Google Drive share links to direct image URLs usable by Discord embeds.
    If already a direct uc?export=view&id= link, returns as-is.
    If not a Drive link or extraction fails, returns the original URL.
    """
    if not url:
        return ""
    url = str(url).strip()

    # Already a direct link
    if "uc?export=view&id=" in url or "uc?id=" in url:
        return url

    # Try to extract file ID and convert
    file_id = _extract_drive_file_id(url)
    if file_id:
        return f"https://drive.google.com/uc?export=view&id={file_id}"

    # Not a recognized Drive link, return original
    return url


def _split_role_csv(value: str) -> set[str]:
    if not value:
        return set()
    parts = [p.strip() for p in str(value).split(",")]
    return {p.lower() for p in parts if p}


def _member_has_any_role_name(member: discord.Member, allowed_lower: set[str]) -> bool:
    if not allowed_lower:
        return False
    return any((r.name or "").strip().lower() in allowed_lower for r in member.roles)


def _get_allowed_roles_from_permissions_sheet(sheet, slash_command: str, column_letter: str) -> set[str]:
    """
    Reads Permissions for slash commands:
      - finds the row where column A == slash_command (e.g. "/view_progress")
      - returns roles from the specified column (e.g. "C")
    Live read, no caching. Safe failure returns empty set.
    """
    if not sheet:
        return set()

    try:
        ws = sheet.worksheet(PERMISSIONS_SHEET)
        data = _get_all_values_cached(ws, PERMISSIONS_SHEET)
    except Exception:
        return set()

    if not data or len(data) < 2:
        return set()

    target = (slash_command or "").strip().lower()
    col_idx = ord(column_letter.upper()) - ord("A")
    if col_idx < 0:
        return set()

    for row in data[1:]:
        cmd = (row[0] if len(row) > 0 else "").strip().lower()
        if cmd == target:
            cell_val = row[col_idx] if len(row) > col_idx else ""
            return _split_role_csv(cell_val)

    return set()


def _get_command_cell(sheet, slash_command: str, column_letter: str) -> str:
    """
    Returns raw cell string from the row where column A == slash_command,
    for the requested column letter. Live read, no caching.
    """
    if not sheet:
        return ""

    try:
        ws = sheet.worksheet(PERMISSIONS_SHEET)
        data = _get_all_values_cached(ws, PERMISSIONS_SHEET)
    except Exception:
        return ""

    if not data or len(data) < 2:
        return ""

    target = (slash_command or "").strip().lower()
    col_idx = ord(column_letter.upper()) - ord("A")
    if col_idx < 0:
        return ""

    for row in data[1:]:
        cmd = (row[0] if len(row) > 0 else "").strip().lower()
        if cmd == target:
            return (row[col_idx] if len(row) > col_idx else "").strip()

    return ""


def _get_minutes_setting(sheet, slash_command: str, column_letter: str, default_minutes: int = 5) -> int:
    """
    Reads a minutes value (like column E) for a command.
    - blank/invalid -> default_minutes
    - 0 -> disabled
    - clamps 0..60 by default
    """
    raw = _get_command_cell(sheet, slash_command, column_letter)
    if raw == "":
        return int(default_minutes)

    try:
        minutes = int(float(raw))
    except Exception:
        return int(default_minutes)

    if minutes < 0:
        minutes = 0
    if minutes > 60:
        minutes = 60

    return minutes


def _get_house_forum_channel_id(sheet) -> int:
    """
    Reads House Forum channel ID from Permissions tab:
      Row where Column A == /view_progress
      Column F == House Forum channel (ID)
    Returns 0 if blank/invalid.
    """
    raw = _get_command_cell(sheet, VIEW_PROGRESS_COMMAND, "F")
    try:
        return int(str(raw).strip())
    except Exception:
        return 0


# v3.2: Helper to find header index from a list of candidates
def _find_header_index_any(headers: list[str], candidates: list[str]) -> int:
    """
    Returns the index of the first header that matches any candidate (case-insensitive).
    Returns -1 if no match found.
    """
    for i, h in enumerate(headers):
        h_lower = str(h).strip().lower()
        for c in candidates:
            if h_lower == c.lower():
                return i
    return -1


# v3.3: prettify stat label
def _pretty_stat_name(name: str) -> str:
    # Display-only: replace underscores, then Patrol/Patrols -> Quest/Quests
    label = str(name or "").replace("_", " ").strip()
    label = re.sub(r"\bPatrols\b", "Quests", label, flags=re.IGNORECASE)
    label = re.sub(r"\bPatrol\b", "Quest", label, flags=re.IGNORECASE)
    return label


# ----------------------------
# House lookup (forum scan)
# ----------------------------
async def _build_house_map_for_guild(guild: discord.Guild, forum_channel_id: int) -> Dict[str, str]:
    """
    Scans the Houses forum channel:
      - 1 thread per banner
      - Thread has an applied tag with NAME matching the banner role name
      - Thread title is the House name

    Returns: { normalized_banner_name: house_name }
    """
    if not guild or not forum_channel_id:
        return {}

    channel = guild.get_channel(int(forum_channel_id))
    if not isinstance(channel, discord.ForumChannel):
        return {}

    mapping: Dict[str, str] = {}

    # Active threads
    try:
        for thread in channel.threads:
            for tag in thread.applied_tags:
                mapping[_normalize_role_key(tag.name)] = thread.name
    except Exception:
        pass

    # Archived threads (optional)
    try:
        async for thread in channel.archived_threads(limit=200):
            for tag in thread.applied_tags:
                key = _normalize_role_key(tag.name)
                mapping.setdefault(key, thread.name)
    except Exception:
        pass

    return mapping


async def _get_house_name_for_banner(
    guild: discord.Guild,
    forum_channel_id: int,
    banner_name: str
) -> Optional[str]:
    """
    Cached lookup for house name by banner role name.
    Cache is keyed by (guild_id, forum_channel_id) so if the forum id changes,
    it automatically uses a different cache bucket.
    """
    if not guild or not forum_channel_id or not banner_name:
        return None

    cache_key = (guild.id, int(forum_channel_id))
    now = discord.utils.utcnow().timestamp()

    cached = _HOUSE_CACHE.get(cache_key)
    if not cached or (now - cached[0]) > HOUSE_CACHE_TTL:
        house_map = await _build_house_map_for_guild(guild, int(forum_channel_id))
        _HOUSE_CACHE[cache_key] = (now, house_map)
    else:
        house_map = cached[1]

    return house_map.get(_normalize_role_key(banner_name))


# ----------------------------
# Paged View (v3.3)
# ----------------------------
class ProgressPagesView(discord.ui.View):
    def __init__(self, owner_id: int, embed_page1: discord.Embed, embed_page2: discord.Embed, timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.page = 1
        self.embed1 = embed_page1
        self.embed2 = embed_page2
        self._sync_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return False
        return True

    def _sync_buttons(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "pg_prev":
                    child.disabled = (self.page == 1)
                if child.custom_id == "pg_next":
                    child.disabled = (self.page == 2)

    @discord.ui.button(label="Prev Page", style=discord.ButtonStyle.secondary, emoji="⬅️", custom_id="pg_prev", row=0)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed1, view=self)

    @discord.ui.button(label="Next Page", style=discord.ButtonStyle.primary, emoji="➡️", custom_id="pg_next", row=0)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = 2
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed2, view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🚪", custom_id="pg_close", row=0)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


# ----------------------------
# Cog
# ----------------------------
class CharacterSheet(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # LAZY: sheet is opened on first use (in a thread), not at startup
        self.sheet = None

    def _ensure_sheet(self):
        """Lazy-open spreadsheet on first use (runs in a thread), not at startup."""
        if self.sheet is None:
            self.sheet = open_spreadsheet()
        return self.sheet

    def _worksheet(self, name: str):
        if not self._ensure_sheet():
            return None
        return self.sheet.worksheet(name)

    def _find_header_index(self, headers: list[str], name: str) -> int:
        name_l = name.strip().lower()
        for i, h in enumerate(headers):
            if str(h).strip().lower() == name_l:
                return i
        return -1

    def _get_member_log_row_dict(self, user_id: str) -> Dict[str, str]:
        """Read Member Log once (cached) and return the user's row as a header->value dict."""
        ws = self._worksheet(MEMBER_LOG_SHEET)
        if not ws:
            return {}

        try:
            data = _get_all_values_cached(ws, MEMBER_LOG_SHEET)
        except Exception:
            return {}

        if not data or len(data) < 2:
            return {}

        headers = data[0]
        uid_idx = self._find_header_index(headers, USER_ID_HEADER)
        if uid_idx == -1:
            return {}

        target = str(user_id).strip()
        for row in data[1:]:
            uid = (row[uid_idx] if uid_idx < len(row) else "").strip()
            if uid == target:
                return {
                    str(headers[i]).strip(): (row[i] if i < len(row) else "").strip()
                    for i in range(len(headers))
                }

        return {}

    # v3.2: Get user's bank balances from Bank sheet
    def _get_bank_balances(self, user_id: str) -> tuple[int, int, int]:
        """
        Reads the Bank sheet and returns (gold, silver, copper) for the given user.
        Uses one get_all_values() call for efficiency.
        Returns (0, 0, 0) if user not found or sheet unavailable.
        """
        ws = self._worksheet(BANK_SHEET)
        if not ws:
            return (0, 0, 0)

        try:
            data = _get_all_values_cached(ws, BANK_SHEET)
        except Exception:
            return (0, 0, 0)

        if not data or len(data) < 2:
            return (0, 0, 0)

        headers = data[0]

        uid_col = _find_header_index_any(headers, BANK_USER_ID_CANDIDATES)
        if uid_col == -1:
            uid_col = 0

        gold_col = _find_header_index_any(headers, BANK_GOLD_CANDIDATES)
        silver_col = _find_header_index_any(headers, BANK_SILVER_CANDIDATES)
        copper_col = _find_header_index_any(headers, BANK_COPPER_CANDIDATES)

        target_id = str(user_id).strip()
        for row in data[1:]:
            row_uid = (row[uid_col] if uid_col < len(row) else "").strip()
            if row_uid == target_id:
                gold = _to_int(row[gold_col] if gold_col != -1 and gold_col < len(row) else "")
                silver = _to_int(row[silver_col] if silver_col != -1 and silver_col < len(row) else "")
                copper = _to_int(row[copper_col] if copper_col != -1 and copper_col < len(row) else "")
                return (gold, silver, copper)

        return (0, 0, 0)

    # v3.3: Patrol totals read (baseline + adjustments merged)
    def _get_patrol_totals_for_user(self, user_id: str) -> Dict[str, int]:
        """
        Returns authoritative stat totals by merging Patrols_User_Totals (baseline)
        with all matching rows in Patrols_User_Adjustments (deltas).
        Never read baseline alone — adjustments are always applied on top.
        """
        out: Dict[str, int] = {k: 0 for k in PATROLS_STATS_COLUMNS}
        target = str(user_id).strip()

        # --- Baseline (Patrols_User_Totals) ---
        ws_totals = self._worksheet(PATROLS_TOTALS_SHEET)
        if ws_totals:
            try:
                data = _get_all_values_cached(ws_totals, PATROLS_TOTALS_SHEET)
                if data and len(data) >= 2:
                    headers = data[0]
                    uid_idx = self._find_header_index(headers, USER_ID_HEADER)
                    if uid_idx == -1:
                        uid_idx = self._find_header_index(headers, "Discord ID")
                    if uid_idx == -1:
                        uid_idx = 0
                    col_map: Dict[str, int] = {
                        stat: self._find_header_index(headers, stat)
                        for stat in PATROLS_STATS_COLUMNS
                    }
                    for row in data[1:]:
                        uid = (row[uid_idx] if uid_idx < len(row) else "").strip()
                        if uid == target:
                            for stat in PATROLS_STATS_COLUMNS:
                                idx = col_map.get(stat, -1)
                                if idx != -1 and idx < len(row):
                                    out[stat] = _to_int(row[idx])
                            break
            except Exception:
                pass

        # --- Adjustments (Patrols_User_Adjustments) ---
        ws_adj = self._worksheet(PATROLS_ADJUSTMENTS_SHEET)
        if ws_adj:
            try:
                data = _get_all_values_cached(ws_adj, PATROLS_ADJUSTMENTS_SHEET)
                if data and len(data) >= 2:
                    headers = data[0]
                    uid_idx = self._find_header_index(headers, USER_ID_HEADER)
                    if uid_idx == -1:
                        uid_idx = 0
                    col_map = {
                        stat: self._find_header_index(headers, stat)
                        for stat in PATROLS_STATS_COLUMNS
                    }
                    for row in data[1:]:
                        uid = (row[uid_idx] if uid_idx < len(row) else "").strip()
                        if uid != target:
                            continue
                        for stat in PATROLS_STATS_COLUMNS:
                            idx = col_map.get(stat, -1)
                            if idx != -1 and idx < len(row):
                                out[stat] += _to_int(row[idx])
            except Exception:
                pass

        return out

    # v2.4: Comprehensive level info for XP progress bar
    def _get_full_level_info(self, rep_xp: int) -> dict:
        ws = self._worksheet(REPUTATION_SHEET)
        if not ws:
            return {
                'level': 0, 'icon_url': '', 'current_threshold': 0,
                'next_threshold': None, 'next_level': None, 'is_max_level': True
            }

        try:
            data = _get_all_values_cached(ws, REPUTATION_SHEET)
        except Exception:
            return {
                'level': 0, 'icon_url': '', 'current_threshold': 0,
                'next_threshold': None, 'next_level': None, 'is_max_level': True
            }

        if len(data) < 2:
            return {
                'level': 0, 'icon_url': '', 'current_threshold': 0,
                'next_threshold': None, 'next_level': None, 'is_max_level': True
            }

        levels: list[tuple[int, int, str]] = []
        for row in data[1:]:
            if len(row) < 2:
                continue
            lvl = _to_int(row[0])
            xp_thresh = _to_int(row[1])
            icon_url = (row[7] if len(row) > 7 else "").strip()
            if lvl > 0:
                levels.append((lvl, xp_thresh, icon_url))

        if not levels:
            return {
                'level': 0, 'icon_url': '', 'current_threshold': 0,
                'next_threshold': None, 'next_level': None, 'is_max_level': True
            }

        levels.sort(key=lambda x: x[1])

        current_idx = 0
        for i, (lvl, thresh, icon) in enumerate(levels):
            if rep_xp >= thresh:
                current_idx = i
            else:
                break

        current_level, current_thresh, current_icon = levels[current_idx]
        current_icon = _normalize_drive_image_url(current_icon)

        is_max = (current_idx >= len(levels) - 1)

        if is_max:
            return {
                'level': current_level,
                'icon_url': current_icon,
                'current_threshold': current_thresh,
                'next_threshold': None,
                'next_level': None,
                'is_max_level': True
            }
        else:
            next_level, next_thresh, _next_icon = levels[current_idx + 1]
            return {
                'level': current_level,
                'icon_url': current_icon,
                'current_threshold': current_thresh,
                'next_threshold': next_thresh,
                'next_level': next_level,
                'is_max_level': False
            }

    def _get_banner_points_headers_and_row(self, user_id: str) -> tuple[Optional[list[str]], Optional[list[str]]]:
        ws = self._worksheet(BANNERS_POINTS_SHEET)
        if not ws:
            return None, None

        data = _get_all_values_cached(ws, BANNERS_POINTS_SHEET)
        if not data or len(data) < 2:
            return None, None

        headers = data[0]
        for row in data[1:]:
            uid = (row[0] if len(row) > 0 else "").strip()
            if uid == str(user_id).strip():
                return headers, row

        return headers, None

    def _build_points_breakdown(self, headers: list[str], row: Optional[list[str]]) -> tuple[list[tuple[str, int]], int]:
        breakdown: list[tuple[str, int]] = []
        total = 0

        for i in range(1, len(headers)):
            banner_name = str(headers[i]).strip()
            if not banner_name:
                continue

            val = row[i] if row and i < len(row) else ""
            pts = _to_int(val)
            breakdown.append((banner_name, pts))
            total += pts

        return breakdown, total

    def _build_progress_icons(self, thresholds: list[int], points: int) -> str:
        if not thresholds:
            return ""
        icons = []
        for t in thresholds:
            icons.append("\u2705" if points >= t else "\u2b1c")
        return " ".join(icons)

    def _format_banners_field_aligned(
        self,
        guild: discord.Guild,
        breakdown: list[tuple[str, int]],
        thresholds: list[int]
    ) -> str:
        if not breakdown:
            return "```\nNo banners configured.\n```"

        sorted_banners = sorted(breakdown, key=lambda kv: kv[1], reverse=True)

        max_name_len = 0
        max_pts_len = 0
        rows_data: list[tuple[str, str, str]] = []

        for banner_name, pts in sorted_banners:
            display_name = _display_banner_name(guild, banner_name)
            pts_str = str(pts)
            progress_icons = self._build_progress_icons(thresholds, pts)

            max_name_len = max(max_name_len, len(display_name))
            max_pts_len = max(max_pts_len, len(pts_str))
            rows_data.append((display_name, pts_str, progress_icons))

        max_name_len = min(max_name_len, 25)

        lines: list[str] = []
        for display_name, pts_str, progress_icons in rows_data:
            if len(display_name) > max_name_len:
                display_name = display_name[:max_name_len - 1] + "\u2026"

            name_col = display_name.ljust(max_name_len)
            pts_col = pts_str.rjust(max_pts_len)

            if progress_icons:
                lines.append(f"{name_col}  {pts_col}  {progress_icons}")
            else:
                lines.append(f"{name_col}  {pts_col}")

        joined = "\n".join(lines)
        result = f"```\n{joined}\n```"

        if len(result) > 1000:
            truncated_lines = []
            current_len = 7
            for line in lines:
                if current_len + len(line) + 1 > 950:
                    break
                truncated_lines.append(line)
                current_len += len(line) + 1
            truncated_lines.append("\u2026")
            result = f"```\n" + "\n".join(truncated_lines) + "\n```"

        return result

    def _get_banner_subrank_config_dynamic(self) -> tuple[list[int], dict[str, list[str]]]:
        thresholds: list[int] = []
        banner_to_ranks: dict[str, list[str]] = {}

        ws = self._worksheet(BANNERS_SHEET)
        if not ws:
            return thresholds, banner_to_ranks

        try:
            data = _get_all_values_cached(ws, BANNERS_SHEET)
        except Exception:
            return thresholds, banner_to_ranks

        if not data or len(data) < 3:
            return thresholds, banner_to_ranks

        row1 = data[0]
        for i in range(1, len(row1)):
            cell = (row1[i] if i < len(row1) else "").strip()
            if cell == "":
                break
            thresholds.append(_to_int(cell))

        if not thresholds:
            thresholds = [0]

        cleaned: list[int] = []
        for t in thresholds:
            t = max(0, int(t))
            if cleaned:
                t = max(cleaned[-1], t)
            cleaned.append(t)
        thresholds = cleaned

        n = len(thresholds)

        for r in data[2:]:
            banner_name = (r[0] if len(r) > 0 else "").strip()
            if not banner_name:
                continue

            ranks: list[str] = []
            for i in range(1, 1 + n):
                name = (r[i] if i < len(r) else "").strip()
                ranks.append(name if name else f"Sub Rank {i-1}")

            banner_to_ranks[_normalize_role_key(banner_name)] = ranks

        return thresholds, banner_to_ranks

    def _get_points_for_banner(self, headers: list[str], row: Optional[list[str]], banner_name: str) -> int:
        if not headers or not banner_name:
            return 0

        target = _normalize_role_key(banner_name)
        col_idx = -1
        for i in range(1, len(headers)):
            if _normalize_role_key(headers[i]) == target:
                col_idx = i
                break

        if col_idx == -1:
            return 0

        val = row[col_idx] if row and col_idx < len(row) else ""
        return _to_int(val)

    def _resolve_subrank_dynamic(
        self,
        thresholds: list[int],
        rank_names: list[str],
        points: int
    ) -> tuple[str, Optional[str], Optional[int], int]:
        if not thresholds:
            thresholds = [0]
        if not rank_names:
            rank_names = ["Sub Rank 0"]

        n = min(len(thresholds), len(rank_names))
        thresholds = thresholds[:n]
        rank_names = rank_names[:n]

        idx = 0
        for i in range(n):
            if points >= thresholds[i]:
                idx = i
            else:
                break

        current_name = rank_names[idx]

        if idx >= n - 1:
            return current_name, None, None, idx

        next_name = rank_names[idx + 1]
        needed = max(0, thresholds[idx + 1] - points)
        return current_name, next_name, needed, idx

    def _build_rank_progress_grid(self, thresholds: list[int], rank_names: list[str], points: int) -> str:
        if not thresholds:
            return "```\nNo rank thresholds configured.\n```"

        n = min(len(thresholds), len(rank_names)) if rank_names else len(thresholds)
        if n <= 0:
            return "```\nNo rank thresholds configured.\n```"

        names: list[str] = []
        for i in range(n):
            nm = (rank_names[i] if rank_names and i < len(rank_names) else "").strip()
            names.append(nm if nm else f"Sub Rank {i}")

        max_name_len = max(len(nm) for nm in names)
        max_name_len = min(max_name_len, 20)
        max_thresh_len = max(len(str(t)) for t in thresholds)

        lines: list[str] = []
        current_rank_idx = 0

        for i in range(n):
            achieved = points >= int(thresholds[i])
            is_highest = (i == n - 1)

            if achieved:
                current_rank_idx = i

            if achieved and is_highest:
                icon = "\U0001F3C6"
            else:
                icon = "\u2705" if achieved else "\u274c"

            display_name = names[i]
            if len(display_name) > max_name_len:
                display_name = display_name[:max_name_len - 1] + "\u2026"

            name_col = display_name.ljust(max_name_len)
            thresh_col = str(thresholds[i]).rjust(max_thresh_len)

            lines.append(f"{icon} {name_col}  {thresh_col}+")

        is_at_max_rank = (current_rank_idx >= n - 1)

        if not is_at_max_rank:
            current_points = points
            target_points = thresholds[current_rank_idx + 1]

            if target_points > 0:
                pct = int((current_points / target_points) * 100)
                pct = max(0, min(100, pct))
            else:
                pct = 100

            filled_blocks = int((pct / 100) * 10)
            empty_blocks = 10 - filled_blocks
            bar = "\u2588" * filled_blocks + "\u2591" * empty_blocks

            lines.append("")
            lines.append(f"Progress to next rank: [{bar}] {pct}% ({current_points}/{target_points})")
        else:
            lines.append("")
            lines.append("Max banner rank reached")

        out = "\n".join(lines)

        if len(out) > 950:
            out = out[:900] + "\n\u2026"

        return f"```\n{out}\n```"

    def _build_patrols_embed(
        self,
        guild: discord.Guild,
        target: discord.Member,
        level_info: dict,
        wallet: tuple[int, int, int],
        patrol_stats: Dict[str, int],
    ) -> discord.Embed:
        embed = discord.Embed(
            title="Character Sheet - Quests",
            description="Quest totals overview.",
            color=discord.Color.dark_teal(),
        )

        # Keep the same thumbnail style as page 1 (level icon preferred)
        level_icon = level_info.get("icon_url", "") or ""
        embed.set_thumbnail(url=level_icon if level_icon else CHARACTER_SHEET_THUMBNAIL)

        # Simple stat render (no underscores)
        lines = ["```", "QUEST TOTALS", "────────────", ""]
        for key in PATROLS_STATS_COLUMNS:
            label = _pretty_stat_name(key)
            val = patrol_stats.get(key, 0)
            lines.append(f"{label}: {val:,}")
        lines.append("```")

        embed.add_field(name="User", value=target.mention, inline=False)
        embed.add_field(name="Stats", value="\n".join(lines), inline=False)

        g, s, c = wallet
        embed.set_footer(text=f"Wallet: Gold - {g:,}  Silver - {s:,}  Copper - {c:,}")
        return embed

    async def _send_progress_for_user(self, interaction: discord.Interaction, target: discord.Member) -> None:
        # --- Fast checks BEFORE defer (no I/O, instant) ---
        if interaction.guild is None:
            return await interaction.response.send_message("This must be used in a server.", ephemeral=True)

        invoker = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not invoker:
            return await interaction.response.send_message("\u274c Member context not available.", ephemeral=True)

        # --- ACK immediately to prevent interaction timeout (3s limit) ---
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

        # --- Slow checks AFTER defer (sheet I/O) ---
        if not await asyncio.to_thread(self._ensure_sheet):
            return await interaction.edit_original_response(content="\u274c Google Sheets not available.")

        allowed_roles = await asyncio.to_thread(
            _get_allowed_roles_from_permissions_sheet, self.sheet, VIEW_PROGRESS_COMMAND, "C"
        )
        if allowed_roles and not _member_has_any_role_name(invoker, allowed_roles):
            return await interaction.edit_original_response(content="\u274c You don't have permission to use this.")

        viewing_other = (invoker.id != target.id)

        try:
            ml = await asyncio.to_thread(self._get_member_log_row_dict, str(target.id))
        except Exception as exc:
            if _is_429(exc):
                return await interaction.edit_original_response(
                    content="\u23f3 Sheets is rate-limiting me. Please try again in 20\u201330 seconds.",
                )
            raise

        active_banner_raw = (ml.get(BANNER_HEADER) or "Unassigned").strip()
        has_active_banner = (_normalize_role_key(active_banner_raw) not in ("", "unassigned"))
        active_banner_display = _display_banner(interaction.guild, active_banner_raw)

        if has_active_banner:
            forum_channel_id = await asyncio.to_thread(_get_house_forum_channel_id, self.sheet)
            house_name = await _get_house_name_for_banner(interaction.guild, forum_channel_id, active_banner_raw)
            banner_name_pretty = _display_banner_name(interaction.guild, active_banner_raw)

            if house_name:
                active_banner_display = f"{house_name}, Banner Of {banner_name_pretty}"
            else:
                active_banner_display = banner_name_pretty

        try:
            headers, user_row = await asyncio.to_thread(
                self._get_banner_points_headers_and_row, str(target.id)
            )
        except Exception as exc:
            if _is_429(exc):
                return await interaction.edit_original_response(
                    content="\u23f3 Sheets is rate-limiting me. Please try again in 20\u201330 seconds.",
                )
            raise
        if not headers:
            return await interaction.edit_original_response(content="\u274c Banner points sheet missing headers.")

        breakdown, total_all_banners = self._build_points_breakdown(headers, user_row)
        thresholds, banner_to_ranks = await asyncio.to_thread(self._get_banner_subrank_config_dynamic)
        banners_text = self._format_banners_field_aligned(interaction.guild, breakdown, thresholds)
        active_points = self._get_points_for_banner(headers, user_row, active_banner_raw) if has_active_banner else 0

        description = "You're viewing another user's progress overview." if viewing_other else "Your progress overview."

        rep_xp = _to_int(ml.get(REPUTATION_XP_HEADER) or "0")
        level_info = await asyncio.to_thread(self._get_full_level_info, rep_xp)

        if level_info['is_max_level']:
            xp_bar_text = f"**Max level reached**\nTotal XP: {rep_xp:,}"
        else:
            cur_thresh = level_info['current_threshold']
            next_thresh = level_info['next_threshold']

            progress_numerator = rep_xp - cur_thresh
            progress_denominator = next_thresh - cur_thresh

            if progress_denominator > 0:
                pct = int((progress_numerator / progress_denominator) * 100)
                pct = max(0, min(100, pct))
            else:
                pct = 100

            filled_blocks = round(pct / 10)
            empty_blocks = 10 - filled_blocks
            bar = "\u2588" * filled_blocks + "\u2591" * empty_blocks

            xp_bar_text = f"[{bar}] {pct}%\n({rep_xp:,} / {next_thresh:,})"

        embed1 = discord.Embed(
            title="Character Sheet - Progress",
            description=description,
            color=discord.Color.blue(),
        )

        profile_url = f"https://orderofthefallenstar.com/profile?playerId={target.id}"
        user_field_value = f"{target.mention}\n[View full profile]({profile_url})"
        embed1.add_field(name="User", value=user_field_value, inline=True)
        embed1.add_field(name="Reputation Level", value=f"Lvl {level_info['level']}", inline=True)
        embed1.add_field(name="XP Progress", value=xp_bar_text, inline=True)

        embed1.add_field(name="\u200b", value="--------------", inline=False)

        if has_active_banner:
            embed1.add_field(name="Active Banner", value=active_banner_display, inline=False)
            embed1.add_field(name="Active Banner Points", value=str(active_points), inline=True)

            ranks = banner_to_ranks.get(_normalize_role_key(active_banner_raw), ["Sub Rank 0"] * len(thresholds))

            current_subrank, next_subrank, needed, _rank_index = self._resolve_subrank_dynamic(
                thresholds,
                ranks,
                active_points
            )

            rank_grid = self._build_rank_progress_grid(thresholds, ranks, active_points)

            embed1.add_field(name="Banner Rank", value=current_subrank, inline=True)

            if next_subrank and needed is not None:
                embed1.add_field(name="Next Rank", value=f"{next_subrank} in `{needed}` point(s)", inline=True)
            else:
                embed1.add_field(name="Next Rank", value="Max banner rank reached", inline=True)

            embed1.add_field(name="Rank Progress", value=rank_grid, inline=False)

        embed1.add_field(name="Total Banner Points", value=str(total_all_banners), inline=False)
        embed1.add_field(name="Banner Points", value=banners_text, inline=False)

        level_icon = level_info['icon_url']
        thumbnail_url = level_icon if level_icon else CHARACTER_SHEET_THUMBNAIL
        embed1.set_thumbnail(url=thumbnail_url)

        gold_amount, silver_amount, copper_amount = await asyncio.to_thread(
            self._get_bank_balances, str(target.id)
        )
        embed1.set_footer(
            text=f"Wallet: Gold - {gold_amount:,}  Silver - {silver_amount:,}  Copper - {copper_amount:,}"
        )

        # v3.3: build page 2 (patrol totals)
        patrol_stats = await asyncio.to_thread(self._get_patrol_totals_for_user, str(target.id))
        embed2 = self._build_patrols_embed(
            guild=interaction.guild,
            target=target,
            level_info=level_info,
            wallet=(gold_amount, silver_amount, copper_amount),
            patrol_stats=patrol_stats,
        )

        view = ProgressPagesView(
            owner_id=invoker.id,
            embed_page1=embed1,
            embed_page2=embed2,
            timeout=300.0,
        )

        await interaction.edit_original_response(
            content=None,
            embed=embed1,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        minutes = await asyncio.to_thread(
            _get_minutes_setting, self.sheet, VIEW_PROGRESS_COMMAND, "E", 5
        )
        if minutes > 0:
            async def _auto_delete():
                try:
                    await asyncio.sleep(minutes * 60)
                    await interaction.delete_original_response()
                except Exception:
                    try:
                        await interaction.edit_original_response(content="(expired)", embed=None, view=None)
                    except Exception:
                        pass

            asyncio.create_task(_auto_delete())

    # ----------------------------
    # /view_progress
    # ----------------------------
    @app_commands.command(name="view_progress", description="View your character progress and Banner Points.")
    async def view_progress(self, interaction: discord.Interaction):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            return await interaction.response.send_message("\u274c Member context not available.", ephemeral=True)

        await self._send_progress_for_user(interaction, member)


# ----------------------------
# USER CONTEXT MENU (module level)
# Right click a user -> Apps -> View Progress
# ----------------------------
@app_commands.context_menu(name="View Progress")
async def view_progress_user_context(interaction: discord.Interaction, member: discord.Member):
    if interaction.guild is None:
        return await interaction.response.send_message("This must be used in a server.", ephemeral=True)

    bot = interaction.client
    if not isinstance(bot, commands.Bot):
        return await interaction.response.send_message("Bot not available.", ephemeral=True)

    cog = bot.get_cog("CharacterSheet")
    if not isinstance(cog, CharacterSheet):
        return await interaction.response.send_message("CharacterSheet cog not loaded.", ephemeral=True)

    await cog._send_progress_for_user(interaction, member)


# ----------------------------
# Setup
# ----------------------------
async def setup(bot: commands.Bot):
    await bot.add_cog(CharacterSheet(bot))

    # Ensure we don't duplicate the user context menu on reload
    try:
        bot.tree.remove_command("View Progress", type=discord.AppCommandType.user)
    except Exception:
        pass
    bot.tree.add_command(view_progress_user_context)
