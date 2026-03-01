# utils/inventory_store.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord

from utils.google_auth import open_spreadsheet

# ----------------------------
# Tabs
# ----------------------------
INVENTORY_SHEET = "inventory"  # you said you already created this tab
ITEM_LIST_SHEET = "Item List"
TRADES_LOG_SHEET = "Trades Log"  # create if you want (recommended)

# ----------------------------
# Expected headers (case-insensitive match)
# ----------------------------
INV_HEADERS = [
    "User ID",
    "Item ID",
    "Item Name",
    "Qty",
    "Category",
    "Description",
    "Role to assign",
    "Last Updated (UTC)",
    "Source",
    "Meta JSON",
]

TRADES_LOG_HEADERS = [
    "Trade ID",
    "Status",
    "Created (UTC)",
    "Resolved (UTC)",
    "Seller User ID",
    "Buyer User ID",
    "Item ID",
    "Item Name",
    "Qty",
    "Price Gold",
    "Price Silver",
    "Price Copper",
    "Notes",
    "Meta JSON",
]

# ----------------------------
# Locks
# ----------------------------
_USER_LOCKS: Dict[int, asyncio.Lock] = {}
_GLOBAL_LOCK = asyncio.Lock()


def _lock_for_user(user_id: int) -> asyncio.Lock:
    if user_id not in _USER_LOCKS:
        _USER_LOCKS[user_id] = asyncio.Lock()
    return _USER_LOCKS[user_id]


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_lower(s: Any) -> str:
    return str(s).strip().lower() if s is not None else ""


def _to_int(v: Any) -> int:
    try:
        if v is None:
            return 0
        s = str(v).strip()
        if not s:
            return 0
        return int(float(s))
    except Exception:
        return 0


def _find_header_index(headers: List[str], name: str) -> int:
    target = name.strip().lower()
    for i, h in enumerate(headers):
        if str(h).strip().lower() == target:
            return i
    return -1


def _find_header_index_contains(headers: List[str], keyword: str) -> int:
    kw = keyword.strip().lower()
    for i, h in enumerate(headers):
        if kw in str(h or "").strip().lower():
            return i
    return -1


def _ws(sheet, name: str):
    try:
        return sheet.worksheet(name)
    except Exception:
        # try case-insensitive match
        for ws in sheet.worksheets():
            if (ws.title or "").strip().lower() == name.strip().lower():
                return ws
    return None


def _ensure_headers(ws, required: List[str]) -> List[str]:
    row1 = ws.row_values(1) or []
    if len([c for c in row1 if str(c).strip()]) < 2:
        ws.update("A1", [required])
        return required
    return row1


@dataclass
class InventoryItem:
    item_id: str
    name: str
    qty: int
    category: str
    description: str
    role_to_assign: str


# ----------------------------
# Item List snapshot (role + desc + category)
# ----------------------------
def _read_item_snapshot_from_item_list_sync(item_id: str) -> Dict[str, str]:
    sheet = open_spreadsheet()
    if not sheet:
        return {}

    ws_items = _ws(sheet, ITEM_LIST_SHEET)
    if not ws_items:
        return {}

    data = ws_items.get_all_values()
    if not data or len(data) < 2:
        return {}

    headers = data[0]

    idx_id = _find_header_index_contains(headers, "item id")
    if idx_id == -1:
        idx_id = _find_header_index_contains(headers, "id")

    idx_name = _find_header_index_contains(headers, "name")
    idx_desc = _find_header_index_contains(headers, "desc")
    idx_cat = _find_header_index_contains(headers, "category")
    idx_role_assign = _find_header_index_contains(headers, "role to assign")
    if idx_role_assign == -1:
        # fallback
        idx_role_assign = _find_header_index_contains(headers, "assign")

    target = str(item_id).strip().lower()

    for row in data[1:]:
        rid = (row[idx_id] if idx_id != -1 and idx_id < len(row) else "").strip().lower()
        rname = (row[idx_name] if idx_name != -1 and idx_name < len(row) else "").strip()
        if rid and rid == target:
            return {
                "item_id": item_id,
                "name": rname,
                "description": (row[idx_desc] if idx_desc != -1 and idx_desc < len(row) else "").strip(),
                "category": (row[idx_cat] if idx_cat != -1 and idx_cat < len(row) else "").strip(),
                "role_to_assign": (row[idx_role_assign] if idx_role_assign != -1 and idx_role_assign < len(row) else "").strip(),
            }

    # If not found, return minimal
    return {"item_id": item_id, "name": item_id, "description": "", "category": "", "role_to_assign": ""}


