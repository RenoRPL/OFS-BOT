#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-time OFS banner-role reconciliation.

Repairs stale Member Log -> Banner cells from current Discord roles.

Run from the production checkout after pulling the commit:

    cd /root/OFS-BOT
    .venv/bin/python scripts/sync_banner_roles_all_once.py --apply

Without --apply, the script performs a dry run and prints the planned changes.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import discord
from dotenv import load_dotenv
from gspread.cell import Cell

# Make repo-root imports work when invoked as a script.
ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.google_auth import open_spreadsheet, safe_call  # noqa: E402

BANNERS_SHEET = "Banners"
MEMBER_LOG_SHEET = "Member Log"
DEFAULT_GUILD_ID = 1385700546434039928


def _norm(value: object) -> str:
    return str(value or "").strip()


def _key(value: object) -> str:
    return _norm(value).lower()


def _header_map(row: Iterable[str]) -> Dict[str, int]:
    return {_key(v): i for i, v in enumerate(row or []) if _norm(v)}


def load_banner_role_map() -> Tuple[Dict[str, str], List[str]]:
    ss = open_spreadsheet()
    if not ss:
        raise RuntimeError("Google spreadsheet unavailable. Check SPREADSHEET_ID and credentials.")

    ws = safe_call(lambda: ss.worksheet(BANNERS_SHEET), label="sync_all_open_banners")
    values = safe_call(lambda: ws.get_all_values(), label="sync_all_get_banners")
    if not values:
        raise RuntimeError("Banners sheet is empty")

    # Expected current layout: row 1 thresholds, row 2 headers, row 3+ data.
    headers = values[1] if len(values) > 1 else values[0]
    hmap = _header_map(headers)
    name_idx = hmap.get("banner name", 0)
    role_id_idx = hmap.get("role id")

    names_by_key: Dict[str, str] = {}
    ordered_names: List[str] = []
    data_rows = values[2:] if len(values) > 2 else values[1:]
    for row in data_rows:
        if not row:
            continue
        name = _norm(row[name_idx] if name_idx < len(row) else row[0])
        if not name or name.lower().startswith("points needed") or name.lower() == "banner name":
            continue
        ordered_names.append(name)
        names_by_key[_key(name)] = name
        if name.lower().startswith("the "):
            names_by_key[_key(name[4:])] = name
        if role_id_idx is not None and role_id_idx < len(row):
            role_id = _norm(row[role_id_idx])
            if role_id:
                names_by_key[role_id] = name

    if not ordered_names:
        raise RuntimeError("No banner names found in Banners sheet")
    return names_by_key, ordered_names


def banner_for_member(member: discord.Member, names_by_key: Dict[str, str], ordered_names: List[str]) -> Tuple[str, List[str]]:
    matched: Set[str] = set()
    for role in member.roles:
        by_id = names_by_key.get(str(role.id))
        by_name = names_by_key.get(_key(role.name))
        if by_id:
            matched.add(by_id)
        if by_name:
            matched.add(by_name)
    if not matched:
        return "", []
    ordered_matches = [name for name in ordered_names if name in matched]
    if ordered_matches:
        return ordered_matches[0], ordered_matches
    fallback = sorted(matched)
    return fallback[0], fallback


async def fetch_members(token: str, guild_id: int) -> Dict[str, discord.Member]:
    intents = discord.Intents.default()
    intents.members = True
    client = discord.Client(intents=intents)
    ready = asyncio.Event()
    result: Dict[str, discord.Member] = {}

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(guild_id)
            if guild is None:
                raise RuntimeError(f"Guild {guild_id} not found. Connected guilds: {[(g.name, g.id) for g in client.guilds]}")
            print(f"Connected as {client.user}; fetching members for {guild.name} ({guild.id})...")
            # guild.fetch_members avoids relying only on gateway cache contents.
            async for member in guild.fetch_members(limit=None):
                result[str(member.id)] = member
            print(f"Fetched {len(result)} guild members")
        finally:
            ready.set()
            await client.close()

    await client.start(token)
    await ready.wait()
    return result


