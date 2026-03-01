# -*- coding: utf-8 -*-
from __future__ import annotations

print("=== LOADED shop.py v1.1 (SHOPKEEPER UI: LIST + BUY + CUSTOM CURRENCY EMOJIS) ===")

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar
from utils import inventory_store

import discord
from discord import app_commands
from discord.ext import commands

from utils.google_auth import open_spreadsheet

# ----------------------------
# Sheets / tabs
# ----------------------------
ITEM_LIST_SHEET = "Item List"
BANK_SHEET = "Bank"
NPC_SHEET = "NPC"
PERMISSIONS_SHEET = "Permissions for slash commands"
SHOP_LOGS_SHEET = "Shop (logs)"
CURRENCY_SHEET = "Currency"  # <-- for custom emoji render like bank.py

BANK_HEADERS_REQUIRED = ["User ID", "Gold", "Silver", "Copper", "Last_Collected"]
SHOP_LOG_HEADERS_REQUIRED = [
    "User ID", "Timestamp (UTC)", "Event ID", "Action",
    "Item ID", "Item Name", "Qty",
    "Cost Gold", "Cost Silver", "Cost Copper",
    "Balance After", "Source", "Meta JSON",
]

PLACEHOLDER_SHOPKEEPER_IMAGE = "https://i.imgur.com/7VqEOmH.png"

# ----------------------------
# Config
# ----------------------------
RETRY_COUNT = 3
RETRY_BASE_DELAY = 2.0
CACHE_TTL_SECONDS = 60

READ_THROTTLE_MAX = 20
READ_THROTTLE_WINDOW = 60.0
WRITE_THROTTLE_MAX = 40
WRITE_THROTTLE_WINDOW = 60.0

SHOP_PAGE_SIZE = 6  # items per page
MAX_STOCK_CLAMP = 10**9

T = TypeVar("T")

# ----------------------------
# Narration
# ----------------------------
class ShopPage(Enum):
    STORE = "store"
    ITEM = "item"
    CLOSED = "closed"


class ImageMode(Enum):
    THUMBNAIL = "thumbnail"
    FULL = "full"


NARRATIVES = {
    ShopPage.STORE: (
        "A bell chimes as you step into the shop.\n"
        "The Shopkeeper looks up from a cluttered counter and smiles.\n\n"
        "\"Browse at your leisure. I only carry what I trust.\""
    ),
    ShopPage.ITEM: (
        "The Shopkeeper lifts the item into the light.\n"
        "They turn it over slowly, watching your eyes.\n\n"
        "\"Quality speaks for itself.\""
    ),
    "purchase_ok": (
        "The Shopkeeper counts the coins with a practiced rhythm.\n"
        "A receipt is stamped, and the goods are wrapped tight.\n\n"
        "\"A fine choice. Use it well.\""
    ),
    "purchase_no_funds": (
        "The Shopkeeper pauses, then slides the item back.\n"
        "Their expression stays polite — but firm.\n\n"
        "\"Not enough coin. Come back when your purse is heavier.\""
    ),
    "purchase_out_of_stock": (
        "The Shopkeeper checks a shelf, then an empty crate.\n"
        "They give a small apologetic shrug.\n\n"
        "\"Sold out. If I find more, you'll be first to know.\""
    ),
    "purchase_role_locked": (
        "The Shopkeeper narrows their eyes, weighing you silently.\n"
        "They set the item down and shake their head.\n\n"
        "\"That is not for your station.\""
    ),
    ShopPage.CLOSED: (
        "The Shopkeeper draws the curtain and locks the register.\n"
        "The shop grows quiet as you step away.\n\n"
        "\"Safe travels.\""
    ),
    "timeout": (
        "The Shopkeeper taps the counter, noticing your absence.\n"
        "With a sigh, they tidy the display.\n\n"
        "\"Another time, then.\""
    ),
    "auto_expired": (
        "The Shopkeeper extinguishes the lantern by the counter.\n"
        "The storefront fades from view.\n\n"
        "\"Session expired. Use /shop to return.\""
    ),
}

# ----------------------------
# Helpers
# ----------------------------
def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_lower(s: Any) -> str:
    return str(s).strip().lower() if s is not None else ""


