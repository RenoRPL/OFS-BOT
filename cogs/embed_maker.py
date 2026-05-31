# -*- coding: utf-8 -*-
# cogs/embed_maker.py
import discord
from discord.ext import commands
from discord import app_commands
from dataclasses import dataclass
from typing import Optional
import datetime
import json
import os

print("=== LOADED embed_maker.py (MESSAGE CONTEXT: EDIT EMBED + UPDATE IN PLACE) ===")

# ---------- tiny helpers ----------
def _has_text(s: Optional[str]) -> bool:
    return bool(s and str(s).strip())


def _int_to_hex_color(value: Optional[int]) -> str:
    try:
        if value is None:
            return ""
        return f"#{int(value) & 0xFFFFFF:06X}"
    except Exception:
        return ""


def _extract_first_link_button(message: discord.Message) -> tuple[str, str]:
    """
    Best-effort extraction of the first URL button from a message.
    Returns (label, url) or ("","") if none.
    """
    try:
        for row in getattr(message, "components", []) or []:
            # discord.ActionRow has .children in newer discord.py
            children = getattr(row, "children", None)
            if children is None:
                # sometimes it's a list-like
                children = row

            for comp in children or []:
                url = getattr(comp, "url", None)
                label = getattr(comp, "label", None)
                if url:
                    return (str(label or "Link"), str(url))
    except Exception:
        pass
    return ("", "")


def _draft_from_message(msg: discord.Message) -> "EmbedDraft":
    """
    Build a draft from an existing Discord message.
    - If message has an embed, draft.message becomes embed.description and mention_text becomes message.content
    - If message has no embed, draft.message becomes message.content and mention_text is cleared (avoids double content)
    """
    draft = EmbedDraft()

    # Message-level content
    content = (msg.content or "").strip()

    if msg.embeds:
        emb = msg.embeds[0]

        draft.mention_text = content  # keep exactly as-is
        draft.title = str(emb.title or "").strip()
        draft.message = str(emb.description or "").strip()

        # images
        try:
            draft.image_url = str(getattr(emb.image, "url", "") or "").strip()
        except Exception:
            draft.image_url = ""
        try:
            draft.thumbnail_url = str(getattr(emb.thumbnail, "url", "") or "").strip()
        except Exception:
            draft.thumbnail_url = ""

        # footer
        try:
            draft.footer_text = str(getattr(emb.footer, "text", "") or "").strip()
            draft.footer_icon_url = str(getattr(emb.footer, "icon_url", "") or "").strip()
        except Exception:
            draft.footer_text = ""
            draft.footer_icon_url = ""

        # author
        try:
            draft.author_name = str(getattr(emb.author, "name", "") or "").strip()
            draft.author_icon_url = str(getattr(emb.author, "icon_url", "") or "").strip()
            draft.author_url = str(getattr(emb.author, "url", "") or "").strip()
        except Exception:
            draft.author_name = ""
            draft.author_icon_url = ""
            draft.author_url = ""

        # color
        try:
            draft.color_hex = _int_to_hex_color(getattr(emb.color, "value", None))
        except Exception:
            draft.color_hex = ""

        # timestamp
        draft.add_timestamp = bool(getattr(emb, "timestamp", None))

        # fields
        draft.fields = []
        try:
            for f in getattr(emb, "fields", []) or []:
                nm = str(getattr(f, "name", "") or "").strip()
                val = str(getattr(f, "value", "") or "").strip()
                inline = bool(getattr(f, "inline", True))
                if nm and val:
                    draft.fields.append({"name": nm, "value": val, "inline": inline})
        except Exception:
            draft.fields = []

        # link button (best effort)
        lbl, url = _extract_first_link_button(msg)
        draft.btn_label = lbl
        draft.btn_url = url

    else:
        # No embed: treat as plain message editing
        draft.mention_text = ""
        draft.title = ""
        draft.message = content

    # Edit target
    if msg.guild:
        draft.edit_guild_id = int(msg.guild.id)
    if msg.channel:
        draft.edit_channel_id = int(msg.channel.id)
    draft.edit_message_id = int(msg.id)

    return draft


# ---------- template management ----------
TEMPLATE_FILE = "embed_templates.json"


def save_embed_template(name: str, draft: "EmbedDraft", guild_id: int):
    """Save an embed template for a specific guild"""
    data = {
        "mention_text": draft.mention_text,
        "title": draft.title,
        "message": draft.message,
        "image_url": draft.image_url,
        "btn_label": draft.btn_label,
        "btn_url": draft.btn_url,
        "footer_text": draft.footer_text,
        "footer_icon_url": draft.footer_icon_url,
        "thumbnail_url": draft.thumbnail_url,
        "author_name": draft.author_name,
        "author_icon_url": draft.author_icon_url,
        "author_url": draft.author_url,
        "color_hex": draft.color_hex,
        "add_timestamp": draft.add_timestamp,
        "fields": draft.fields,
    }

    if os.path.exists(TEMPLATE_FILE):
        with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
            all_templates = json.load(f)
    else:
        all_templates = {}

    guild_templates = all_templates.get(str(guild_id), {})
    guild_templates[name] = data
    all_templates[str(guild_id)] = guild_templates

    with open(TEMPLATE_FILE, "w", encoding="utf-8") as f:
        json.dump(all_templates, f, indent=2)


def load_embed_template(name: str, guild_id: int) -> Optional[dict]:
    """Load an embed template for a specific guild"""
    if os.path.exists(TEMPLATE_FILE):
        with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
            all_templates = json.load(f)
        guild_templates = all_templates.get(str(guild_id), {})
        return guild_templates.get(name)
    return None


def list_embed_templates(guild_id: int) -> list:
    """List all template names for a specific guild"""
    if os.path.exists(TEMPLATE_FILE):
        with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
            all_templates = json.load(f)
        guild_templates = all_templates.get(str(guild_id), {})
        return list(guild_templates.keys())
    return []


def delete_embed_template(name: str, guild_id: int) -> bool:
    """Delete an embed template for a specific guild"""
    try:
        if not os.path.exists(TEMPLATE_FILE):
            return False

        with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
            all_templates = json.load(f)

        guild_templates = all_templates.get(str(guild_id), {})
        if name not in guild_templates:
            return False

        del guild_templates[name]
        all_templates[str(guild_id)] = guild_templates

        with open(TEMPLATE_FILE, "w", encoding="utf-8") as f:
            json.dump(all_templates, f, indent=2)

        return True
    except Exception as e:
        print(f"Error deleting template: {e}")
        return False


