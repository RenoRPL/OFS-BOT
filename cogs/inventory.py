# cogs/inventory.py
# -*- coding: utf-8 -*-
from __future__ import annotations

print("=== LOADED inventory.py v2.0 (TRADE TERMS OPTIONAL MONEY + BLANK=0) ===")

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from utils.google_auth import open_worksheet
from utils import inventory_store


# ----------------------------
# Sheets / tabs
# ----------------------------
PERMISSIONS_SHEET = "Permissions for slash commands"
NPC_SHEET = "NPC"
ITEM_LIST_SHEET = "Item List"

# ----------------------------
# Config
# ----------------------------
PAGE_SIZE = 6
TRADE_TIMEOUT_SECONDS = 300  # 5 minutes
PLACEHOLDER_IMAGE = "https://i.imgur.com/7VqEOmH.png"

# You asked inventory to use the row "Hideout"
NPC_NAME_FOR_INVENTORY = "Hideout"

CACHE_TTL_SECONDS = 120


class InvPage(Enum):
    LIST = "list"
    ITEM = "item"
    CLOSED = "closed"


class ImageMode(Enum):
    THUMBNAIL = "thumbnail"
    FULL = "full"


def _now_utc_short() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _fmt_price_unicode(g: int, s: int, c: int) -> str:
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


def _drive_to_direct_image(url_or_id: str) -> str:
    """
    Accepts:
      - Google Drive share url
      - direct 'uc?export=view&id=' url
      - a raw Drive file id stored in the sheet
      - any normal http(s) image url
    Returns a usable image URL for Discord embeds.
    """
    u = (url_or_id or "").strip()
    if not u:
        return ""

    # Raw file-id in sheet
    if not u.startswith("http"):
        if len(u) >= 12 and re.fullmatch(r"[A-Za-z0-9_-]+", u):
            return f"https://drive.google.com/uc?export=view&id={u}"
        return u

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


# ----------------------------
# Simple TTL cache + anti-stampede locks
# ----------------------------
class _CacheEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, ttl: float):
        self.value = value
        self.expires_at = time.monotonic() + ttl

    def valid(self) -> bool:
        return time.monotonic() < self.expires_at


