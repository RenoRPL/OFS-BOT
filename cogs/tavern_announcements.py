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
from typing import List, Optional, Set

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


def _split_title_body(content: str) -> tuple[str, str]:
    lines = [line.strip() for line in (content or "").splitlines()]
    non_empty = [line for line in lines if line]
    if not non_empty:
        return "OFS Announcement", ""

    title = _clean_title(non_empty[0]) or "OFS Announcement"
    body_lines = non_empty[1:]

    # If the post is a single paragraph, keep the title compact and leave the
    # full text as the body so the Tavern card still has useful content.
    if not body_lines and len(title) > 90:
        full = title
        title = full[:87].rstrip() + "..."
        return title, full

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
    if not values:
        safe_call(lambda: ws.append_row(EXPECTED_HEADERS, value_input_option="USER_ENTERED"), label="tavern_announcements_append_headers")
        return [EXPECTED_HEADERS]

    first = values[0] if values else []
    if not first or _norm(first[0]).lower() != "id":
        # Preserve unknown user data. The site expects headers in row 1, so log
        # the repair needed instead of shifting data behind an operator's back.
        print(f"[TavernAnnouncements] Unexpected '{ANNOUNCEMENTS_SHEET}' header row. Expected: {EXPECTED_HEADERS}")
    return values


class TavernAnnouncements(commands.Cog):
    """Captures #announcements posts for the Tavern only after 📣 admin approval."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.channel_id != ANNOUNCEMENTS_CHANNEL_ID:
            return
        if str(payload.emoji) != CAPTURE_REACTION:
            return
        if payload.user_id == getattr(self.bot.user, "id", None):
            return

        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        member = payload.member or (guild.get_member(payload.user_id) if guild else None)
        if not _is_authorized(member):
            print(f"[TavernAnnouncements] Ignored {CAPTURE_REACTION} from unauthorized user {payload.user_id}")
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
            safe_call(lambda: ws.append_row(EXPECTED_HEADERS, value_input_option="USER_ENTERED"), label="tavern_announcements_append_headers")
            values = [EXPECTED_HEADERS]
        else:
            values = safe_call(lambda: ws.get_all_values(), label="tavern_announcements_get_all_values")
            values = _ensure_headers(ws, values)

        title, body = _split_title_body(message.content or "")
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
            if existing and _norm(existing[0]) == ann_id:
                row_num = idx
                break

        if row_num:
            end_col = chr(ord("A") + len(EXPECTED_HEADERS) - 1)
            safe_call(lambda: ws.update(f"A{row_num}:{end_col}{row_num}", [row], value_input_option="USER_ENTERED"), label="tavern_announcements_update_row")
        else:
            safe_call(lambda: ws.append_row(row, value_input_option="USER_ENTERED"), label="tavern_announcements_append_row")


async def setup(bot: commands.Bot):
    await bot.add_cog(TavernAnnouncements(bot))
