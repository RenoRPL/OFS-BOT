print("=== LOADED banner_points.py v8.0 (UNIQUE REQUEST ID IN ALL EMBEDS + LOG) ===")
# -*- coding: utf-8 -*-
import os
import json
import secrets
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.google_auth import open_spreadsheet

# ----------------------------
# Sheets / tabs
# ----------------------------
MEMBER_LOG_SHEET = "Member Log"
BANNERS_POINTS_SHEET = "Banners points per user"
BANNER_POINTS_LOG_SHEET = "Banner points log"
GAMES_SHEET = "Games"

USER_ID_HEADER = "User ID"
BANNER_HEADER = "Banner"

# ----------------------------
# Config / persistence
# ----------------------------
CONFIG_PATH = "data/banner_points_config.json"
REQUESTS_PATH = "data/banner_points_requests.json"   # persists requests across restarts

# Permissions sheet config
PERMISSIONS_TAB = "Permissions for slash commands"

# Thumbnail image for DM embeds
BANNER_POINTS_THUMBNAIL = "https://drive.google.com/uc?export=view&id=12TZP67kKru_-sskUN5tnJjgXnudxUNe-"

LOG_HEADERS = [
    "Timestamp UTC",
    "Guild ID",
    "Review Message ID",
    "Review Channel ID",
    "Event",                # Submitted / Edited / Approved / Denied
    "Game",
    "Target User ID",
    "Target Tag",
    "Requester User ID",
    "Requester Tag",
    "Banner",
    "Points (Current)",
    "Delta Applied",
    "Old Total",
    "New Total",
    "Reason",
    "Reviewer User ID",
    "Reviewer Tag",
    "Request ID",
]

# ----------------------------
# Helpers
# ----------------------------
def _ensure_dir(path: str) -> None:
    folder = os.path.dirname(path)
    if folder and not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)

def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

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

def _status_color(status: str) -> discord.Color:
    s = (status or "").lower()
    if "denied" in s:
        return discord.Color.red()
    if "approved" in s:
        return discord.Color.green()
    if "edit" in s:
        return discord.Color.blue()
    if "pending" in s:
        return discord.Color.dark_grey()
    return discord.Color.dark_grey()

def _generate_request_id() -> str:
    """Generate a unique request ID in format BP-XXXXXXXX (8 hex chars)."""
    return f"BP-{secrets.token_hex(4).upper()}"

def _apply_request_footer(embed: discord.Embed, request_id: str) -> None:
    """Apply the request ID footer to an embed."""
    if request_id:
        embed.set_footer(text=f"Request ID: {request_id}")

def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def _save_json(path: str, data: dict) -> None:
    _ensure_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def _has_role(member: discord.Member, role_name: str) -> bool:
    # exact match
    return any(r.name == role_name for r in member.roles)

# ----------------------------
# Sheet-based permission helpers
# ----------------------------
def _split_role_csv(value: str) -> set[str]:
    """Converts 'Commander, Primarch,' into {'commander','primarch'} (lowercased, trimmed)."""
    if not value:
        return set()
    return {r.strip().lower() for r in value.split(",") if r.strip()}

def _member_has_any_role_name(member: discord.Member, allowed_lower: set[str]) -> bool:
    """Returns True if member has any role whose name matches allowed_lower (case-insensitive)."""
    if not allowed_lower:
        return False
    for role in member.roles:
        if role.name.lower() in allowed_lower:
            return True
    return False

def _get_allowed_roles_from_permissions_sheet(sheet, slash_command: str, column_letter: str) -> set[str]:
    """
    Reads the 'Permissions for slash commands' tab.
    Finds row where column A matches slash_command (e.g. '/banner_points').
    Returns parsed roles from the specified column (C or D), lowercased.
    Safe if sheet/tab/row missing or cell empty -> returns empty set.
    """
    if not sheet:
        return set()
    try:
        ws = sheet.worksheet(PERMISSIONS_TAB)
        data = ws.get_all_values()
    except Exception:
        return set()

    if not data:
        return set()

    # Column letter to index: A=0, B=1, C=2, D=3
    col_index = ord(column_letter.upper()) - ord('A')

    slash_lower = slash_command.strip().lower()
    for row in data:
        if not row:
            continue
        cell_a = (row[0] if len(row) > 0 else "").strip().lower()
        if cell_a == slash_lower:
            cell_value = row[col_index] if col_index < len(row) else ""
            return _split_role_csv(cell_value)

    return set()