def get_template_info(name: str, guild_id: int) -> Optional[dict]:
    """Get detailed info about a template"""
    try:
        template_data = load_embed_template(name, guild_id)
        if not template_data:
            return None

        content_parts = []
        if template_data.get("title"):
            content_parts.append("Title")
        if template_data.get("message"):
            content_parts.append("Message")
        if template_data.get("image_url"):
            content_parts.append("Image")
        if template_data.get("btn_label") and template_data.get("btn_url"):
            content_parts.append("Button")
        if template_data.get("footer_text"):
            content_parts.append("Footer")
        if template_data.get("author_name"):
            content_parts.append("Author")
        if template_data.get("thumbnail_url"):
            content_parts.append("Thumbnail")
        if template_data.get("fields"):
            content_parts.append(f"{len(template_data['fields'])} Fields")

        return {
            "name": name,
            "content_parts": content_parts,
            "content_summary": ", ".join(content_parts) if content_parts else "Empty template",
        }
    except Exception as e:
        print(f"Error getting template info: {e}")
        return None


def make_embed(draft: "EmbedDraft") -> Optional[discord.Embed]:
    """Build an embed with all the advanced options."""
    has_title = _has_text(draft.title)
    has_desc = _has_text(draft.message)
    has_img = _has_text(draft.image_url)
    has_content = (
        has_title
        or has_desc
        or has_img
        or _has_text(draft.thumbnail_url)
        or _has_text(draft.footer_text)
        or _has_text(draft.author_name)
        or (draft.fields and len(draft.fields) > 0)
    )

    if not has_content:
        return None

    # Discord requires at least title OR description
    if not (has_title or has_desc):
        if has_img or _has_text(draft.thumbnail_url) or _has_text(draft.footer_text) or _has_text(draft.author_name):
            has_desc = True
            draft.message = "​"  # Invisible character

    # Set color
    color = discord.Color.gold()
    if _has_text(draft.color_hex):
        try:
            color = discord.Color(int(draft.color_hex.lstrip("#"), 16))
        except ValueError:
            color = discord.Color.gold()

    kwargs = {"color": color}
    if has_title:
        kwargs["title"] = str(draft.title).strip()
    if has_desc:
        kwargs["description"] = str(draft.message).strip()

    # Add timestamp if requested (builder always uses "now")
    if draft.add_timestamp:
        kwargs["timestamp"] = datetime.datetime.now(datetime.timezone.utc)

    emb = discord.Embed(**kwargs)

    if has_img:
        emb.set_image(url=str(draft.image_url).strip())

    if _has_text(draft.thumbnail_url):
        emb.set_thumbnail(url=str(draft.thumbnail_url).strip())

    if _has_text(draft.footer_text):
        footer_kwargs = {"text": str(draft.footer_text).strip()}
        if _has_text(draft.footer_icon_url):
            footer_kwargs["icon_url"] = str(draft.footer_icon_url).strip()
        emb.set_footer(**footer_kwargs)

    if _has_text(draft.author_name):
        author_kwargs = {"name": str(draft.author_name).strip()}
        if _has_text(draft.author_icon_url):
            author_kwargs["icon_url"] = str(draft.author_icon_url).strip()
        if _has_text(draft.author_url):
            author_kwargs["url"] = str(draft.author_url).strip()
        emb.set_author(**author_kwargs)

    if draft.fields:
        for field in draft.fields:
            if field.get("name") and field.get("value"):
                emb.add_field(
                    name=str(field["name"]).strip()[:256],
                    value=str(field["value"]).strip()[:1024],
                    inline=bool(field.get("inline", True)),
                )

    return emb


# ---------- shared modals ----------
class ContentModal(discord.ui.Modal, title="Edit Content"):
    title_input = discord.ui.TextInput(
        label="Embed Title (optional)",
        style=discord.TextStyle.short,
        required=False,
        max_length=256,
    )
    message_input = discord.ui.TextInput(
        label="Message (plain or embed text)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=4000,
    )

    def __init__(self, draft: "EmbedDraft"):
        super().__init__()
        self.draft = draft
        self.title_input.default = draft.title
        self.message_input.default = draft.message

    async def on_submit(self, interaction: discord.Interaction):
        self.draft.title = str(self.title_input.value or "").strip()
        self.draft.message = str(self.message_input.value or "").strip()
        await interaction.response.send_message("✅ Updated content.", ephemeral=True, delete_after=3)

        if hasattr(self.draft, "_view_ref") and self.draft._view_ref:
            try:
                await self.draft._view_ref.update_preview_direct()
            except Exception:
                pass


class ImageModal(discord.ui.Modal, title="Set Images"):
    image_input = discord.ui.TextInput(
        label="Main Image URL (large image)",
        style=discord.TextStyle.short,
        required=False,
        max_length=1024,
        placeholder="https://...",
    )
    thumbnail_input = discord.ui.TextInput(
        label="Thumbnail URL (small top-right image)",
        style=discord.TextStyle.short,
        required=False,
        max_length=1024,
        placeholder="https://...",
    )

    def __init__(self, draft: "EmbedDraft"):
        super().__init__()
        self.draft = draft
        self.image_input.default = draft.image_url
        self.thumbnail_input.default = draft.thumbnail_url

    async def on_submit(self, interaction: discord.Interaction):
        self.draft.image_url = str(self.image_input.value or "").strip()
        self.draft.thumbnail_url = str(self.thumbnail_input.value or "").strip()
        await interaction.response.send_message("✅ Updated images.", ephemeral=True, delete_after=3)

        if hasattr(self.draft, "_view_ref") and self.draft._view_ref:
            try:
                await self.draft._view_ref.update_preview_direct()
            except Exception:
                pass