async def read_item_snapshot(item_id: str) -> Dict[str, str]:
    return await asyncio.to_thread(_read_item_snapshot_from_item_list_sync, item_id)


# ----------------------------
# Inventory CRUD
# ----------------------------
def _get_inventory_ws_sync():
    sheet = open_spreadsheet()
    if not sheet:
        return None, None, []
    ws = _ws(sheet, INVENTORY_SHEET)
    if not ws:
        return sheet, None, []
    headers = _ensure_headers(ws, INV_HEADERS)
    return sheet, ws, headers


def _find_inv_row_by_user_item_sync(ws, headers: List[str], user_id: int, item_id: str) -> int:
    idx_uid = _find_header_index(headers, "User ID")
    idx_item = _find_header_index(headers, "Item ID")
    if idx_uid == -1:
        idx_uid = 0
    if idx_item == -1:
        idx_item = 1

    uid_str = str(user_id).strip()
    iid_str = str(item_id).strip().lower()

    data = ws.get_all_values()
    if not data or len(data) < 2:
        return 0

    for r_i, row in enumerate(data[1:], start=2):
        ru = (row[idx_uid] if idx_uid < len(row) else "").strip()
        ri = (row[idx_item] if idx_item < len(row) else "").strip().lower()
        if ru == uid_str and ri == iid_str:
            return r_i
    return 0


def _col_to_a1(col_idx_0: int) -> str:
    n = col_idx_0 + 1
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _upsert_inventory_row_sync(
    user_id: int,
    snapshot: Dict[str, str],
    delta_qty: int,
    source: str,
    meta: Dict[str, Any],
) -> Tuple[int, int, InventoryItem]:
    """
    Returns: (old_qty, new_qty, item)
    """
    sheet, ws, headers = _get_inventory_ws_sync()
    if not ws or not headers:
        raise RuntimeError("Inventory sheet not available")

    idx_uid = _find_header_index(headers, "User ID")
    idx_item = _find_header_index(headers, "Item ID")
    idx_name = _find_header_index(headers, "Item Name")
    idx_qty = _find_header_index(headers, "Qty")
    idx_cat = _find_header_index(headers, "Category")
    idx_desc = _find_header_index(headers, "Description")
    idx_role = _find_header_index(headers, "Role to assign")
    idx_ts = _find_header_index(headers, "Last Updated (UTC)")
    idx_source = _find_header_index(headers, "Source")
    idx_meta = _find_header_index(headers, "Meta JSON")

    # fallbacks if headers not exact
    if idx_uid == -1:
        idx_uid = 0
    if idx_item == -1:
        idx_item = 1
    if idx_name == -1:
        idx_name = 2
    if idx_qty == -1:
        idx_qty = 3

    item_id = snapshot.get("item_id", "").strip()
    name = snapshot.get("name", "").strip() or item_id
    cat = snapshot.get("category", "").strip()
    desc = snapshot.get("description", "").strip()
    role_to_assign = snapshot.get("role_to_assign", "").strip()

    row_idx = _find_inv_row_by_user_item_sync(ws, headers, user_id, item_id)

    old_qty = 0
    if row_idx:
        row = ws.row_values(row_idx)
        old_qty = _to_int(row[idx_qty] if idx_qty < len(row) else 0)
        new_qty = max(0, old_qty + int(delta_qty))

        # Update qty + metadata
        updates = []
        updates.append({"range": f"{_col_to_a1(idx_qty)}{row_idx}", "values": [[str(new_qty)]]})
        if idx_ts != -1:
            updates.append({"range": f"{_col_to_a1(idx_ts)}{row_idx}", "values": [[_now_utc_iso()]]})
        if idx_source != -1:
            updates.append({"range": f"{_col_to_a1(idx_source)}{row_idx}", "values": [[source]]})
        if idx_meta != -1:
            updates.append({"range": f"{_col_to_a1(idx_meta)}{row_idx}", "values": [[json.dumps(meta or {}, ensure_ascii=False)]]})
        # snapshot refresh (optional)
        if idx_name != -1:
            updates.append({"range": f"{_col_to_a1(idx_name)}{row_idx}", "values": [[name]]})
        if idx_cat != -1:
            updates.append({"range": f"{_col_to_a1(idx_cat)}{row_idx}", "values": [[cat]]})
        if idx_desc != -1:
            updates.append({"range": f"{_col_to_a1(idx_desc)}{row_idx}", "values": [[desc]]})
        if idx_role != -1:
            updates.append({"range": f"{_col_to_a1(idx_role)}{row_idx}", "values": [[role_to_assign]]})

        ws.batch_update(updates, value_input_option="RAW")

        item = InventoryItem(item_id=item_id, name=name, qty=new_qty, category=cat, description=desc, role_to_assign=role_to_assign)
        return old_qty, new_qty, item

    # create new row
    new_qty = max(0, int(delta_qty))
    new_row = [""] * len(headers)
    new_row[idx_uid] = str(user_id)
    new_row[idx_item] = item_id
    if idx_name != -1:
        new_row[idx_name] = name
    if idx_qty != -1:
        new_row[idx_qty] = str(new_qty)
    if idx_cat != -1:
        new_row[idx_cat] = cat
    if idx_desc != -1:
        new_row[idx_desc] = desc
    if idx_role != -1:
        new_row[idx_role] = role_to_assign
    if idx_ts != -1:
        new_row[idx_ts] = _now_utc_iso()
    if idx_source != -1:
        new_row[idx_source] = source
    if idx_meta != -1:
        new_row[idx_meta] = json.dumps(meta or {}, ensure_ascii=False)

    ws.append_row(new_row, value_input_option="RAW")

    item = InventoryItem(item_id=item_id, name=name, qty=new_qty, category=cat, description=desc, role_to_assign=role_to_assign)
    return 0, new_qty, item


