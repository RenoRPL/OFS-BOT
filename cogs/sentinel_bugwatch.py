# -*- coding: utf-8 -*-
"""The Sentinel — OFS bug/feature intake and admin ticket workflow.

MVP scope:
- Watches the public Bug reports channel.
- Creates temporary intake state before official submission.
- Assigns official ticket IDs only after useful details exist.
- Writes official records to the Bug Reports Google Sheet tab.
- Posts admin ticket cards and creates admin ticket threads.
- Lets whitelisted admins add evidence in threads, change severity, and dismiss reports.
- Lets the primary approver change status and approve/complete workflows later.

Hard boundaries:
- No code edits, deployments, service restarts, role changes, message deletion, or non-intake
  sheet mutations are performed by this cog.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from utils.google_auth import open_worksheet_async

print("=== LOADED sentinel_bugwatch.py (The Sentinel Bugwatch MVP) ===")

# ---------------------------------------------------------------------------
# Fixed OFS Sentinel configuration
# ---------------------------------------------------------------------------
SENTINEL_NAME = "The Sentinel"
BUG_REPORTS_CHANNEL_ID = 1507803277348311201
ADMIN_REPORT_CHANNEL_ID = 1507805812901417171
PRIMARY_APPROVER_ID = 527694877773922324
# Prefer the Oracle bot/user ID for reliable Hermes gateway routing. The role is a fallback only.
ORACLE_BOT_USER_ID = int(os.getenv("ORACLE_BOT_USER_ID", "0") or "0")
ORACLE_ROLE_ID = 1507463985543512215
SPREADSHEET_ID = "1YW5A_gk5WwmKbwxqrhIut3JUBSjaTO8vEf09F5QjpLo"
BUG_REPORTS_TAB = "Bug Reports"
WHITELIST_ADMIN_TAB = "White list Admin"

# Intake / spam protection
INTAKE_TTL_SECONDS = 30 * 60
NEW_INTAKE_COOLDOWN_SECONDS = 120
MAX_MESSAGE_CHARS = 3500
WHITELIST_CACHE_TTL_SECONDS = 120

# Ticket UI / field values
SEVERITIES = ["Critical", "High", "Medium", "Low", "Unknown", "Feature Request"]
STATUSES = [
    "Needs Info",
    "Triaged",
    "Needs Verification",
    "Confirmed Bug",
    "In Review",
    "Fix Proposed",
    "Resolved Pending Verification",
    "Dismissed",
    "Completed",
    "Duplicate",
]
DISMISS_REASONS = [
    "Working as Intended",
    "User Error",
    "Duplicate",
    "Spam / Bogus",
    "Insufficient Details",
    "Cannot Reproduce",
    "Not an OFS System Issue",
    "Feature Request Reclassified",
]

DANGEROUS_SECRET_PATTERNS = [
    re.compile(r"(?i)(token|api[_ -]?key|secret|password|passwd|pwd)\s*[:=]\s*[^\s`]+"),
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]{20,}"),
    re.compile(r"(?i)discord(?:[_ -]?bot)?[_ -]?token\s*[:=]\s*[^\s`]+"),
]

SYSTEM_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("OFS-BOT / Bank Economy", ["bank", "gold", "silver", "copper", "wallet", "pay", "payout", "economy"]),
    ("OFS-BOT / Quests & Crusades", ["quest", "crusade", "patrol", "join quest", "complete quest", "roster"]),
    ("OFS-BOT / Inventory", ["inventory", "item", "trade", "shop", "buy", "sell"]),
    ("OFS Website", ["website", "site", "page", "login", "web", "dashboard"]),
    ("Cloudflare Worker", ["worker", "api", "cloudflare", "ofs-api"]),
    ("Google Sheets / Database", ["sheet", "spreadsheet", "database", "row", "tab"]),
    ("Discord Permissions", ["permission", "role", "rank", "channel", "thread", "discord"]),
    ("Lore / Content", ["lore", "content", "typo", "canon", "archive"]),
]

FEATURE_KEYWORDS = ["feature", "suggest", "suggestion", "request", "add", "could we", "can we", "would like"]
CRITICAL_KEYWORDS = ["down", "offline", "crash", "crashed", "exploit", "leak", "data loss", "everyone", "all users", "cannot login"]
VAGUE_REPORTS = {"bug", "broken", "help", "it broke", "not working", "doesnt work", "doesn't work", "fix", "issue"}

# Common header aliases. The code writes only when the matching header exists.
HEADER_ALIASES: Dict[str, List[str]] = {
    "ticket_id": ["Ticket ID", "Ticket", "ID"],
    "type": ["Type", "Report Type"],
    "status": ["Status"],
    "verification": ["Verification Status", "Verification"],
    "severity": ["Severity"],
    "created_at": ["Created At", "Created UTC", "Timestamp", "Date Created"],
    "updated_at": ["Updated At", "Last Updated", "Updated UTC"],
    "reporter_name": ["Reporter Name", "Reporter", "User"],
    "reporter_id": ["Reporter Discord ID", "Reporter ID", "Discord ID", "User ID"],
    "public_channel_id": ["Public Channel ID", "Source Channel ID"],
    "public_message_id": ["Public Message ID", "Source Message ID"],
    "public_message_link": ["Public Message Link", "Message Link", "Source Link"],
    "admin_channel_id": ["Admin Channel ID"],
    "admin_message_id": ["Admin Message ID", "Ticket Message ID"],
    "admin_thread_id": ["Admin Thread ID", "Thread ID"],
    "admin_thread_link": ["Admin Thread Link", "Thread Link"],
    "affected_system": ["Affected System", "System"],
    "subsystem": ["Subsystem"],
    "summary": ["Summary"],
    "original_report": ["Original Report", "Claim", "Report"],
    "clarifying_questions": ["Clarifying Questions", "Questions Asked"],
    "clarifying_answers": ["Clarifying Answers", "Follow-up Details", "Follow Ups", "Followups"],
    "attachments": ["Attachments", "Attachment URLs", "Screenshot URLs"],
    "related_ticket_ids": ["Related Ticket IDs", "Related Tickets"],
    "duplicate_of": ["Duplicate Of"],
    "escalated": ["Escalated"],
    "escalated_to": ["Escalated To"],
    "recommendation": ["Recommendation", "Oracle Recommendation", "Next Recommended Step"],
    "next_step": ["Next Step"],
    "approval_required": ["Approval Required"],
    "admin_notes": ["Admin Notes"],
    "evidence_log": ["Evidence Log"],
    "dismissed_by": ["Dismissed By"],
    "dismissal_reason": ["Dismissal Reason"],
    "resolution_notes": ["Resolution Notes"],
    "closed_at": ["Closed At", "Dismissed At", "Completed At"],
    "last_updated_by": ["Last Updated By", "Updated By"],
}


@dataclass
class IntakeState:
    user_id: int
    channel_id: int
    first_message_id: int
    first_message_link: str
    reporter_name: str
    created_at: float
    updated_at: float
    messages: List[str] = field(default_factory=list)
    attachment_urls: List[str] = field(default_factory=list)
    questions_asked: bool = False
    submitted_ticket_id: str = ""
    admin_thread_id: int = 0


@dataclass
class TicketRecord:
    ticket_id: str
    report_type: str
    status: str
    verification: str
    severity: str
    reporter_name: str
    reporter_id: str
    public_channel_id: str
    public_message_id: str
    public_message_link: str
    affected_system: str
    summary: str
    original_report: str
    attachment_urls: List[str]
    recommendation: str
    admin_message_id: str = ""
    admin_thread_id: str = ""
    admin_thread_link: str = ""
    row_number: int = 0


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat(timespec="seconds")


def _clean_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    value = (text or "").strip()
    for pattern in DANGEROUS_SECRET_PATTERNS:
        value = pattern.sub(lambda m: m.group(0).split("=", 1)[0].split(":", 1)[0] + ": [REDACTED]", value)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > limit:
        return value[: limit - 3] + "..."
    return value


def _norm_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _find_header(headers: List[str], aliases: List[str]) -> Optional[int]:
    norm_to_idx = {_norm_header(h): i for i, h in enumerate(headers)}
    for alias in aliases:
        idx = norm_to_idx.get(_norm_header(alias))
        if idx is not None:
            return idx
    return None


def _header_map(headers: List[str]) -> Dict[str, int]:
    mapped: Dict[str, int] = {}
    for key, aliases in HEADER_ALIASES.items():
        idx = _find_header(headers, aliases)
        if idx is not None:
            mapped[key] = idx
    return mapped


def _get_row_value(row: List[str], idx: Optional[int]) -> str:
    if idx is None or idx >= len(row):
        return ""
    return str(row[idx] or "").strip()


def _append_note(existing: str, note: str) -> str:
    existing = (existing or "").strip()
    note = (note or "").strip()
    if not existing:
        return note
    if not note:
        return existing
    return f"{existing}\n---\n{note}"


def _message_link(message: discord.Message) -> str:
    return f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}" if message.guild else ""


def _thread_link(thread: discord.Thread) -> str:
    guild_id = thread.guild.id if thread.guild else "@me"
    return f"https://discord.com/channels/{guild_id}/{thread.id}"


def _truncate_field(value: str, limit: int = 1024) -> str:
    value = str(value or "").strip()
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _has_meaningful_content(text: str, attachments: List[str]) -> bool:
    cleaned = _clean_text(text, limit=500).lower().strip(" .!?")
    if attachments and len(cleaned) >= 3:
        return True
    if cleaned in VAGUE_REPORTS:
        return False
    words = [w for w in re.split(r"\W+", cleaned) if w]
    return len(words) >= 5 or len(cleaned) >= 35


def _is_feature_request(text: str) -> bool:
    lowered = (text or "").lower()
    return any(k in lowered for k in FEATURE_KEYWORDS)


def _classify_system(text: str) -> str:
    lowered = (text or "").lower()
    for system, keywords in SYSTEM_KEYWORDS:
        if any(k in lowered for k in keywords):
            return system
    return "Unknown"


def _classify_severity(text: str, report_type: str) -> str:
    if report_type == "Feature Request":
        return "Feature Request"
    lowered = (text or "").lower()
    if any(k in lowered for k in CRITICAL_KEYWORDS):
        return "High"
    if any(k in lowered for k in ["error", "failed", "cannot", "can't", "stuck", "wrong balance", "missing"]):
        return "Medium"
    return "Unknown"


def _summarize(text: str, report_type: str, affected_system: str) -> str:
    cleaned = _clean_text(text, limit=220)
    if cleaned:
        return cleaned
    return f"{report_type} report for {affected_system} with attachment(s)."


def _recommendation(report_type: str, affected_system: str, severity: str) -> str:
    if report_type == "Feature Request":
        return "Review request value, scope, affected OFS system, and priority before planning work."
    if severity in {"High", "Critical"}:
        return "Gather evidence immediately, verify impact, and request primary approver review before any repair action."
    return "Verify the reported behavior with reproduction steps, logs, screenshots, or admin confirmation before proposing code changes."


def _confirmed_recommendation(affected_system: str = "") -> str:
    target = affected_system or "the affected OFS system"
    return (
        f"Evidence/reproduction has been logged. Ask The Oracle to reason over {target}, identify the likely failure path, "
        "and propose safe next steps for primary approver review."
    )


def _infer_verification_from_context(current: str, thread_facts: str = "") -> str:
    current = (current or "").strip()
    if current and current.lower() not in {"unverified", "unknown"}:
        return current
    facts = (thread_facts or "").lower()
    if "verification: confirmed bug" in facts or "confirmed bug" in facts:
        return "Confirmed Bug"
    if any(term in facts for term in ("i verified", "verified the issue", "reproduced", "i can reproduce", "confirmation logged")):
        return "Confirmed Bug"
    return current or "Unverified"


def _oracle_mention() -> str:
    if ORACLE_BOT_USER_ID:
        return f"<@{ORACLE_BOT_USER_ID}>"
    return f"<@&{ORACLE_ROLE_ID}>"


def _same_meaning(a: str, b: str) -> bool:
    norm_a = re.sub(r"\W+", " ", (a or "").lower()).strip()
    norm_b = re.sub(r"\W+", " ", (b or "").lower()).strip()
    return bool(norm_a and norm_a == norm_b)


def _extract_reproduction_notes(thread_facts: str) -> List[str]:
    notes: List[str] = []
    for line in (thread_facts or "").splitlines():
        clean = _clean_text(line.lstrip("- "), limit=450)
        lower = clean.lower()
        if not clean or clean.startswith("Sentinel:"):
            continue
        if any(term in lower for term in ("verified", "reproduced", "i tested", "i can reproduce", "format", "paragraph", "banner", "codex", "styled")):
            if clean not in notes:
                notes.append(clean)
        if len(notes) >= 3:
            break
    return notes


def _evidence_summary(evidence: str, attachments: str, thread_facts: str) -> str:
    evidence = (evidence or "").strip()
    attachments = (attachments or "").strip()
    thread_facts = (thread_facts or "").strip()
    parts: List[str] = []
    if attachments and "no attachment" not in attachments.lower():
        parts.append("- Screenshot/attachment evidence was provided by the reporter.")
    facts_l = thread_facts.lower()
    if "confirmed bug" in facts_l or "confirmation logged" in facts_l or "verified the issue" in facts_l or "reproduced" in facts_l:
        parts.append("- Authorized admin reproduction/confirmation is present in the ticket thread.")
    for note in _extract_reproduction_notes(thread_facts):
        parts.append(f"- Reproduction note: {note}")
    if evidence and "no sheet evidence" not in evidence.lower():
        parts.append(f"- Sheet evidence/admin notes: {_truncate_field(evidence, 700)}")
    return "\n".join(parts) if parts else "No evidence captured yet. Ask for screenshot, reproduction steps, logs, or admin confirmation."


def _ticket_description_for(verification: str, status: str) -> str:
    verification_l = (verification or "").lower()
    status_l = (status or "").lower()
    if verification_l in {"confirmed bug", "reproduced"}:
        return (
            "Evidence or admin reproduction has been logged for this report. "
            "The Sentinel is preserving the record for Oracle review; no repair action is approved until explicitly authorized."
        )
    if status_l == "dismissed":
        return "This report has been dismissed by an authorized admin. No code changes were made by The Sentinel."
    return (
        "A user-submitted report has entered IT review. "
        "This is an **unverified claim** until evidence or reproduction confirms it."
    )


def _ticket_color(severity: str, status: str = "") -> discord.Color:
    if status.lower() == "dismissed":
        return discord.Color.dark_grey()
    sev = (severity or "").lower()
    if sev == "critical":
        return discord.Color.red()
    if sev == "high":
        return discord.Color.orange()
    if sev == "medium":
        return discord.Color.gold()
    if sev == "low":
        return discord.Color.green()
    if sev == "feature request":
        return discord.Color.purple()
    return discord.Color.blue()


# ---------------------------------------------------------------------------
# Discord UI
# ---------------------------------------------------------------------------
class SentinelTextModal(discord.ui.Modal):
    def __init__(self, cog: "SentinelBugwatch", action: str, title: str, label: str, placeholder: str = ""):
        super().__init__(title=title, timeout=300)
        self.cog = cog
        self.action = action
        self.value = discord.ui.TextInput(
            label=label,
            placeholder=placeholder,
            required=True,
            max_length=500,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_ticket_action(interaction, self.action, str(self.value.value))


class SentinelChoiceSelect(discord.ui.Select):
    def __init__(self, cog: "SentinelBugwatch", action: str, values: List[str], placeholder: str, ticket_id: str, admin_message_id: int):
        options = [discord.SelectOption(label=value, value=value) for value in values[:25]]
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)
        self.cog = cog
        self.action = action
        self.ticket_id = ticket_id
        self.admin_message_id = admin_message_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_ticket_action(
            interaction,
            self.action,
            self.values[0],
            ticket_id_override=self.ticket_id,
            admin_message_id_override=self.admin_message_id,
        )


class SentinelChoiceView(discord.ui.View):
    def __init__(self, cog: "SentinelBugwatch", action: str, values: List[str], placeholder: str, ticket_id: str, admin_message_id: int):
        super().__init__(timeout=180)
        self.add_item(SentinelChoiceSelect(cog, action, values, placeholder, ticket_id, admin_message_id))


class SentinelTicketView(discord.ui.View):
    def __init__(self, cog: "SentinelBugwatch"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Set Severity", style=discord.ButtonStyle.primary, custom_id="sentinel:set_severity")
    async def set_severity(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        ticket_id = self.cog._extract_ticket_id_from_interaction(interaction)
        if not ticket_id or not interaction.message:
            await interaction.response.send_message("Unable to identify ticket for this action.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select the new ticket severity:",
            view=SentinelChoiceView(self.cog, "severity", SEVERITIES, "Choose severity", ticket_id, interaction.message.id),
            ephemeral=True,
        )

    @discord.ui.button(label="Dismiss Report", style=discord.ButtonStyle.danger, custom_id="sentinel:dismiss")
    async def dismiss(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            SentinelTextModal(
                self.cog,
                "dismiss",
                "Dismiss Report",
                "Dismissal reason",
                f"Examples: {', '.join(DISMISS_REASONS[:4])}",
            )
        )

    @discord.ui.button(label="Set Status", style=discord.ButtonStyle.secondary, custom_id="sentinel:set_status")
    async def set_status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        ticket_id = self.cog._extract_ticket_id_from_interaction(interaction)
        if not ticket_id or not interaction.message:
            await interaction.response.send_message("Unable to identify ticket for this action.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select the new ticket status:",
            view=SentinelChoiceView(self.cog, "status", STATUSES, "Choose status", ticket_id, interaction.message.id),
            ephemeral=True,
        )

    @discord.ui.button(label="Prepare Oracle Brief", style=discord.ButtonStyle.success, custom_id="sentinel:request_oracle")
    async def prepare_oracle_brief(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.handle_ticket_action(interaction, "oracle_brief", "Requested")


# ---------------------------------------------------------------------------
# The Sentinel Cog
# ---------------------------------------------------------------------------
class SentinelBugwatch(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.intakes: Dict[int, IntakeState] = {}
        self.last_new_intake_at: Dict[int, float] = {}
        self._whitelist_cache: Tuple[float, set[int]] = (0.0, set())
        self._ticket_view = SentinelTicketView(self)
        try:
            self.bot.add_view(self._ticket_view)
        except Exception as e:
            print(f"[Sentinel] Failed to register persistent ticket view: {e}")

    # ----------------------------
    # Sheet helpers
    # ----------------------------
    async def _open_bug_ws(self):
        return await open_worksheet_async(BUG_REPORTS_TAB, SPREADSHEET_ID)

    async def _open_whitelist_ws(self):
        return await open_worksheet_async(WHITELIST_ADMIN_TAB, SPREADSHEET_ID)

    async def _get_sheet_values(self, ws) -> List[List[str]]:
        return await asyncio.to_thread(ws.get_all_values)

    async def _append_row(self, ws, row: List[str]) -> None:
        await asyncio.to_thread(ws.append_row, row, value_input_option="USER_ENTERED")

    async def _update_cells(self, ws, row_number: int, updates: Dict[int, str]) -> None:
        # gspread batch_update expects A1 ranges; keep writes compact and explicit.
        payload = []
        for idx, value in sorted(updates.items()):
            col = self._col_to_a1(idx)
            payload.append({"range": f"{col}{row_number}", "values": [[value]]})
        if payload:
            await asyncio.to_thread(ws.batch_update, payload, value_input_option="USER_ENTERED")

    @staticmethod
    def _col_to_a1(idx_0: int) -> str:
        n = idx_0 + 1
        out = ""
        while n:
            n, rem = divmod(n - 1, 26)
            out = chr(65 + rem) + out
        return out

    async def _load_bug_headers(self) -> Tuple[Any, List[str], Dict[str, int], List[List[str]]]:
        ws = await self._open_bug_ws()
        if not ws:
            raise RuntimeError("Bug Reports worksheet is not available")
        values = await self._get_sheet_values(ws)
        headers = values[0] if values else []
        if not headers:
            raise RuntimeError("Bug Reports tab has no header row")
        return ws, headers, _header_map(headers), values

    async def _authorized_admin_ids(self, force: bool = False) -> set[int]:
        now = time.monotonic()
        cached_at, cached = self._whitelist_cache
        if not force and cached and now - cached_at < WHITELIST_CACHE_TTL_SECONDS:
            return cached

        ws = await self._open_whitelist_ws()
        if not ws:
            return set()
        values = await self._get_sheet_values(ws)
        if not values:
            return set()

        headers = values[0]
        discord_idx = _find_header(headers, ["Discord ID"]) or 1  # fallback column B
        bug_edit_idx = _find_header(headers, ["Bug edits (dangerous)"]) or 3  # fallback column D

        allowed: set[int] = set()
        for row in values[1:]:
            discord_id = _get_row_value(row, discord_idx)
            flag = _get_row_value(row, bug_edit_idx).strip().upper()
            digits = re.sub(r"\D+", "", discord_id)
            if digits and flag == "X":
                try:
                    allowed.add(int(digits))
                except ValueError:
                    continue
        self._whitelist_cache = (now, allowed)
        return allowed

    async def _is_whitelisted_admin(self, user_id: int) -> bool:
        if int(user_id) == PRIMARY_APPROVER_ID:
            return True
        allowed = await self._authorized_admin_ids()
        return int(user_id) in allowed

    async def _find_ticket_by_message_or_thread(self, message_id: Optional[int] = None, thread_id: Optional[int] = None) -> Tuple[Any, List[str], Dict[str, int], List[str], int]:
        ws, headers, hmap, values = await self._load_bug_headers()
        admin_msg_idx = hmap.get("admin_message_id")
        thread_idx = hmap.get("admin_thread_id")
        ticket_idx = hmap.get("ticket_id")
        if ticket_idx is None:
            raise RuntimeError("Bug Reports tab needs a Ticket ID column")

        for row_num, row in enumerate(values[1:], start=2):
            if message_id and admin_msg_idx is not None and _get_row_value(row, admin_msg_idx) == str(message_id):
                return ws, headers, hmap, row, row_num
            if thread_id and thread_idx is not None and _get_row_value(row, thread_idx) == str(thread_id):
                return ws, headers, hmap, row, row_num
        raise RuntimeError("Ticket row not found")

    async def _generate_ticket_id(self, report_type: str) -> str:
        _, _, hmap, values = await self._load_bug_headers()
        ticket_idx = hmap.get("ticket_id")
        prefix = "FR" if report_type == "Feature Request" else "BUG"
        date_part = _now_utc().strftime("%Y%m%d")
        base = f"{prefix}-{date_part}-"
        max_num = 0
        if ticket_idx is not None:
            for row in values[1:]:
                tid = _get_row_value(row, ticket_idx)
                if tid.startswith(base):
                    try:
                        max_num = max(max_num, int(tid.rsplit("-", 1)[-1]))
                    except ValueError:
                        pass
        return f"{base}{max_num + 1:03d}"

    async def _write_new_ticket(self, record: TicketRecord) -> int:
        ws, headers, hmap, _values = await self._load_bug_headers()
        now = _now_iso()
        fields = {
            "ticket_id": record.ticket_id,
            "type": record.report_type,
            "status": record.status,
            "verification": record.verification,
            "severity": record.severity,
            "created_at": now,
            "updated_at": now,
            "reporter_name": record.reporter_name,
            "reporter_id": record.reporter_id,
            "public_channel_id": record.public_channel_id,
            "public_message_id": record.public_message_id,
            "public_message_link": record.public_message_link,
            "admin_channel_id": str(ADMIN_REPORT_CHANNEL_ID),
            "affected_system": record.affected_system,
            "summary": record.summary,
            "original_report": record.original_report,
            "attachments": "\n".join(record.attachment_urls),
            "escalated": "Yes" if record.severity in {"Critical", "High"} else "No",
            "escalated_to": f"<@{PRIMARY_APPROVER_ID}>" if record.severity in {"Critical", "High"} else "",
            "recommendation": record.recommendation,
            "approval_required": "Yes for code edits, deployments, restarts, status changes, completion, or Oracle action approval.",
        }
        row = [""] * len(headers)
        for key, value in fields.items():
            if key in hmap:
                row[hmap[key]] = str(value)
        await self._append_row(ws, row)
        # Append row number is current values + header + new row. Fetching again is safer with concurrent appends.
        values = await self._get_sheet_values(ws)
        ticket_idx = hmap.get("ticket_id")
        if ticket_idx is not None:
            for row_num, data in enumerate(values[1:], start=2):
                if _get_row_value(data, ticket_idx) == record.ticket_id:
                    return row_num
        return len(values)

    async def _update_ticket_row_by_id(self, ticket_id: str, fields: Dict[str, str]) -> None:
        ws, headers, hmap, values = await self._load_bug_headers()
        ticket_idx = hmap.get("ticket_id")
        if ticket_idx is None:
            return
        row_num = 0
        for idx, row in enumerate(values[1:], start=2):
            if _get_row_value(row, ticket_idx) == ticket_id:
                row_num = idx
                break
        if not row_num:
            return
        updates: Dict[int, str] = {}
        for key, value in fields.items():
            if key in hmap:
                updates[hmap[key]] = str(value)
        if "updated_at" in hmap and "updated_at" not in fields:
            updates[hmap["updated_at"]] = _now_iso()
        await self._update_cells(ws, row_num, updates)

    # ----------------------------
    # Ticket building / Discord IO
    # ----------------------------
    def _build_ticket_record(self, intake: IntakeState) -> TicketRecord:
        combined = "\n".join(intake.messages).strip()
        report_type = "Feature Request" if _is_feature_request(combined) else "Bug"
        system = _classify_system(combined)
        severity = _classify_severity(combined, report_type)
        summary = _summarize(combined, report_type, system)
        return TicketRecord(
            ticket_id="",
            report_type=report_type,
            status="Submitted",
            verification="Unverified",
            severity=severity,
            reporter_name=intake.reporter_name,
            reporter_id=str(intake.user_id),
            public_channel_id=str(intake.channel_id),
            public_message_id=str(intake.first_message_id),
            public_message_link=intake.first_message_link,
            affected_system=system,
            summary=summary,
            original_report=_clean_text(combined, limit=2500),
            attachment_urls=list(dict.fromkeys(intake.attachment_urls)),
            recommendation=_recommendation(report_type, system, severity),
        )

    def _build_admin_embed(self, record: TicketRecord) -> discord.Embed:
        title = f"🛡️ {SENTINEL_NAME} Ticket — {record.ticket_id}"
        desc = _ticket_description_for(record.verification, record.status)
        embed = discord.Embed(title=title, description=desc, color=_ticket_color(record.severity, record.status), timestamp=_now_utc())
        embed.add_field(name="Type", value=record.report_type, inline=True)
        embed.add_field(name="Status", value=record.status, inline=True)
        embed.add_field(name="Verification", value=record.verification, inline=True)
        embed.add_field(name="Severity", value=record.severity, inline=True)
        embed.add_field(name="Affected System", value=record.affected_system, inline=True)
        embed.add_field(name="Reporter", value=f"{record.reporter_name}\n`{record.reporter_id}`", inline=True)
        embed.add_field(name="Public Report", value=f"[Jump to message]({record.public_message_link})" if record.public_message_link else "Unavailable", inline=False)
        embed.add_field(name="Claim / Summary", value=_truncate_field(record.summary), inline=False)
        evidence = "\n".join(record.attachment_urls) if record.attachment_urls else "No attachments provided."
        embed.add_field(name="Attachment URLs", value=_truncate_field(evidence), inline=False)
        embed.add_field(name="Oracle Recommendation", value=_truncate_field(record.recommendation), inline=False)
        embed.add_field(
            name="Operational Boundary",
            value="No code edits, deployments, restarts, role changes, message deletions, or non-intake Sheet mutations without approval.",
            inline=False,
        )
        if record.severity in {"Critical", "High"}:
            embed.add_field(name="Escalation", value=f"<@{PRIMARY_APPROVER_ID}> High-priority review requested.", inline=False)
        embed.set_footer(text=f"Ticket ID: {record.ticket_id}")
        return embed

    def _build_workspace_embed(
        self,
        ticket_id: str,
        status: str,
        verification: str,
        severity: str,
        recommendation: str,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"🧵 Ticket Workspace — {ticket_id}",
            description=(
                "Use this thread for evidence, reproduction notes, Oracle analysis, proposed actions, approvals, "
                "and final review.\n\n"
                "Triage admins may add evidence, set severity, or dismiss invalid reports. "
                f"Only <@{PRIMARY_APPROVER_ID}> may approve repair actions, deployments, restarts, or completion."
            ),
            color=_ticket_color(severity, status),
            timestamp=_now_utc(),
        )
        embed.add_field(name="Current Status", value=status or "Unknown", inline=True)
        embed.add_field(name="Verification", value=verification or "Unverified", inline=True)
        embed.add_field(name="Severity", value=severity or "Unknown", inline=True)
        embed.add_field(name="Next Recommended Step", value=_truncate_field(recommendation or "Awaiting triage."), inline=False)
        return embed

    async def _update_workspace_embed(
        self,
        thread_id: str,
        ticket_id: str,
        status: str,
        verification: str,
        severity: str,
        recommendation: str,
    ) -> None:
        if not str(thread_id or "").isdigit():
            return
        thread = self.bot.get_channel(int(thread_id))
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(int(thread_id))
            except Exception:
                return
        if not isinstance(thread, discord.Thread):
            return
        try:
            async for msg in thread.history(limit=20, oldest_first=True):
                if not msg.author.bot or not msg.embeds:
                    continue
                embed = msg.embeds[0]
                if embed.title and embed.title.startswith(f"🧵 Ticket Workspace — {ticket_id}"):
                    await msg.edit(embed=self._build_workspace_embed(ticket_id, status, verification, severity, recommendation))
                    return
        except Exception as e:
            print(f"[Sentinel] Failed to update workspace embed for {ticket_id}: {e}")

    def _extract_ticket_id_from_interaction(self, interaction: discord.Interaction) -> str:
        msg = interaction.message
        if not msg:
            return ""
        for embed in msg.embeds:
            if embed.footer and embed.footer.text:
                m = re.search(r"Ticket ID:\s*([A-Z]+-\d{8}-\d{3})", embed.footer.text)
                if m:
                    return m.group(1)
            if embed.title:
                m = re.search(r"([A-Z]+-\d{8}-\d{3})", embed.title)
                if m:
                    return m.group(1)
        return ""

    async def _create_admin_ticket(self, record: TicketRecord) -> Tuple[discord.Message, Optional[discord.Thread]]:
        channel = self.bot.get_channel(ADMIN_REPORT_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(ADMIN_REPORT_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("Admin report channel is not a text channel")

        content = f"<@{PRIMARY_APPROVER_ID}>" if record.severity in {"Critical", "High"} else None
        admin_msg = await channel.send(content=content, embed=self._build_admin_embed(record), view=self._ticket_view)

        thread = None
        try:
            safe_system = re.sub(r"[^A-Za-z0-9 /&_-]+", "", record.affected_system).strip() or "Ticket"
            thread_name = f"{record.ticket_id} — {safe_system}"[:95]
            thread = await admin_msg.create_thread(name=thread_name, auto_archive_duration=10080)
            await thread.send(embed=self._build_workspace_embed(record.ticket_id, record.status, record.verification, record.severity, record.recommendation))
        except Exception as e:
            print(f"[Sentinel] Failed to create ticket thread for {record.ticket_id}: {e}")
        return admin_msg, thread

    async def _submit_intake(self, message: discord.Message, intake: IntakeState) -> Optional[TicketRecord]:
        record = self._build_ticket_record(intake)
        record.ticket_id = await self._generate_ticket_id(record.report_type)
        row_num = await self._write_new_ticket(record)
        record.row_number = row_num

        admin_msg, thread = await self._create_admin_ticket(record)
        record.admin_message_id = str(admin_msg.id)
        if thread:
            record.admin_thread_id = str(thread.id)
            record.admin_thread_link = _thread_link(thread)
            intake.admin_thread_id = thread.id

        await self._update_ticket_row_by_id(
            record.ticket_id,
            {
                "admin_message_id": record.admin_message_id,
                "admin_thread_id": record.admin_thread_id,
                "admin_thread_link": record.admin_thread_link,
            },
        )

        intake.submitted_ticket_id = record.ticket_id
        public = discord.Embed(
            title=f"✅ Report Submitted to {SENTINEL_NAME}",
            description="Thank you. Your report has been submitted for IT review.",
            color=_ticket_color(record.severity, record.status),
            timestamp=_now_utc(),
        )
        public.add_field(name="Ticket", value=record.ticket_id, inline=True)
        public.add_field(name="Type", value=record.report_type, inline=True)
        public.add_field(name="Status", value=record.status, inline=True)
        public.add_field(name="Verification", value=record.verification, inline=True)
        public.add_field(name="Severity", value=record.severity, inline=True)
        public.add_field(name="Affected System", value=record.affected_system, inline=True)
        public.add_field(
            name="Important",
            value="This report is not considered a confirmed bug until evidence, reproduction, or admin review verifies it.",
            inline=False,
        )
        try:
            await message.reply(embed=public, mention_author=True)
        except Exception:
            await message.channel.send(embed=public)
        return record

    async def _append_followup(self, message: discord.Message, intake: IntakeState) -> None:
        text = _clean_text(message.content or "")
        attachment_urls = [a.url for a in message.attachments]
        if text:
            intake.messages.append(text)
        intake.attachment_urls.extend(attachment_urls)
        intake.updated_at = time.monotonic()

        if not intake.submitted_ticket_id:
            return

        note = f"[{_now_iso()}] Follow-up from {message.author} ({message.author.id}): {text or '[attachment only]'}"
        if attachment_urls:
            note += "\nAttachments:\n" + "\n".join(attachment_urls)
        await self._append_ticket_note(intake.submitted_ticket_id, note, attachments=attachment_urls)

        if intake.admin_thread_id:
            thread = self.bot.get_channel(intake.admin_thread_id)
            if thread is None:
                try:
                    thread = await self.bot.fetch_channel(intake.admin_thread_id)
                except Exception:
                    thread = None
            if isinstance(thread, discord.Thread):
                embed = discord.Embed(title="📎 Public Follow-up Added", description=_truncate_field(text or "Attachment-only follow-up"), color=discord.Color.blue(), timestamp=_now_utc())
                if attachment_urls:
                    embed.add_field(name="Attachment URLs", value=_truncate_field("\n".join(attachment_urls)), inline=False)
                await thread.send(embed=embed)

        try:
            await message.reply(f"📎 Follow-up added to ticket **{intake.submitted_ticket_id}**.", mention_author=True)
        except Exception:
            pass

    async def _append_ticket_note(self, ticket_id: str, note: str, attachments: Optional[List[str]] = None) -> None:
        ws, headers, hmap, values = await self._load_bug_headers()
        ticket_idx = hmap.get("ticket_id")
        if ticket_idx is None:
            return
        for row_num, row in enumerate(values[1:], start=2):
            if _get_row_value(row, ticket_idx) != ticket_id:
                continue
            updates: Dict[int, str] = {}
            if "clarifying_answers" in hmap:
                existing = _get_row_value(row, hmap["clarifying_answers"])
                updates[hmap["clarifying_answers"]] = _append_note(existing, note)
            elif "admin_notes" in hmap:
                existing = _get_row_value(row, hmap["admin_notes"])
                updates[hmap["admin_notes"]] = _append_note(existing, note)
            elif "resolution_notes" in hmap:
                existing = _get_row_value(row, hmap["resolution_notes"])
                updates[hmap["resolution_notes"]] = _append_note(existing, note)
            if attachments and "attachments" in hmap:
                existing_att = _get_row_value(row, hmap["attachments"])
                new_att = _append_note(existing_att, "\n".join(attachments))
                updates[hmap["attachments"]] = new_att
            if "updated_at" in hmap:
                updates[hmap["updated_at"]] = _now_iso()
            await self._update_cells(ws, row_num, updates)
            return

    # ----------------------------
    # Message listeners
    # ----------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        # Public bug reports channel intake.
        if message.channel.id == BUG_REPORTS_CHANNEL_ID:
            await self._handle_public_bug_message(message)
            return

        # Admin ticket thread evidence logging.
        if isinstance(message.channel, discord.Thread) and message.channel.parent_id == ADMIN_REPORT_CHANNEL_ID:
            await self._handle_admin_thread_message(message)

    async def _handle_public_bug_message(self, message: discord.Message) -> None:
        now = time.monotonic()
        user_id = int(message.author.id)
        text = _clean_text(message.content or "")
        attachment_urls = [a.url for a in message.attachments]

        # Existing active intake/ticket: treat as follow-up.
        intake = self.intakes.get(user_id)
        if intake and now - intake.updated_at <= INTAKE_TTL_SECONDS:
            await self._append_followup(message, intake)
            if not intake.submitted_ticket_id and _has_meaningful_content("\n".join(intake.messages), intake.attachment_urls):
                await self._submit_intake(message, intake)
            return

        # Cooldown for brand new intakes.
        last_new = self.last_new_intake_at.get(user_id, 0.0)
        if now - last_new < NEW_INTAKE_COOLDOWN_SECONDS:
            remaining = int(NEW_INTAKE_COOLDOWN_SECONDS - (now - last_new))
            await message.reply(
                f"🛡️ {SENTINEL_NAME} is rate-limiting new reports. Please add details to your existing report or wait **{remaining}s**.",
                mention_author=True,
            )
            return

        self.last_new_intake_at[user_id] = now
        intake = IntakeState(
            user_id=user_id,
            channel_id=message.channel.id,
            first_message_id=message.id,
            first_message_link=_message_link(message),
            reporter_name=str(message.author),
            created_at=now,
            updated_at=now,
            messages=[text] if text else [],
            attachment_urls=attachment_urls,
        )
        self.intakes[user_id] = intake

        if not _has_meaningful_content(text, attachment_urls):
            intake.questions_asked = True
            embed = discord.Embed(
                title=f"🛡️ {SENTINEL_NAME} Received Your Report",
                description="Status: **Gathering Details**\nVerification: **Unverified**",
                color=discord.Color.blue(),
                timestamp=_now_utc(),
            )
            embed.add_field(
                name="Needed Before IT Submission",
                value=(
                    "Please provide:\n"
                    "1. What were you trying to do?\n"
                    "2. What happened instead?\n"
                    "3. What command, button, page, or feature was involved?\n"
                    "4. Screenshot or exact error text, if available."
                ),
                inline=False,
            )
            embed.set_footer(text="Temporary intake created. No official ticket ID assigned yet.")
            await message.reply(embed=embed, mention_author=True)
            return

        await self._submit_intake(message, intake)

    async def _handle_admin_thread_message(self, message: discord.Message) -> None:
        # Thread messages become evidence only for whitelisted admins / primary approver.
        if not await self._is_whitelisted_admin(message.author.id):
            return
        text = _clean_text(message.content or "")
        attachment_urls = [a.url for a in message.attachments]
        if not text and not attachment_urls:
            return
        try:
            ws, _headers, hmap, row, row_num = await self._find_ticket_by_message_or_thread(thread_id=message.channel.id)
        except Exception:
            return

        ticket_id = _get_row_value(row, hmap.get("ticket_id"))
        note = f"[{_now_iso()}] Evidence/Admin note from {message.author} ({message.author.id}): {text or '[attachment only]'}"
        if attachment_urls:
            note += "\nAttachments:\n" + "\n".join(attachment_urls)
        updates: Dict[int, str] = {}
        if "evidence_log" in hmap:
            updates[hmap["evidence_log"]] = _append_note(_get_row_value(row, hmap["evidence_log"]), note)
        elif "admin_notes" in hmap:
            updates[hmap["admin_notes"]] = _append_note(_get_row_value(row, hmap["admin_notes"]), note)
        elif "resolution_notes" in hmap:
            updates[hmap["resolution_notes"]] = _append_note(_get_row_value(row, hmap["resolution_notes"]), note)
        if attachment_urls and "attachments" in hmap:
            updates[hmap["attachments"]] = _append_note(_get_row_value(row, hmap["attachments"]), "\n".join(attachment_urls))
        if "updated_at" in hmap:
            updates[hmap["updated_at"]] = _now_iso()
        if "last_updated_by" in hmap:
            updates[hmap["last_updated_by"]] = f"{message.author} ({message.author.id})"
        confirmation_terms = ("confirm", "confirmed", "verified", "reproduced", "i can reproduce", "i tested", "same issue")
        confirmation_detected = bool(text and any(term in text.lower() for term in confirmation_terms))
        new_verification = "Confirmed Bug" if int(message.author.id) == PRIMARY_APPROVER_ID else "Reproduced"
        if confirmation_detected and "verification" in hmap:
            updates[hmap["verification"]] = new_verification
        current_status_for_confirmation = _get_row_value(row, hmap.get("status")) or "Submitted"
        if confirmation_detected and current_status_for_confirmation in {"Submitted", "Needs Verification", "Needs Info", "Triaged"} and "status" in hmap:
            updates[hmap["status"]] = "In Review"
        if confirmation_detected and "recommendation" in hmap:
            updates[hmap["recommendation"]] = _confirmed_recommendation(_get_row_value(row, hmap.get("affected_system")))
        if confirmation_detected and "next_step" in hmap:
            updates[hmap["next_step"]] = "Prepare Oracle brief from the ticket thread and gathered evidence."
        await self._update_cells(ws, row_num, updates)

        if confirmation_detected:
            admin_msg_id = _get_row_value(row, hmap.get("admin_message_id"))
            if admin_msg_id.isdigit():
                try:
                    admin_channel = self.bot.get_channel(ADMIN_REPORT_CHANNEL_ID) or await self.bot.fetch_channel(ADMIN_REPORT_CHANNEL_ID)
                    if isinstance(admin_channel, discord.TextChannel):
                        admin_msg = await admin_channel.fetch_message(int(admin_msg_id))
                        if admin_msg.embeds:
                            embed = admin_msg.embeds[0]
                            new_status = updates.get(hmap.get("status", -1), _get_row_value(row, hmap.get("status")))
                            new_recommendation = updates.get(hmap.get("recommendation", -1), _confirmed_recommendation(_get_row_value(row, hmap.get("affected_system"))))
                            embed.description = _ticket_description_for(new_verification, new_status)
                            embed.color = _ticket_color(_get_row_value(row, hmap.get("severity")), new_status)
                            for i, field in enumerate(embed.fields):
                                if field.name == "Verification":
                                    embed.set_field_at(i, name="Verification", value=new_verification, inline=True)
                                elif field.name == "Status":
                                    embed.set_field_at(i, name="Status", value=new_status, inline=True)
                                elif field.name == "Oracle Recommendation":
                                    embed.set_field_at(
                                        i,
                                        name="Oracle Recommendation",
                                        value=_truncate_field(new_recommendation),
                                        inline=False,
                                    )
                            await admin_msg.edit(embed=embed, view=self._ticket_view)
                            await self._update_workspace_embed(
                                _get_row_value(row, hmap.get("admin_thread_id")),
                                ticket_id,
                                new_status,
                                new_verification,
                                _get_row_value(row, hmap.get("severity")) or "Unknown",
                                new_recommendation,
                            )
                except Exception as e:
                    print(f"[Sentinel] Failed to update verification embed for {ticket_id}: {e}")

        visible = discord.Embed(
            title="✅ Evidence Logged" if not confirmation_detected else "✅ Confirmation Logged",
            description=_truncate_field(text or "Attachment-only evidence", 900),
            color=discord.Color.green() if confirmation_detected else discord.Color.blue(),
            timestamp=_now_utc(),
        )
        visible.add_field(name="Ticket", value=ticket_id or "Unknown", inline=True)
        visible.add_field(name="Logged By", value=message.author.mention, inline=True)
        if confirmation_detected:
            visible.add_field(name="Verification", value=new_verification, inline=True)
            visible.add_field(
                name="Next Step",
                value=(
                    "Use **Prepare Oracle Brief** on the ticket card to create a Hermes-ready handoff, then mention **The Oracle** in this thread for investigation. "
                    "No patch, deployment, restart, or completion is approved by confirmation alone."
                ),
                inline=False,
            )
        elif attachment_urls:
            visible.add_field(name="Attachment URLs", value=_truncate_field("\n".join(attachment_urls), 900), inline=False)
        await message.channel.send(embed=visible)
        try:
            await message.add_reaction("📝")
        except Exception:
            pass
        print(f"[Sentinel] Logged admin evidence for {ticket_id} from {message.author.id}")

    async def _collect_thread_facts(self, thread_id: str, limit: int = 30) -> str:
        if not thread_id or not str(thread_id).isdigit():
            return "No ticket thread available."
        thread = self.bot.get_channel(int(thread_id))
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(int(thread_id))
            except Exception:
                thread = None
        if not isinstance(thread, discord.Thread):
            return "Ticket thread could not be read."

        facts: List[str] = []
        try:
            async for msg in thread.history(limit=limit, oldest_first=True):
                if msg.author.bot and not msg.embeds:
                    continue
                parts: List[str] = []
                content = _clean_text(msg.content or "", limit=500)
                if content:
                    parts.append(content)
                for embed in msg.embeds:
                    if embed.title:
                        parts.append(f"[{embed.title}]")
                    if embed.description:
                        parts.append(_clean_text(embed.description, limit=700))
                    for field in embed.fields[:6]:
                        parts.append(f"{field.name}: {_clean_text(str(field.value), limit=500)}")
                if msg.attachments:
                    parts.append("Attachments: " + ", ".join(a.url for a in msg.attachments))
                if parts:
                    author = "Sentinel" if msg.author.bot else str(msg.author)
                    facts.append(f"- {author}: " + " | ".join(parts))
        except Exception as e:
            return f"Thread history could not be collected: {e}"
        return "\n".join(facts[-12:]) if facts else "No readable ticket thread facts found."

    # ----------------------------
    # Button / modal actions
    # ----------------------------
    async def handle_ticket_action(
        self,
        interaction: discord.Interaction,
        action: str,
        value: str,
        ticket_id_override: str = "",
        admin_message_id_override: Optional[int] = None,
    ) -> None:
        user_id = int(interaction.user.id)
        ticket_id = ticket_id_override or self._extract_ticket_id_from_interaction(interaction)
        if not ticket_id:
            await interaction.response.send_message("Unable to identify ticket for this action.", ephemeral=True)
            return

        is_primary = user_id == PRIMARY_APPROVER_ID
        is_whitelisted = await self._is_whitelisted_admin(user_id)

        if action in {"severity", "dismiss"}:
            if not is_whitelisted:
                await interaction.response.send_message("⛔ Only whitelisted bug admins may use this action.", ephemeral=True)
                return
        elif action == "status":
            if not is_primary:
                await interaction.response.send_message(f"⛔ Only <@{PRIMARY_APPROVER_ID}> may change ticket status.", ephemeral=True)
                return
        elif action == "oracle_brief":
            if not is_primary:
                await interaction.response.send_message(f"⛔ Only <@{PRIMARY_APPROVER_ID}> may prepare Oracle briefs.", ephemeral=True)
                return
        else:
            await interaction.response.send_message("Unknown Sentinel action.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            ws, headers, hmap, row, row_num = await self._find_ticket_by_message_or_thread(
                message_id=admin_message_id_override or (interaction.message.id if interaction.message else None)
            )
        except Exception as e:
            await interaction.followup.send(f"Ticket row not found: `{e}`", ephemeral=True)
            return

        old_severity = _get_row_value(row, hmap.get("severity"))
        old_status = _get_row_value(row, hmap.get("status"))
        updates: Dict[int, str] = {}
        thread_note_title = ""
        thread_note = ""
        thread_content: Optional[str] = None

        if action == "severity":
            new_value = value.strip().title()
            # Preserve Feature Request capitalization and unknown alternatives.
            for sev in SEVERITIES:
                if new_value.lower() == sev.lower():
                    new_value = sev
                    break
            if new_value not in SEVERITIES:
                await interaction.followup.send(f"Invalid severity. Use one of: {', '.join(SEVERITIES)}", ephemeral=True)
                return
            if "severity" in hmap:
                updates[hmap["severity"]] = new_value
            thread_note_title = "⚠️ Severity Updated"
            thread_note = f"Updated by: {interaction.user.mention}\nOld Severity: **{old_severity or 'Unknown'}**\nNew Severity: **{new_value}**"

        elif action == "dismiss":
            reason = _clean_text(value, limit=450)
            if "status" in hmap:
                updates[hmap["status"]] = "Dismissed"
            if "dismissed_by" in hmap:
                updates[hmap["dismissed_by"]] = f"{interaction.user} ({interaction.user.id})"
            if "dismissal_reason" in hmap:
                updates[hmap["dismissal_reason"]] = reason
            if "resolution_notes" in hmap:
                updates[hmap["resolution_notes"]] = _append_note(
                    _get_row_value(row, hmap["resolution_notes"]),
                    f"[{_now_iso()}] Dismissed by {interaction.user} ({interaction.user.id}): {reason}",
                )
            if "closed_at" in hmap:
                updates[hmap["closed_at"]] = _now_iso()
            thread_note_title = "🛑 Report Dismissed"
            thread_note = f"Dismissed by: {interaction.user.mention}\nReason: {reason}\n\nNo code changes were made."

        elif action == "status":
            new_value = value.strip().title()
            for st in STATUSES:
                if new_value.lower() == st.lower():
                    new_value = st
                    break
            if new_value not in STATUSES:
                await interaction.followup.send(f"Invalid status. Use one of: {', '.join(STATUSES)}", ephemeral=True)
                return
            if "status" in hmap:
                updates[hmap["status"]] = new_value
            if new_value in {"Completed", "Dismissed"} and "closed_at" in hmap:
                updates[hmap["closed_at"]] = _now_iso()
            thread_note_title = "📌 Status Updated"
            thread_note = f"Updated by: {interaction.user.mention}\nOld Status: **{old_status or 'Unknown'}**\nNew Status: **{new_value}**"

        elif action == "oracle_brief":
            if "status" in hmap:
                updates[hmap["status"]] = "In Review"
            affected_system = _get_row_value(row, hmap.get("affected_system"))
            current_summary = _get_row_value(row, hmap.get("summary")) or "No summary recorded."
            original_report = _get_row_value(row, hmap.get("original_report")) or current_summary
            raw_evidence = (
                _get_row_value(row, hmap.get("evidence_log"))
                or _get_row_value(row, hmap.get("admin_notes"))
                or _get_row_value(row, hmap.get("clarifying_answers"))
                or ""
            )
            attachments = _get_row_value(row, hmap.get("attachments")) or "No attachment URLs recorded."
            thread_id_for_facts = _get_row_value(row, hmap.get("admin_thread_id"))
            thread_facts = await self._collect_thread_facts(thread_id_for_facts)
            current_verification = _infer_verification_from_context(_get_row_value(row, hmap.get("verification")), thread_facts)
            evidence = _evidence_summary(raw_evidence, attachments, thread_facts)
            recommendation = _confirmed_recommendation(affected_system)
            if "verification" in hmap and current_verification.lower() != (_get_row_value(row, hmap.get("verification")) or "").lower():
                updates[hmap["verification"]] = current_verification
            if "recommendation" in hmap:
                updates[hmap["recommendation"]] = recommendation
            if "next_step" in hmap:
                updates[hmap["next_step"]] = "The Oracle has been mentioned in-thread with a concise investigation request. Await Oracle reasoning or gateway response."
            report_section = f"**Reporter Claim**\n{_truncate_field(original_report, 800)}\n\n"
            if not _same_meaning(original_report, current_summary):
                report_section += f"**Current Summary**\n{_truncate_field(current_summary, 600)}\n\n"
            oracle_mention = _oracle_mention()
            thread_content = f"{oracle_mention} Oracle investigation requested for **{ticket_id}**. Reason over the evidence below and propose next steps only."
            thread_note_title = f"🧠 Oracle Investigation Request — {ticket_id}"
            thread_note = (
                f"Prepared by: {interaction.user.mention}\n"
                f"Status: **In Review** | Verification: **{current_verification}** | System: **{affected_system or 'Unknown'}**\n\n"
                f"{report_section}"
                f"**Evidence & Reproduction**\n{_truncate_field(evidence, 1700)}\n\n"
                f"**Attachment / Screenshot Evidence**\n{_truncate_field(attachments, 600)}\n\n"
                "**Oracle Task**\n"
                "Investigate this Sentinel ticket from the facts above. Return:\n"
                "1. likely affected system/file/path\n"
                "2. most likely cause based on current evidence\n"
                "3. evidence quality and remaining unknowns\n"
                "4. recommended inspection steps\n"
                "5. what approval is required before any code or deployment action\n\n"
                f"**Recommended Direction**\n{recommendation}\n\n"
                "**Boundary**\nReasoning only. No code edits, commits, deployments, restarts, ticket closure, role changes, message deletions, or non-intake Sheet mutations without Oner approval."
            )

        if "updated_at" in hmap:
            updates[hmap["updated_at"]] = _now_iso()
        if "last_updated_by" in hmap:
            updates[hmap["last_updated_by"]] = f"{interaction.user} ({interaction.user.id})"

        await self._update_cells(ws, row_num, updates)

        # Update admin embed if possible by rebuilding lightweight fields in place.
        target_message = interaction.message if interaction.message and interaction.message.embeds and not admin_message_id_override else None
        if target_message is None:
            msg_id = admin_message_id_override or int(_get_row_value(row, hmap.get("admin_message_id")) or 0)
            if msg_id:
                try:
                    admin_channel = self.bot.get_channel(ADMIN_REPORT_CHANNEL_ID) or await self.bot.fetch_channel(ADMIN_REPORT_CHANNEL_ID)
                    if isinstance(admin_channel, discord.TextChannel):
                        target_message = await admin_channel.fetch_message(msg_id)
                except Exception as e:
                    print(f"[Sentinel] Failed to fetch admin message for {ticket_id}: {e}")

        if target_message and target_message.embeds:
            embed = target_message.embeds[0]
            current_status = updates.get(hmap.get("status", -1), old_status) if hmap else old_status
            current_sev = updates.get(hmap.get("severity", -1), old_severity) if hmap else old_severity
            embed.color = _ticket_color(current_sev or old_severity, current_status or old_status)
            for i, field in enumerate(embed.fields):
                if field.name == "Severity" and action == "severity":
                    embed.set_field_at(i, name="Severity", value=updates.get(hmap.get("severity", -1), old_severity), inline=True)
                if field.name == "Status" and action in {"status", "dismiss", "oracle_brief"}:
                    embed.set_field_at(i, name="Status", value=updates.get(hmap.get("status", -1), "Dismissed"), inline=True)
                if field.name == "Verification" and action == "oracle_brief":
                    embed.set_field_at(i, name="Verification", value=updates.get(hmap.get("verification", -1), _get_row_value(row, hmap.get("verification")) or "Unverified"), inline=True)
                if field.name == "Oracle Recommendation" and action == "oracle_brief":
                    embed.set_field_at(
                        i,
                        name="Oracle Recommendation",
                        value=_truncate_field(updates.get(hmap.get("recommendation", -1), _confirmed_recommendation(_get_row_value(row, hmap.get("affected_system"))))),
                        inline=False,
                    )
            if action in {"oracle_brief", "status", "dismiss"}:
                new_status_for_desc = updates.get(hmap.get("status", -1), old_status)
                new_verification_for_desc = updates.get(hmap.get("verification", -1), _get_row_value(row, hmap.get("verification")))
                embed.description = _ticket_description_for(new_verification_for_desc, new_status_for_desc)
            try:
                await target_message.edit(embed=embed, view=self._ticket_view)
            except Exception as e:
                print(f"[Sentinel] Failed to update admin embed for {ticket_id}: {e}")

        # Post audit note into thread.
        thread_id = _get_row_value(row, hmap.get("admin_thread_id"))
        if thread_id.isdigit():
            thread = self.bot.get_channel(int(thread_id))
            if thread is None:
                try:
                    thread = await self.bot.fetch_channel(int(thread_id))
                except Exception:
                    thread = None
            if isinstance(thread, discord.Thread):
                workspace_status = updates.get(hmap.get("status", -1), old_status or _get_row_value(row, hmap.get("status")) or "Unknown")
                workspace_verification = updates.get(hmap.get("verification", -1), _get_row_value(row, hmap.get("verification")) or "Unverified")
                workspace_severity = updates.get(hmap.get("severity", -1), old_severity or _get_row_value(row, hmap.get("severity")) or "Unknown")
                workspace_recommendation = updates.get(hmap.get("recommendation", -1), _get_row_value(row, hmap.get("recommendation")) or "Awaiting triage.")
                await self._update_workspace_embed(thread_id, ticket_id, workspace_status, workspace_verification, workspace_severity, workspace_recommendation)
                note_embed = discord.Embed(title=thread_note_title, description=_truncate_field(thread_note, 4000), color=discord.Color.dark_grey(), timestamp=_now_utc())
                await thread.send(
                    content=thread_content,
                    embed=note_embed,
                    allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
                )

        await interaction.followup.send(f"✅ Sentinel action recorded for **{ticket_id}**.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SentinelBugwatch(bot))
