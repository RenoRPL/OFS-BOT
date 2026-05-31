# -*- coding: utf-8 -*-
"""Tavern announcement capture.

Watches the approved OFS announcements channel and publishes a message to the
website Tavern announcement feed only after an authorized admin reacts with 📣.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from typing import List, Optional, Set, cast

import discord
from discord.ext import commands

from utils.google_auth import open_spreadsheet, open_worksheet, safe_call

print("=== LOADED tavern_announcements.py (TAVERN ANNOUNCEMENT CAPTURE) ===")

ANNOUNCEMENTS_CHANNEL_ID = int(os.getenv("TAVERN_ANNOUNCEMENTS_CHANNEL_ID", "1387913290423861288") or "1387913290423861288")
CAPTURE_REACTION = os.getenv("TAVERN_ANNOUNCEMENT_CAPTURE_REACTION", "📣") or "📣"
ANNOUNCEMENTS_SHEET = os.getenv("TAVERN_ANNOUNCEMENTS_SHEET", "Tavern_Announcements").strip() or "Tavern_Announcements"
ADMIN_IDS: Set[int] = {
    int(part)
    for part in re.split(r"[,\s]+", os.getenv("TAVERN_ANNOUNCEMENT_ADMIN_IDS", "527694877773922324").strip())
    if part.isdigit()
}

EXPECTED_HEADERS = [
    "ID",
    "Title",
    "Body",
    "Date",
    "Author",
    "Pinned",
    "Category",
    "Image URL",
    "Message URL",
    "Source Channel ID",
    "Source Message ID",
    "Captured By",
    "Captured At",
    "Active",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(value: object) -> str:
    return str(value or "").strip()


def _message_link(message: discord.Message) -> str:
    if not message.guild:
        return ""
    return f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"


def _clean_title(line: str) -> str:
    title = re.sub(r"^\s*#+\s*", "", line or "").strip()
    title = re.sub(r"^[📣\-–—:\s]+", "", title).strip()
    return title[:140]


def _content_lines(content: str) -> List[str]:
    lines = []
    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        # Forwarded event/invite posts often have only a Discord URL or @here in
        # message.content while the actual title/body live in the embed.
        if re.fullmatch(r"https?://\S+", line, flags=re.I):
            continue
        if re.fullmatch(r"<@&?\d+>|@here|@everyone", line, flags=re.I):
            continue
        if line.lower() == "forwarded":
            continue
        lines.append(line)
    return lines


def _embed_title_body(message: discord.Message) -> tuple[str, str]:
    for embed in message.embeds:
        title = _clean_title(getattr(embed, "title", "") or "")
        body_parts = []
        description = getattr(embed, "description", "") or ""
        if description.strip():
            body_parts.append(description.strip())
        for field in getattr(embed, "fields", []) or []:
            name = str(getattr(field, "name", "") or "").strip()
            value = str(getattr(field, "value", "") or "").strip()
            if name and value:
                body_parts.append(f"{name}: {value}")
            elif value:
                body_parts.append(value)
        if title or body_parts:
            return title or "OFS Announcement", "\n".join(body_parts).strip()
    return "OFS Announcement", ""


def _split_title_body(message: discord.Message) -> tuple[str, str]:
    non_empty = _content_lines(message.content or "")
    if not non_empty:
        return _embed_title_body(message)

    title = _clean_title(non_empty[0]) or "OFS Announcement"
    body_lines = non_empty[1:]

    # If the post is a single paragraph, keep the title compact and leave the
    # full text as the body so the Tavern card still has useful content.
    if not body_lines and len(title) > 90:
        full = title
        title = full[:87].rstrip() + "..."
        return title, full

    if not body_lines:
        embed_title, embed_body = _embed_title_body(message)
        if title == "OFS Announcement" and embed_title != "OFS Announcement":
            return embed_title, embed_body
        if embed_body:
            return title, embed_body

    return title, "\n".join(body_lines).strip()


def _first_image_url(message: discord.Message) -> str:
    for attachment in message.attachments:
        content_type = (attachment.content_type or "").lower()
        filename = (attachment.filename or "").lower()
        if content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            return attachment.url
    for embed in message.embeds:
        if embed.image and embed.image.url:
            return embed.image.url
        if embed.thumbnail and embed.thumbnail.url:
            return embed.thumbnail.url
    return ""


def _is_authorized(member: Optional[discord.abc.User]) -> bool:
    if not member or getattr(member, "bot", False):
        return False
    if int(member.id) in ADMIN_IDS:
        return True
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild or perms.manage_messages))


def _ensure_headers(ws, values: List[List[str]]) -> List[List[str]]:
    first = values[0] if values else []
    expected_width = len(EXPECTED_HEADERS)
    needs_repair = len(first) < expected_width or any(_norm(first[idx]) != header for idx, header in enumerate(EXPECTED_HEADERS))
    if needs_repair:
        end_col = chr(ord("A") + expected_width - 1)
        safe_call(
            lambda: ws.update(f"A1:{end_col}1", [EXPECTED_HEADERS], value_input_option="USER_ENTERED"),
            label="tavern_announcements_repair_headers",
        )
        if values:
            values[0] = EXPECTED_HEADERS
        else:
            values = [EXPECTED_HEADERS]
        print(f"[TavernAnnouncements] Repaired '{ANNOUNCEMENTS_SHEET}' headers in A1:{end_col}1")
    return values


class TavernAnnouncements(commands.Cog):
    """Captures #announcements posts for the Tavern only after 📣 admin approval."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _authorized_member(self, payload: discord.RawReactionActionEvent) -> Optional[discord.abc.User]:
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        member = payload.member or (guild.get_member(payload.user_id) if guild else None)
        if member is None and guild is not None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except Exception as exc:
                print(f"[TavernAnnouncements] Failed to fetch reacting member {payload.user_id}: {exc}")
                return None
        if not _is_authorized(member):
            print(f"[TavernAnnouncements] Ignored {CAPTURE_REACTION} from unauthorized user {payload.user_id}")
            return None
        return cast(discord.abc.User, member)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != ANNOUNCEMENTS_CHANNEL_ID:
            return
        if str(payload.emoji) != CAPTURE_REACTION:
            return
        if payload.user_id == getattr(self.bot.user, "id", None):
            return

        member = await self._authorized_member(payload)
        if member is None:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except Exception as exc:
                print(f"[TavernAnnouncements] Failed to fetch channel {payload.channel_id}: {exc}")
                return

        try:
            message = await channel.fetch_message(payload.message_id)  # type: ignore[attr-defined]
        except Exception as exc:
            print(f"[TavernAnnouncements] Failed to fetch message {payload.message_id}: {exc}")
            return

        if message.author.bot:
            print(f"[TavernAnnouncements] Ignored bot-authored announcement message {message.id}")
            return

        try:
            await asyncio.to_thread(self._upsert_announcement, message, member)
            try:
                await message.add_reaction("✅")
            except Exception:
                pass
            print(f"[TavernAnnouncements] Captured announcement message {message.id}")
        except Exception as exc:
            print(f"[TavernAnnouncements] Capture failed for message {payload.message_id}: {exc}")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != ANNOUNCEMENTS_CHANNEL_ID:
            return
        if str(payload.emoji) != CAPTURE_REACTION:
            return
        if payload.user_id == getattr(self.bot.user, "id", None):
            return

        member = await self._authorized_member(payload)
        if member is None:
            return

        try:
            deactivated = await asyncio.to_thread(self._deactivate_announcement, payload.message_id)
            if deactivated:
                channel = self.bot.get_channel(payload.channel_id)
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(payload.channel_id)
                    except Exception:
                        channel = None
                if channel is not None:
                    try:
                        message = await channel.fetch_message(payload.message_id)  # type: ignore[attr-defined]
                        if self.bot.user:
                            await message.remove_reaction("✅", self.bot.user)
                    except Exception:
                        pass
                print(f"[TavernAnnouncements] Deactivated announcement message {payload.message_id}")
            else:
                print(f"[TavernAnnouncements] No active row found to deactivate for message {payload.message_id}")
        except Exception as exc:
            print(f"[TavernAnnouncements] Deactivate failed for message {payload.message_id}: {exc}")

    def _upsert_announcement(self, message: discord.Message, captured_by: discord.abc.User):
        ws = open_worksheet(ANNOUNCEMENTS_SHEET)
        if not ws:
            ss = open_spreadsheet()
            if not ss:
                raise RuntimeError("Spreadsheet is not available")
            ws = safe_call(
                lambda: ss.add_worksheet(title=ANNOUNCEMENTS_SHEET, rows=200, cols=len(EXPECTED_HEADERS)),
                label="tavern_announcements_create_sheet",
            )
            safe_call(lambda: ws.update("A1:N1", [EXPECTED_HEADERS], value_input_option="USER_ENTERED"), label="tavern_announcements_write_headers")
            values = [EXPECTED_HEADERS]
        else:
            values = safe_call(lambda: ws.get_all_values(), label="tavern_announcements_get_all_values")
            values = _ensure_headers(ws, values)

        title, body = _split_title_body(message)
        author_name = getattr(message.author, "display_name", None) or str(message.author)
        captured_name = getattr(captured_by, "display_name", None) or str(captured_by)
        ann_id = f"discord-{message.id}"
        row = [
            ann_id,
            title,
            body,
            message.created_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
            author_name,
            "FALSE",
            "important",
            _first_image_url(message),
            _message_link(message),
            str(message.channel.id),
            str(message.id),
            f"{captured_name} ({captured_by.id})",
            _utc_now(),
            "TRUE",
        ]

        row_num = None
        for idx, existing in enumerate(values[1:], start=2):
            existing_cells = [_norm(cell) for cell in existing]
            if ann_id in existing_cells or str(message.id) in existing_cells:
                row_num = idx
                break

        clear_width = 25
        write_row = row + [""] * (clear_width - len(row))
        end_col = chr(ord("A") + clear_width - 1)
        if row_num:
            safe_call(lambda: ws.update(f"A{row_num}:{end_col}{row_num}", [write_row], value_input_option="USER_ENTERED"), label="tavern_announcements_update_row")
        else:
            next_row = max(len(values) + 1, 2)
            safe_call(lambda: ws.update(f"A{next_row}:{end_col}{next_row}", [write_row], value_input_option="USER_ENTERED"), label="tavern_announcements_insert_row")

    def _deactivate_announcement(self, message_id: int) -> bool:
        ws = open_worksheet(ANNOUNCEMENTS_SHEET)
        if not ws:
            return False
        values = safe_call(lambda: ws.get_all_values(), label="tavern_announcements_deactivate_get_all_values")
        if not values:
            return False
        ann_id = f"discord-{message_id}"
        for idx, existing in enumerate(values[1:], start=2):
            existing_id = _norm(existing[0]) if len(existing) > 0 else ""
            existing_msg_id = _norm(existing[10]) if len(existing) > 10 else ""
            if existing_id == ann_id or existing_msg_id == str(message_id):
                safe_call(lambda: ws.update_cell(idx, 14, "FALSE"), label="tavern_announcements_deactivate_active")
                return True
        return False


async def setup(bot: commands.Bot):
    await bot.add_cog(TavernAnnouncements(bot))