def reconcile(member_by_id: Dict[str, discord.Member], names_by_key: Dict[str, str], ordered_names: List[str], apply: bool) -> Dict[str, int]:
    ss = open_spreadsheet()
    if not ss:
        raise RuntimeError("Google spreadsheet unavailable. Check SPREADSHEET_ID and credentials.")
    ws = safe_call(lambda: ss.worksheet(MEMBER_LOG_SHEET), label="sync_all_open_member_log")
    values = safe_call(lambda: ws.get_all_values(), label="sync_all_get_member_log")
    if not values:
        raise RuntimeError("Member Log is empty")

    hmap = _header_map(values[0])
    user_idx = hmap.get("user id")
    banner_idx = hmap.get("banner")
    username_idx = hmap.get("username")
    if user_idx is None or banner_idx is None:
        raise RuntimeError("Member Log missing required headers: User ID and/or Banner")

    changes: List[Cell] = []
    stats = {
        "rows": 0,
        "missing_discord_member": 0,
        "unchanged": 0,
        "updates": 0,
        "clears": 0,
        "ambiguous": 0,
    }

    preview: List[str] = []
    for row_num, row in enumerate(values[1:], start=2):
        uid = _norm(row[user_idx] if user_idx < len(row) else "")
        if not uid:
            continue
        stats["rows"] += 1
        member = member_by_id.get(uid)
        username = _norm(row[username_idx] if username_idx is not None and username_idx < len(row) else uid)
        current = _norm(row[banner_idx] if banner_idx < len(row) else "")
        if member is None:
            stats["missing_discord_member"] += 1
            continue
        target, matches = banner_for_member(member, names_by_key, ordered_names)
        if len(matches) > 1:
            stats["ambiguous"] += 1
        if current == target:
            stats["unchanged"] += 1
            continue
        if target:
            stats["updates"] += 1
        else:
            stats["clears"] += 1
        changes.append(Cell(row_num, banner_idx + 1, target))
        if len(preview) < 60:
            suffix = f" multiple={matches}" if len(matches) > 1 else ""
            preview.append(f"row {row_num}: {username} ({uid}) | {current or 'blank'} -> {target or 'blank'}{suffix}")

    print("\nPlanned changes:")
    for line in preview:
        print("  " + line)
    if len(changes) > len(preview):
        print(f"  ... {len(changes) - len(preview)} more")

    if changes and apply:
        safe_call(lambda: ws.update_cells(changes, value_input_option="USER_ENTERED"), label="sync_all_update_member_log")
        print(f"\nAPPLIED {len(changes)} Member Log banner changes")
    elif changes:
        print(f"\nDRY RUN ONLY: {len(changes)} changes would be applied. Re-run with --apply to write them.")
    else:
        print("\nNo Member Log banner changes needed.")

    return stats


async def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile Member Log Banner cells from current Discord banner roles.")
    parser.add_argument("--apply", action="store_true", help="Write changes to Member Log. Default is dry run.")
    parser.add_argument("--guild-id", type=int, default=int(os.getenv("OFS_MEMBER_SCAN_GUILD_ID", DEFAULT_GUILD_ID)))
    args = parser.parse_args()

    load_dotenv(ROOT / ".env", override=False)
    token = _norm(os.getenv("DISCORD_TOKEN"))
    if not token:
        raise RuntimeError("DISCORD_TOKEN missing from environment/.env")

    names_by_key, ordered_names = load_banner_role_map()
    print(f"Loaded {len(ordered_names)} banner definitions: {', '.join(ordered_names)}")
    members = await fetch_members(token, args.guild_id)
    stats = reconcile(members, names_by_key, ordered_names, args.apply)

    print("\nSummary:")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