class ButtonModal(discord.ui.Modal, title="Add Link Button (optional)"):
    label_input = discord.ui.TextInput(
        label="Button Label",
        style=discord.TextStyle.short,
        required=False,
        max_length=80,
        placeholder="e.g., Sign Up",
    )
    url_input = discord.ui.TextInput(
        label="Button URL",
        style=discord.TextStyle.short,
        required=False,
        max_length=1024,
        placeholder="https://...",
    )

    def __init__(self, draft: "EmbedDraft"):
        super().__init__()
        self.draft = draft
        self.label_input.default = draft.btn_label
        self.url_input.default = draft.btn_url

    async def on_submit(self, interaction: discord.Interaction):
        self.draft.btn_label = str(self.label_input.value or "").strip()
        self.draft.btn_url = str(self.url_input.value or "").strip()
        await interaction.response.send_message("✅ Updated button.", ephemeral=True, delete_after=3)

        if hasattr(self.draft, "_view_ref") and self.draft._view_ref:
            try:
                await self.draft._view_ref.update_preview_direct()
            except Exception:
                pass


class MentionDropdown(discord.ui.Select):
    def __init__(self, draft: "EmbedDraft", guild: discord.Guild):
        self.draft = draft

        options = [
            discord.SelectOption(label="None", value="none", description="Clear mention", emoji="❌"),
            discord.SelectOption(label="@here", value="@here", description="Mention online users", emoji="🟢"),
            discord.SelectOption(label="@everyone", value="@everyone", description="Mention all users", emoji="🔴"),
        ]

        mentionable_roles = [
            r for r in guild.roles if r.name != "@everyone" and not r.managed and not r.is_bot_managed()
        ]
        mentionable_roles.sort(key=lambda r: r.position, reverse=True)

        for role in mentionable_roles[:22]:
            emoji = "👑" if role.permissions.administrator else "🎭"
            options.append(
                discord.SelectOption(
                    label=f"@{role.name}",
                    value=role.mention,
                    description=f"Mention {role.name} role ({len(role.members)} members)",
                    emoji=emoji,
                )
            )

        super().__init__(placeholder="Choose who to mention...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        selected_value = self.values[0]

        if selected_value == "none":
            self.draft.mention_text = ""
            await interaction.response.send_message("✅ Mention cleared.", ephemeral=True, delete_after=3)
        elif selected_value in ["@here", "@everyone"]:
            self.draft.mention_text = selected_value
            await interaction.response.send_message(f"✅ Set mention to {selected_value}", ephemeral=True, delete_after=3)
        else:
            self.draft.mention_text = selected_value
            try:
                role_id = selected_value.strip("<@&>")
                guild = interaction.guild
                role = guild.get_role(int(role_id)) if guild else None
                role_name = role.name if role else "Role"
                await interaction.response.send_message(f"✅ Set mention to @{role_name}", ephemeral=True, delete_after=3)
            except Exception:
                await interaction.response.send_message("✅ Set mention to role", ephemeral=True, delete_after=3)

        if hasattr(self.draft, "_view_ref") and self.draft._view_ref:
            try:
                await self.draft._view_ref.update_preview_direct()
            except Exception:
                pass


class MentionView(discord.ui.View):
    def __init__(self, draft: "EmbedDraft", guild: discord.Guild):
        super().__init__(timeout=60)
        self.add_item(MentionDropdown(draft, guild))


class FooterModal(discord.ui.Modal, title="Set Footer"):
    footer_text_input = discord.ui.TextInput(
        label="Footer Text",
        style=discord.TextStyle.short,
        required=False,
        max_length=2048,
        placeholder="Footer text...",
    )
    footer_icon_input = discord.ui.TextInput(
        label="Footer Icon URL (optional)",
        style=discord.TextStyle.short,
        required=False,
        max_length=1024,
        placeholder="https://...",
    )

    def __init__(self, draft: "EmbedDraft"):
        super().__init__()
        self.draft = draft
        self.footer_text_input.default = draft.footer_text
        self.footer_icon_input.default = draft.footer_icon_url

    async def on_submit(self, interaction: discord.Interaction):
        self.draft.footer_text = str(self.footer_text_input.value or "").strip()
        self.draft.footer_icon_url = str(self.footer_icon_input.value or "").strip()
        await interaction.response.send_message("✅ Updated footer.", ephemeral=True, delete_after=3)

        if hasattr(self.draft, "_view_ref") and self.draft._view_ref:
            try:
                await self.draft._view_ref.update_preview_direct()
            except Exception:
                pass


class AuthorModal(discord.ui.Modal, title="Set Author"):
    author_name_input = discord.ui.TextInput(
        label="Author Name",
        style=discord.TextStyle.short,
        required=False,
        max_length=256,
        placeholder="Author name...",
    )
    author_icon_input = discord.ui.TextInput(
        label="Author Icon URL (optional)",
        style=discord.TextStyle.short,
        required=False,
        max_length=1024,
        placeholder="https://...",
    )
    author_url_input = discord.ui.TextInput(
        label="Author URL (optional)",
        style=discord.TextStyle.short,
        required=False,
        max_length=1024,
        placeholder="https://...",
    )

    def __init__(self, draft: "EmbedDraft"):
        super().__init__()
        self.draft = draft
        self.author_name_input.default = draft.author_name
        self.author_icon_input.default = draft.author_icon_url
        self.author_url_input.default = draft.author_url

    async def on_submit(self, interaction: discord.Interaction):
        self.draft.author_name = str(self.author_name_input.value or "").strip()
        self.draft.author_icon_url = str(self.author_icon_input.value or "").strip()
        self.draft.author_url = str(self.author_url_input.value or "").strip()
        await interaction.response.send_message("✅ Updated author.", ephemeral=True, delete_after=3)

        if hasattr(self.draft, "_view_ref") and self.draft._view_ref:
            try:
                await self.draft._view_ref.update_preview_direct()
            except Exception:
                pass


class AdvancedModal(discord.ui.Modal, title="Color & Style"):
    color_input = discord.ui.TextInput(
        label="Color (hex like #FF0000, optional)",
        style=discord.TextStyle.short,
        required=False,
        max_length=7,
        placeholder="#FF0000",
    )

    def __init__(self, draft: "EmbedDraft"):
        super().__init__()
        self.draft = draft
        self.color_input.default = draft.color_hex

    async def on_submit(self, interaction: discord.Interaction):
        self.draft.color_hex = str(self.color_input.value or "").strip()
        await interaction.response.send_message("✅ Updated color.", ephemeral=True, delete_after=3)

        if hasattr(self.draft, "_view_ref") and self.draft._view_ref:
            try:
                await self.draft._view_ref.update_preview_direct()
            except Exception:
                pass


class FieldsModal(discord.ui.Modal, title="Add/Edit Fields"):
    field1_name = discord.ui.TextInput(
        label="Field 1 Name (optional)",
        style=discord.TextStyle.short,
        required=False,
        max_length=256,
        placeholder="Field name...",
    )
    field1_value = discord.ui.TextInput(
        label="Field 1 Value (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1024,
        placeholder="Field value...",
    )
    field2_name = discord.ui.TextInput(
        label="Field 2 Name (optional)",
        style=discord.TextStyle.short,
        required=False,
        max_length=256,
        placeholder="Field name...",
    )
    field2_value = discord.ui.TextInput(
        label="Field 2 Value (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1024,
        placeholder="Field value...",
    )
    inline_setting = discord.ui.TextInput(
        label="Inline (true/false for all fields)",
        style=discord.TextStyle.short,
        required=False,
        max_length=5,
        placeholder="true",
        default="true",
    )

    def __init__(self, draft: "EmbedDraft"):
        super().__init__()
        self.draft = draft
        if len(draft.fields) > 0:
            self.field1_name.default = draft.fields[0].get("name", "")
            self.field1_value.default = draft.fields[0].get("value", "")
        if len(draft.fields) > 1:
            self.field2_name.default = draft.fields[1].get("name", "")
            self.field2_value.default = draft.fields[1].get("value", "")

    async def on_submit(self, interaction: discord.Interaction):
        inline_str = str(self.inline_setting.value or "true").strip().lower()
        inline = inline_str in ("true", "yes", "1", "on")

        self.draft.fields = []

        f1n = str(self.field1_name.value or "").strip()
        f1v = str(self.field1_value.value or "").strip()
        if f1n and f1v:
            self.draft.fields.append({"name": f1n, "value": f1v, "inline": inline})

        f2n = str(self.field2_name.value or "").strip()
        f2v = str(self.field2_value.value or "").strip()
        if f2n and f2v:
            self.draft.fields.append({"name": f2n, "value": f2v, "inline": inline})

        field_count = len(self.draft.fields)
        await interaction.response.send_message(
            f"✅ Updated fields ({field_count} field{'s' if field_count != 1 else ''}).",
            ephemeral=True,
            delete_after=3,
        )

        if hasattr(self.draft, "_view_ref") and self.draft._view_ref:
            try:
                await self.draft._view_ref.update_preview_direct()
            except Exception:
                pass


class TemplateNameModal(discord.ui.Modal, title="Save Embed as Template"):
    template_name = discord.ui.TextInput(
        label="Template Name",
        style=discord.TextStyle.short,
        required=True,
        max_length=32,
        placeholder="MyTemplate",
    )

    def __init__(self, draft: "EmbedDraft", guild_id: int):
        super().__init__()
        self.draft = draft
        self.guild_id = guild_id

        if draft.loaded_template_name:
            self.template_name.default = draft.loaded_template_name
            self.template_name.placeholder = f"Update: {draft.loaded_template_name}"

            self.warning = discord.ui.TextInput(
                label="⚠️ Updating existing template",
                style=discord.TextStyle.short,
                required=False,
                max_length=50,
                default=f"Will overwrite '{draft.loaded_template_name}'",
                placeholder="Existing template will be overwritten",
            )
            self.add_item(self.warning)

    async def on_submit(self, interaction: discord.Interaction):
        name = str(self.template_name.value).strip()
        if not name:
            await interaction.response.send_message("❌ Template name cannot be empty.", ephemeral=True, delete_after=3)
            return

        existing_templates = list_embed_templates(self.guild_id)
        is_loaded_template_update = name == self.draft.loaded_template_name and self.draft.loaded_template_name
        is_overwriting_different = name in existing_templates and not is_loaded_template_update

        if is_overwriting_different:
            await interaction.response.send_message(
                f"⚠️ Template '{name}' already exists and will be overwritten.\n"
                f"Use 'Save Template' again to confirm, or change the name to create a new template.",
                ephemeral=True,
                delete_after=8,
            )
            return

        save_embed_template(name, self.draft, self.guild_id)

        if is_loaded_template_update:
            await interaction.response.send_message(
                f"✅ Updated template '{name}' for this server!", ephemeral=True, delete_after=3
            )
        else:
            await interaction.response.send_message(f"✅ Saved template '{name}' for this server!", ephemeral=True, delete_after=3)
            self.draft.loaded_template_name = name


class TemplateSelectView(discord.ui.View):
    def __init__(self, draft: "EmbedDraft", main_view: "EmbedMakerView", guild_id: int, templates: list):
        super().__init__(timeout=60)
        self.draft = draft
        self.main_view = main_view
        self.guild_id = guild_id

        options = []
        for template_name in templates[:25]:
            options.append(
                discord.SelectOption(label=template_name, description=f"Load template: {template_name}", value=template_name)
            )

        if options:
            self.template_select.options = options
        else:
            self.template_select.disabled = True
            self.template_select.placeholder = "No templates available"

    @discord.ui.select(placeholder="Choose a template to load...", min_values=1, max_values=1)
    async def template_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        try:
            template_name = select.values[0]

            data = load_embed_template(template_name, self.guild_id)
            if not data:
                await interaction.response.send_message(
                    f"❌ Template '{template_name}' not found or corrupted.", ephemeral=True, delete_after=5
                )
                return

            self.draft.mention_text = data.get("mention_text", "")
            self.draft.title = data.get("title", "")
            self.draft.message = data.get("message", "")
            self.draft.image_url = data.get("image_url", "")
            self.draft.btn_label = data.get("btn_label", "")
            self.draft.btn_url = data.get("btn_url", "")
            self.draft.footer_text = data.get("footer_text", "")
            self.draft.footer_icon_url = data.get("footer_icon_url", "")
            self.draft.thumbnail_url = data.get("thumbnail_url", "")
            self.draft.author_name = data.get("author_name", "")
            self.draft.author_icon_url = data.get("author_icon_url", "")
            self.draft.author_url = data.get("author_url", "")
            self.draft.color_hex = data.get("color_hex", "")
            self.draft.add_timestamp = data.get("add_timestamp", False)
            self.draft.fields = data.get("fields", [])

            self.draft.loaded_template_name = template_name

            await interaction.response.send_message(
                f"✅ Loaded template '{template_name}'! Use 'Save Template' to update it.",
                ephemeral=True,
                delete_after=3,
            )

            if hasattr(self.draft, "_view_ref") and self.draft._view_ref:
                try:
                    await self.draft._view_ref.update_preview_direct()
                except Exception as update_error:
                    print(f"Failed to update preview after loading template: {update_error}")

        except Exception as e:
            print(f"Error in template_select: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        f"❌ An error occurred loading the template: {str(e)}", ephemeral=True, delete_after=5
                    )
                else:
                    await interaction.followup.send(
                        f"❌ An error occurred loading the template: {str(e)}", ephemeral=True, delete_after=5
                    )
            except Exception:
                pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_load(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("❌ Template loading cancelled.", ephemeral=True, delete_after=2)


class TemplateManageView(discord.ui.View):
    def __init__(self, guild_id: int, templates: list):
        super().__init__(timeout=60)
        self.guild_id = guild_id

        options = []
        for template_name in templates[:25]:
            info = get_template_info(template_name, guild_id)
            description = info["content_summary"][:100] if info else "Template info unavailable"
            options.append(discord.SelectOption(label=template_name, description=description, value=template_name))

        if options:
            self.template_select.options = options
        else:
            self.template_select.disabled = True
            self.template_select.placeholder = "No templates available"

    @discord.ui.select(placeholder="Choose a template to manage...", min_values=1, max_values=1)
    async def template_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        template_name = select.values[0]

        info = get_template_info(template_name, self.guild_id)
        if not info:
            await interaction.response.send_message(f"❌ Template '{template_name}' not found.", ephemeral=True, delete_after=5)
            return

        manage_view = TemplateActionView(template_name, self.guild_id)

        content = f"**Template Management: {template_name}**\n"
        content += f"📋 **Content:** {info['content_summary']}\n"
        content += "Choose an action:"

        await interaction.response.send_message(content, view=manage_view, ephemeral=True, delete_after=30)


class TemplateActionView(discord.ui.View):
    def __init__(self, template_name: str, guild_id: int):
        super().__init__(timeout=30)
        self.template_name = template_name
        self.guild_id = guild_id

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger)
    async def delete_template(self, interaction: discord.Interaction, button: discord.ui.Button):
        confirm_view = TemplateDeleteConfirmView(self.template_name, self.guild_id)
        await interaction.response.send_message(
            f"⚠️ **Are you sure you want to delete template '{self.template_name}'?**\nThis action cannot be undone!",
            view=confirm_view,
            ephemeral=True,
            delete_after=20,
        )

    @discord.ui.button(label="📝 Update", style=discord.ButtonStyle.primary)
    async def update_template(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"💡 **To update template '{self.template_name}':**\n"
            f"1. Use `/embed_maker` to create a new embed\n"
            f"2. Set up your embed exactly how you want it\n"
            f"3. Click 'Save Template' and use the same name: `{self.template_name}`\n"
            f"4. The template will be automatically updated!\n\n"
            f"*Tip: The old version will be completely replaced with your new settings.*",
            ephemeral=True,
            delete_after=15,
        )


class TemplateDeleteConfirmView(discord.ui.View):
    def __init__(self, template_name: str, guild_id: int):
        super().__init__(timeout=20)
        self.template_name = template_name
        self.guild_id = guild_id

    @discord.ui.button(label="✅ Yes, Delete", style=discord.ButtonStyle.danger)
    async def confirm_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        success = delete_embed_template(self.template_name, self.guild_id)
        if success:
            await interaction.response.send_message(
                f"✅ Template '{self.template_name}' has been deleted.", ephemeral=True, delete_after=5
            )
        else:
            await interaction.response.send_message(
                f"❌ Failed to delete template '{self.template_name}'. It may have already been deleted.",
                ephemeral=True,
                delete_after=5,
            )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("❌ Deletion cancelled.", ephemeral=True, delete_after=3)


# ---------- UI state ----------
@dataclass
class EmbedDraft:
    mention_text: str = ""  # content line (often role mention / text)
    title: str = ""
    message: str = ""
    image_url: str = ""
    btn_label: str = ""
    btn_url: str = ""
    footer_text: str = ""
    footer_icon_url: str = ""
    thumbnail_url: str = ""
    author_name: str = ""
    author_icon_url: str = ""
    author_url: str = ""
    color_hex: str = ""
    add_timestamp: bool = False
    fields: list = None
    loaded_template_name: str = ""

    # NEW: edit target tracking
    edit_guild_id: int = 0
    edit_channel_id: int = 0
    edit_message_id: int = 0

    def __post_init__(self):
        if self.fields is None:
            self.fields = []

    def build_embed(self) -> Optional[discord.Embed]:
        return make_embed(self)

    def build_view(self) -> Optional[discord.ui.View]:
        if _has_text(self.btn_label) and _has_text(self.btn_url):
            class LinkOnly(discord.ui.View):
                def __init__(self, label: str, url: str):
                    super().__init__(timeout=None)
                    self.add_item(discord.ui.Button(label=label, url=url))
            return LinkOnly(self.btn_label, self.btn_url)
        return None


# ---------- Builder View ----------
class EmbedMakerView(discord.ui.View):
    def __init__(self, draft: EmbedDraft, author_id: int):
        super().__init__(timeout=600)
        self.draft = draft
        self.author_id = author_id
        self.message = None

        # If we're not editing an existing message, disable update button
        try:
            for child in self.children:
                if isinstance(child, discord.ui.Button) and getattr(child, "custom_id", None) == "update_this_message":
                    child.disabled = not bool(self.draft.edit_message_id)
        except Exception:
            pass

        self.update_button_styles()

    def build_preview_content(self):
        try:
            content_parts = ["**Embed Maker** (Live Preview)"]

            if self.draft.loaded_template_name:
                content_parts.append(f"📋 **Template:** {self.draft.loaded_template_name} (loaded)")

            # Edit target banner
            if self.draft.edit_message_id:
                content_parts.append(
                    f"🛠️ **Editing Message:** `{self.draft.edit_message_id}` (use **Update This Message**)"
                )

            if self.draft.mention_text:
                content_parts.append(f"✅ **Message Content:** {self.draft.mention_text}")
            else:
                content_parts.append("❌ **Message Content:** None")

            if self.draft.title:
                content_parts.append(f"✅ **Title:** {self.draft.title}")
            else:
                content_parts.append("❌ **Title:** Not set")

            if self.draft.message:
                preview_msg = self.draft.message[:100] + "..." if len(self.draft.message) > 100 else self.draft.message
                content_parts.append(f"✅ **Embed Text:** {preview_msg}")
            else:
                content_parts.append("❌ **Embed Text:** Not set")

            image_status = []
            if self.draft.image_url:
                image_status.append("Main")
            if self.draft.thumbnail_url:
                image_status.append("Thumbnail")

            if image_status:
                content_parts.append(f"✅ **Images:** {', '.join(image_status)}")
            else:
                content_parts.append("❌ **Images:** Not set")

            if _has_text(self.draft.btn_label) and _has_text(self.draft.btn_url):
                content_parts.append(
                    f"✅ **Button:** {self.draft.btn_label} → {self.draft.btn_url[:50]}{'...' if len(self.draft.btn_url) > 50 else ''}"
                )
            else:
                content_parts.append("❌ **Button:** Not set")

            if self.draft.author_name:
                content_parts.append(f"✅ **Author:** {self.draft.author_name}")
            else:
                content_parts.append("❌ **Author:** Not set")

            if self.draft.footer_text:
                content_parts.append(f"✅ **Footer:** {self.draft.footer_text[:30]}{'...' if len(self.draft.footer_text) > 30 else ''}")
            else:
                content_parts.append("❌ **Footer:** Not set")

            if self.draft.color_hex:
                content_parts.append(f"✅ **Color:** {self.draft.color_hex}")
            else:
                content_parts.append("❌ **Color:** Default (Gold)")

            if self.draft.add_timestamp:
                content_parts.append("✅ **Timestamp:** Enabled")
            else:
                content_parts.append("❌ **Timestamp:** Disabled")

            field_count = len(self.draft.fields) if self.draft.fields else 0
            if field_count > 0:
                field_names = [f["name"][:20] + ("..." if len(f["name"]) > 20 else "") for f in self.draft.fields[:3]]
                content_parts.append(f"✅ **Fields:** {field_count} field{'s' if field_count != 1 else ''} ({', '.join(field_names)})")
            else:
                content_parts.append("❌ **Fields:** Not set")

            content_parts.append("\n*Use the buttons below to edit your embed:*")
            return "\n".join(content_parts)
        except Exception as e:
            print(f"Error building preview content: {e}")
            return "**Embed Maker** (Error loading preview - but buttons should still work)"

    async def update_preview_direct(self):
        if self.message:
            try:
                content = self.build_preview_content()
                embed = self.draft.build_embed()

                preview_embed = None
                if embed:
                    preview_embed = discord.Embed(title=embed.title, description=embed.description, color=embed.color)
                    if embed.image:
                        preview_embed.set_image(url=embed.image.url)
                    if embed.thumbnail:
                        preview_embed.set_thumbnail(url=embed.thumbnail.url)
                    if embed.footer:
                        footer_kwargs = {"text": embed.footer.text}
                        if embed.footer.icon_url:
                            footer_kwargs["icon_url"] = embed.footer.icon_url
                        preview_embed.set_footer(**footer_kwargs)
                    if embed.author:
                        author_kwargs = {"name": embed.author.name}
                        if embed.author.icon_url:
                            author_kwargs["icon_url"] = embed.author.icon_url
                        if embed.author.url:
                            author_kwargs["url"] = embed.author.url
                        preview_embed.set_author(**author_kwargs)
                    if embed.timestamp:
                        preview_embed.timestamp = embed.timestamp
                    elif self.draft.add_timestamp:
                        preview_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)

                if preview_embed and self.draft.fields:
                    # If embed already has fields from build_embed, it’s fine to mirror them as-is.
                    # Here we ensure preview shows the current draft fields.
                    preview_embed.clear_fields()
                    for field in self.draft.fields:
                        preview_embed.add_field(
                            name=field["name"],
                            value=field["value"],
                            inline=field.get("inline", True),
                        )

                self.update_button_styles()

                kwargs = {"content": content, "view": self}
                if preview_embed:
                    kwargs["embed"] = preview_embed

                await self.message.edit(**kwargs)

            except discord.NotFound:
                print("Message not found - it may have been deleted")
                self.message = None
            except discord.HTTPException as e:
                if "Unknown interaction" in str(e):
                    print("Interaction expired - clearing message reference")
                    self.message = None
                else:
                    print(f"HTTP error updating preview: {e}")
            except Exception as e:
                print(f"Failed to update preview: {e}")
        else:
            print("No message reference available for preview update")

    async def update_preview(self, interaction: discord.Interaction):
        await self.update_preview_direct()

    async def on_timeout(self):
        try:
            for child in self.children:
                child.disabled = True

            if self.message:
                await self.message.edit(
                    content="⏰ Builder timed out after 10 minutes. Use `/embed_maker` to create a new one.",
                    view=self,
                )
        except Exception as e:
            print(f"Error in on_timeout: {e}")

    def update_button_styles(self):
        pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This embed maker belongs to someone else. Use `/embed_maker` to create your own!",
                ephemeral=True,
                delete_after=5,
            )
            return False
        return True

    # Row 0 — Core content buttons
    @discord.ui.button(label="Edit Content", style=discord.ButtonStyle.secondary, row=0)
    async def edit_content(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ContentModal(self.draft))

    @discord.ui.button(label="Set Images", style=discord.ButtonStyle.secondary, row=0)
    async def set_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ImageModal(self.draft))

    @discord.ui.button(label="Set Mention", style=discord.ButtonStyle.secondary, row=0)
    async def set_mention(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True, delete_after=5)
            return

        view = MentionView(self.draft, interaction.guild)
        await interaction.response.send_message("Choose who to mention:", view=view, ephemeral=True, delete_after=30)

    @discord.ui.button(label="Add Button", style=discord.ButtonStyle.secondary, row=0)
    async def set_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ButtonModal(self.draft))

    # Row 1 — Advanced options
    @discord.ui.button(label="Set Author", style=discord.ButtonStyle.secondary, row=1)
    async def set_author(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AuthorModal(self.draft))

    @discord.ui.button(label="Set Footer", style=discord.ButtonStyle.secondary, row=1)
    async def set_footer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FooterModal(self.draft))

    @discord.ui.button(label="Set Color", style=discord.ButtonStyle.secondary, row=1)
    async def set_advanced(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdvancedModal(self.draft))

    @discord.ui.button(label="Edit Fields", style=discord.ButtonStyle.secondary, row=1)
    async def set_fields(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FieldsModal(self.draft))

    # Row 2 — Templates and actions
    @discord.ui.button(label="Save Template", style=discord.ButtonStyle.secondary, row=2)
    async def save_template(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("❌ Templates can only be saved in servers.", ephemeral=True, delete_after=5)
            return
        await interaction.response.send_modal(TemplateNameModal(self.draft, interaction.guild.id))

    @discord.ui.button(label="Load Template", style=discord.ButtonStyle.secondary, row=2)
    async def load_template(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("❌ Templates can only be loaded in servers.", ephemeral=True, delete_after=5)
            return

        templates = list_embed_templates(interaction.guild.id)
        if not templates:
            await interaction.response.send_message(
                "❌ No templates saved for this server yet. Create one with the 'Save Template' button!",
                ephemeral=True,
                delete_after=5,
            )
            return

        template_view = TemplateSelectView(self.draft, self, interaction.guild.id, templates)
        await interaction.response.send_message("📋 **Select a template to load:**", view=template_view, ephemeral=True, delete_after=60)

    @discord.ui.button(label="Manage Templates", style=discord.ButtonStyle.secondary, row=2)
    async def manage_templates(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("❌ Templates can only be managed in servers.", ephemeral=True, delete_after=5)
            return

        templates = list_embed_templates(interaction.guild.id)
        if not templates:
            await interaction.response.send_message(
                "❌ No templates saved for this server yet. Create one with the 'Save Template' button first!",
                ephemeral=True,
                delete_after=5,
            )
            return

        manage_view = TemplateManageView(interaction.guild.id, templates)
        await interaction.response.send_message(
            "🛠️ **Template Management**\nSelect a template to delete or update:",
            view=manage_view,
            ephemeral=True,
            delete_after=60,
        )

    # Row 3 — Time and Preview
    @discord.ui.button(label="Toggle Time", style=discord.ButtonStyle.secondary, row=3)
    async def toggle_timestamp(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.draft.add_timestamp = not self.draft.add_timestamp
        status = "enabled" if self.draft.add_timestamp else "disabled"
        await interaction.response.send_message(f"⏰ Timestamp {status}.", ephemeral=True, delete_after=3)

        if hasattr(self.draft, "_view_ref") and self.draft._view_ref:
            try:
                await self.draft._view_ref.update_preview_direct()
            except Exception:
                pass

    @discord.ui.button(label="Test Preview", style=discord.ButtonStyle.secondary, row=3)
    async def test_preview(self, interaction: discord.Interaction, button: discord.ui.Button):
        emb = self.draft.build_embed()
        view = self.draft.build_view()

        body = None if emb else (self.draft.message or "*No message*")
        if self.draft.mention_text:
            body = (body or "") + ("\n" if body else "") + f"**Message content:** {self.draft.mention_text}"

        header = "**Test Preview** (exactly how it will look when sent)"
        content = header if body is None else f"{header}\n{body}"

        kwargs = {"content": content, "ephemeral": True, "delete_after": 15}
        if emb is not None:
            kwargs["embed"] = emb
        if view is not None:
            kwargs["view"] = view
        await interaction.response.send_message(**kwargs)

    # Row 4 — Update / Send / Cancel
    @discord.ui.button(
        label="Update This Message",
        style=discord.ButtonStyle.primary,
        row=4,
        custom_id="update_this_message",
    )
    async def update_this_message(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Admin only
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "You need **Administrator** permissions to update messages.",
                ephemeral=True,
                delete_after=5,
            )

        if not self.draft.edit_channel_id or not self.draft.edit_message_id:
            return await interaction.response.send_message(
                "❌ No edit target set for this builder.",
                ephemeral=True,
                delete_after=5,
            )

        await interaction.response.defer(ephemeral=True)

        try:
            # Fetch channel + message
            channel = interaction.guild.get_channel(self.draft.edit_channel_id) if interaction.guild else None
            if channel is None:
                channel = await interaction.client.fetch_channel(self.draft.edit_channel_id)

            if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel)):
                # Threads are common for forum posts; TextChannel/Thread are the main ones.
                pass

            msg = await channel.fetch_message(self.draft.edit_message_id)

            emb = self.draft.build_embed()
            v = self.draft.build_view()

            allowed_mentions = discord.AllowedMentions.all()

            if emb is not None:
                # Embed message: message content = mention_text exactly
                content = str(self.draft.mention_text or "").strip()
                content = content if content else None
                await msg.edit(content=content, embed=emb, view=v, allowed_mentions=allowed_mentions)
            else:
                # Plain message: use draft.message; do NOT force mention_text append
                content = str(self.draft.message or "").strip()
                if self.draft.mention_text:
                    # If they intentionally set message content in mention_text while using plain text, append it
                    # (keeps existing builder behavior usable)
                    content = (content + ("\n" if content else "") + str(self.draft.mention_text).strip()).strip()
                await msg.edit(content=content or None, embed=None, view=v, allowed_mentions=allowed_mentions)

            await interaction.followup.send("✅ Message updated.", ephemeral=True, delete_after=3)

        except Exception as e:
            await interaction.followup.send(f"❌ Update failed: `{e.__class__.__name__}: {str(e)}`", ephemeral=True, delete_after=8)

    @discord.ui.button(label="Send Here", style=discord.ButtonStyle.success, row=4)
    async def send_here(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "You need **Administrator** permissions to send.", ephemeral=True, delete_after=5
            )

        if not (_has_text(self.draft.message) or _has_text(self.draft.title) or _has_text(self.draft.image_url)):
            return await interaction.response.send_message(
                "Add a message or a title/image before sending.", ephemeral=True, delete_after=5
            )

        channel = interaction.channel
        if channel is None:
            return await interaction.response.send_message("❌ No channel context.", ephemeral=True, delete_after=5)

        await interaction.response.defer(ephemeral=True)

        emb = self.draft.build_embed()
        view = self.draft.build_view()

        # NOTE: Keeping existing behavior exactly:
        # If no embed -> content is draft.message, then append mention_text
        # If embed -> content None, then append mention_text (role pings etc.)
        content = None if emb else (self.draft.message or "")
        if self.draft.mention_text:
            content = (content or "") + ("\n" if content else "") + self.draft.mention_text

        allowed_mentions = discord.AllowedMentions.all()

        try:
            if isinstance(channel, discord.ForumChannel):
                kwargs = {"name": self.draft.title or "Announcement"}
                if content:
                    kwargs["content"] = content
                else:
                    kwargs["content"] = discord.utils.MISSING
                if emb is not None:
                    kwargs["embed"] = emb
                if view is not None:
                    kwargs["view"] = view
                kwargs["allowed_mentions"] = allowed_mentions
                await channel.create_thread(**kwargs)
            else:
                kwargs = {}
                if content:
                    kwargs["content"] = content
                if emb is not None:
                    kwargs["embed"] = emb
                if view is not None:
                    kwargs["view"] = view
                kwargs["allowed_mentions"] = allowed_mentions
                await channel.send(**kwargs)
        except Exception as e:
            return await interaction.followup.send(
                f"❌ Send failed: `{e.__class__.__name__}: {str(e)}`", ephemeral=True, delete_after=8
            )

        for child in self.children:
            child.disabled = True

        try:
            await interaction.followup.edit_message(
                interaction.message.id,
                content="✅ Embed sent successfully! Closing in 2 seconds...",
                view=self,
            )
        except Exception:
            await interaction.followup.send("✅ Embed sent successfully!", ephemeral=True, delete_after=2)

        try:
            await discord.utils.sleep_until(datetime.datetime.now().astimezone() + datetime.timedelta(seconds=2))
            await interaction.delete_original_response()
        except Exception:
            pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=4)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(content="❌ Builder cancelled. Closing...", view=self)

        try:
            await discord.utils.sleep_until(datetime.datetime.now().astimezone() + datetime.timedelta(seconds=1))
            await interaction.delete_original_response()
        except Exception:
            pass


# ---------- Cog ----------
class EmbedMaker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="embed_maker", description="Open a simple embed builder for this channel.")
    async def embed_maker(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            try:
                await interaction.response.send_message(
                    "You need **Administrator** permissions to use this.", ephemeral=True, delete_after=5
                )
            except Exception:
                pass
            return

        try:
            draft = EmbedDraft()
            view = EmbedMakerView(draft, author_id=interaction.user.id)
            draft._view_ref = view

            content = view.build_preview_content()
            embed = draft.build_embed()

            kwargs = {"content": content, "view": view, "ephemeral": True}
            if embed:
                preview_embed = discord.Embed(title=embed.title, description=embed.description, color=embed.color)
                if embed.image:
                    preview_embed.set_image(url=embed.image.url)
                if embed.thumbnail:
                    preview_embed.set_thumbnail(url=embed.thumbnail.url)
                if embed.footer:
                    footer_kwargs = {"text": embed.footer.text}
                    if embed.footer.icon_url:
                        footer_kwargs["icon_url"] = embed.footer.icon_url
                    preview_embed.set_footer(**footer_kwargs)
                if embed.author:
                    author_kwargs = {"name": embed.author.name}
                    if embed.author.icon_url:
                        author_kwargs["icon_url"] = embed.author.icon_url
                    if embed.author.url:
                        author_kwargs["url"] = embed.author.url
                    preview_embed.set_author(**author_kwargs)
                if embed.timestamp:
                    preview_embed.timestamp = embed.timestamp
                elif view.draft.add_timestamp:
                    preview_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)

                if draft.fields:
                    preview_embed.clear_fields()
                    for field in draft.fields:
                        preview_embed.add_field(
                            name=field["name"],
                            value=field["value"],
                            inline=field.get("inline", True),
                        )

                kwargs["embed"] = preview_embed

            await interaction.response.send_message(**kwargs)

            try:
                view.message = await interaction.original_response()
            except Exception:
                print("Could not store message reference - live updates may not work")

        except Exception as e:
            print(f"Error in embed_maker command: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Failed to create embed maker. Please try again.", ephemeral=True, delete_after=5
                    )
            except Exception as error_send_fail:
                print(f"Could not send error message: {error_send_fail}")