# ----------------------------
# Config (review channel)
# ----------------------------
def _load_config() -> dict:
    return _load_json(CONFIG_PATH)

def _save_config(cfg: dict) -> None:
    _save_json(CONFIG_PATH, cfg)

def _get_review_channel_id(guild_id: int) -> Optional[int]:
    cfg = _load_config()
    cid = cfg.get(str(guild_id), {}).get("review_channel_id")
    return int(cid) if cid else None

def _set_review_channel_id(guild_id: int, channel_id: int) -> None:
    cfg = _load_config()
    cfg.setdefault(str(guild_id), {})
    cfg[str(guild_id)]["review_channel_id"] = int(channel_id)
    _save_config(cfg)

# ----------------------------
# Persistent request store
# ----------------------------
def _requests_load() -> dict:
    return _load_json(REQUESTS_PATH)

def _requests_save(store: dict) -> None:
    _save_json(REQUESTS_PATH, store)

def _requests_put(review_message_id: int, payload: dict) -> None:
    store = _requests_load()
    store[str(review_message_id)] = payload
    _requests_save(store)

def _requests_get(review_message_id: int) -> Optional[dict]:
    store = _requests_load()
    return store.get(str(review_message_id))

# ----------------------------
# Views / Modals
# ----------------------------
class ReviewChannelSetupView(discord.ui.View):
    """Shown when review channel isn't set. Lets an admin pick a channel."""
    def __init__(self, guild_id: int, requester_id: int, timeout: int = 120):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.requester_id = requester_id

        self.select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="Select the Banner Points review channel...",
            min_values=1,
            max_values=1,
        )
        self.select.callback = self._on_select  # type: ignore
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user is not None and interaction.user.id == self.requester_id

    async def _on_select(self, interaction: discord.Interaction):
        channel = self.select.values[0]
        _set_review_channel_id(self.guild_id, channel.id)

        embed = discord.Embed(
            title="Review Channel Saved",
            description=f"Banner Points requests will be sent to {channel.mention}.",
        )
        await interaction.response.edit_message(embed=embed, view=None)

