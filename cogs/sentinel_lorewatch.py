# -*- coding: utf-8 -*-
"""The Sentinel — OFS lore intake and Codex/Chronicle approval workflow.

Scope:
- Watches the Chronicles channel for lore posts.
- Silently reacts to captured lore with 📜.
- Writes durable intake rows to the `Lore Intake` Google Sheet tab.
- Opens private/admin review tickets in the Oracle report channel.
- Does not publish to the website or mutate Codex/Timeline content without approval.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

print("=== LOADED sentinel_lorewatch.py (The Sentinel Lorewatch) ===")

CHRONICLES_CHANNEL_ID = int(os.getenv("LORE_CHRONICLES_CHANNEL_ID", "1411573864642641952") or "1411573864642641952")
ADMIN_REPORT_CHANNEL_ID = int(os.getenv("LORE_ADMIN_REPORT_CHANNEL_ID", "1507805812901417171") or "1507805812901417171")
ORACLE_BOT_USER_ID = int(os.getenv("ORACLE_BOT_USER_ID", "1507459854825033838") or "1507459854825033838")
PRIMARY_APPROVER_ID = int(os.getenv("LORE_PRIMARY_APPROVER_ID", "527694877773922324") or "527694877773922324")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1YW5A_gk5WwmKbwxqrhIut3JUBSjaTO8vEf09F5QjpLo").strip()
LORE_INTAKE_TAB = os.getenv("LORE_INTAKE_TAB", "Lore Intake").strip() or "Lore Intake"
LORE_CAPTURE_REACTION = os.getenv("LORE_CAPTURE_REACTION", "📜") or "📜"
MAX_LORE_TEXT_CHARS = 5000
DEFAULT_BACKFILL_LIMIT = int(os.getenv("LORE_BACKFILL_LIMIT", "500") or "500")
MAX_BACKFILL_LIMIT = int(os.getenv("LORE_BACKFILL_MAX_LIMIT", "1000") or "1000")
LOREWATCH_CHECKPOINT_ID = os.getenv("LOREWATCH_CHECKPOINT_ID", "LOREWATCH-CHECKPOINT").strip() or "LOREWATCH-CHECKPOINT"
LOREWATCH_BASELINE_DATE = os.getenv("LOREWATCH_BASELINE_DATE", "2026-05-01").strip() or "2026-05-01"
LORE_GROUP_GAP_HOURS = float(os.getenv("LORE_GROUP_GAP_HOURS", "6") or "6")
LORE_MATURITY_HOURS = float(os.getenv("LORE_MATURITY_HOURS", "24") or "24")
LOREWATCH_DAILY_ENABLED = (os.getenv("LOREWATCH_DAILY_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"})

LORE_HEADER_ALIASES: Dict[str, List[str]] = {
    "lore_id": ["Lore ID", "ID", "Ticket ID"],
    "status": ["Status"],
    "created_at": ["Created At", "Created UTC", "Timestamp", "Date Created"],
    "updated_at": ["Updated At", "Last Updated", "Updated UTC"],
    "author_name": ["Author Name", "Author", "Reporter Name"],
    "author_discord_id": ["Author Discord ID", "Author ID", "Reporter Discord ID", "User ID"],
    "source_channel_id": ["Source Channel ID", "Channel ID"],
    "source_message_id": ["Source Message ID", "Message ID"],
    "source_message_ids": ["Source Message IDs", "Grouped Source Message IDs", "Message IDs", "Included Message IDs"],
    "source_message_link": ["Source Message Link", "Message Link", "Source Link"],
    "original_text": ["Original Text", "Original Lore", "Source Text", "Lore Text"],
    "attachment_urls": ["Attachment URLs", "Attachments", "Image URLs", "Screenshot URLs"],
    "suggested_area": ["Suggested Area", "Canon Area", "Site Area"],
    "suggested_placement": ["Suggested Placement", "Timeline Placement", "Codex Placement"],
    "oracle_summary": ["Oracle Summary", "Summary"],
    "oracle_draft": ["Oracle Draft", "Site-Ready Draft", "Draft"],
    "site_target": ["Site Target", "Target Page", "Publish Target"],
    "site_payload": ["Site Payload", "Publish Payload", "Content Payload"],
    "admin_thread_id": ["Admin Thread ID", "Thread ID"],
    "admin_message_id": ["Admin Message ID", "Ticket Message ID"],
    "approved_by": ["Approved By"],
    "approved_at": ["Approved At"],
    "published_at": ["Published At"],
    "notes": ["Notes", "Admin Notes"],
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat(timespec="seconds")


def _default_april_backfill_start() -> datetime:
    now = _now_utc()
    return datetime(now.year, 4, 1, tzinfo=timezone.utc)


def _default_may_backfill_start() -> datetime:
    return datetime(2026, 5, 1, tzinfo=timezone.utc)


def _default_may_backfill_end() -> datetime:
    return datetime(2026, 6, 1, tzinfo=timezone.utc)


def _parse_simple_utc_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("Use date format YYYY-MM-DD, for example 2026-05-01.") from exc


def _parse_backfill_since(value: Optional[str]) -> datetime:
    raw = (value or "").strip()
    if not raw:
        return _default_may_backfill_start()
    return _parse_simple_utc_date(raw)


def _parse_backfill_until(value: Optional[str]) -> datetime:
    raw = (value or "").strip()
    if not raw:
        return _default_may_backfill_end()
    return _parse_simple_utc_date(raw)


def _norm_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _header_map(headers: List[str], aliases: Dict[str, List[str]]) -> Dict[str, int]:
    norm_to_idx = {_norm_header(h): i for i, h in enumerate(headers)}
    mapped: Dict[str, int] = {}
    for key, names in aliases.items():
        for alias in names:
            idx = norm_to_idx.get(_norm_header(alias))
            if idx is not None:
                mapped[key] = idx
                break
    return mapped


def _get_row_value(row: List[str], idx: Optional[int]) -> str:
    if idx is None or idx >= len(row):
        return ""
    return str(row[idx] or "").strip()


def _clean_lore_text(text: str, limit: int = MAX_LORE_TEXT_CHARS) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) > limit:
        return cleaned[: limit - 3] + "..."
    return cleaned


def _lore_fingerprint(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _is_probably_substantial_lore(text: str, attachment_urls: List[str]) -> bool:
    cleaned = _lore_fingerprint(text)
    if attachment_urls and not cleaned:
        return True
    if len(cleaned) < 25:
        return False
    words = [w for w in cleaned.split() if w]
    if len(words) < 7:
        return False
    return True


def _message_link(message: discord.Message) -> str:
    return f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}" if message.guild else ""


def _truncate_field(value: str, limit: int = 1024) -> str:
    value = str(value or "").strip()
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _oracle_mention() -> str:
    return f"<@{ORACLE_BOT_USER_ID}>"


def _oracle_handoff_message(
    lore_id: str,
    message: discord.Message,
    area: str,
    placement: str,
    text: str,
    attachment_urls: List[str],
    *,
    limit: int = 1900,
) -> str:
    """Build a plain-text Oracle handoff so Hermes can read more than embeds."""
    attachments = "\n".join(attachment_urls) if attachment_urls else "None"
    lore_text = text or "[No text supplied; review attachment(s) and source post.]"
    handoff = (
        f"{_oracle_mention()}\n\n"
        "ORACLE LORE REVIEW REQUEST\n"
        f"Lore ID: {lore_id}\n"
        f"Source: {_message_link(message)}\n"
        f"Author: {message.author} (`{message.author.id}`)\n\n"
        "Initial Classification:\n"
        f"{area}\n\n"
        "Placement Hint:\n"
        f"{placement}\n\n"
        "Original Lore:\n"
        f"{lore_text}\n\n"
        "Attachments:\n"
        f"{attachments}\n\n"
        "Required Output:\n"
        "- Canon Classification\n"
        "- Suggested Site Target\n"
        "- Suggested Timeline Placement\n"
        "- Suggested Codex Placement\n"
        "- Chronicle Summary\n"
        "- Site-Ready Draft\n"
        "- Image Use\n"
        "- Canon Conflicts / Duplicate Risk\n"
        "- Approval Needed\n\n"
        "Boundary: do not publish or mutate site content without explicit human approval."
    )
    return handoff if len(handoff) <= limit else handoff[: limit - 3] + "..."


def _infer_suggested_area(text: str, attachment_urls: List[str]) -> Tuple[str, str]:
    lowered = (text or "").lower()
    if any(k in lowered for k in ("battle", "war", "campaign", "crusade", "operation", "victory", "defeat", "fleet")):
        return "Chronicles / Timeline", "Chronicles sequence; Oracle should determine exact Chronicle/Age placement."
    if any(k in lowered for k in ("codex", "law", "oath", "rank", "doctrine", "article", "rule", "banner")):
        return "Codex", "Relevant Codex article/appendix; preserve structured page layout."
    if attachment_urls and not text:
        return "Chronicles / Media", "Image-only lore capture; Oracle should request/derive caption and placement."
    return "Lore / Canon Review", "Oracle should classify as Codex, Timeline/Chronicles, House lore, or archive note."


def _message_created_at(message: discord.Message) -> datetime:
    created = getattr(message, "created_at", None)
    if created is None:
        return _now_utc()
    if created.tzinfo is None:
        return created.replace(tzinfo=timezone.utc)
    return created.astimezone(timezone.utc)


def _message_group_source_ids(messages: List[discord.Message]) -> str:
    return ",".join(str(m.id) for m in messages)


def _message_group_text(messages: List[discord.Message]) -> str:
    parts: List[str] = []
    for message in messages:
        text = _clean_lore_text(getattr(message, "content", "") or "")
        if text:
            parts.append(f"[Discord message {message.id}]\n{text}")
    return _clean_lore_text("\n\n".join(parts))


def _message_group_attachment_urls(messages: List[discord.Message]) -> List[str]:
    urls: List[str] = []
    seen: set[str] = set()
    for message in messages:
        for attachment in getattr(message, "attachments", []) or []:
            url = getattr(attachment, "url", "")
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _chunk_discord_text(text: str, *, limit: int = 1900) -> List[str]:
    """Split text into Discord-safe chunks while preserving exact content."""
    if limit <= 0:
        raise ValueError("chunk limit must be positive")
    if not text:
        return [""]
    chunks: List[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + limit])
        start += limit
    return chunks


def _build_lore_source_packet(
    lore_id: str,
    messages: List[discord.Message],
    area: str,
    placement: str,
) -> str:
    """Build the full plain-text context packet that Oracle/Hermes can read in-thread."""
    first = messages[0]
    attachment_urls = _message_group_attachment_urls(messages)
    lines = [
        "FULL LORE SOURCE PACKET",
        f"Lore ID: {lore_id}",
        f"Source Channel ID: {getattr(first.channel, 'id', '')}",
        f"Author: {first.author} (`{getattr(first.author, 'id', '')}`)",
        f"Initial Classification: {area}",
        f"Placement Hint: {placement}",
        f"Grouped Source Message IDs: {_message_group_source_ids(messages)}",
        "",
        "Source Links:",
    ]
    for message in messages:
        lines.append(f"- Discord message {message.id}: {_message_link(message)}")
    lines.extend(["", "Attachments:"])
    if attachment_urls:
        lines.extend(f"- {url}" for url in attachment_urls)
    else:
        lines.append("- None")
    lines.extend(["", "Original Chronicle Text:"])
    for index, message in enumerate(messages, start=1):
        created = _message_created_at(message).isoformat(timespec="seconds")
        content = (getattr(message, "content", "") or "").strip() or "[No text content]"
        lines.extend(
            [
                "",
                f"--- Message {index}/{len(messages)} | Discord ID {message.id} | {created} ---",
                content,
            ]
        )
    lines.extend(
        [
            "",
            "Required Oracle Output:",
            "- Canon Classification",
            "- Suggested Site Target",
            "- Suggested Timeline Placement",
            "- Suggested Codex Placement",
            "- Chronicle Summary",
            "- Site-Ready Draft",
            "- Image Use",
            "- Canon Conflicts / Duplicate Risk",
            "- Approval Needed",
            "",
            "Boundary: do not publish or mutate site content without explicit human approval.",
        ]
    )
    return "\n".join(lines)


def _group_chronicle_messages(
    messages: List[discord.Message],
    *,
    max_gap: Optional[timedelta] = None,
) -> List[List[discord.Message]]:
    """Group uninterrupted same-author Chronicle posts into one reviewable lore entry.

    Discord visually suppresses repeated avatars for uninterrupted runs from the
    same author. For Lorewatch, that means a long story split across several
    messages should remain one ticket even if the writer pauses for a while.
    A different author or short/non-lore chatter creates a break.
    """
    sorted_messages = sorted(messages, key=lambda m: _message_created_at(m))
    groups: List[List[discord.Message]] = []
    current: List[discord.Message] = []

    for message in sorted_messages:
        text = _clean_lore_text(getattr(message, "content", "") or "")
        attachment_urls = _message_group_attachment_urls([message])
        if not _is_probably_substantial_lore(text, attachment_urls):
            if current:
                groups.append(current)
                current = []
            continue

        if not current:
            current = [message]
            continue

        previous = current[-1]
        same_author = getattr(previous.author, "id", None) == getattr(message.author, "id", None)
        if same_author:
            current.append(message)
        else:
            groups.append(current)
            current = [message]

    if current:
        groups.append(current)
    return groups


def _split_message_ids(value: str) -> set[str]:
    ids: set[str] = set()
    for part in re.split(r"[^0-9]+", value or ""):
        if part:
            ids.add(part)
    return ids


def _message_ids_already_ticketed(rows: List[List[str]], hmap: Dict[str, int]) -> set[str]:
    ticketed: set[str] = set()
    for row in rows[1:]:
        lore_id = _get_row_value(row, hmap.get("lore_id"))
        if lore_id == LOREWATCH_CHECKPOINT_ID:
            continue
        ticketed.update(_split_message_ids(_get_row_value(row, hmap.get("source_message_id"))))
        ticketed.update(_split_message_ids(_get_row_value(row, hmap.get("source_message_ids"))))
        notes = _get_row_value(row, hmap.get("notes"))
        if "Discord message IDs" in notes or "Grouped Chronicle source" in notes:
            ticketed.update(_split_message_ids(notes))
    return ticketed


def _find_duplicate_lore(
    rows: List[List[str]],
    hmap: Dict[str, int],
    *,
    source_message_id: str,
    source_text: str,
) -> Optional[Dict[str, str]]:
    source_fingerprint = _lore_fingerprint(source_text)
    for row in rows[1:]:
        lore_id = _get_row_value(row, hmap.get("lore_id"))
        existing_source_id = _get_row_value(row, hmap.get("source_message_id"))
        if source_message_id and existing_source_id and source_message_id == existing_source_id:
            return {"lore_id": lore_id, "reason": "source_message_id"}

        existing_text = _get_row_value(row, hmap.get("original_text"))
        existing_fingerprint = _lore_fingerprint(existing_text)
        if source_fingerprint and existing_fingerprint and source_fingerprint == existing_fingerprint:
            return {"lore_id": lore_id, "reason": "normalized_text"}
    return None


class SentinelLorewatch(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._capture_lock = asyncio.Lock()
        self._recent_message_ids: set[int] = set()
        print(f"[Lorewatch] Chronicles intake channel: {CHRONICLES_CHANNEL_ID}")
        print(f"[Lorewatch] Lore admin/workbench channel: {ADMIN_REPORT_CHANNEL_ID}")
        print(f"[Lorewatch] Daily mature-group task enabled? {LOREWATCH_DAILY_ENABLED}")

    async def cog_load(self) -> None:
        if LOREWATCH_DAILY_ENABLED and not self.lorewatch_daily.is_running():
            self.lorewatch_daily.start()

    async def cog_unload(self) -> None:
        if self.lorewatch_daily.is_running():
            self.lorewatch_daily.cancel()

    async def _open_lore_sheet(self) -> Any:
        # Import lazily so pure helper tests can run even in environments that do not
        # have the production Google Sheets dependencies installed.
        from utils.google_auth import open_worksheet_async

        ws = await open_worksheet_async(LORE_INTAKE_TAB, spreadsheet_id=SPREADSHEET_ID)
        if ws is None:
            raise RuntimeError(f"Could not open worksheet '{LORE_INTAKE_TAB}'")
        return ws

    async def _get_sheet_rows(self, ws: Any) -> Tuple[List[str], List[List[str]], Dict[str, int]]:
        rows = await asyncio.to_thread(ws.get_all_values)
        headers = rows[0] if rows else []
        hmap = _header_map(headers, LORE_HEADER_ALIASES)
        return headers, rows, hmap

    async def _generate_lore_id(self, rows: List[List[str]], hmap: Dict[str, int]) -> str:
        today = _now_utc().strftime("%Y%m%d")
        prefix = f"LORE-{today}-"
        max_num = 0
        idx = hmap.get("lore_id")
        for row in rows[1:]:
            lore_id = _get_row_value(row, idx)
            if lore_id.startswith(prefix):
                try:
                    max_num = max(max_num, int(lore_id.rsplit("-", 1)[1]))
                except Exception:
                    pass
        return f"{prefix}{max_num + 1:03d}"

    def _build_row(self, headers: List[str], hmap: Dict[str, int], values: Dict[str, str]) -> List[str]:
        row = [""] * len(headers)
        for key, value in values.items():
            idx = hmap.get(key)
            if idx is not None:
                row[idx] = value
        return row

    async def _append_lore_row(self, ws: Any, row: List[str]) -> None:
        await asyncio.to_thread(ws.append_row, row, value_input_option="USER_ENTERED")

    async def _update_lore_row_by_id(self, ws: Any, lore_id: str, updates: Dict[str, str]) -> None:
        headers, rows, hmap = await self._get_sheet_rows(ws)
        lore_idx = hmap.get("lore_id")
        if lore_idx is None:
            return
        target_row_num = 0
        for offset, row in enumerate(rows[1:], start=2):
            if _get_row_value(row, lore_idx) == lore_id:
                target_row_num = offset
                break
        if not target_row_num:
            return
        cells = []
        for key, value in updates.items():
            idx = hmap.get(key)
            if idx is not None:
                cells.append({"range": f"{chr(65 + idx)}{target_row_num}", "values": [[value]]})
        if cells:
            await asyncio.to_thread(ws.batch_update, cells)

    async def _upsert_lorewatch_checkpoint(
        self,
        ws: Any,
        headers: List[str],
        rows: List[List[str]],
        hmap: Dict[str, int],
        message: discord.Message,
        *,
        note: str,
    ) -> None:
        values = {
            "lore_id": LOREWATCH_CHECKPOINT_ID,
            "status": "System Checkpoint",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "author_name": "The Sentinel",
            "source_channel_id": str(message.channel.id),
            "source_message_id": str(message.id),
            "source_message_link": _message_link(message),
            "original_text": "Lorewatch processing checkpoint; not a lore ticket.",
            "notes": f"{note}\nBaseline before {LOREWATCH_BASELINE_DATE} is considered already handled/on-site.",
        }
        lore_idx = hmap.get("lore_id")
        target_row_num = 0
        if lore_idx is not None:
            for offset, row in enumerate(rows[1:], start=2):
                if _get_row_value(row, lore_idx) == LOREWATCH_CHECKPOINT_ID:
                    target_row_num = offset
                    break
        if target_row_num:
            cells = []
            for key, value in values.items():
                idx = hmap.get(key)
                if idx is not None:
                    cells.append({"range": f"{chr(65 + idx)}{target_row_num}", "values": [[value]]})
            if cells:
                await asyncio.to_thread(ws.batch_update, cells)
        else:
            await self._append_lore_row(ws, self._build_row(headers, hmap, values))

    async def _lorewatch_after_marker(self, ws: Any) -> Any:
        headers, rows, hmap = await self._get_sheet_rows(ws)
        lore_idx = hmap.get("lore_id")
        if lore_idx is not None:
            for row in rows[1:]:
                if _get_row_value(row, lore_idx) == LOREWATCH_CHECKPOINT_ID:
                    source_id = _get_row_value(row, hmap.get("source_message_id"))
                    if source_id.isdigit():
                        return discord.Object(id=int(source_id))
        return _parse_backfill_since(LOREWATCH_BASELINE_DATE)

    def _build_admin_embed(self, lore_id: str, message: discord.Message, area: str, placement: str, text: str, attachment_urls: List[str]) -> discord.Embed:
        embed = discord.Embed(
            title=f"📜 Lore Captured — {lore_id}",
            description="A Chronicles post has been captured for canon review. No site content has been changed.",
            color=discord.Color.gold(),
            timestamp=_now_utc(),
        )
        embed.add_field(name="Status", value="Captured", inline=True)
        embed.add_field(name="Approval Required", value="Yes", inline=True)
        embed.add_field(name="Author", value=f"{message.author} (`{message.author.id}`)", inline=False)
        embed.add_field(name="Suggested Canon Area", value=area, inline=True)
        embed.add_field(name="Suggested Placement", value=_truncate_field(placement, 700), inline=False)
        embed.add_field(name="Source", value=f"[Open Discord post]({_message_link(message)})", inline=False)
        if text:
            embed.add_field(name="Original Lore", value=_truncate_field(text, 1000), inline=False)
        if attachment_urls:
            embed.add_field(name="Attachments", value=_truncate_field("\n".join(attachment_urls), 1000), inline=False)
            embed.set_image(url=attachment_urls[0])
        embed.set_footer(text=f"Lore ID: {lore_id}")
        return embed

    def _build_workspace_embed(self, lore_id: str, area: str, placement: str) -> discord.Embed:
        embed = discord.Embed(
            title=f"Oracle Lore Workbench — {lore_id}",
            description=(
                "Lore review requested.\n\n"
                "Determine where this belongs in the Codex / Timeline / Chronicles, draft the site-ready update, "
                "and wait for human approval before publishing."
            ),
            color=discord.Color.gold(),
            timestamp=_now_utc(),
        )
        embed.add_field(name="Initial Classification", value=area, inline=True)
        embed.add_field(name="Initial Placement Hint", value=_truncate_field(placement, 900), inline=False)
        embed.add_field(
            name="Required Oracle Draft Format",
            value=(
                "Canon Classification\n"
                "Suggested Site Target\n"
                "Suggested Timeline Placement\n"
                "Suggested Codex Placement\n"
                "Chronicle Summary\n"
                "Site-Ready Draft\n"
                "Image Use\n"
                "Canon Conflicts / Duplicate Risk\n"
                "Approval Needed"
            ),
            inline=False,
        )
        return embed

    async def _create_admin_ticket(self, lore_id: str, messages: List[discord.Message], area: str, placement: str, text: str, attachment_urls: List[str]) -> Tuple[discord.Message, Optional[discord.Thread]]:
        message = messages[0]
        channel = self.bot.get_channel(ADMIN_REPORT_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(ADMIN_REPORT_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("Lore admin report channel is not a text channel")
        content = f"<@{PRIMARY_APPROVER_ID}>"
        admin_msg = await channel.send(content=content, embed=self._build_admin_embed(lore_id, message, area, placement, text, attachment_urls))
        thread = None
        try:
            thread = await admin_msg.create_thread(name=f"⬜ {lore_id} — Lore Review", auto_archive_duration=10080)
            source_packet = _build_lore_source_packet(lore_id, messages, area, placement)
            packet_chunks = _chunk_discord_text(source_packet, limit=1750)
            for index, chunk in enumerate(packet_chunks, start=1):
                await thread.send(
                    content=f"FULL LORE SOURCE PACKET {index}/{len(packet_chunks)}\n```text\n{chunk}\n```",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            await thread.send(embed=self._build_workspace_embed(lore_id, area, placement))
            await thread.send(
                content=(
                    "SOURCE PACKET COMPLETE — Oracle may now review the full context above.\n\n"
                    f"{_oracle_handoff_message(lore_id, message, area, placement, text, attachment_urls)}"
                ),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except Exception as e:
            print(f"[Lorewatch] Failed to create lore thread for {lore_id}: {e}")
        return admin_msg, thread

    async def _capture_lore_group(self, messages: List[discord.Message], *, update_checkpoint: bool = True) -> str:
        if not messages:
            return "ignored"

        first = messages[0]
        text = _message_group_text(messages)
        attachment_urls = _message_group_attachment_urls(messages)
        if not _is_probably_substantial_lore(text, attachment_urls):
            return "ignored"

        source_ids = _message_group_source_ids(messages)
        async with self._capture_lock:
            ws = await self._open_lore_sheet()
            headers, rows, hmap = await self._get_sheet_rows(ws)
            required = {"lore_id", "status", "created_at", "updated_at", "source_message_id", "original_text"}
            missing = sorted(required - set(hmap))
            if missing:
                raise RuntimeError(f"Lore Intake sheet is missing required mapped header(s): {', '.join(missing)}")

            already_ticketed = _message_ids_already_ticketed(rows, hmap)
            if any(str(message.id) in already_ticketed for message in messages):
                for message in messages:
                    try:
                        await message.add_reaction("👁️")
                    except Exception:
                        pass
                print(f"[Lorewatch] Skipped duplicate lore group containing message IDs: {source_ids}")
                return "duplicate"

            duplicate = _find_duplicate_lore(rows, hmap, source_message_id=str(first.id), source_text=text)
            if duplicate:
                print(f"[Lorewatch] Skipped duplicate lore group {source_ids}: {duplicate}")
                return "duplicate"

            lore_id = await self._generate_lore_id(rows, hmap)
            area, placement = _infer_suggested_area(text, attachment_urls)
            now = _now_iso()
            source_links = "\n".join(_message_link(message) for message in messages)
            row = self._build_row(
                headers,
                hmap,
                {
                    "lore_id": lore_id,
                    "status": "Captured",
                    "created_at": now,
                    "updated_at": now,
                    "author_name": str(first.author),
                    "author_discord_id": str(first.author.id),
                    "source_channel_id": str(first.channel.id),
                    "source_message_id": str(first.id),
                    "source_message_ids": source_ids,
                    "source_message_link": _message_link(first),
                    "original_text": text,
                    "attachment_urls": "\n".join(attachment_urls),
                    "suggested_area": area,
                    "suggested_placement": placement,
                    "notes": (
                        "Captured by Sentinel Lorewatch as a grouped Chronicle source. "
                        "No site content changed; approval required before publish.\n\n"
                        f"Discord message IDs: {source_ids}\n"
                        f"Grouped Chronicle source links:\n{source_links}"
                    ),
                },
            )
            await self._append_lore_row(ws, row)
            admin_msg, thread = await self._create_admin_ticket(lore_id, messages, area, placement, text, attachment_urls)
            updates = {
                "admin_message_id": str(admin_msg.id),
                "updated_at": _now_iso(),
            }
            if thread:
                updates["admin_thread_id"] = str(thread.id)
            await self._update_lore_row_by_id(ws, lore_id, updates)
            if update_checkpoint:
                await self._upsert_lorewatch_checkpoint(ws, headers, rows, hmap, messages[-1], note=f"Last grouped ticket: {lore_id}")

        for message in messages:
            try:
                await message.add_reaction(LORE_CAPTURE_REACTION)
            except Exception as e:
                print(f"[Lorewatch] Failed to react to captured lore {message.id}: {e}")
        print(f"[Lorewatch] Captured grouped lore {lore_id} from message IDs: {source_ids}")
        return "captured"

    async def _capture_lore_message(self, message: discord.Message, *, backfill: bool = False) -> str:
        return await self._capture_lore_group([message], update_checkpoint=backfill)

    async def _user_can_run_backfill(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        return int(getattr(user, "id", 0)) == PRIMARY_APPROVER_ID

    async def _backfill_chronicles(
        self,
        *,
        since: datetime,
        until: datetime,
        limit: int,
        apply: bool = False,
        max_tickets: int = 3,
    ) -> Dict[str, int]:
        if until <= since:
            raise ValueError("Backfill end date must be after start date.")
        channel = self.bot.get_channel(CHRONICLES_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(CHRONICLES_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("Chronicles channel is not a text channel")

        counts = {"scanned": 0, "groups": 0, "captured": 0, "duplicate": 0, "ignored": 0, "errors": 0, "aborted": 0}
        messages: List[discord.Message] = []
        async for message in channel.history(limit=limit, after=since, before=until, oldest_first=True):
            if message.author.bot:
                continue
            counts["scanned"] += 1
            messages.append(message)

        groups = _group_chronicle_messages(messages)
        counts["groups"] = len(groups)
        grouped_ids = {message.id for group in groups for message in group}
        counts["ignored"] = len([message for message in messages if message.id not in grouped_ids])

        if not apply:
            return counts
        if len(groups) > max_tickets:
            counts["aborted"] = 1
            print(
                f"[Lorewatch] Backfill safety abort: {len(groups)} grouped lore entries exceeds max_tickets={max_tickets}. "
                "No tickets were created."
            )
            return counts

        for group in groups:
            try:
                outcome = await self._capture_lore_group(group, update_checkpoint=False)
            except Exception as e:
                counts["errors"] += 1
                print(f"[Lorewatch] Backfill failed for group {_message_group_source_ids(group)}: {e}")
                continue
            if outcome == "captured":
                counts["captured"] += 1
            elif outcome == "duplicate":
                counts["duplicate"] += 1
            else:
                counts["ignored"] += len(group)

        if messages:
            ws = await self._open_lore_sheet()
            headers, rows, hmap = await self._get_sheet_rows(ws)
            await self._upsert_lorewatch_checkpoint(
                ws,
                headers,
                rows,
                hmap,
                messages[-1],
                note=f"Manual backfill checkpoint from {since.date().isoformat()} through {until.date().isoformat()}; captured {counts['captured']} grouped lore ticket(s).",
            )
        return counts

    async def _process_mature_chronicles(self, *, limit: int = DEFAULT_BACKFILL_LIMIT) -> Dict[str, int]:
        channel = self.bot.get_channel(CHRONICLES_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(CHRONICLES_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("Chronicles channel is not a text channel")

        ws = await self._open_lore_sheet()
        after = await self._lorewatch_after_marker(ws)
        before = _now_utc() - timedelta(hours=LORE_MATURITY_HOURS)
        counts = {"scanned": 0, "groups": 0, "captured": 0, "duplicate": 0, "ignored": 0, "errors": 0}
        messages: List[discord.Message] = []
        async for message in channel.history(limit=limit, after=after, before=before, oldest_first=True):
            if message.author.bot:
                continue
            counts["scanned"] += 1
            messages.append(message)

        groups = _group_chronicle_messages(messages)
        counts["groups"] = len(groups)
        grouped_ids = {message.id for group in groups for message in group}
        counts["ignored"] = len([message for message in messages if message.id not in grouped_ids])
        for group in groups:
            try:
                outcome = await self._capture_lore_group(group, update_checkpoint=False)
            except Exception as e:
                counts["errors"] += 1
                print(f"[Lorewatch] Daily processing failed for group {_message_group_source_ids(group)}: {e}")
                continue
            if outcome == "captured":
                counts["captured"] += 1
            elif outcome == "duplicate":
                counts["duplicate"] += 1
            else:
                counts["ignored"] += len(group)

        if messages:
            headers, rows, hmap = await self._get_sheet_rows(ws)
            await self._upsert_lorewatch_checkpoint(
                ws,
                headers,
                rows,
                hmap,
                messages[-1],
                note=f"Daily mature Lorewatch checkpoint; captured {counts['captured']} grouped lore ticket(s).",
            )
        return counts

    @app_commands.command(name="lore_backfill", description="Preview/apply May 2026 Chronicles lore backfill into grouped review tickets.")
    @app_commands.describe(
        since="Start date YYYY-MM-DD. Default is 2026-05-01.",
        until="Exclusive end date YYYY-MM-DD. Default is 2026-06-01, so only May 2026 is scanned.",
        limit="Maximum messages to scan, cap 1000. Default 500.",
        apply="False previews only. True creates tickets after safety checks.",
        max_tickets="Safety cap for apply mode. Default 3; aborts if grouped entries exceed this.",
    )
    async def lore_backfill(
        self,
        interaction: discord.Interaction,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: Optional[int] = None,
        apply: Optional[bool] = False,
        max_tickets: Optional[int] = 3,
    ):
        if not await self._user_can_run_backfill(interaction):
            await interaction.response.send_message("Only authorized admins can run Lorewatch backfill.", ephemeral=True)
            return
        try:
            since_dt = _parse_backfill_since(since)
            until_dt = _parse_backfill_until(until)
            if until_dt <= since_dt:
                raise ValueError("Backfill end date must be after start date.")
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        max_messages = max(1, min(int(limit or DEFAULT_BACKFILL_LIMIT), MAX_BACKFILL_LIMIT))
        ticket_cap = max(1, min(int(max_tickets or 3), 25))

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            counts = await self._backfill_chronicles(
                since=since_dt,
                until=until_dt,
                limit=max_messages,
                apply=bool(apply),
                max_tickets=ticket_cap,
            )
        except Exception as e:
            print(f"[Lorewatch] Backfill command failed: {e}")
            await interaction.followup.send(f"Lorewatch backfill failed: `{e}`", ephemeral=True)
            return

        mode = "APPLY" if apply else "PREVIEW ONLY — no tickets created"
        safety_line = ""
        if counts.get("aborted"):
            safety_line = (
                f"\n⚠️ Safety abort: grouped lore entries (**{counts['groups']}**) exceeded max_tickets (**{ticket_cap}**). "
                "No tickets were created. Raise max_tickets only after previewing the count.\n"
            )
        elif not apply:
            safety_line = "\nRun again with `apply: True` to create tickets after confirming the preview count.\n"

        await interaction.followup.send(
            f"Lorewatch backfill {mode}.\n"
            f"Window: `{since_dt.date().isoformat()}` through `{until_dt.date().isoformat()}` exclusive\n"
            f"Scanned messages: **{counts['scanned']}**\n"
            f"Grouped lore entries: **{counts['groups']}**\n"
            f"Tickets created: **{counts['captured']}**\n"
            f"Duplicates skipped: **{counts['duplicate']}**\n"
            f"Ignored as non-lore/short chatter: **{counts['ignored']}**\n"
            f"Errors: **{counts['errors']}**\n"
            f"{safety_line}\n"
            f"May 2026 is the default historical catch-up window; future scheduled runs use the checkpoint and only process newer mature lore.",
            ephemeral=True,
        )

    @tasks.loop(hours=24)
    async def lorewatch_daily(self) -> None:
        try:
            counts = await self._process_mature_chronicles(limit=DEFAULT_BACKFILL_LIMIT)
            print(f"[Lorewatch] Daily mature processing complete: {counts}")
        except Exception as e:
            print(f"[Lorewatch] Daily mature processing failed: {e}")

    @lorewatch_daily.before_loop
    async def before_lorewatch_daily(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if message.channel.id != CHRONICLES_CHANNEL_ID:
            return
        text = _clean_lore_text(message.content or "")
        attachment_urls = [a.url for a in message.attachments]
        if not _is_probably_substantial_lore(text, attachment_urls):
            return
        try:
            await message.add_reaction("👁️")
            print(f"[Lorewatch] Observed Chronicles lore candidate {message.id}; ticketing is deferred to backfill/daily mature processing.")
        except Exception as e:
            print(f"[Lorewatch] Failed to observe lore candidate {message.id}: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(SentinelLorewatch(bot))