# ----------------------------
# MESSAGE CONTEXT MENU
# Right click a message -> Apps -> Edit Embed
# ----------------------------
@app_commands.context_menu(name="Edit Embed")
async def edit_embed_message_context(interaction: discord.Interaction, message: discord.Message):
    if interaction.guild is None:
        return await interaction.response.send_message("This must be used in a server.", ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "You need **Administrator** permissions to use this.",
            ephemeral=True,
            delete_after=5,
        )

    try:
        draft = _draft_from_message(message)
        view = EmbedMakerView(draft, author_id=interaction.user.id)
        draft._view_ref = view

        content = view.build_preview_content()
        embed = draft.build_embed()

        kwargs = {"content": content, "view": view, "ephemeral": True}
        if embed:
            preview_embed = discord.Embed(title=embed.title, description=embed.description, color=embed.color)
            if embed.image:
                preview_embed.set_image(url=embed.image.url)
            if embed.thumbnail:
                preview_embed.set_thumbnail(url=embed.thumbnail.url)
            if embed.footer:
                footer_kwargs = {"text": embed.footer.text}
                if embed.footer.icon_url:
                    footer_kwargs["icon_url"] = embed.footer.icon_url
                preview_embed.set_footer(**footer_kwargs)
            if embed.author:
                author_kwargs = {"name": embed.author.name}
                if embed.author.icon_url:
                    author_kwargs["icon_url"] = embed.author.icon_url
                if embed.author.url:
                    author_kwargs["url"] = embed.author.url
                preview_embed.set_author(**author_kwargs)
            if embed.timestamp:
                preview_embed.timestamp = embed.timestamp
            elif view.draft.add_timestamp:
                preview_embed.timestamp = datetime.datetime.now(datetime.timezone.utc)

            if draft.fields:
                preview_embed.clear_fields()
                for field in draft.fields:
                    preview_embed.add_field(
                        name=field["name"],
                        value=field["value"],
                        inline=field.get("inline", True),
                    )

            kwargs["embed"] = preview_embed

        await interaction.response.send_message(**kwargs)

        try:
            view.message = await interaction.original_response()
        except Exception:
            print("Could not store message reference - live updates may not work")

    except Exception as e:
        print(f"Error in Edit Embed context menu: {e}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Failed to open editor.", ephemeral=True, delete_after=5)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(EmbedMaker(bot))

    # Ensure we don't duplicate the MESSAGE context menu on reload
    try:
        bot.tree.remove_command("Edit Embed", type=discord.AppCommandType.message)
    except Exception:
        pass
    bot.tree.add_command(edit_embed_message_context)
