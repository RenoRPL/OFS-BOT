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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

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

LORE_HEADER_ALIASES: Dict[str, List[str]] = {
    "lore_id": ["Lore ID", "ID", "Ticket ID"],
    "status": ["Status"],
    "created_at": ["Created At", "Created UTC", "Timestamp", "Date Created"],
    "updated_at": ["Updated At", "Last Updated", "Updated UTC"],
    "author_name": ["Author Name", "Author", "Reporter Name"],
    "author_discord_id": ["Author Discord ID", "Author ID", "Reporter Discord ID", "User ID"],
    "source_channel_id": ["Source Channel ID", "Channel ID"],
    "source_message_id": ["Source Message ID", "Message ID"],
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


def _parse_backfill_since(value: Optional[str]) -> datetime:
    raw = (value or "").strip()
    if not raw:
        return _default_april_backfill_start()
    try:
        # Slash command input is intentionally simple: YYYY-MM-DD.
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("Use date format YYYY-MM-DD, for example 2026-04-01.") from exc


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


def _infer_suggested_area(text: str, attachment_urls: List[str]) -> Tuple[str, str]:
    lowered = (text or "").lower()
    if any(k in lowered for k in ("battle", "war", "campaign", "crusade", "operation", "victory", "defeat", "fleet")):
        return "Chronicles / Timeline", "Chronicles sequence; Oracle should determine exact Chronicle/Age placement."
    if any(k in lowered for k in ("codex", "law", "oath", "rank", "doctrine", "article", "rule", "banner")):
        return "Codex", "Relevant Codex article/appendix; preserve structured page layout."
    if attachment_urls and not text:
        return "Chronicles / Media", "Image-only lore capture; Oracle should request/derive caption and placement."
    return "Lore / Canon Review", "Oracle should classify as Codex, Timeline/Chronicles, House lore, or archive note."


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
                f"{_oracle_mention()} Lore review requested.\n\n"
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

    async def _create_admin_ticket(self, lore_id: str, message: discord.Message, area: str, placement: str, text: str, attachment_urls: List[str]) -> Tuple[discord.Message, Optional[discord.Thread]]:
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
            await thread.send(embed=self._build_workspace_embed(lore_id, area, placement))
        except Exception as e:
            print(f"[Lorewatch] Failed to create lore thread for {lore_id}: {e}")
        return admin_msg, thread

    async def _capture_lore_message(self, message: discord.Message, *, backfill: bool = False) -> str:
        text = _clean_lore_text(message.content or "")
        attachment_urls = [a.url for a in message.attachments]
        if not _is_probably_substantial_lore(text, attachment_urls):
            return "ignored"

        async with self._capture_lock:
            if message.id in self._recent_message_ids:
                return "recent_duplicate"
            self._recent_message_ids.add(message.id)
            if len(self._recent_message_ids) > 500:
                self._recent_message_ids = set(list(self._recent_message_ids)[-250:])

            ws = await self._open_lore_sheet()
            headers, rows, hmap = await self._get_sheet_rows(ws)
            required = {"lore_id", "status", "created_at", "updated_at", "source_message_id", "original_text"}
            missing = sorted(required - set(hmap))
            if missing:
                raise RuntimeError(f"Lore Intake sheet is missing required mapped header(s): {', '.join(missing)}")

            duplicate = _find_duplicate_lore(rows, hmap, source_message_id=str(message.id), source_text=text)
            if duplicate:
                try:
                    await message.add_reaction("👁️")
                except Exception:
                    pass
                print(f"[Lorewatch] Skipped duplicate lore message {message.id}: {duplicate}")
                return "duplicate"

            lore_id = await self._generate_lore_id(rows, hmap)
            area, placement = _infer_suggested_area(text, attachment_urls)
            now = _now_iso()
            row = self._build_row(
                headers,
                hmap,
                {
                    "lore_id": lore_id,
                    "status": "Captured",
                    "created_at": now,
                    "updated_at": now,
                    "author_name": str(message.author),
                    "author_discord_id": str(message.author.id),
                    "source_channel_id": str(message.channel.id),
                    "source_message_id": str(message.id),
                    "source_message_link": _message_link(message),
                    "original_text": text,
                    "attachment_urls": "\n".join(attachment_urls),
                    "suggested_area": area,
                    "suggested_placement": placement,
                    "notes": "Captured by Sentinel Lorewatch. No site content changed; approval required before publish.",
                },
            )
            await self._append_lore_row(ws, row)
            admin_msg, thread = await self._create_admin_ticket(lore_id, message, area, placement, text, attachment_urls)
            updates = {
                "admin_message_id": str(admin_msg.id),
                "updated_at": _now_iso(),
            }
            if thread:
                updates["admin_thread_id"] = str(thread.id)
            await self._update_lore_row_by_id(ws, lore_id, updates)

        try:
            await message.add_reaction(LORE_CAPTURE_REACTION)
        except Exception as e:
            print(f"[Lorewatch] Failed to react to captured lore {message.id}: {e}")
        print(f"[Lorewatch] Captured lore {lore_id} from message {message.id}")
        return "captured"

    async def _user_can_run_backfill(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        return int(getattr(user, "id", 0)) == PRIMARY_APPROVER_ID

    async def _backfill_chronicles(self, *, since: datetime, limit: int) -> Dict[str, int]:
        channel = self.bot.get_channel(CHRONICLES_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(CHRONICLES_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("Chronicles channel is not a text channel")

        counts = {"scanned": 0, "captured": 0, "duplicate": 0, "ignored": 0, "errors": 0}
        async for message in channel.history(limit=limit, after=since, oldest_first=True):
            if message.author.bot:
                continue
            counts["scanned"] += 1
            try:
                outcome = await self._capture_lore_message(message, backfill=True)
            except Exception as e:
                counts["errors"] += 1
                print(f"[Lorewatch] Backfill failed for message {message.id}: {e}")
                continue
            if outcome == "captured":
                counts["captured"] += 1
            elif outcome in {"duplicate", "recent_duplicate"}:
                counts["duplicate"] += 1
            else:
                counts["ignored"] += 1
        return counts

    @app_commands.command(name="lore_backfill", description="Backfill Chronicles lore posts into Lore Intake review tickets.")
    @app_commands.describe(
        since="Start date in YYYY-MM-DD format. Defaults to April 1 of the current year.",
        limit="Maximum messages to scan, newest cap 1000. Default 500.",
    )
    async def lore_backfill(self, interaction: discord.Interaction, since: Optional[str] = None, limit: Optional[int] = None):
        if not await self._user_can_run_backfill(interaction):
            await interaction.response.send_message("Only authorized admins can run Lorewatch backfill.", ephemeral=True)
            return
        try:
            since_dt = _parse_backfill_since(since)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        max_messages = max(1, min(int(limit or DEFAULT_BACKFILL_LIMIT), MAX_BACKFILL_LIMIT))

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            counts = await self._backfill_chronicles(since=since_dt, limit=max_messages)
        except Exception as e:
            print(f"[Lorewatch] Backfill command failed: {e}")
            await interaction.followup.send(f"Lorewatch backfill failed: `{e}`", ephemeral=True)
            return

        await interaction.followup.send(
            "Lorewatch backfill complete.\n"
            f"Since: `{since_dt.date().isoformat()}`\n"
            f"Scanned: **{counts['scanned']}**\n"
            f"Captured: **{counts['captured']}**\n"
            f"Duplicates skipped: **{counts['duplicate']}**\n"
            f"Ignored as non-lore/short chatter: **{counts['ignored']}**\n"
            f"Errors: **{counts['errors']}**\n\n"
            f"Captured entries were sent to <#{ADMIN_REPORT_CHANNEL_ID}> for Oracle/admin approval.",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if message.channel.id != CHRONICLES_CHANNEL_ID:
            return
        try:
            await self._capture_lore_message(message)
        except Exception as e:
            print(f"[Lorewatch] Failed to capture lore message {message.id}: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(SentinelLorewatch(bot))