def _to_int(v: Any) -> int:
    try:
        if v is None:
            return 0
        s = str(v).strip()
        if s == "":
            return 0
        return int(float(s))
    except Exception:
        return 0


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _col_to_a1(col_idx_0: int) -> str:
    n = col_idx_0 + 1
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _drive_to_direct_image(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if "drive.google.com/uc" in u and "id=" in u:
        return u
    m = re.search(r"/file/d/([^/]+)/", u)
    if not m:
        m2 = re.search(r"[?&]id=([^&]+)", u)
        if m2:
            file_id = m2.group(1).strip()
            return f"https://drive.google.com/uc?export=view&id={file_id}"
        return u
    file_id = m.group(1).strip()
    return f"https://drive.google.com/uc?export=view&id={file_id}"


def _split_csv_roles(value: str) -> List[str]:
    if not value:
        return []
    parts = [p.strip() for p in str(value).split(",")]
    return [p for p in parts if p]


def _member_has_role_name(member: discord.Member, role_name: str) -> bool:
    rn = (role_name or "").strip().lower()
    if not rn:
        return True
    return any((r.name or "").strip().lower() == rn for r in member.roles)


def _member_has_any_role(member: discord.Member, roles_csv: str) -> bool:
    roles = [r.strip().lower() for r in _split_csv_roles(roles_csv)]
    if not roles:
        return True
    return any((rr.name or "").strip().lower() in roles for rr in member.roles)


def _normalize_uid_cell(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    if s.isdigit():
        return s
    s2 = s.replace(",", "").replace(" ", "")
    try:
        if "e" in s2.lower() or "." in s2:
            as_int = int(float(s2))
            return str(as_int)
    except Exception:
        pass
    digits = re.sub(r"\D+", "", s2)
    return digits


def _as_custom_emoji(name: str, emoji_id: str, fallback: str = "") -> str:
    """
    Same idea as bank.py: name=':Gold:' and id='123...' -> '<:Gold:123...>'
    """
    name = (name or "").strip().strip(":").strip("<").strip(">").strip()
    emoji_id = str(emoji_id or "").strip()
    if not name or not emoji_id or not emoji_id.isdigit():
        return fallback
    return f"<:{name}:{emoji_id}>"


def _find_role_by_name(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    """Case-insensitive exact match for a role by name."""
    target = (name or "").strip().lower()
    if not target:
        return None
    for role in guild.roles:
        if (role.name or "").strip().lower() == target:
            return role
    return None


async def _grant_role(member: discord.Member, role: discord.Role) -> None:
    """Add a role to a member. Caller should wrap in try/except."""
    if role not in member.roles:
        await member.add_roles(role, reason="Shop purchase: role granted on buy")


def _is_rate_limit_error(e: Exception) -> bool:
    err_str = str(e).lower()
    return "429" in err_str or "quota" in err_str or "rate" in err_str


async def _retry_async_graceful(
    func: Callable[[], T],
    fallback: T,
    retries: int = RETRY_COUNT,
    base_delay: float = RETRY_BASE_DELAY,
    operation_name: str = "operation",
) -> Tuple[T, bool]:
    for attempt in range(retries):
        try:
            result = await asyncio.to_thread(func)
            return result, True
        except Exception as e:
            if attempt < retries - 1:
                delay = base_delay * ((4 if _is_rate_limit_error(e) else 2) ** attempt)
                print(
                    f"[ShopCog] {operation_name} failed (attempt {attempt + 1}/{retries}): "
                    f"{type(e).__name__} - {e}. Retrying in {delay:.2f}s..."
                )
                await asyncio.sleep(delay)
            else:
                print(
                    f"[ShopCog] {operation_name} failed after {retries} attempts, using fallback: "
                    f"{type(e).__name__} - {e}"
                )
                return fallback, False
    return fallback, False


async def _retry_async(
    func: Callable[[], T],
    retries: int = RETRY_COUNT,
    base_delay: float = RETRY_BASE_DELAY,
    operation_name: str = "operation",
) -> T:
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            return await asyncio.to_thread(func)
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                delay = base_delay * ((4 if _is_rate_limit_error(e) else 2) ** attempt)
                print(
                    f"[ShopCog] {operation_name} failed (attempt {attempt + 1}/{retries}): "
                    f"{type(e).__name__} - {e}. Retrying in {delay:.2f}s..."
                )
                await asyncio.sleep(delay)
            else:
                print(
                    f"[ShopCog] {operation_name} failed after {retries} attempts: "
                    f"{type(e).__name__} - {e}"
                )
    raise last_exc  # type: ignore


# ----------------------------
# Cache
# ----------------------------
class CacheEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, ttl: float):
        self.value = value
        self.expires_at = time.monotonic() + ttl

    def is_valid(self) -> bool:
        return time.monotonic() < self.expires_at


# ----------------------------
# Data model
# ----------------------------
@dataclass
class ShopItem:
    item_id: str
    name: str
    description: str
    price_gold: int
    price_silver: int
    price_copper: int
    stock: Optional[int]   # None = unlimited
    image_url: str
    category: str
    role_required: str
    role_to_assign: str    # role name to grant on purchase (from "Role to assign" column)
    row_index: int         # 1-based row in sheet (for stock updates)


def _fmt_price_with(get_emoji: Callable[[str, str], str], g: int, s: int, c: int) -> str:
    """
    Use custom emojis when available; fall back to unicode.
    """
    parts = []
    if g:
        parts.append(f"{get_emoji('gold', '🟡')} {g:,}")
    if s:
        parts.append(f"{get_emoji('silver', '⚪')} {s:,}")
    if c:
        parts.append(f"{get_emoji('copper', '🟠')} {c:,}")
    if not parts:
        return "Free"
    return "  ".join(parts)


def _fmt_price_unicode(g: int, s: int, c: int) -> str:
    """Unicode-only price string — used in code blocks and dropdown labels
    where custom Discord emojis cannot render."""
    parts = []
    if g:
        parts.append(f"🟡 {g:,}")
    if s:
        parts.append(f"⚪ {s:,}")
    if c:
        parts.append(f"🟠 {c:,}")
    if not parts:
        return "Free"
    return "  ".join(parts)


# ----------------------------
# Cog
# ----------------------------
class ShopCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # LAZY: sheet is opened on first use (in a thread), not at startup
        self.sheet = None

        self._cache: Dict[str, CacheEntry] = {}
        self._read_timestamps: deque = deque()
        self._write_timestamps: deque = deque()
        self._throttle_lock = asyncio.Lock()

        self._purchase_locks: Dict[int, asyncio.Lock] = {}

    # -------------
    # Throttles
    # -------------
    async def _throttle_read(self) -> bool:
        async with self._throttle_lock:
            now = time.monotonic()
            while self._read_timestamps and (now - self._read_timestamps[0]) > READ_THROTTLE_WINDOW:
                self._read_timestamps.popleft()

            if len(self._read_timestamps) >= READ_THROTTLE_MAX:
                oldest = self._read_timestamps[0]
                wait_time = READ_THROTTLE_WINDOW - (now - oldest) + 0.5
                if wait_time > 0:
                    print(f"[ShopCog] Read throttle exceeded, skipping read (would wait {wait_time:.2f}s)")
                    return False

            self._read_timestamps.append(time.monotonic())
            return True

    async def _throttle_write(self) -> bool:
        async with self._throttle_lock:
            now = time.monotonic()
            while self._write_timestamps and (now - self._write_timestamps[0]) > WRITE_THROTTLE_WINDOW:
                self._write_timestamps.popleft()

            if len(self._write_timestamps) >= WRITE_THROTTLE_MAX:
                oldest = self._write_timestamps[0]
                wait_time = WRITE_THROTTLE_WINDOW - (now - oldest) + 0.5
                if wait_time > 0:
                    print(f"[ShopCog] Write throttle: waiting {wait_time:.2f}s")
                    await asyncio.sleep(wait_time)
                    now = time.monotonic()
                    while self._write_timestamps and (now - self._write_timestamps[0]) > WRITE_THROTTLE_WINDOW:
                        self._write_timestamps.popleft()

            self._write_timestamps.append(time.monotonic())
            return True

    # -------------
    # Cache utils
    # -------------
    def _get_cached(self, key: str) -> Optional[Any]:
        entry = self._cache.get(key)
        if entry and entry.is_valid():
            return entry.value
        return None

    def _set_cached(self, key: str, value: Any, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._cache[key] = CacheEntry(value, ttl)

    def _invalidate_cache(self, key: str) -> None:
        self._cache.pop(key, None)

    # -------------
    # Sheets helpers
    # -------------
    def _ensure_sheet(self):
        """Lazy-open spreadsheet on first use (runs in a thread, never at startup)."""
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
        name_l = name.strip().lower()
        for i, h in enumerate(headers):
            if str(h).strip().lower() == name_l:
                return i
        return -1

    def _find_header_index_contains(self, headers: List[str], keyword: str) -> int:
        kw = (keyword or "").strip().lower()
        if not kw:
            return -1
        for i, h in enumerate(headers):
            hl = str(h or "").strip().lower()
            if kw in hl:
                return i
        return -1

    def _find_header_index_contains_all(self, headers: List[str], keywords: List[str]) -> int:
        kws = [k.strip().lower() for k in (keywords or []) if k and str(k).strip()]
        if not kws:
            return -1
        for i, h in enumerate(headers):
            hl = str(h or "").strip().lower()
            if all(k in hl for k in kws):
                return i
        return -1

    def _get_lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._purchase_locks:
            self._purchase_locks[user_id] = asyncio.Lock()
        return self._purchase_locks[user_id]

    # ----------------------------
    # Currency meta (CUSTOM EMOJIS) - from Currency sheet like bank.py
    # ----------------------------
    def _read_currency_meta_sync(self) -> Dict[str, Dict[str, Any]]:
        """
        Expects Currency sheet columns:
          A: Currency Type (Gold/Silver/Copper)
          B: Emoji (like :Gold:)
          C: Emoji ID (digits)

        Returns:
          { "gold": {"emoji_render":"<:Gold:...>", ...}, ... }
        """
        ws = self._ws(CURRENCY_SHEET)
        if not ws:
            return {}

        try:
            data = ws.get_all_values()
        except Exception:
            return {}

        if not data or len(data) < 2:
            return {}

        headers = data[0]
        idx_type = self._find_header_index_contains(headers, "currency")
        if idx_type == -1:
            idx_type = 0

        idx_emoji = self._find_header_index_contains(headers, "emoji")
        if idx_emoji == -1:
            idx_emoji = 1

        idx_id = self._find_header_index_contains_all(headers, ["emoji", "id"])
        if idx_id == -1:
            idx_id = 2

        meta: Dict[str, Dict[str, Any]] = {}

        for r in data[1:]:
            ctype = (r[idx_type] if idx_type < len(r) else "").strip()
            raw_emoji = (r[idx_emoji] if idx_emoji < len(r) else "").strip()
            raw_id = (r[idx_id] if idx_id < len(r) else "").strip()

            if not ctype:
                continue

            emoji_name = raw_emoji.strip().strip(":")
            render = _as_custom_emoji(emoji_name, raw_id, fallback="")

            meta[_safe_lower(ctype)] = {
                "type": ctype,
                "emoji_name": emoji_name,
                "emoji_id": raw_id,
                "emoji_render": render,
            }

        return meta

    async def fetch_currency_meta_cached(self) -> Dict[str, Dict[str, Any]]:
        cached = self._get_cached("currency_meta")
        if cached is not None:
            return cached

        if not await self._throttle_read():
            return {}

        result, ok = await _retry_async_graceful(
            self._read_currency_meta_sync,
            fallback={},
            operation_name="fetch_currency_meta",
        )
        if ok:
            self._set_cached("currency_meta", result, ttl=120)
        return result

    # ----------------------------
    # Permissions (timer + image mode)
    # ----------------------------
    def _read_permissions_row_sync(self, command_name: str) -> Tuple[int, ImageMode]:
        ws = self._ws(PERMISSIONS_SHEET)
        if not ws:
            return 0, ImageMode.THUMBNAIL

        data = ws.get_all_values()
        if not data or len(data) < 2:
            return 0, ImageMode.THUMBNAIL

        target = command_name.strip().lower()
        for row in data[1:]:
            cmd = (row[0] if len(row) > 0 else "").strip().lower()
            if cmd != target:
                continue

            raw_timer = (row[4] if len(row) > 4 else "").strip()
            raw_img = (row[7] if len(row) > 7 else "").strip()

            timer = _clamp(_to_int(raw_timer), 0, 60)

            img_l = raw_img.strip().lower()
            image_mode = ImageMode.FULL if img_l.startswith("full") else ImageMode.THUMBNAIL

            return timer, image_mode

        return 0, ImageMode.THUMBNAIL

    async def fetch_permissions_cached(self, command_name: str) -> Tuple[int, ImageMode]:
        cache_key = f"perms_{command_name.lower()}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        if not await self._throttle_read():
            return 0, ImageMode.THUMBNAIL

        result, ok = await _retry_async_graceful(
            lambda: self._read_permissions_row_sync(command_name),
            fallback=(0, ImageMode.THUMBNAIL),
            operation_name=f"fetch_permissions({command_name})",
        )
        if ok:
            self._set_cached(cache_key, result)
        return result

    # ----------------------------
    # NPC image
    # ----------------------------
    def _read_shopkeeper_image_sync(self) -> str:
        ws = self._ws(NPC_SHEET)
        if not ws:
            return PLACEHOLDER_SHOPKEEPER_IMAGE
        data = ws.get_all_values()
        if not data or len(data) < 2:
            return PLACEHOLDER_SHOPKEEPER_IMAGE

        headers = data[0]
        idx_name = self._find_header_index(headers, "NPC Name")
        idx_img = self._find_header_index(headers, "Image")
        if idx_name == -1:
            idx_name = 0
        if idx_img == -1:
            idx_img = 1

        target = "the shopkeeper"
        for row in data[1:]:
            name = (row[idx_name] if idx_name < len(row) else "").strip().lower()
            if name == target:
                raw_url = (row[idx_img] if idx_img < len(row) else "").strip()
                result = _drive_to_direct_image(raw_url)
                return result if result else PLACEHOLDER_SHOPKEEPER_IMAGE

        return PLACEHOLDER_SHOPKEEPER_IMAGE

    async def fetch_shopkeeper_image_cached(self) -> str:
        cached = self._get_cached("shopkeeper_image")
        if cached is not None:
            return cached
        if not await self._throttle_read():
            return PLACEHOLDER_SHOPKEEPER_IMAGE
        result, ok = await _retry_async_graceful(
            self._read_shopkeeper_image_sync,
            fallback=PLACEHOLDER_SHOPKEEPER_IMAGE,
            operation_name="fetch_shopkeeper_image",
        )
        if ok:
            self._set_cached("shopkeeper_image", result)
        return result

    # ----------------------------
    # Bank wallet read/write (minimal, self-contained)
    # ----------------------------
    def _get_bank_headers_sync(self) -> Optional[List[str]]:
        cached = self._get_cached("bank_headers")
        if cached is not None:
            return cached
        ws = self._ws(BANK_SHEET)
        if not ws:
            return None
        row1 = ws.row_values(1)
        if row1:
            self._set_cached("bank_headers", row1)
        return row1

    def _get_user_row_map_bank_sync(self) -> Dict[str, int]:
        cached = self._get_cached("bank_user_row_map")
        if cached is not None:
            return cached
        ws = self._ws(BANK_SHEET)
        if not ws:
            return {}
        headers = self._get_bank_headers_sync()
        if not headers:
            return {}
        uid_idx = self._find_header_index(headers, "User ID")
        if uid_idx == -1:
            return {}
        col_values = ws.col_values(uid_idx + 1)
        user_map: Dict[str, int] = {}
        for i, val in enumerate(col_values[1:], start=2):
            uid = str(val).strip()
            if uid:
                user_map[uid] = i
        self._set_cached("bank_user_row_map", user_map)
        return user_map

    def _ensure_bank_row_sync(self, user_id: str) -> Tuple[int, List[str]]:
        ws = self._ws(BANK_SHEET)
        if not ws:
            raise RuntimeError("Bank sheet not available")

        headers = self._get_bank_headers_sync()
        if not headers:
            ws.append_row(BANK_HEADERS_REQUIRED, value_input_option="RAW")
            self._invalidate_cache("bank_headers")
            headers = self._get_bank_headers_sync()

        if not headers:
            raise RuntimeError("Bank sheet missing headers")

        uid_idx = self._find_header_index(headers, "User ID")
        if uid_idx == -1:
            raise RuntimeError("Bank sheet missing 'User ID' header")

        user_map = self._get_user_row_map_bank_sync()
        row_idx = user_map.get(user_id.strip())
        if row_idx:
            return row_idx, headers

        new_row = [""] * len(headers)
        new_row[uid_idx] = str(user_id).strip()
        for col_name in ["Gold", "Silver", "Copper"]:
            ci = self._find_header_index(headers, col_name)
            if ci != -1 and ci < len(new_row):
                new_row[ci] = "0"

        ws.append_row(new_row, value_input_option="RAW")
        self._invalidate_cache("bank_user_row_map")
        user_map = self._get_user_row_map_bank_sync()
        row_idx = user_map.get(user_id.strip())
        if not row_idx:
            raise RuntimeError("Failed to create bank row")
        return row_idx, headers

    def _read_wallet_only_sync(self, user_id: str) -> Dict[str, int]:
        headers = self._get_bank_headers_sync()
        if not headers:
            return {"gold": 0, "silver": 0, "copper": 0}

        user_map = self._get_user_row_map_bank_sync()
        row_idx = user_map.get(user_id.strip())
        if not row_idx:
            return {"gold": 0, "silver": 0, "copper": 0}

        ws = self._ws(BANK_SHEET)
        if not ws:
            return {"gold": 0, "silver": 0, "copper": 0}

        row = ws.row_values(row_idx)
        if not row:
            return {"gold": 0, "silver": 0, "copper": 0}

        idx_gold = self._find_header_index(headers, "Gold")
        idx_silver = self._find_header_index(headers, "Silver")
        idx_copper = self._find_header_index(headers, "Copper")
        return {
            "gold": _to_int(row[idx_gold] if idx_gold != -1 and idx_gold < len(row) else 0),
            "silver": _to_int(row[idx_silver] if idx_silver != -1 and idx_silver < len(row) else 0),
            "copper": _to_int(row[idx_copper] if idx_copper != -1 and idx_copper < len(row) else 0),
        }

    async def fetch_wallet_only(self, user_id: int) -> Tuple[Dict[str, int], bool]:
        cache_key = f"wallet_{user_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached, True

        if not await self._throttle_read():
            return {"gold": 0, "silver": 0, "copper": 0}, False

        result, ok = await _retry_async_graceful(
            lambda: self._read_wallet_only_sync(str(user_id)),
            fallback={"gold": 0, "silver": 0, "copper": 0},
            operation_name="fetch_wallet_only",
        )
        if ok:
            self._set_cached(cache_key, result, ttl=30)
        return result, ok

    # ----------------------------
    # Item list reader
    # ----------------------------
    def _read_items_sync(self) -> List[ShopItem]:
        ws = self._ws(ITEM_LIST_SHEET)
        if not ws:
            return []
        data = ws.get_all_values()
        if not data or len(data) < 2:
            return []

        headers = data[0]
        idx_id = self._find_header_index_contains_all(headers, ["item", "id"])
        idx_name = self._find_header_index(headers, "Name")
        if idx_name == -1:
            idx_name = self._find_header_index_contains(headers, "name")

        idx_desc = self._find_header_index(headers, "Description")
        if idx_desc == -1:
            idx_desc = self._find_header_index_contains(headers, "desc")

        idx_pg = self._find_header_index_contains_all(headers, ["price", "gold"])
        idx_ps = self._find_header_index_contains_all(headers, ["price", "silver"])
        idx_pc = self._find_header_index_contains_all(headers, ["price", "copper"])

        idx_stock = self._find_header_index(headers, "Stock")
        if idx_stock == -1:
            idx_stock = self._find_header_index_contains(headers, "stock")

        idx_img = self._find_header_index(headers, "Image")
        if idx_img == -1:
            idx_img = self._find_header_index_contains(headers, "image")

        idx_cat = self._find_header_index(headers, "Category")
        if idx_cat == -1:
            idx_cat = self._find_header_index_contains(headers, "category")

        idx_role = self._find_header_index_contains_all(headers, ["role", "required"])
        if idx_role == -1:
            idx_role = self._find_header_index_contains(headers, "role")

        idx_role_assign = self._find_header_index(headers, "Role to assign")
        if idx_role_assign == -1:
            idx_role_assign = self._find_header_index_contains_all(headers, ["role", "assign"])

        idx_enabled = self._find_header_index(headers, "Enabled")
        if idx_enabled == -1:
            idx_enabled = self._find_header_index_contains(headers, "enabled")

        items: List[ShopItem] = []
        for r_i, row in enumerate(data[1:], start=2):
            # Skip items explicitly disabled via "Enabled" column
            if idx_enabled != -1 and idx_enabled < len(row):
                if str(row[idx_enabled]).strip().lower().startswith("n"):
                    continue

            item_id = (row[idx_id] if idx_id != -1 and idx_id < len(row) else "").strip()
            name = (row[idx_name] if idx_name != -1 and idx_name < len(row) else "").strip()

            if not item_id and name:
                item_id = name
            if not item_id or not name:
                continue

            desc = (row[idx_desc] if idx_desc != -1 and idx_desc < len(row) else "").strip()
            pg = _to_int(row[idx_pg] if idx_pg != -1 and idx_pg < len(row) else 0)
            ps = _to_int(row[idx_ps] if idx_ps != -1 and idx_ps < len(row) else 0)
            pc = _to_int(row[idx_pc] if idx_pc != -1 and idx_pc < len(row) else 0)

            raw_stock = (row[idx_stock] if idx_stock != -1 and idx_stock < len(row) else "").strip()
            stock: Optional[int]
            if raw_stock == "":
                stock = None
            else:
                stock = _clamp(_to_int(raw_stock), 0, MAX_STOCK_CLAMP)

            raw_img = (row[idx_img] if idx_img != -1 and idx_img < len(row) else "").strip()
            image_url = _drive_to_direct_image(raw_img) if raw_img else ""

            category = (row[idx_cat] if idx_cat != -1 and idx_cat < len(row) else "").strip()
            role_required = (row[idx_role] if idx_role != -1 and idx_role < len(row) else "").strip()
            role_to_assign = (row[idx_role_assign] if idx_role_assign != -1 and idx_role_assign < len(row) else "").strip()

            items.append(
                ShopItem(
                    item_id=item_id,
                    name=name,
                    description=desc,
                    price_gold=max(0, pg),
                    price_silver=max(0, ps),
                    price_copper=max(0, pc),
                    stock=stock,
                    image_url=image_url,
                    category=category,
                    role_required=role_required,
                    role_to_assign=role_to_assign,
                    row_index=r_i,
                )
            )

        items.sort(key=lambda x: (_safe_lower(x.category), _safe_lower(x.name)))
        return items

    async def fetch_items_cached(self) -> List[ShopItem]:
        cached = self._get_cached("items")
        if cached is not None:
            return cached

        if not await self._throttle_read():
            return []

        result, ok = await _retry_async_graceful(
            self._read_items_sync,
            fallback=[],
            operation_name="fetch_items",
        )
        if ok:
            self._set_cached("items", result, ttl=30)
        return result

    # ----------------------------
    # Shop logs ensure
    # ----------------------------
    def _ensure_shop_logs_headers_sync(self) -> None:
        ws = self._ws(SHOP_LOGS_SHEET)
        if not ws:
            return
        row1 = ws.row_values(1)
        if not row1 or len([c for c in row1 if str(c).strip()]) < 3:
            ws.update("A1", [SHOP_LOG_HEADERS_REQUIRED])

    # ----------------------------
    # Purchase write
    # ----------------------------
    def _purchase_item_sync(
        self,
        user_id: str,
        item: ShopItem,
        qty: int,
        source: str,
    ) -> Tuple[bool, str, Dict[str, int]]:
        """
        Returns: (ok, reason_key, new_wallet)
        """
        ws_bank = self._ws(BANK_SHEET)
        ws_items = self._ws(ITEM_LIST_SHEET)
        ws_log = self._ws(SHOP_LOGS_SHEET)

        if not ws_bank or not ws_items:
            return False, "error", {"gold": 0, "silver": 0, "copper": 0}

        self._ensure_shop_logs_headers_sync()

        # Fresh wallet read
        row_i, bank_headers = self._ensure_bank_row_sync(user_id)
        bank_row = ws_bank.row_values(row_i)

        idx_gold = self._find_header_index(bank_headers, "Gold")
        idx_silver = self._find_header_index(bank_headers, "Silver")
        idx_copper = self._find_header_index(bank_headers, "Copper")
        idx_last = self._find_header_index(bank_headers, "Last_Collected")

        cur_gold = _to_int(bank_row[idx_gold] if idx_gold != -1 and idx_gold < len(bank_row) else 0)
        cur_silver = _to_int(bank_row[idx_silver] if idx_silver != -1 and idx_silver < len(bank_row) else 0)
        cur_copper = _to_int(bank_row[idx_copper] if idx_copper != -1 and idx_copper < len(bank_row) else 0)

        cost_g = item.price_gold * qty
        cost_s = item.price_silver * qty
        cost_c = item.price_copper * qty

        # Stock check
        if item.stock is not None:
            if item.stock <= 0 or item.stock < qty:
                return False, "purchase_out_of_stock", {"gold": cur_gold, "silver": cur_silver, "copper": cur_copper}

        # Funds check (NO normalization)
        if cur_gold < cost_g or cur_silver < cost_s or cur_copper < cost_c:
            return False, "purchase_no_funds", {"gold": cur_gold, "silver": cur_silver, "copper": cur_copper}

        # Deduct
        new_gold = cur_gold - cost_g
        new_silver = cur_silver - cost_s
        new_copper = cur_copper - cost_c

        # Update bank
        updates = []
        if idx_gold != -1:
            updates.append({"range": f"{_col_to_a1(idx_gold)}{row_i}", "values": [[str(_clamp(new_gold, 0, 10**12))]]})
        if idx_silver != -1:
            updates.append({"range": f"{_col_to_a1(idx_silver)}{row_i}", "values": [[str(_clamp(new_silver, 0, 10**12))]]})
        if idx_copper != -1:
            updates.append({"range": f"{_col_to_a1(idx_copper)}{row_i}", "values": [[str(_clamp(new_copper, 0, 10**12))]]})
        if idx_last != -1:
            updates.append({"range": f"{_col_to_a1(idx_last)}{row_i}", "values": [[_now_utc_iso()]]})
        if updates:
            ws_bank.batch_update(updates, value_input_option="RAW")

        # Decrement stock if present
        if item.stock is not None:
            item_headers = ws_items.row_values(1)
            idx_stock = self._find_header_index(item_headers, "Stock")
            if idx_stock == -1:
                idx_stock = self._find_header_index_contains(item_headers, "stock")
            if idx_stock != -1:
                new_stock = _clamp((item.stock or 0) - qty, 0, MAX_STOCK_CLAMP)
                ws_items.update(
                    f"{_col_to_a1(idx_stock)}{item.row_index}",
                    [[str(new_stock)]],
                    value_input_option="RAW",
                )

        # Log
        if ws_log:
            event_id = f"SHOP-{user_id}-{int(datetime.now(timezone.utc).timestamp())}"
            balance_after = {"gold": new_gold, "silver": new_silver, "copper": new_copper}
            meta = {
                "qty": qty,
                "item": {"item_id": item.item_id, "name": item.name, "category": item.category},
            }
            log_row = [
                str(user_id),
                _now_utc_iso(),
                event_id,
                "PURCHASE",
                item.item_id,
                item.name,
                str(qty),
                str(cost_g),
                str(cost_s),
                str(cost_c),
                json.dumps(balance_after, ensure_ascii=False),
                source,
                json.dumps(meta, ensure_ascii=False),
            ]
            ws_log.append_row(log_row, value_input_option="RAW")

        # Invalidate caches
        self._invalidate_cache(f"wallet_{user_id}")
        self._invalidate_cache("bank_user_row_map")
        self._invalidate_cache("items")

        return True, "purchase_ok", {"gold": new_gold, "silver": new_silver, "copper": new_copper}

    async def purchase_item(
        self,
        user_id: int,
        item: ShopItem,
        qty: int,
        source: str = "shop_ui",
    ) -> Tuple[bool, str, Dict[str, int]]:
        await self._throttle_write()
        return await _retry_async(
            lambda: self._purchase_item_sync(str(user_id), item, qty, source),
            operation_name="purchase_item",
        )

    # ----------------------------
    # Slash command
    # ----------------------------
    @app_commands.command(name="shop", description="Open the Shopkeeper interface")
    async def shop_command(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            await interaction.response.send_message("Member context not available.", ephemeral=True)
            return

        timer_minutes, image_mode = await self.fetch_permissions_cached("/shop")

        view = ShopView(
            cog=self,
            owner_id=member.id,
            member=member,
            timeout=180.0,
            image_mode=image_mode,
        )

        try:
            embed = await view.build_embed_for_page(ShopPage.STORE)
        except Exception as e:
            print(f"[ShopCog] shop_command embed build failed: {e}")
            embed = view.build_error_embed(f"Failed to load shop: {type(e).__name__}")

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

        if timer_minutes > 0 and view.message:
            view.start_auto_delete_timer(timer_minutes)


# ----------------------------
# View
# ----------------------------
class ShopView(discord.ui.View):
    def __init__(
        self,
        cog: ShopCog,
        owner_id: int,
        member: discord.Member,
        timeout: float = 180.0,
        image_mode: ImageMode = ImageMode.THUMBNAIL,
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = owner_id
        self.member = member
        self.current_page = ShopPage.STORE
        self.message: Optional[discord.Message] = None
        self._is_closed = False
        self._auto_delete_task: Optional[asyncio.Task] = None
        self._image_mode = image_mode

        self._shopkeeper_image: Optional[str] = None
        self._currency_meta: Optional[Dict[str, Dict[str, Any]]] = None

        # Store state
        self._items: List[ShopItem] = []
        self._store_page_index: int = 0
        self._selected_item_index: int = 0
        self._purchase_in_progress: bool = False

        # Cached wallet
        self._wallet: Dict[str, int] = {"gold": 0, "silver": 0, "copper": 0}

        # Dropdown (rebuilt after load)
        self._select: Optional[discord.ui.Select] = None

    # ----------------------------
    # Lifetime
    # ----------------------------
    def start_auto_delete_timer(self, minutes: int) -> None:
        if self._auto_delete_task is not None:
            return
        self._auto_delete_task = asyncio.create_task(self._auto_delete_after(minutes))

    async def _auto_delete_after(self, minutes: int) -> None:
        try:
            await asyncio.sleep(minutes * 60)
            if self._is_closed:
                return
            self._is_closed = True
            self._disable_all_components()
            if self.message:
                try:
                    await self.message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    try:
                        embed = self._build_auto_expired_embed()
                        await self.message.edit(embed=embed, view=self)
                    except (discord.NotFound, discord.HTTPException):
                        pass
            self.stop()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[ShopView] _auto_delete_after error: {e}")

    def _cancel_auto_delete_timer(self) -> None:
        if self._auto_delete_task is not None and not self._auto_delete_task.done():
            self._auto_delete_task.cancel()
            self._auto_delete_task = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self._is_closed:
            return
        self._is_closed = True
        self._cancel_auto_delete_timer()
        self._disable_all_components()
        if self.message:
            try:
                embed = self._build_timeout_embed()
                await self.message.edit(embed=embed, view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    def _disable_all_components(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True

    def _enable_all_components(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = False

    # ----------------------------
    # Static data
    # ----------------------------
    async def _ensure_static_data(self) -> None:
        if self._shopkeeper_image is None:
            self._shopkeeper_image = await self.cog.fetch_shopkeeper_image_cached()
        if self._currency_meta is None:
            self._currency_meta = await self.cog.fetch_currency_meta_cached()

    def _apply_image_mode(self, embed: discord.Embed) -> None:
        img = self._shopkeeper_image or PLACEHOLDER_SHOPKEEPER_IMAGE
        embed.set_author(name="The Shop", icon_url=img)

        if self._image_mode == ImageMode.FULL:
            embed.set_image(url=img)
            embed.set_thumbnail(url=None)
        else:
            embed.set_thumbnail(url=img)
            embed.set_image(url=None)

    def _get_emoji(self, currency_type: str, fallback: str) -> str:
        if self._currency_meta:
            rendered = self._currency_meta.get(currency_type, {}).get("emoji_render", "") or ""
            return rendered if rendered else fallback
        return fallback

    # ----------------------------
    # Data loaders
    # ----------------------------
    def _cycle_selected(self, delta: int) -> None:
        """Cycle _selected_item_index by delta with wraparound (matches inventory.py)."""
        if not self._items:
            self._selected_item_index = 0
            return
        n = len(self._items)
        self._selected_item_index = (self._selected_item_index + delta) % n

    async def _load_store_data(self) -> None:
        self._items = await self.cog.fetch_items_cached()
        wallet, ok = await self.cog.fetch_wallet_only(self.member.id)
        if ok:
            self._wallet = wallet

        if self._selected_item_index >= len(self._items):
            self._selected_item_index = 0
        if self._store_page_index < 0:
            self._store_page_index = 0

        # Sync page index to the page containing the selected item
        total_pages = max(1, (len(self._items) + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)
        if self._store_page_index >= total_pages:
            self._store_page_index = total_pages - 1
        if self._items:
            self._store_page_index = _clamp(
                self._selected_item_index // SHOP_PAGE_SIZE, 0, max(0, total_pages - 1)
            )

        self._rebuild_select()

    def _rebuild_select(self) -> None:
        if self._select is not None:
            try:
                self.remove_item(self._select)
            except Exception:
                pass
            self._select = None

        if not self._items:
            return

        start = self._store_page_index * SHOP_PAGE_SIZE
        end = min(start + SHOP_PAGE_SIZE, len(self._items))
        page_items = self._items[start:end]

        options: List[discord.SelectOption] = []
        for i, it in enumerate(page_items):
            absolute_idx = start + i
            stock_txt = ""
            if it.stock is not None:
                stock_txt = f" • Stock: {it.stock}"
            price_txt = _fmt_price_unicode(it.price_gold, it.price_silver, it.price_copper)
            label = (it.name[:95] + "…") if len(it.name) > 96 else it.name
            desc = f"{price_txt}{stock_txt}"
            if len(desc) > 100:
                desc = desc[:99] + "…"
            opt = discord.SelectOption(
                label=label,
                description=desc,
                value=str(absolute_idx),
            )
            # Mark the currently selected item as default in the dropdown
            if absolute_idx == self._selected_item_index:
                opt.default = True
            options.append(opt)

        self._select = ShopItemSelect(options=options, view=self)
        self.add_item(self._select)

    # ----------------------------
    # Embed builders
    # ----------------------------
    async def build_embed_for_page(
        self,
        page: ShopPage,
        narrative_override: Optional[str] = None,
        notice: str = "",
    ) -> discord.Embed:
        await self._ensure_static_data()

        embed = discord.Embed(color=0xD4AF37)
        self._apply_image_mode(embed)

        narrative = NARRATIVES.get(narrative_override, NARRATIVES.get(page, "")) if narrative_override else NARRATIVES.get(page, "")
        desc = f"```\n{narrative}\n```"
        if notice:
            desc += f"\n{notice}"
        embed.description = desc

        try:
            if page in (ShopPage.STORE, ShopPage.ITEM):
                await self._load_store_data()

            if page == ShopPage.STORE:
                self._add_store_content(embed)
            elif page == ShopPage.ITEM:
                self._add_item_content(embed)
            elif page == ShopPage.CLOSED:
                embed.add_field(name="Session Ended", value="*The shop shutters close.*", inline=False)

        except Exception as e:
            print(f"[ShopView] build_embed_for_page content failed: {type(e).__name__}: {e}")
            embed.add_field(
                name="⚠️ Error",
                value=f"```\nThe Shopkeeper frowns at a torn label.\n({type(e).__name__})\n```",
                inline=False,
            )

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        embed.set_footer(text=f"Customer: {self.member.display_name} • {ts}")
        return embed

    def _wallet_block(self) -> str:
        w = self._wallet or {"gold": 0, "silver": 0, "copper": 0}
        g = self._get_emoji("gold", "🟡")
        s = self._get_emoji("silver", "⚪")
        c = self._get_emoji("copper", "🟠")
        return (
            f"{g} **Gold:** {w.get('gold', 0):,}\n"
            f"{s} **Silver:** {w.get('silver', 0):,}\n"
            f"{c} **Copper:** {w.get('copper', 0):,}"
        )

    def _add_store_content(self, embed: discord.Embed) -> None:
        embed.add_field(name="Your Wallet", value=self._wallet_block(), inline=True)

        if not self._items:
            embed.add_field(name="For Sale", value="*The shelves are bare for now.*", inline=False)
            return

        start = self._store_page_index * SHOP_PAGE_SIZE
        end = min(start + SHOP_PAGE_SIZE, len(self._items))
        page_items = self._items[start:end]

        lines = ["```", "STOCK ON DISPLAY", "───────────────", ""]
        for it in page_items:
            stock = "∞" if it.stock is None else str(it.stock)
            price = _fmt_price_unicode(it.price_gold, it.price_silver, it.price_copper)
            cat = f"[{it.category}] " if it.category else ""
            safe_name = (it.name[:36] + "…") if len(it.name) > 37 else it.name
            lines.append(f"{cat}{safe_name}  |  {price}  |  Stock: {stock}")
        lines.append("```")

        embed.add_field(name="For Sale", value="\n".join(lines), inline=False)

        total_pages = max(1, (len(self._items) + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)
        embed.add_field(
            name="Page",
            value=f"Showing **{start + 1}–{end}** of **{len(self._items)}** items • Page **{self._store_page_index + 1}/{total_pages}**",
            inline=False,
        )

        embed.add_field(
            name="Tip",
            value="Use the dropdown to inspect an item. Press **Buy** to purchase the selected item (qty 1).",
            inline=False,
        )

    def _add_item_content(self, embed: discord.Embed) -> None:
        embed.add_field(name="Your Wallet", value=self._wallet_block(), inline=True)

        if not self._items:
            embed.add_field(name="Item", value="*Nothing to inspect.*", inline=False)
            return

        it = self._items[self._selected_item_index]
        price = _fmt_price_with(
            lambda ct, fb: self._get_emoji(ct, fb),
            it.price_gold,
            it.price_silver,
            it.price_copper,
        )
        stock = "Unlimited" if it.stock is None else f"{it.stock}"
        cat = it.category or "—"
        role_req = it.role_required or "—"

        embed.add_field(
            name="Item",
            value=(
                f"**{it.name}**\n"
                f"*ID:* `{it.item_id}`\n"
                f"*Category:* {cat}\n"
                f"*Role Required:* {role_req}\n"
                f"*Stock:* {stock}\n"
                f"*Price:* {price}"
            ),
            inline=False,
        )
        # --- Role grant note in description (only when item grants a role) ---
        role_grant_note = ""
        if it.role_to_assign:
            role_grant_note = (
                f"\n\n**Grants Role:** `{it.role_to_assign}`\n"
                f"*This item grants a role while it is in your possession.*"
            )

        desc_text = (it.description or "").strip()
        if desc_text or role_grant_note:
            combined = (desc_text + role_grant_note).strip()
            embed.add_field(name="Description", value=combined[:1024], inline=False)

        # Keep item image on item page
        if it.image_url:
            embed.set_image(url=it.image_url)

    def build_error_embed(self, error_msg: str) -> discord.Embed:
        embed = discord.Embed(color=0xFF0000)
        self._apply_image_mode(embed)
        embed.description = (
            "```\n"
            "The Shopkeeper sighs and rubs their temples.\n"
            "\"Something is wrong with the inventory ledger.\"\n"
            "```"
        )
        embed.add_field(name="⚠️ Error", value=f"```\n{error_msg}\n```", inline=False)
        return embed

    def _build_timeout_embed(self) -> discord.Embed:
        embed = discord.Embed(color=0x808080)
        self._apply_image_mode(embed)
        embed.description = f"```\n{NARRATIVES['timeout']}\n```"
        embed.add_field(name="Panel Expired", value="*Use `/shop` to start a new session.*", inline=False)
        return embed

    def _build_auto_expired_embed(self) -> discord.Embed:
        embed = discord.Embed(color=0x808080)
        self._apply_image_mode(embed)
        embed.description = f"```\n{NARRATIVES['auto_expired']}\n```"
        embed.add_field(name="Session Expired", value="*Use `/shop` to start a new session.*", inline=False)
        return embed

    # ----------------------------
    # Message update
    # ----------------------------
    def _update_button_styles(self) -> None:
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue

            if child.custom_id == "shop_close":
                child.style = discord.ButtonStyle.danger
                continue

            if child.custom_id == "shop_store":
                child.style = discord.ButtonStyle.primary if self.current_page == ShopPage.STORE else discord.ButtonStyle.secondary
                continue

            if child.custom_id == "shop_item":
                child.style = discord.ButtonStyle.primary if self.current_page == ShopPage.ITEM else discord.ButtonStyle.secondary
                continue

    async def _update_message(
        self,
        interaction: discord.Interaction,
        page: ShopPage,
        narrative_override: Optional[str] = None,
        notice: str = "",
    ) -> None:
        self.current_page = page
        self._update_button_styles()

        if page == ShopPage.STORE:
            await self._load_store_data()

        embed = await self.build_embed_for_page(page, narrative_override=narrative_override, notice=notice)

        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.InteractionResponded:
            try:
                await interaction.followup.edit_message(
                    message_id=interaction.message.id,
                    embed=embed,
                    view=self,
                )
            except discord.HTTPException:
                pass

    # ----------------------------
    # Actions
    # ----------------------------
    async def _handle_buy(self, interaction: discord.Interaction) -> None:
        if not self._items:
            await self._update_message(interaction, ShopPage.STORE, notice="*No items available.*")
            return

        it = self._items[self._selected_item_index]

        # Role gate
        if it.role_required and not _member_has_any_role(self.member, it.role_required):
            await self._update_message(interaction, ShopPage.ITEM, narrative_override="purchase_role_locked")
            return

        lock = self.cog._get_lock(self.owner_id)
        if lock.locked():
            await interaction.response.defer()
            return

        async with lock:
            self._purchase_in_progress = True
            self._disable_all_components()
            try:
                await interaction.response.edit_message(view=self)
            except discord.InteractionResponded:
                pass

            try:
                ok, reason_key, new_wallet = await self.cog.purchase_item(
                    user_id=self.member.id,
                    item=it,
                    qty=1,
                    source="shop_ui",
                )

                self._wallet = new_wallet
                self._items = await self.cog.fetch_items_cached()
                if self._selected_item_index >= len(self._items):
                    self._selected_item_index = 0

                self._purchase_in_progress = False
                self._enable_all_components()
                self._rebuild_select()

                if ok:
                    # --- Role grant on successful purchase ---
                    role_note = ""
                    if it.role_to_assign:
                        try:
                            guild = interaction.guild
                            if guild is not None:
                                role = _find_role_by_name(guild, it.role_to_assign)
                                if role is not None:
                                    await _grant_role(self.member, role)
                                else:
                                    role_note = f"\n*(Role not found: {it.role_to_assign})*"
                                    print(f"[ShopView] Role to assign not found in guild: '{it.role_to_assign}'")
                        except Exception as role_err:
                            role_note = f"\n*(Could not grant role: {it.role_to_assign})*"
                            print(f"[ShopView] Failed to grant role '{it.role_to_assign}': {type(role_err).__name__}: {role_err}")

                    # --- Add purchased item to inventory ---
                    try:
                        await inventory_store.add_item(
                            user_id=self.member.id,
                            item_id=it.item_id,
                            qty=1,
                            source="shop_purchase",
                            meta={"item_name": it.name, "category": it.category},
                        )
                    except Exception as inv_err:
                        print(f"[ShopView] inventory add_item failed: {type(inv_err).__name__}: {inv_err}")
                        role_note += "\n*(Inventory update failed)*"

                    await self._update_message(interaction, ShopPage.ITEM, narrative_override="purchase_ok", notice=role_note)
                else:
                    await self._update_message(interaction, ShopPage.ITEM, narrative_override=reason_key)

            except Exception as e:
                print(f"[ShopView] buy failed: {type(e).__name__}: {e}")
                self._purchase_in_progress = False
                self._enable_all_components()
                self._update_button_styles()
                try:
                    embed = self.build_error_embed("Failed to complete purchase. Please try again.")
                    await interaction.followup.edit_message(
                        message_id=interaction.message.id,
                        embed=embed,
                        view=self,
                    )
                except discord.HTTPException:
                    pass

    # ----------------------------
    # Buttons
    # ----------------------------
    @discord.ui.button(
        label="Storefront",
        style=discord.ButtonStyle.primary,
        custom_id="shop_store",
        emoji="🏪",
        row=0,
    )
    async def storefront_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._update_message(interaction, ShopPage.STORE)

    @discord.ui.button(
        label="Inspect",
        style=discord.ButtonStyle.secondary,
        custom_id="shop_item",
        emoji="🔎",
        row=0,
    )
    async def inspect_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._update_message(interaction, ShopPage.ITEM)

    @discord.ui.button(
        label="Prev",
        style=discord.ButtonStyle.secondary,
        custom_id="shop_prev",
        emoji="⬅️",
        row=1,
    )
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._items:
            await self._update_message(interaction, ShopPage.STORE)
            return

        # ITEM page: cycle to previous item (wraps around)
        if self.current_page == ShopPage.ITEM:
            self._cycle_selected(-1)
            await self._update_message(interaction, ShopPage.ITEM)
            return

        # STORE page: move to previous page, clamped
        self._store_page_index = max(0, self._store_page_index - 1)
        await self._update_message(interaction, ShopPage.STORE)

    @discord.ui.button(
        label="Next",
        style=discord.ButtonStyle.secondary,
        custom_id="shop_next",
        emoji="➡️",
        row=1,
    )
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._items:
            await self._update_message(interaction, ShopPage.STORE)
            return

        # ITEM page: cycle to next item (wraps around)
        if self.current_page == ShopPage.ITEM:
            self._cycle_selected(+1)
            await self._update_message(interaction, ShopPage.ITEM)
            return

        # STORE page: move to next page, clamped
        total_pages = max(1, (len(self._items) + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)
        self._store_page_index = min(total_pages - 1, self._store_page_index + 1)
        await self._update_message(interaction, ShopPage.STORE)

    @discord.ui.button(
        label="Buy",
        style=discord.ButtonStyle.success,
        custom_id="shop_buy",
        emoji="🛒",
        row=1,
    )
    async def buy_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.current_page != ShopPage.ITEM:
            await self._update_message(interaction, ShopPage.ITEM)
        await self._handle_buy(interaction)

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.danger,
        custom_id="shop_close",
        emoji="🚪",
        row=0,
    )
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._is_closed = True
        self._cancel_auto_delete_timer()
        self._disable_all_components()
        self.current_page = ShopPage.CLOSED

        embed = await self.build_embed_for_page(ShopPage.CLOSED)
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.InteractionResponded:
            try:
                await interaction.followup.edit_message(
                    message_id=interaction.message.id,
                    embed=embed,
                    view=self,
                )
            except discord.HTTPException:
                pass
        self.stop()


# ----------------------------
# Select (dropdown)
# ----------------------------
class ShopItemSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption], view: ShopView):
        super().__init__(
            placeholder="Select an item…",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )
        self._shop_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            chosen = int(self.values[0])
        except Exception:
            chosen = 0

        self._shop_view._selected_item_index = _clamp(chosen, 0, max(0, len(self._shop_view._items) - 1))
        # Sync page index to match the newly selected item
        self._shop_view._store_page_index = _clamp(
            self._shop_view._selected_item_index // SHOP_PAGE_SIZE,
            0,
            max(0, (len(self._shop_view._items) - 1) // SHOP_PAGE_SIZE),
        )
        await self._shop_view._update_message(interaction, ShopPage.ITEM)


# ----------------------------
# Setup
# ----------------------------
async def setup(bot: commands.Bot):
    await bot.add_cog(ShopCog(bot))