# ----------------------------
# Cog
# ----------------------------
class InventoryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: Dict[str, _CacheEntry] = {}
        self._inflight: Dict[str, asyncio.Lock] = {}

    # ----------------------------
    # Cache helpers
    # ----------------------------
    def _get_cached(self, key: str) -> Optional[Any]:
        e = self._cache.get(key)
        if e and e.valid():
            return e.value
        return None

    def _set_cached(self, key: str, value: Any, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._cache[key] = _CacheEntry(value, ttl)

    def _lock_for(self, key: str) -> asyncio.Lock:
        if key not in self._inflight:
            self._inflight[key] = asyncio.Lock()
        return self._inflight[key]

    # ----------------------------
    # Header helpers
    # ----------------------------
    def _find_header_index(self, headers: List[str], name: str) -> int:
        target = (name or "").strip().lower()
        for i, h in enumerate(headers or []):
            if str(h).strip().lower() == target:
                return i
        return -1

    def _find_header_index_contains(self, headers: List[str], keyword: str) -> int:
        kw = (keyword or "").strip().lower()
        if not kw:
            return -1
        for i, h in enumerate(headers or []):
            hl = str(h or "").strip().lower()
            if kw in hl:
                return i
        return -1

    def _find_header_index_contains_all(self, headers: List[str], keywords: List[str]) -> int:
        kws = [k.strip().lower() for k in (keywords or []) if k and str(k).strip()]
        if not kws:
            return -1
        for i, h in enumerate(headers or []):
            hl = str(h or "").strip().lower()
            if all(k in hl for k in kws):
                return i
        return -1

    # ----------------------------
    # Read Permissions: Image Thumbnail or Full
    # ----------------------------
    def _read_permissions_image_mode_sync(self, command_name: str) -> ImageMode:
        ws = open_worksheet(PERMISSIONS_SHEET)
        if not ws:
            return ImageMode.THUMBNAIL

        try:
            data = ws.get_all_values()
        except Exception:
            return ImageMode.THUMBNAIL

        if not data or len(data) < 2:
            return ImageMode.THUMBNAIL

        headers = data[0]
        idx_cmd = self._find_header_index_contains(headers, "slash")
        if idx_cmd == -1:
            idx_cmd = 0

        idx_img = self._find_header_index_contains_all(headers, ["image", "thumbnail"])
        if idx_img == -1:
            idx_img = self._find_header_index_contains(headers, "image")
        if idx_img == -1:
            idx_img = 7

        target = (command_name or "").strip().lower()
        for row in data[1:]:
            cmd = (row[idx_cmd] if idx_cmd < len(row) else "").strip().lower()
            if cmd != target:
                continue
            raw = (row[idx_img] if idx_img < len(row) else "").strip().lower()
            return ImageMode.FULL if raw.startswith("full") else ImageMode.THUMBNAIL

        return ImageMode.THUMBNAIL

    async def fetch_image_mode(self, command_name: str) -> ImageMode:
        cache_key = f"imgmode:{command_name.lower()}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        lock = self._lock_for(cache_key)
        async with lock:
            cached2 = self._get_cached(cache_key)
            if cached2 is not None:
                return cached2

            mode = await asyncio.to_thread(self._read_permissions_image_mode_sync, command_name)
            self._set_cached(cache_key, mode, ttl=120)
            return mode

    # ----------------------------
    # Read NPC images (Image + Closed Image)
    # ----------------------------
    def _read_npc_images_sync(self, npc_name: str) -> Tuple[str, str]:
        ws = open_worksheet(NPC_SHEET)
        if not ws:
            return (PLACEHOLDER_IMAGE, PLACEHOLDER_IMAGE)

        try:
            data = ws.get_all_values()
        except Exception:
            return (PLACEHOLDER_IMAGE, PLACEHOLDER_IMAGE)

        if not data or len(data) < 2:
            return (PLACEHOLDER_IMAGE, PLACEHOLDER_IMAGE)

        headers = data[0]
        idx_name = self._find_header_index_contains(headers, "npc")
        if idx_name == -1:
            idx_name = 0

        idx_img = self._find_header_index_contains(headers, "image")
        if idx_img == -1:
            idx_img = 1

        idx_closed = self._find_header_index_contains_all(headers, ["closed", "image"])
        if idx_closed == -1:
            idx_closed = self._find_header_index_contains(headers, "closed")
        if idx_closed == -1:
            idx_closed = 2  # NPC tab: Closed Image is usually col C

        target = (npc_name or "").strip().lower()
        for row in data[1:]:
            name = (row[idx_name] if idx_name < len(row) else "").strip().lower()
            if name != target:
                continue

            raw_open = (row[idx_img] if idx_img < len(row) else "").strip()
            raw_closed = (row[idx_closed] if idx_closed < len(row) else "").strip()

            open_img = _drive_to_direct_image(raw_open) if raw_open else ""
            closed_img = _drive_to_direct_image(raw_closed) if raw_closed else ""

            if not open_img:
                open_img = PLACEHOLDER_IMAGE
            if not closed_img:
                closed_img = open_img

            return (open_img, closed_img)

        return (PLACEHOLDER_IMAGE, PLACEHOLDER_IMAGE)

    async def fetch_npc_images(self, npc_name: str) -> Tuple[str, str]:
        cache_key = f"npcimgs:{npc_name.lower()}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        lock = self._lock_for(cache_key)
        async with lock:
            cached2 = self._get_cached(cache_key)
            if cached2 is not None:
                return cached2

            imgs = await asyncio.to_thread(self._read_npc_images_sync, npc_name)
            self._set_cached(cache_key, imgs, ttl=300)
            return imgs

    # ----------------------------
    # Map Item ID -> Role to assign (from Item List)
    # ----------------------------
    def _read_item_roles_sync(self) -> Dict[str, str]:
        ws = open_worksheet(ITEM_LIST_SHEET)
        if not ws:
            return {}

        try:
            data = ws.get_all_values()
        except Exception:
            return {}

        if not data or len(data) < 2:
            return {}

        headers = data[0]
        idx_id = self._find_header_index_contains_all(headers, ["item", "id"])
        if idx_id == -1:
            idx_id = 0

        idx_name = self._find_header_index_contains(headers, "name")
        if idx_name == -1:
            idx_name = 1

        idx_role_assign = self._find_header_index(headers, "Role to assign")
        if idx_role_assign == -1:
            idx_role_assign = self._find_header_index_contains_all(headers, ["role", "assign"])

        roles: Dict[str, str] = {}
        for row in data[1:]:
            item_id = (row[idx_id] if idx_id < len(row) else "").strip()
            name = (row[idx_name] if idx_name < len(row) else "").strip()
            role_to_assign = (row[idx_role_assign] if idx_role_assign != -1 and idx_role_assign < len(row) else "").strip()

            if not item_id and name:
                item_id = name
            if not item_id:
                continue

            roles[item_id] = role_to_assign

        return roles

    async def fetch_item_roles_map(self) -> Dict[str, str]:
        cache_key = "item_roles_map"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        lock = self._lock_for(cache_key)
        async with lock:
            cached2 = self._get_cached(cache_key)
            if cached2 is not None:
                return cached2

            roles = await asyncio.to_thread(self._read_item_roles_sync)
            self._set_cached(cache_key, roles, ttl=120)
            return roles

    # ----------------------------
    # Map Item ID -> Item Image (from Item List)
    # ----------------------------
    def _read_item_images_sync(self) -> Dict[str, str]:
        ws = open_worksheet(ITEM_LIST_SHEET)
        if not ws:
            return {}

        try:
            data = ws.get_all_values()
        except Exception:
            return {}

        if not data or len(data) < 2:
            return {}

        headers = data[0]

        idx_id = self._find_header_index(headers, "Item_ID")
        if idx_id == -1:
            idx_id = self._find_header_index_contains_all(headers, ["item", "id"])
        if idx_id == -1:
            idx_id = 6  # Item_ID in col G (0-based 6)

        idx_img = self._find_header_index(headers, "Item Image")
        if idx_img == -1:
            idx_img = self._find_header_index_contains_all(headers, ["item", "image"])
        if idx_img == -1:
            idx_img = 1  # Item Image in col B (0-based 1)

        out: Dict[str, str] = {}
        for row in data[1:]:
            item_id = (row[idx_id] if idx_id < len(row) else "").strip()
            raw_img = (row[idx_img] if idx_img < len(row) else "").strip()
            if not item_id:
                continue
            if raw_img:
                out[item_id] = _drive_to_direct_image(raw_img)
        return out

    async def fetch_item_images_map(self) -> Dict[str, str]:
        cache_key = "item_images_map"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        lock = self._lock_for(cache_key)
        async with lock:
            cached2 = self._get_cached(cache_key)
            if cached2 is not None:
                return cached2

            images = await asyncio.to_thread(self._read_item_images_sync)
            self._set_cached(cache_key, images, ttl=120)
            return images

    # ----------------------------
    # Slash command
    # ----------------------------
    @app_commands.command(name="inventory", description="Open your inventory")
    async def inventory_command(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            await interaction.response.send_message("Member context not available.", ephemeral=True)
            return

        image_mode = await self.fetch_image_mode("/inventory")
        npc_open_img, npc_closed_img = await self.fetch_npc_images(NPC_NAME_FOR_INVENTORY)

        view = InventoryView(
            cog=self,
            owner_id=member.id,
            member=member,
            timeout=180.0,
            image_mode=image_mode,
            npc_image=npc_open_img,
            npc_closed_image=npc_closed_img,
        )

        embed = await view.build_embed(InvPage.LIST)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass


# ----------------------------
# Trade Flow Views
# ----------------------------
class TradeState(Enum):
    PICK_BUYER = "pick_buyer"
    ENTER_TERMS = "enter_terms"
    PENDING = "pending"
    DONE = "done"


@dataclass
class PendingTrade:
    trade_id: str
    seller_id: int
    buyer_id: int
    item_id: str
    item_name: str
    qty: int
    price_gold: int
    price_silver: int
    price_copper: int
    created_at: float


class TradeTermsModal(discord.ui.Modal, title="Trade Terms"):
    # Qty stays required
    qty = discord.ui.TextInput(label="Quantity", placeholder="1", required=True, max_length=8)

    # Money is OPTIONAL now. Blank => 0.
    gold = discord.ui.TextInput(
        label="Price Gold (buyer pays)",
        placeholder="0 (optional)",
        required=False,
        max_length=12,
    )
    silver = discord.ui.TextInput(
        label="Price Silver (buyer pays)",
        placeholder="0 (optional)",
        required=False,
        max_length=12,
    )
    copper = discord.ui.TextInput(
        label="Price Copper (buyer pays)",
        placeholder="0 (optional)",
        required=False,
        max_length=12,
    )

    def __init__(self, view: "InventoryView"):
        super().__init__()
        self.inv_view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        def to_int(x: Optional[str]) -> int:
            # Blank / None => 0
            s = (x or "").strip()
            if not s:
                return 0
            try:
                return max(0, int(float(s)))
            except Exception:
                return 0

        qty = max(1, to_int(self.qty.value))
        pg = to_int(self.gold.value)
        ps = to_int(self.silver.value)
        pc = to_int(self.copper.value)

        self.inv_view._trade_qty = qty
        self.inv_view._trade_price = (pg, ps, pc)

        await self.inv_view._begin_trade_request(interaction)


class TradeRequestView(discord.ui.View):
    def __init__(self, inv_view: "InventoryView", pending: PendingTrade):
        super().__init__(timeout=TRADE_TIMEOUT_SECONDS)
        self.inv_view = inv_view
        self.pending = pending
        self._resolved = False

    async def on_timeout(self) -> None:
        if self._resolved:
            return
        self._resolved = True
        for c in self.children:
            if hasattr(c, "disabled"):
                c.disabled = True
        try:
            await self.inv_view._notify_trade_result(self.pending, ok=False, msg="Trade request expired (timeout).")
        except Exception:
            pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._resolved:
            await interaction.response.defer()
            return

        if interaction.user.id != self.pending.buyer_id:
            await interaction.response.send_message("Only the invited buyer can accept this trade.", ephemeral=True)
            return

        self._resolved = True
        for c in self.children:
            if hasattr(c, "disabled"):
                c.disabled = True
        await interaction.response.edit_message(view=self)

        ok, msg = await inventory_store.commit_trade(
            trade_id=self.pending.trade_id,
            seller_id=self.pending.seller_id,
            buyer_id=self.pending.buyer_id,
            item_id=self.pending.item_id,
            qty=self.pending.qty,
            price_gold=self.pending.price_gold,
            price_silver=self.pending.price_silver,
            price_copper=self.pending.price_copper,
            meta={"source": "inventory_trade"},
        )

        try:
            guild = interaction.guild
            if guild:
                seller_m = guild.get_member(self.pending.seller_id)
                buyer_m = guild.get_member(self.pending.buyer_id)
                if seller_m:
                    await inventory_store.sync_roles_for_user(seller_m)
                if buyer_m:
                    await inventory_store.sync_roles_for_user(buyer_m)
        except Exception:
            pass

        await self.inv_view._notify_trade_result(self.pending, ok=ok, msg=msg)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._resolved:
            await interaction.response.defer()
            return

        if interaction.user.id != self.pending.buyer_id:
            await interaction.response.send_message("Only the invited buyer can decline this trade.", ephemeral=True)
            return

        self._resolved = True
        for c in self.children:
            if hasattr(c, "disabled"):
                c.disabled = True

        await interaction.response.edit_message(view=self)
        await self.inv_view._notify_trade_result(self.pending, ok=False, msg="Trade declined.")


# ----------------------------
# Inventory UI View
# ----------------------------
class InventoryView(discord.ui.View):
    def __init__(
        self,
        cog: InventoryCog,
        owner_id: int,
        member: discord.Member,
        timeout: float = 180.0,
        image_mode: ImageMode = ImageMode.THUMBNAIL,
        npc_image: str = PLACEHOLDER_IMAGE,
        npc_closed_image: str = PLACEHOLDER_IMAGE,
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = owner_id
        self.member = member
        self.message: Optional[discord.Message] = None

        self.page = InvPage.LIST
        self._items: List[inventory_store.InventoryItem] = []
        self._page_index = 0
        self._selected_index = 0
        self._select: Optional[discord.ui.Select] = None

        self._image_mode = image_mode
        self._npc_image = npc_image or PLACEHOLDER_IMAGE
        self._npc_closed_image = npc_closed_image or self._npc_image or PLACEHOLDER_IMAGE

        self._item_roles_map: Dict[str, str] = {}
        self._item_images_map: Dict[str, str] = {}

        self._trade_buyer_id: Optional[int] = None
        self._trade_qty: int = 1
        self._trade_price: Tuple[int, int, int] = (0, 0, 0)

        self._rebuild_select()
        # NOTE: do NOT call _update_button_styles() here; items are not loaded yet.

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message:
            try:
                embed = await self.build_embed(InvPage.CLOSED, notice="Session expired. Use `/inventory` again.")
                await self.message.edit(embed=embed, view=self)
            except Exception:
                pass

    async def _load(self) -> None:
        self._items = await inventory_store.get_inventory_items(self.member.id)

        try:
            self._item_roles_map = await self.cog.fetch_item_roles_map()
        except Exception:
            self._item_roles_map = {}

        try:
            self._item_images_map = await self.cog.fetch_item_images_map()
        except Exception:
            self._item_images_map = {}

        if self._selected_index >= len(self._items):
            self._selected_index = 0
        if self._page_index < 0:
            self._page_index = 0

        total_pages = max(1, (len(self._items) + PAGE_SIZE - 1) // PAGE_SIZE)
        if self._page_index >= total_pages:
            self._page_index = total_pages - 1

        if self._items:
            self._page_index = _clamp(self._selected_index // PAGE_SIZE, 0, max(0, total_pages - 1))

        self._rebuild_select()

        # ✅ now that items are loaded, update labels/disabled state immediately
        self._update_button_styles()

    def _granted_role_for_item(self, it: inventory_store.InventoryItem) -> str:
        rid = str(getattr(it, "item_id", "") or "").strip()
        role = (self._item_roles_map.get(rid, "") or "").strip()
        if not role:
            role = str(getattr(it, "role_to_assign", "") or "").strip()
        return role.strip()

    def _image_for_item(self, it: Optional[inventory_store.InventoryItem]) -> str:
        if not it:
            return ""
        item_id = str(getattr(it, "item_id", "") or "").strip()
        if item_id:
            img = (self._item_images_map.get(item_id, "") or "").strip()
            if img:
                return img
        name = str(getattr(it, "name", "") or "").strip()
        if name:
            img2 = (self._item_images_map.get(name, "") or "").strip()
            if img2:
                return img2
        return ""

    def _apply_image_mode(self, embed: discord.Embed, img: str) -> None:
        img = img or PLACEHOLDER_IMAGE
        embed.set_author(name="Inventory", icon_url=img)
        if self._image_mode == ImageMode.FULL:
            embed.set_image(url=img)
            embed.set_thumbnail(url=None)
        else:
            embed.set_thumbnail(url=img)
            embed.set_image(url=None)

    def _apply_image_mode_list(self, embed: discord.Embed) -> None:
        self._apply_image_mode(embed, self._npc_image)

    def _apply_image_mode_closed(self, embed: discord.Embed) -> None:
        self._apply_image_mode(embed, self._npc_closed_image)

    def _apply_image_mode_inspect(self, embed: discord.Embed, it: Optional[inventory_store.InventoryItem]) -> None:
        npc = self._npc_image or PLACEHOLDER_IMAGE
        embed.set_author(name="Inventory", icon_url=npc)

        item_img = self._image_for_item(it)
        if item_img:
            embed.set_image(url=item_img)
            embed.set_thumbnail(url=npc)
        else:
            if self._image_mode == ImageMode.FULL:
                embed.set_image(url=npc)
                embed.set_thumbnail(url=None)
            else:
                embed.set_thumbnail(url=npc)
                embed.set_image(url=None)

    def _rebuild_select(self) -> None:
        if self._select is not None:
            try:
                self.remove_item(self._select)
            except Exception:
                pass
            self._select = None

        if not self._items:
            return

        start = self._page_index * PAGE_SIZE
        end = min(start + PAGE_SIZE, len(self._items))
        page_items = self._items[start:end]

        options: List[discord.SelectOption] = []
        for i, it in enumerate(page_items):
            abs_i = start + i

            granted_role = self._granted_role_for_item(it)
            if granted_role:
                desc = f'Qty: {it.qty} • Granted Role: "{granted_role}"'
            else:
                desc = f"Qty: {it.qty} • Granted Role: —"

            if len(desc) > 100:
                desc = desc[:99] + "…"

            label = it.name if len(it.name) <= 96 else it.name[:95] + "…"
            opt = discord.SelectOption(label=label, description=desc, value=str(abs_i))
            if abs_i == self._selected_index:
                opt.default = True
            options.append(opt)

        self._select = InventorySelect(options=options, view=self)
        self.add_item(self._select)

    async def build_embed(self, page: InvPage, notice: str = "") -> discord.Embed:
        await self._load()

        embed = discord.Embed(color=0x8A7D5A)
        embed.description = (f"{notice}\n\n" if notice else "")

        if page == InvPage.LIST:
            self._apply_image_mode_list(embed)
            embed.description += "```\nYour pack is laid open on the counter.\n```"
            self._add_list(embed)
        elif page == InvPage.ITEM:
            it = self._items[self._selected_index] if self._items else None
            self._apply_image_mode_inspect(embed, it)
            embed.description += "```\nYou inspect the item closely.\n```"
            self._add_item(embed)
        else:
            self._apply_image_mode_closed(embed)
            embed.description += "```\nThe pack is closed.\n```"

        embed.set_footer(text=f"{self.member.display_name} • {_now_utc_short()}")
        return embed

    def _add_list(self, embed: discord.Embed) -> None:
        if not self._items:
            embed.add_field(name="Inventory", value="*You aren’t carrying anything yet.*", inline=False)
            return

        start = self._page_index * PAGE_SIZE
        end = min(start + PAGE_SIZE, len(self._items))
        page_items = self._items[start:end]

        lines = ["```", "ITEMS CARRIED", "────────────", ""]
        for it in page_items:
            name = it.name if len(it.name) <= 30 else it.name[:29] + "…"
            granted_role = self._granted_role_for_item(it)
            if granted_role:
                lines.append(f'{name} | Qty: {it.qty} | Granted Role: "{granted_role}"')
            else:
                lines.append(f"{name} | Qty: {it.qty} | Granted Role: —")
        lines.append("```")

        embed.add_field(name="Inventory", value="\n".join(lines), inline=False)

        total_pages = max(1, (len(self._items) + PAGE_SIZE - 1) // PAGE_SIZE)
        embed.add_field(
            name="Page",
            value=f"Showing **{start + 1}-{end}** of **{len(self._items)}** • Page **{self._page_index + 1}/{total_pages}**",
            inline=False,
        )
        embed.add_field(name="Tip", value="Use the dropdown to select an item, then press **Inspect**.", inline=False)

    def _add_item(self, embed: discord.Embed) -> None:
        if not self._items:
            embed.add_field(name="Item", value="*Nothing selected.*", inline=False)
            return

        it = self._items[self._selected_index]
        granted_role = self._granted_role_for_item(it)
        role_line = f'"{granted_role}"' if granted_role else "—"

        details = (
            f"**{it.name}**\n"
            f"*ID:* `{str(getattr(it, 'item_id', '') or '')}`\n"
            f"*Qty:* {it.qty}\n"
            f"*Category:* {it.category or '—'}\n"
            f"*Granted Role:* {role_line}\n"
        )
        if granted_role:
            details += f"\nThis item grants the role **{granted_role}** while it is in your possession."

        embed.add_field(name="Item", value=details, inline=False)

        if it.description:
            embed.add_field(name="Description", value=it.description[:1024], inline=False)

    async def _update(self, interaction: discord.Interaction, page: InvPage, notice: str = "") -> None:
        self.page = page
        embed = await self.build_embed(page, notice=notice)
        self._update_button_styles()
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.InteractionResponded:
            try:
                await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=self)
            except Exception:
                pass

    def _update_button_styles(self) -> None:
        """
        Label rules:
          - LIST: "Select Trade"
          - ITEM: "Trade"
        Enable rules:
          - Enabled if there is at least one item and the currently-selected item has qty > 0.
        """
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue

            if child.custom_id == "inv_list":
                child.style = discord.ButtonStyle.primary if self.page == InvPage.LIST else discord.ButtonStyle.secondary

            if child.custom_id == "inv_item":
                child.style = discord.ButtonStyle.primary if self.page == InvPage.ITEM else discord.ButtonStyle.secondary

            if child.custom_id == "inv_trade":
                child.label = "Select Trade" if self.page == InvPage.LIST else "Trade"
                enable = (
                    bool(self._items)
                    and (0 <= self._selected_index < len(self._items))
                    and int(getattr(self._items[self._selected_index], "qty", 0) or 0) > 0
                )
                child.disabled = not enable

    def _cycle_selected(self, delta: int) -> None:
        if not self._items:
            self._selected_index = 0
            return
        n = len(self._items)
        self._selected_index = (self._selected_index + delta) % n

    def _selected_item_summary(self) -> Tuple[str, str, int, str]:
        """
        Returns (name, item_id, qty, item_image_url)
        """
        if not self._items:
            return ("", "", 0, "")
        it = self._items[self._selected_index]
        name = str(getattr(it, "name", "") or "").strip()
        item_id = str(getattr(it, "item_id", "") or "").strip()
        qty = int(getattr(it, "qty", 0) or 0)
        img = self._image_for_item(it)
        return (name, item_id, qty, img)

    async def _begin_trade_request(self, interaction: discord.Interaction) -> None:
        if not self._items:
            await self._update(interaction, InvPage.LIST, notice="*No items to trade.*")
            return

        it = self._items[self._selected_index]
        if it.qty <= 0:
            await self._update(interaction, InvPage.ITEM, notice="*You don’t have any of that item.*")
            return

        if not self._trade_buyer_id:
            await interaction.response.send_message("Pick a buyer first.", ephemeral=True)
            return

        qty = max(1, int(self._trade_qty))
        if qty > it.qty:
            qty = it.qty

        pg, ps, pc = self._trade_price
        trade_id = f"TRD-{self.owner_id}-{int(time.time())}"

        pending = PendingTrade(
            trade_id=trade_id,
            seller_id=self.owner_id,
            buyer_id=self._trade_buyer_id,
            item_id=str(getattr(it, "item_id", "") or ""),
            item_name=str(getattr(it, "name", "") or ""),
            qty=qty,
            price_gold=pg,
            price_silver=ps,
            price_copper=pc,
            created_at=time.time(),
        )

        price_txt = _fmt_price_unicode(pg, ps, pc)
        req = discord.Embed(color=0x4B7BE5)
        req.title = "Trade Request"
        req.description = (
            f"Seller: <@{pending.seller_id}>\n"
            f"Buyer: <@{pending.buyer_id}>\n\n"
            f"Item: **{pending.item_name}** (`{pending.item_id}`)\n"
            f"Qty: **{pending.qty}**\n"
            f"Price (you pay): **{price_txt}**\n\n"
            "Accept or decline below."
        )

        item_img = self._image_for_item(it)
        if item_img:
            req.set_thumbnail(url=item_img)

        req.set_footer(text=f"Trade ID: {pending.trade_id} • Expires in 5 minutes")

        view = TradeRequestView(inv_view=self, pending=pending)

        sent = False
        try:
            buyer_member = interaction.guild.get_member(pending.buyer_id) if interaction.guild else None
            if buyer_member:
                dm = await buyer_member.create_dm()
                await dm.send(embed=req, view=view)
                sent = True
        except Exception:
            sent = False

        if not sent:
            try:
                await interaction.channel.send(content=f"<@{pending.buyer_id}>", embed=req, view=view)
                sent = True
            except Exception:
                sent = False

        if sent:
            await self._update(interaction, InvPage.ITEM, notice=f"✅ Trade request sent to <@{pending.buyer_id}>.")
        else:
            await self._update(interaction, InvPage.ITEM, notice="❌ Could not send trade request (DM and channel send failed).")

    async def _notify_trade_result(self, pending: PendingTrade, ok: bool, msg: str) -> None:
        try:
            guild = self.member.guild
            seller = guild.get_member(pending.seller_id) if guild else None
            buyer = guild.get_member(pending.buyer_id) if guild else None
            embed = discord.Embed(color=0x2ECC71 if ok else 0xE74C3C, title="Trade Result", description=msg)
            embed.add_field(name="Item", value=f"{pending.item_name} (`{pending.item_id}`) x{pending.qty}", inline=False)
            embed.add_field(
                name="Buyer Paid",
                value=_fmt_price_unicode(pending.price_gold, pending.price_silver, pending.price_copper),
                inline=False,
            )
            embed.set_footer(text=f"Trade ID: {pending.trade_id}")

            if seller:
                try:
                    await seller.send(embed=embed)
                except Exception:
                    pass
            if buyer:
                try:
                    await buyer.send(embed=embed)
                except Exception:
                    pass
        except Exception:
            pass

    # ----------------------------
    # Buttons
    # ----------------------------
    @discord.ui.button(label="Inventory", style=discord.ButtonStyle.primary, custom_id="inv_list", emoji="🎒", row=0)
    async def inv_list_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._update(interaction, InvPage.LIST)

    @discord.ui.button(label="Inspect", style=discord.ButtonStyle.secondary, custom_id="inv_item", emoji="🔎", row=0)
    async def inv_item_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._update(interaction, InvPage.ITEM)

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary, custom_id="inv_prev", emoji="⬅️", row=1)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._load()
        if not self._items:
            await self._update(interaction, InvPage.LIST)
            return

        if self.page == InvPage.ITEM:
            self._cycle_selected(-1)
            await self._update(interaction, InvPage.ITEM)
            return

        self._page_index = max(0, self._page_index - 1)
        await self._update(interaction, InvPage.LIST)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="inv_next", emoji="➡️", row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._load()
        if not self._items:
            await self._update(interaction, InvPage.LIST)
            return

        if self.page == InvPage.ITEM:
            self._cycle_selected(+1)
            await self._update(interaction, InvPage.ITEM)
            return

        total_pages = max(1, (len(self._items) + PAGE_SIZE - 1) // PAGE_SIZE)
        self._page_index = min(total_pages - 1, self._page_index + 1)
        await self._update(interaction, InvPage.LIST)

    @discord.ui.button(label="Select Trade", style=discord.ButtonStyle.success, custom_id="inv_trade", emoji="🤝", row=1)
    async def trade_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._items:
            await self._update(interaction, InvPage.LIST, notice="*No items to trade.*")
            return

        # Must pick item first (Inspect screen)
        if self.page != InvPage.ITEM:
            await self._update(
                interaction,
                InvPage.ITEM,
                notice="*You reach for your trade satchel… then pause.*\nChoose an item, **Inspect** it, then try **Trade** again.",
            )
            return

        name, item_id, qty, img = self._selected_item_summary()
        if qty <= 0:
            await self._update(interaction, InvPage.ITEM, notice="*You don’t have any of that item.*")
            return

        picker = BuyerPickerView(inv_view=self)

        embed = discord.Embed(
            color=0x5865F2,
            title="Pick a buyer",
            description=f"You set **{name}** on the counter.\nNow choose who you want to trade with.",
        )
        embed.add_field(name="Item", value=f"**{name}** (`{item_id}`)", inline=False)
        embed.add_field(name="Qty Available", value=str(qty), inline=True)
        if img:
            embed.set_thumbnail(url=img)

        await interaction.response.send_message(embed=embed, view=picker, ephemeral=True)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="inv_close", emoji="🚪", row=0)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        await self._update(interaction, InvPage.CLOSED, notice="Session closed.")
        self.stop()


class BuyerPickerView(discord.ui.View):
    def __init__(self, inv_view: InventoryView):
        super().__init__(timeout=60)
        self.inv_view = inv_view
        self.user_select = discord.ui.UserSelect(placeholder="Choose a user…", min_values=1, max_values=1)
        self.user_select.callback = self._on_pick  # type: ignore
        self.add_item(self.user_select)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        picked = self.user_select.values[0]
        self.inv_view._trade_buyer_id = picked.id
        modal = TradeTermsModal(self.inv_view)
        await interaction.response.send_modal(modal)


class InventorySelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption], view: InventoryView):
        super().__init__(placeholder="Select an item…", min_values=1, max_values=1, options=options, row=2)
        self.inv_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            chosen = int(self.values[0])
        except Exception:
            chosen = 0

        self.inv_view._selected_index = _clamp(chosen, 0, max(0, len(self.inv_view._items) - 1))
        self.inv_view._page_index = _clamp(
            self.inv_view._selected_index // PAGE_SIZE,
            0,
            max(0, (len(self.inv_view._items) - 1) // PAGE_SIZE),
        )
        await self.inv_view._update(interaction, InvPage.ITEM)


async def setup(bot: commands.Bot):
    await bot.add_cog(InventoryCog(bot))