class BannerPointsReviewView(discord.ui.View):
    def __init__(
        self,
        cog: "BannerPoints",
        *,
        requester_id: int,
        requester_tag: str,
        target_id: int,
        target_tag: str,
        game: str,
        reason: str,
        points: int,
        banner: str,
        applied_old_total: int,
        applied_new_total: int,
        applied_delta: int,
        review_channel_id: int,
        review_message_id: int,
        target_dm_message_id: Optional[int] = None,
        final_status: str = "",  # "", "Approved", "Denied"
        request_id: str = "",
        timeout: int = 60 * 60,
    ):
        super().__init__(timeout=timeout)
        self.cog = cog

        self.requester_id = int(requester_id)
        self.requester_tag = requester_tag
        self.target_id = int(target_id)
        self.target_tag = target_tag

        self.game = game
        self.reason = reason
        self.points = int(points)
        self.banner = banner

        self.applied_old_total = int(applied_old_total)
        self.applied_new_total = int(applied_new_total)
        self.applied_delta = int(applied_delta)  # how much THIS request currently applies (0 after deny)

        self.review_channel_id = int(review_channel_id)
        self.review_message_id = int(review_message_id)

        # We only store the DM message id; we will re-open DM via user.create_dm() (works after restart)
        self.target_dm_message_id = target_dm_message_id

        self.final_status = final_status  # "", "Approved", "Denied"
        self.request_id = request_id or _generate_request_id()  # Generate if not provided

    # ---------- persistence ----------
    def to_payload(self) -> dict:
        return {
            "requester_id": self.requester_id,
            "requester_tag": self.requester_tag,
            "target_id": self.target_id,
            "target_tag": self.target_tag,
            "game": self.game,
            "reason": self.reason,
            "points": self.points,
            "banner": self.banner,
            "applied_old_total": self.applied_old_total,
            "applied_new_total": self.applied_new_total,
            "applied_delta": self.applied_delta,
            "review_channel_id": self.review_channel_id,
            "review_message_id": self.review_message_id,
            "target_dm_message_id": self.target_dm_message_id,
            "final_status": self.final_status,
            "request_id": self.request_id,
        }

    def persist(self) -> None:
        _requests_put(self.review_message_id, self.to_payload())

    # ---------- embeds ----------
    def build_review_embed(self, status: str = "Pending") -> discord.Embed:
        status_lower = (status or "").lower()

        if "approved" in status_lower:
            e = discord.Embed(title="Approved", color=discord.Color.green())
        elif "denied" in status_lower or "reverted" in status_lower:
            e = discord.Embed(title="Denied", color=discord.Color.red())
        elif "edited" in status_lower:
            e = discord.Embed(title="Edited", color=discord.Color.blue())
        else:
            e = discord.Embed(
                title="Banner Points Request",
                description=f"Status: **{status}**\nApplied to Sheets (pending): **{self.applied_delta:+}**",
                color=discord.Color.dark_grey(),
            )

        e.add_field(name="Requesting User", value=f"<@{self.requester_id}>", inline=False)
        e.add_field(name="User Getting Points", value=f"<@{self.target_id}>", inline=False)
        e.add_field(name="Game", value=self.game, inline=True)
        e.add_field(name="Points", value=str(self.points), inline=True)
        e.add_field(name="Banner", value=self.banner or "Unassigned", inline=True)
        e.add_field(name="Reason / Notes", value=self.reason or "None provided", inline=False)
        e.add_field(name="Total (after apply)", value=str(self.applied_new_total), inline=False)
        e.set_thumbnail(url=BANNER_POINTS_THUMBNAIL)
        _apply_request_footer(e, self.request_id)
        return e

    async def _finalize_message(self, message: discord.Message, embed: discord.Embed):
        # IMPORTANT: remove the buttons entirely so they cannot be clicked again
        await message.edit(embed=embed, view=None)

        # Keep request in store so the context menu can still open it later.
        self.persist()

    async def _edit_target_dm(self, status: str, total: int):
        """
        Edit the original target DM message.
        We do NOT rely on cached DMChannel IDs (those break after restart).
        """
        if not self.target_dm_message_id:
            return

        try:
            user = self.cog.bot.get_user(self.target_id)
            if user is None:
                user = await self.cog.bot.fetch_user(self.target_id)

            dm = await user.create_dm()
            msg = await dm.fetch_message(int(self.target_dm_message_id))

            embed = discord.Embed(
                title=f"Banner Points Requested - {status}",
                description=(
                    f"A banner points request has been submitted for you.\n"
                    f"**Game:** {self.game}\n"
                    f"**Points:** {self.points} under **{self.banner}**\n"
                    f"**Total Banner Points:** {total}\n"
                    f"**Reason:** {self.reason or 'None provided'}\n"
                    f"**Status:** {status}"
                ),
                color=_status_color(status),
            )
            embed.set_thumbnail(url=BANNER_POINTS_THUMBNAIL)
            _apply_request_footer(embed, self.request_id)
            await msg.edit(embed=embed)
        except Exception:
            pass

    # ---------- buttons ----------
    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if self.final_status:
            return

        guild = interaction.guild
        if guild is None:
            return

        reviewer = interaction.user if isinstance(interaction.user, discord.Member) else None

        # Approve does NOT change points (already applied on submit)
        self.final_status = "Approved"
        self.persist()

        await self._edit_target_dm("Approved", self.applied_new_total)

        self.cog._log_event(
            guild_id=guild.id,
            review_message_id=self.review_message_id,
            review_channel_id=self.review_channel_id,
            event="Approved",
            game=self.game,
            target_id=self.target_id,
            target_tag=self.target_tag,
            requester_id=self.requester_id,
            requester_tag=self.requester_tag,
            banner=self.banner,
            points_current=self.points,
            delta_applied=0,
            old_total=self.applied_new_total,
            new_total=self.applied_new_total,
            reason=self.reason,
            reviewer=reviewer,
            request_id=self.request_id,
        )

        embed = self.build_review_embed(status="Approved (final)")
        await self._finalize_message(interaction.message, embed)

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.final_status:
            return await interaction.response.send_message(
                "This request is finalized. Use the right-click menu: **Open Request for Banner Points**.",
                ephemeral=True,
            )
        await interaction.response.send_modal(EditBannerPointsModal(self))

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if self.final_status:
            return

        guild = interaction.guild
        if guild is None:
            return

        reviewer = interaction.user if isinstance(interaction.user, discord.Member) else None

        if not self.cog.sheet:
            return

        # Deny reverts the applied delta
        try:
            reverted_total, old_total = self.cog._upsert_banner_points(
                str(self.target_id),
                self.banner,
                -int(self.applied_delta),
            )
        except Exception:
            return

        self.cog._log_event(
            guild_id=guild.id,
            review_message_id=self.review_message_id,
            review_channel_id=self.review_channel_id,
            event="Denied",
            game=self.game,
            target_id=self.target_id,
            target_tag=self.target_tag,
            requester_id=self.requester_id,
            requester_tag=self.requester_tag,
            banner=self.banner,
            points_current=self.points,
            delta_applied=-int(self.applied_delta),
            old_total=old_total,
            new_total=reverted_total,
            reason=self.reason,
            reviewer=reviewer,
            request_id=self.request_id,
        )

        self.applied_new_total = int(reverted_total)
        self.applied_delta = 0
        self.final_status = "Denied"
        self.persist()

        await self._edit_target_dm("Denied", self.applied_new_total)

        embed = self.build_review_embed(status="Denied (reverted)")
        await self._finalize_message(interaction.message, embed)