async def add_item(
    user_id: int,
    item_id: str,
    qty: int,
    source: str = "unknown",
    meta: Optional[Dict[str, Any]] = None,
) -> Tuple[int, int, InventoryItem]:
    snap = await read_item_snapshot(item_id)
    async with _lock_for_user(user_id):
        old_qty, new_qty, item = await asyncio.to_thread(_upsert_inventory_row_sync, user_id, snap, qty, source, meta or {})
    return old_qty, new_qty, item


async def remove_item(
    user_id: int,
    item_id: str,
    qty: int,
    source: str = "unknown",
    meta: Optional[Dict[str, Any]] = None,
) -> Tuple[int, int, InventoryItem]:
    snap = await read_item_snapshot(item_id)
    async with _lock_for_user(user_id):
        old_qty, new_qty, item = await asyncio.to_thread(_upsert_inventory_row_sync, user_id, snap, -abs(int(qty)), source, meta or {})
    return old_qty, new_qty, item


def _read_inventory_items_sync(user_id: int) -> List[InventoryItem]:
    sheet, ws, headers = _get_inventory_ws_sync()
    if not ws or not headers:
        return []

    idx_uid = _find_header_index(headers, "User ID")
    idx_item = _find_header_index(headers, "Item ID")
    idx_name = _find_header_index(headers, "Item Name")
    idx_qty = _find_header_index(headers, "Qty")
    idx_cat = _find_header_index(headers, "Category")
    idx_desc = _find_header_index(headers, "Description")
    idx_role = _find_header_index(headers, "Role to assign")

    if idx_uid == -1:
        idx_uid = 0
    if idx_item == -1:
        idx_item = 1
    if idx_name == -1:
        idx_name = 2
    if idx_qty == -1:
        idx_qty = 3

    uid = str(user_id).strip()

    data = ws.get_all_values()
    if not data or len(data) < 2:
        return []

    out: List[InventoryItem] = []
    for row in data[1:]:
        ru = (row[idx_uid] if idx_uid < len(row) else "").strip()
        if ru != uid:
            continue
        item_id = (row[idx_item] if idx_item < len(row) else "").strip()
        name = (row[idx_name] if idx_name < len(row) else "").strip() or item_id
        qty = _to_int(row[idx_qty] if idx_qty < len(row) else 0)
        if qty <= 0:
            continue
        cat = (row[idx_cat] if idx_cat != -1 and idx_cat < len(row) else "").strip()
        desc = (row[idx_desc] if idx_desc != -1 and idx_desc < len(row) else "").strip()
        role = (row[idx_role] if idx_role != -1 and idx_role < len(row) else "").strip()
        out.append(InventoryItem(item_id=item_id, name=name, qty=qty, category=cat, description=desc, role_to_assign=role))

    out.sort(key=lambda x: (_safe_lower(x.category), _safe_lower(x.name)))
    return out


async def get_inventory_items(user_id: int) -> List[InventoryItem]:
    return await asyncio.to_thread(_read_inventory_items_sync, user_id)


# ----------------------------
# Role handling
# ----------------------------
def _find_role_by_name(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    target = (name or "").strip().lower()
    if not target:
        return None
    for role in guild.roles:
        if (role.name or "").strip().lower() == target:
            return role
    return None


async def ensure_role_state(member: discord.Member, role_name: str, should_have: bool) -> str:
    """
    Returns a short note for UI/logging.
    """
    role_name = (role_name or "").strip()
    if not role_name:
        return ""

    guild = member.guild
    role = _find_role_by_name(guild, role_name)
    if role is None:
        return f"Role not found: {role_name}"

    try:
        if should_have and role not in member.roles:
            await member.add_roles(role, reason="Inventory: item possession grants role")
            return f"Granted: {role_name}"
        if (not should_have) and role in member.roles:
            await member.remove_roles(role, reason="Inventory: item possession ended")
            return f"Removed: {role_name}"
    except Exception:
        return f"Role change failed: {role_name}"

    return ""


async def sync_roles_for_user(member: discord.Member) -> List[str]:
    """
    For each inventory item that has Role to assign, ensure:
    Qty > 0 -> has role
    Qty == 0 -> does not have role
    """
    notes: List[str] = []
    items = await get_inventory_items(member.id)
    for it in items:
        if it.role_to_assign:
            note = await ensure_role_state(member, it.role_to_assign, it.qty > 0)
            if note:
                notes.append(note)
    return notes


# ----------------------------
# Bank helpers (trade needs these)
# NOTE: these are minimal and assume the same Bank headers you use elsewhere.
# ----------------------------
BANK_SHEET = "Bank"
BANK_UID_HEADER = "User ID"
BANK_GOLD = "Gold"
BANK_SILVER = "Silver"
BANK_COPPER = "Copper"


def _read_bank_wallet_sync(user_id: int) -> Dict[str, int]:
    sheet = open_spreadsheet()
    if not sheet:
        return {"gold": 0, "silver": 0, "copper": 0}

    ws = _ws(sheet, BANK_SHEET)
    if not ws:
        return {"gold": 0, "silver": 0, "copper": 0}

    headers = ws.row_values(1) or []
    idx_uid = _find_header_index(headers, BANK_UID_HEADER)
    idx_g = _find_header_index(headers, BANK_GOLD)
    idx_s = _find_header_index(headers, BANK_SILVER)
    idx_c = _find_header_index(headers, BANK_COPPER)

    if idx_uid == -1:
        idx_uid = 0

    data = ws.get_all_values()
    if not data or len(data) < 2:
        return {"gold": 0, "silver": 0, "copper": 0}

    uid = str(user_id).strip()
    for row in data[1:]:
        ru = (row[idx_uid] if idx_uid < len(row) else "").strip()
        if ru != uid:
            continue
        return {
            "gold": _to_int(row[idx_g] if idx_g != -1 and idx_g < len(row) else 0),
            "silver": _to_int(row[idx_s] if idx_s != -1 and idx_s < len(row) else 0),
            "copper": _to_int(row[idx_c] if idx_c != -1 and idx_c < len(row) else 0),
        }

    return {"gold": 0, "silver": 0, "copper": 0}


def _update_bank_wallet_sync(user_id: int, new_wallet: Dict[str, int]) -> None:
    sheet = open_spreadsheet()
    if not sheet:
        raise RuntimeError("Spreadsheet not available")

    ws = _ws(sheet, BANK_SHEET)
    if not ws:
        raise RuntimeError("Bank sheet not available")

    headers = ws.row_values(1) or []
    idx_uid = _find_header_index(headers, BANK_UID_HEADER)
    idx_g = _find_header_index(headers, BANK_GOLD)
    idx_s = _find_header_index(headers, BANK_SILVER)
    idx_c = _find_header_index(headers, BANK_COPPER)

    if idx_uid == -1:
        idx_uid = 0

    uid = str(user_id).strip()
    data = ws.get_all_values()
    row_idx = 0
    for r_i, row in enumerate(data[1:], start=2):
        ru = (row[idx_uid] if idx_uid < len(row) else "").strip()
        if ru == uid:
            row_idx = r_i
            break

    if not row_idx:
        # create row
        new_row = [""] * max(len(headers), 4)
        new_row[idx_uid] = uid
        if idx_g != -1:
            new_row[idx_g] = str(max(0, int(new_wallet.get("gold", 0))))
        if idx_s != -1:
            new_row[idx_s] = str(max(0, int(new_wallet.get("silver", 0))))
        if idx_c != -1:
            new_row[idx_c] = str(max(0, int(new_wallet.get("copper", 0))))
        ws.append_row(new_row, value_input_option="RAW")
        return

    updates = []
    if idx_g != -1:
        updates.append({"range": f"{_col_to_a1(idx_g)}{row_idx}", "values": [[str(max(0, int(new_wallet.get("gold", 0))))]]})
    if idx_s != -1:
        updates.append({"range": f"{_col_to_a1(idx_s)}{row_idx}", "values": [[str(max(0, int(new_wallet.get("silver", 0))))]]})
    if idx_c != -1:
        updates.append({"range": f"{_col_to_a1(idx_c)}{row_idx}", "values": [[str(max(0, int(new_wallet.get("copper", 0))))]]})
    if updates:
        ws.batch_update(updates, value_input_option="RAW")


async def read_bank_wallet(user_id: int) -> Dict[str, int]:
    return await asyncio.to_thread(_read_bank_wallet_sync, user_id)


async def update_bank_wallet(user_id: int, new_wallet: Dict[str, int]) -> None:
    await asyncio.to_thread(_update_bank_wallet_sync, user_id, new_wallet)


# ----------------------------
# Trade commit (atomic-ish using locks)
# ----------------------------
def _ensure_trades_log_headers_sync() -> None:
    sheet = open_spreadsheet()
    if not sheet:
        return
    ws = _ws(sheet, TRADES_LOG_SHEET)
    if not ws:
        return
    _ensure_headers(ws, TRADES_LOG_HEADERS)


def _append_trade_log_sync(row: List[str]) -> None:
    sheet = open_spreadsheet()
    if not sheet:
        return
    ws = _ws(sheet, TRADES_LOG_SHEET)
    if not ws:
        return
    _ensure_trades_log_headers_sync()
    ws.append_row(row, value_input_option="RAW")


async def commit_trade(
    *,
    trade_id: str,
    seller_id: int,
    buyer_id: int,
    item_id: str,
    qty: int,
    price_gold: int,
    price_silver: int,
    price_copper: int,
    meta: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    Atomic-ish:
    - lock both users (sorted to avoid deadlock)
    - re-check seller qty and buyer funds
    - update bank both sides
    - update inventory both sides
    - sync roles for both sides
    - log
    """
    qty = max(1, int(qty))
    pg = max(0, int(price_gold))
    ps = max(0, int(price_silver))
    pc = max(0, int(price_copper))

    first, second = (seller_id, buyer_id) if seller_id < buyer_id else (buyer_id, seller_id)

    async with _lock_for_user(first):
        async with _lock_for_user(second):
            # Re-check seller inventory
            seller_items = await get_inventory_items(seller_id)
            seller_item = next((x for x in seller_items if x.item_id.strip().lower() == item_id.strip().lower()), None)
            if not seller_item or seller_item.qty < qty:
                return False, "Seller no longer has enough of that item."

            # Re-check buyer funds
            buyer_wallet = await read_bank_wallet(buyer_id)
            if buyer_wallet.get("gold", 0) < pg or buyer_wallet.get("silver", 0) < ps or buyer_wallet.get("copper", 0) < pc:
                return False, "Buyer no longer has enough currency."

            # Transfer currency buyer -> seller
            seller_wallet = await read_bank_wallet(seller_id)

            buyer_new = {
                "gold": buyer_wallet["gold"] - pg,
                "silver": buyer_wallet["silver"] - ps,
                "copper": buyer_wallet["copper"] - pc,
            }
            seller_new = {
                "gold": seller_wallet["gold"] + pg,
                "silver": seller_wallet["silver"] + ps,
                "copper": seller_wallet["copper"] + pc,
            }

            await update_bank_wallet(buyer_id, buyer_new)
            await update_bank_wallet(seller_id, seller_new)

            # Transfer item qty seller -> buyer
            await remove_item(seller_id, item_id, qty, source="trade_out", meta={"trade_id": trade_id, **(meta or {})})
            await add_item(buyer_id, item_id, qty, source="trade_in", meta={"trade_id": trade_id, **(meta or {})})

            # Log (best-effort)
            try:
                row = [
                    trade_id,
                    "COMPLETED",
                    _now_utc_iso(),
                    _now_utc_iso(),
                    str(seller_id),
                    str(buyer_id),
                    item_id,
                    seller_item.name,
                    str(qty),
                    str(pg),
                    str(ps),
                    str(pc),
                    "OK",
                    json.dumps(meta or {}, ensure_ascii=False),
                ]
                await asyncio.to_thread(_append_trade_log_sync, row)
            except Exception:
                pass

            return True, "Trade completed."