class EditBannerPointsModal(discord.ui.Modal, title="Edit Banner Points Request"):
    points = discord.ui.TextInput(label="Points (1-3)", placeholder="Enter 1, 2, or 3", required=True, max_length=1)
    reason = discord.ui.TextInput(
        label="Reason / Notes (optional)",
        placeholder="Update the reason for points…",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, parent_view: BannerPointsReviewView):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            new_points = int(str(self.points.value).strip())
        except Exception:
            return await interaction.followup.send("Points must be 1, 2, or 3.", ephemeral=True)
        if new_points not in (1, 2, 3):
            return await interaction.followup.send("Points must be 1, 2, or 3.", ephemeral=True)

        new_reason = (self.reason.value or "").strip()

        old_points = int(self.parent_view.points)
        delta = int(new_points) - old_points

        guild = interaction.guild
        if guild is None:
            return await interaction.followup.send("Guild not found.", ephemeral=True)

        reviewer = interaction.user if isinstance(interaction.user, discord.Member) else None

        old_total_for_log = int(self.parent_view.applied_new_total)
        new_total_for_log = int(self.parent_view.applied_new_total)

        if delta != 0:
            if not self.parent_view.cog.sheet:
                return await interaction.followup.send("❌ Google Sheets not available.", ephemeral=True)
            try:
                new_total, old_total_sheet = self.parent_view.cog._upsert_banner_points(
                    str(self.parent_view.target_id),
                    self.parent_view.banner,
                    delta,
                )
                self.parent_view.applied_new_total = int(new_total)
                self.parent_view.applied_delta = int(self.parent_view.applied_delta) + int(delta)
                old_total_for_log = int(old_total_sheet)
                new_total_for_log = int(new_total)
            except Exception as e:
                return await interaction.followup.send(f"❌ Failed to update points: `{e}`", ephemeral=True)

        self.parent_view.points = int(new_points)
        if new_reason:
            self.parent_view.reason = new_reason

        self.parent_view.cog._log_event(
            guild_id=guild.id,
            review_message_id=self.parent_view.review_message_id,
            review_channel_id=self.parent_view.review_channel_id,
            event="Edited",
            game=self.parent_view.game,
            target_id=self.parent_view.target_id,
            target_tag=self.parent_view.target_tag,
            requester_id=self.parent_view.requester_id,
            requester_tag=self.parent_view.requester_tag,
            banner=self.parent_view.banner,
            points_current=int(new_points),
            delta_applied=int(delta),
            old_total=int(old_total_for_log),
            new_total=int(new_total_for_log),
            reason=self.parent_view.reason,
            reviewer=reviewer,
            request_id=self.parent_view.request_id,
        )

        await self.parent_view._edit_target_dm("Edited", self.parent_view.applied_new_total)

        # Mark as finalized so buttons are permanently removed
        self.parent_view.final_status = "Edited"

        # Persist updated state (important for restart + context menu)
        self.parent_view.persist()

        # Remove buttons permanently (view=None); future edits via context menu only
        embed = self.parent_view.build_review_embed(status="Edited (final)")
        try:
            await interaction.message.edit(embed=embed, view=None)
        except Exception:
            pass

        await interaction.followup.send("✅ Request updated. Further changes via right-click menu only.", ephemeral=True)

class ContextEditModal(discord.ui.Modal, title="Open Request for Banner Points"):
    points = discord.ui.TextInput(label="Points (1-3)", placeholder="Enter 1, 2, or 3", required=True, max_length=1)
    reason = discord.ui.TextInput(
        label="Reason / Notes (optional)",
        placeholder="Update the reason for points…",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, cog: "BannerPoints", message: discord.Message, payload: dict):
        super().__init__()
        self.cog = cog
        self.message = message
        self.payload = payload

        # Pre-fill fields when possible
        try:
            self.points.default = str(payload.get("points", "1"))
            self.reason.default = str(payload.get("reason", ""))[:500]
        except Exception:
            pass

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None:
            return await interaction.followup.send("This must be used in a server.", ephemeral=True)

        # If it was denied, we do not allow reopening/changes that would re-apply points.
        if (self.payload.get("final_status") or "").lower() == "denied":
            return await interaction.followup.send("This request is **Denied** and can’t be edited.", ephemeral=True)

        try:
            new_points = int(str(self.points.value).strip())
        except Exception:
            return await interaction.followup.send("Points must be 1, 2, or 3.", ephemeral=True)
        if new_points not in (1, 2, 3):
            return await interaction.followup.send("Points must be 1, 2, or 3.", ephemeral=True)

        new_reason = (self.reason.value or "").strip()

        view = self.cog.view_from_payload(self.payload)

        old_points = int(view.points)
        delta = int(new_points) - old_points

        reviewer = interaction.user if isinstance(interaction.user, discord.Member) else None
        old_total_for_log = int(view.applied_new_total)
        new_total_for_log = int(view.applied_new_total)

        # Only allow point edits if it’s not denied (approved or pending)
        if delta != 0:
            if not self.cog.sheet:
                return await interaction.followup.send("❌ Google Sheets not available.", ephemeral=True)
            try:
                new_total, old_total_sheet = self.cog._upsert_banner_points(str(view.target_id), view.banner, delta)
                view.applied_new_total = int(new_total)
                view.applied_delta = int(view.applied_delta) + int(delta)
                old_total_for_log = int(old_total_sheet)
                new_total_for_log = int(new_total)
            except Exception as e:
                return await interaction.followup.send(f"❌ Failed to update points: `{e}`", ephemeral=True)

        view.points = int(new_points)
        if new_reason:
            view.reason = new_reason

        # Always mark as Edited when changed via context menu (overrides Approved/Pending)
        view.final_status = "Edited"
        await view._edit_target_dm("Edited", view.applied_new_total)

        self.cog._log_event(
            guild_id=interaction.guild.id,
            review_message_id=view.review_message_id,
            review_channel_id=view.review_channel_id,
            event="Edited",
            game=view.game,
            target_id=view.target_id,
            target_tag=view.target_tag,
            requester_id=view.requester_id,
            requester_tag=view.requester_tag,
            banner=view.banner,
            points_current=int(new_points),
            delta_applied=int(delta),
            old_total=int(old_total_for_log),
            new_total=int(new_total_for_log),
            reason=view.reason,
            reviewer=reviewer,
            request_id=view.request_id,
        )

        # Persist updated state back to disk
        view.persist()

        # IMPORTANT: keep view=None so buttons never "come back" from context menu edits
        embed = view.build_review_embed(status="Edited (final)")
        await self.message.edit(embed=embed, view=None)

        await interaction.followup.send("✅ Request updated.", ephemeral=True)

# ----------------------------
# Cog
# ----------------------------
class BannerPoints(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # LAZY: sheet is opened on first use (in a thread), not at startup
        self.sheet = None

    def _ensure_sheet(self):
        """Lazy-open spreadsheet on first use (runs in a thread, never at startup)."""
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

    def _ensure_log_sheet_headers(self) -> None:
        if not self._ensure_sheet():
            return
        try:
            ws = self.sheet.worksheet(BANNER_POINTS_LOG_SHEET)
        except Exception:
            try:
                ws = self.sheet.add_worksheet(title=BANNER_POINTS_LOG_SHEET, rows=2000, cols=len(LOG_HEADERS) + 2)
            except Exception:
                return
        try:
            existing = ws.row_values(1)
            if not existing or all(str(x).strip() == "" for x in existing):
                ws.update("A1", [LOG_HEADERS])
        except Exception:
            pass

    def _log_event(
        self,
        *,
        guild_id: int,
        review_message_id: int,
        review_channel_id: int,
        event: str,
        game: str,
        target_id: int,
        target_tag: str,
        requester_id: int,
        requester_tag: str,
        banner: str,
        points_current: int,
        delta_applied: int,
        old_total: int,
        new_total: int,
        reason: str,
        reviewer: Optional[discord.Member],
        request_id: str = "",
    ) -> None:
        if not self._ensure_sheet():
            return
        try:
            ws = self.sheet.worksheet(BANNER_POINTS_LOG_SHEET)
        except Exception:
            return

        reviewer_id = str(reviewer.id) if reviewer else ""
        reviewer_tag = str(reviewer) if reviewer else ""

        row = [
            _utc_iso_now(),
            str(guild_id),
            str(review_message_id),
            str(review_channel_id),
            str(event),
            str(game),
            str(target_id),
            str(target_tag),
            str(requester_id),
            str(requester_tag),
            str(banner or ""),
            str(int(points_current)),
            str(int(delta_applied)),
            str(int(old_total)),
            str(int(new_total)),
            str(reason or ""),
            reviewer_id,
            reviewer_tag,
            str(request_id or ""),
        ]
        try:
            ws.append_row(row, value_input_option="USER_ENTERED")
        except Exception:
            pass

    def _get_user_banner_from_member_log(self, user_id: str) -> Optional[str]:
        ws = self._worksheet(MEMBER_LOG_SHEET)
        if not ws:
            return None

        values = ws.get_all_values()
        if len(values) < 2:
            return None

        headers = values[0]
        uid_col = self._find_header_index(headers, USER_ID_HEADER)
        banner_col = self._find_header_index(headers, BANNER_HEADER)
        if uid_col == -1 or banner_col == -1:
            return None

        for row in values[1:]:
            uid = (row[uid_col] if uid_col < len(row) else "").strip()
            if uid == user_id:
                banner = (row[banner_col] if banner_col < len(row) else "").strip()
                return banner or None
        return None

    def _upsert_banner_points(self, user_id: str, banner_name: str, delta_points: int) -> tuple[int, int]:
        ws = self._worksheet(BANNERS_POINTS_SHEET)
        if not ws:
            raise RuntimeError("Banners points per user worksheet not found")

        data = ws.get_all_values()
        if not data:
            raise RuntimeError("Banners points per user sheet is empty")

        header_row = data[0]
        banner_col_idx = self._find_header_index(header_row, banner_name)
        if banner_col_idx == -1:
            raise RuntimeError(f"Banner '{banner_name}' not found in row 1 headers of '{BANNERS_POINTS_SHEET}'")

        user_row_idx = -1
        for i in range(1, len(data)):
            uid = (data[i][0] if len(data[i]) > 0 else "").strip()
            if uid == user_id:
                user_row_idx = i
                break

        if user_row_idx == -1:
            ws.append_row([user_id], value_input_option="USER_ENTERED")
            data = ws.get_all_values()
            user_row_idx = len(data) - 1

        row = data[user_row_idx]
        old_val = _to_int(row[banner_col_idx] if banner_col_idx < len(row) else 0)
        new_val = old_val + int(delta_points)

        ws.update_cell(user_row_idx + 1, banner_col_idx + 1, new_val)
        return new_val, old_val

    def _get_active_games(self) -> list[str]:
        """Active game list from Games tab where Banners checkbox = TRUE"""
        if not self._ensure_sheet():
            return []
        try:
            ws = self.sheet.worksheet(GAMES_SHEET)
            data = ws.get_all_values()
            if len(data) < 2:
                return []

            header_row = data[0]
            game_col_idx = self._find_header_index(header_row, "Games")
            banner_col_idx = self._find_header_index(header_row, "Banners")
            if game_col_idx == -1 or banner_col_idx == -1:
                return []

            seen = set()
            active = []
            for row in data[1:]:
                if len(row) <= max(game_col_idx, banner_col_idx):
                    continue
                game_name = row[game_col_idx].strip()
                flag = row[banner_col_idx].strip().upper()
                if flag == "TRUE" and game_name and game_name not in seen:
                    seen.add(game_name)
                    active.append(game_name)
            return active
        except Exception:
            return []

    async def game_autocomplete(self, interaction: discord.Interaction, current: str):
        games = self._get_active_games()
        if not current:
            return [app_commands.Choice(name=g, value=g) for g in games[:25]]
        cur = current.lower()
        matches = [g for g in games if cur in g.lower()]
        return [app_commands.Choice(name=g, value=g) for g in matches[:25]]

    async def safe_dm(self, member: discord.Member, embed: discord.Embed) -> Optional[int]:
        """Returns dm_message_id if sent."""
        try:
            msg = await member.send(embed=embed)
            return msg.id if msg else None
        except Exception:
            return None

    def view_from_payload(self, payload: dict) -> BannerPointsReviewView:
        # Backward compatibility: generate request_id if missing from old payloads
        request_id = payload.get("request_id", "")
        if not request_id:
            request_id = _generate_request_id()
            # Persist the newly generated ID back to the store
            payload["request_id"] = request_id
            review_msg_id = int(payload.get("review_message_id", 0))
            if review_msg_id:
                _requests_put(review_msg_id, payload)

        return BannerPointsReviewView(
            self,
            requester_id=payload["requester_id"],
            requester_tag=payload.get("requester_tag", ""),
            target_id=payload["target_id"],
            target_tag=payload.get("target_tag", ""),
            game=payload.get("game", ""),
            reason=payload.get("reason", ""),
            points=int(payload.get("points", 1)),
            banner=payload.get("banner", ""),
            applied_old_total=int(payload.get("applied_old_total", 0)),
            applied_new_total=int(payload.get("applied_new_total", 0)),
            applied_delta=int(payload.get("applied_delta", 0)),
            review_channel_id=int(payload.get("review_channel_id", 0)),
            review_message_id=int(payload.get("review_message_id", 0)),
            target_dm_message_id=payload.get("target_dm_message_id"),
            final_status=payload.get("final_status", ""),
            request_id=request_id,
        )

    # ----------------------------
    # Admin set channel
    # ----------------------------
    @app_commands.command(
        name="set_banner_points_review_channel",
        description="Admin: Set the channel where Banner Points requests are reviewed."
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(channel="Channel to receive Banner Points review requests")
    async def set_banner_points_review_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            return await interaction.response.send_message("This must be used in a server.", ephemeral=True)
        _set_review_channel_id(interaction.guild.id, channel.id)
        await interaction.response.send_message(f"✅ Review channel set to {channel.mention}", ephemeral=True)

    # ----------------------------
    # Main command
    # ----------------------------
    @app_commands.command(name="banner_points", description="Submit a Banner Points request for review.")
    @app_commands.describe(
        game="Game / match reference",
        user="User receiving points",
        reason="Reason / notes",
        points="How many points (1-3)",
    )
    @app_commands.autocomplete(game=game_autocomplete)
    @app_commands.choices(points=[
        app_commands.Choice(name="1", value=1),
        app_commands.Choice(name="2", value=2),
        app_commands.Choice(name="3", value=3),
    ])
    async def banner_points(self, interaction: discord.Interaction, game: str, user: discord.Member, reason: str, points: int):
        if interaction.guild is None:
            return await interaction.response.send_message("This must be used in a server.", ephemeral=True)

        if not self._ensure_sheet():
            return await interaction.response.send_message("❌ Google Sheets not available.", ephemeral=True)

        # Permission check: read allowed roles from column C of Permissions sheet
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            return await interaction.response.send_message("No member context.", ephemeral=True)

        allowed = _get_allowed_roles_from_permissions_sheet(self.sheet, "/banner_points", "C")
        if not _member_has_any_role_name(member, allowed):
            return await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True
            )

        review_channel_id = _get_review_channel_id(interaction.guild.id)
        if not review_channel_id:
            member = interaction.user if isinstance(interaction.user, discord.Member) else None
            is_adminish = bool(member and (member.guild_permissions.administrator or member.guild_permissions.manage_guild))
            if is_adminish:
                embed = discord.Embed(
                    title="Banner Points Review Channel Not Set",
                    description="Select the channel where Banner Points requests should be sent.",
                )
                return await interaction.response.send_message(
                    embed=embed,
                    view=ReviewChannelSetupView(interaction.guild.id, interaction.user.id),
                    ephemeral=True,
                )
            return await interaction.response.send_message("❌ Review channel not set.", ephemeral=True)

        review_channel = interaction.guild.get_channel(review_channel_id)
        if not isinstance(review_channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Saved review channel is invalid.", ephemeral=True)

        banner = self._get_user_banner_from_member_log(str(user.id))
        if not banner:
            return await interaction.response.send_message(f"❌ No Banner found for {user.mention}.", ephemeral=True)

        # Generate unique request ID at creation time
        request_id = _generate_request_id()

        # Apply immediately (pending review)
        try:
            applied_new_total, applied_old_total = self._upsert_banner_points(str(user.id), banner, int(points))
        except Exception as e:
            return await interaction.response.send_message(f"❌ Failed to apply points: `{e}`", ephemeral=True)

        # Ephemeral confirm to requester
        submit_embed = discord.Embed(title="You have submitted Banner Points Request", color=discord.Color.dark_grey())
        submit_embed.add_field(name="For User - Points requested", value=f"{user.mention} - **{points}**", inline=False)
        submit_embed.add_field(name="Under Banner", value=banner, inline=False)
        submit_embed.add_field(name="Total Banner Points (after apply)", value=str(applied_new_total), inline=False)
        submit_embed.set_thumbnail(url=BANNER_POINTS_THUMBNAIL)
        _apply_request_footer(submit_embed, request_id)
        await interaction.response.send_message(embed=submit_embed, ephemeral=True)

        # DM target immediately (PENDING - gray)
        dm_embed = discord.Embed(
            title="Banner Points Requested - Pending",
            description=(
                f"A banner points request has been submitted for you.\n"
                f"**Game:** {game}\n"
                f"**Points:** {points} under **{banner}**\n"
                f"**Total Banner Points:** {applied_new_total}\n"
                f"**Reason:** {reason or 'None provided'}\n"
                f"**Status:** Pending"
            ),
            color=_status_color("pending"),
        )
        dm_embed.set_thumbnail(url=BANNER_POINTS_THUMBNAIL)
        _apply_request_footer(dm_embed, request_id)
        dm_message_id = await self.safe_dm(user, dm_embed)

        # Create review message placeholder first
        review_msg = await review_channel.send(embed=discord.Embed(title="Banner Points Request", description="Status: **Pending**"))

        requester_tag = str(interaction.user)
        target_tag = str(user)

        view = BannerPointsReviewView(
            self,
            requester_id=interaction.user.id,
            requester_tag=requester_tag,
            target_id=user.id,
            target_tag=target_tag,
            game=game,
            reason=reason,
            points=int(points),
            banner=banner,
            applied_old_total=applied_old_total,
            applied_new_total=applied_new_total,
            applied_delta=int(points),
            review_channel_id=review_channel.id,
            review_message_id=review_msg.id,
            target_dm_message_id=dm_message_id,
            final_status="",
            request_id=request_id,
        )

        # Persist request so context menu works after restart (and after approve/deny)
        view.persist()

        # Log submitted
        self._log_event(
            guild_id=interaction.guild.id,
            review_message_id=review_msg.id,
            review_channel_id=review_channel.id,
            event="Submitted",
            game=game,
            target_id=user.id,
            target_tag=target_tag,
            requester_id=interaction.user.id,
            requester_tag=requester_tag,
            banner=banner,
            points_current=int(points),
            delta_applied=int(points),
            old_total=applied_old_total,
            new_total=applied_new_total,
            reason=reason,
            reviewer=None,
            request_id=request_id,
        )

        await review_msg.edit(embed=view.build_review_embed(status="Pending"), view=view)

# ----------------------------
# Context menu (module level) ✅ NOT inside the class
# Permission controlled via "Permissions for slash commands" sheet column D
# ----------------------------
@app_commands.context_menu(name="Open Request for Banner Points")
async def open_request_for_banner_points(interaction: discord.Interaction, message: discord.Message):
    if interaction.guild is None:
        return await interaction.response.send_message("This must be used in a server.", ephemeral=True)

    bot = interaction.client
    if not isinstance(bot, commands.Bot):
        return await interaction.response.send_message("Bot not available.", ephemeral=True)

    cog = bot.get_cog("BannerPoints")
    if not isinstance(cog, BannerPoints):
        return await interaction.response.send_message("BannerPoints cog not loaded.", ephemeral=True)

    review_channel_id = _get_review_channel_id(interaction.guild.id)
    if not review_channel_id or message.channel.id != int(review_channel_id):
        return await interaction.response.send_message("This is not a Banner Points request message.", ephemeral=True)

    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if not member:
        return await interaction.response.send_message("No member context.", ephemeral=True)

    # Permission check: read allowed roles from column D of Permissions sheet
    allowed = _get_allowed_roles_from_permissions_sheet(cog.sheet, "/banner_points", "D")
    if not _member_has_any_role_name(member, allowed):
        return await interaction.response.send_message(
            "This action is restricted to Banner Points reviewers.", ephemeral=True
        )

    payload = _requests_get(message.id)
    if not payload:
        return await interaction.response.send_message(
            "I can’t open this request because it wasn’t found in the request store. "
            "If this message existed before v7.7, re-create the request once so it can be persisted.",
            ephemeral=True
        )

    # Always ensure buttons are removed (so they don't “come back”)
    try:
        await message.edit(view=None)
    except Exception:
        pass

    await interaction.response.send_modal(ContextEditModal(cog, message, payload))

# ----------------------------
# Setup
# ----------------------------
async def setup(bot: commands.Bot):
    await bot.add_cog(BannerPoints(bot))

    # Ensure we don't duplicate the context menu on reload
    try:
        bot.tree.remove_command("Open Request for Banner Points", type=discord.AppCommandType.message)
    except Exception:
        pass
    bot.tree.add_command(open_request_for_banner_points)
