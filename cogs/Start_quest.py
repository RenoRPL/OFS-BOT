# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
from discord import app_commands
import json
import os
from typing import Optional
import time
import gspread
from google.oauth2.service_account import Credentials
import asyncio
from datetime import datetime
from utils.google_auth import get_google_credentials

class QuestMakerView(discord.ui.View):
    def __init__(self, leader: discord.Member, game: str, quest_id: int, guild_id: int, quest_cog, leader_info: dict, quest_type: str = "Quest"):
        super().__init__(timeout=300)
        self.leader = leader
        self.game = game
        self.quest_id = quest_id
        self.guild_id = guild_id
        self.quest_cog = quest_cog
        self.leader_info = leader_info  # Contains rank and role from Member Log
        self.quest_name = ""
        self.quest_description = ""
        # Get guild-specific default image
        guild_settings = quest_cog.get_guild_settings(guild_id)
        self.quest_image = guild_settings.get("default_quest_image", "")
        self.quest_length = 60  # Default 60 minutes
        self.quest_scroll = "📜"  # Default scroll emoji for forum post (Knight era theme)
        self.quest_type = quest_type  # Quest or Crusade
        self.created_at = datetime.utcnow()  # Track when quest was created
        self.original_interaction = None  # Store the original interaction for updating
        self.template_interaction = None  # Store template loading interaction for cleanup
        self.image_upload_interaction = None  # Store image upload interaction for cleanup
        self.emoji_picker_interaction = None  # Store emoji picker interaction for cleanup
        self.creating_quest = False  # Prevent double-tapping create quest button
    
    def create_embed(self):
        embed = discord.Embed(
            title=f"{self.quest_scroll} Quest Maker",
            description="Create your quest details below",
            color=0x0099ff
        )
        embed.add_field(name="Quest ID", value=f"`{self.quest_id}`", inline=True)
        embed.add_field(name="Type", value=self.quest_type, inline=True)
        embed.add_field(name="Game", value=self.game, inline=True)
        embed.add_field(name="Length", value=f"{self.quest_length} minutes", inline=True)
        embed.add_field(name="Quest Leader", value=self.leader.mention, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)  # Empty field for spacing
        embed.add_field(name="Quest Name", value=self.quest_name or "*Not set*", inline=False)
        embed.add_field(name="Description", value=self.quest_description or "*Not set*", inline=False)
        
        # Always show an image (default or custom) but don't show the URL
        guild_settings = self.quest_cog.get_guild_settings(self.guild_id)
        image_url = self.quest_image or guild_settings.get("default_quest_image", "")
        if image_url:
            embed.set_image(url=image_url)
        
        return embed
    
    def get_time_since_created(self):
        """Get a human-readable time since quest was created"""
        now = datetime.utcnow()
        time_diff = now - self.created_at
        
        total_seconds = int(time_diff.total_seconds())
        
        if total_seconds < 60:
            return f"{total_seconds} second{'s' if total_seconds != 1 else ''} ago"
        elif total_seconds < 3600:  # Less than 1 hour
            minutes = total_seconds // 60
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        elif total_seconds < 86400:  # Less than 1 day
            hours = total_seconds // 3600
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        else:  # 1 day or more
            days = total_seconds // 86400
            return f"{days} day{'s' if days != 1 else ''} ago"
    
    async def update_original_message(self):
        """Update the original Quest Maker message"""
        if self.original_interaction:
            embed = self.create_embed()
            try:
                await self.original_interaction.edit_original_response(embed=embed, view=self)
            except Exception as e:
                print(f"Failed to update original message: {e}")
    
    @discord.ui.button(label="Set Name", style=discord.ButtonStyle.secondary, emoji="📝")
    async def set_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = QuestNameModal(self)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Set Description", style=discord.ButtonStyle.secondary, emoji="📄")
    async def set_description(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = QuestDescriptionModal(self)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Set Image", style=discord.ButtonStyle.secondary, emoji="🖼️")
    async def set_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Store the interaction for cleanup later
        self.image_upload_interaction = interaction
        
        # Send helpful instructions first
        await interaction.response.send_message(
            "📷 **How to add a custom image:**\n"
            "1. Upload your image to any Discord channel\n"
            "2. Right-click the uploaded image\n"
            "3. Select **Copy Link**\n"
            "4. Click the button below and paste the link\n\n"
            "*Or leave blank to use the default image*",
            ephemeral=True,
            view=ImageUploadView(self)
        )
    
    @discord.ui.button(label="Set Length", style=discord.ButtonStyle.secondary, emoji="⏱️")
    async def set_length(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = QuestLengthModal(self)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Set Scroll", style=discord.ButtonStyle.secondary, emoji="📜")
    async def set_scroll(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Store the interaction for cleanup later
        self.emoji_picker_interaction = interaction
        
        # Create emoji selection view with dropdown
        emoji_view = EmojiSelectView(self)
        
        # Create embed showing current emoji and instructions
        scroll_embed = discord.Embed(
            title="📜 Choose Quest Scroll Emoji",
            description="Select an emoji from the dropdown below that will appear in your quest forum post title:",
            color=0x0099ff
        )
        scroll_embed.add_field(
            name="Current Emoji", 
            value=f"{self.quest_scroll}", 
            inline=True
        )
        scroll_embed.add_field(
            name="Preview", 
            value=f"{self.quest_scroll} Your Quest Name", 
            inline=True
        )
        
        await interaction.response.send_message(embed=scroll_embed, view=emoji_view, ephemeral=True)
    
    @discord.ui.button(label="Create Quest", style=discord.ButtonStyle.success, emoji="✅", row=1)
    async def create_quest(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Prevent double-tapping
        if self.creating_quest:
            await interaction.response.send_message("⏳ Quest is already being created. Please wait...", ephemeral=True)
            return
        
        if not self.quest_name or not self.quest_description:
            await interaction.response.send_message("❌ Please set both Quest Name and Description before creating the quest.", ephemeral=True)
            return
        
        # Set flag to prevent double execution
        self.creating_quest = True
        
        # Send immediate confirmation (this speeds up response!)
        await interaction.response.send_message("✅ Quest Created Successfully! Processing...", ephemeral=True)
        
        # Save quest to Google Sheets
        try:
            print(f"Debug: Creating quest for leader {self.leader.display_name} (ID: {self.leader.id})")
            print(f"Debug: Leader info: {self.leader_info}")
            patrol_id = await self.save_quest_to_sheets()
            print(f"Debug: Quest saved with patrol ID: {patrol_id}")
            
            # Add Crusade prefix to quest name if it's a Crusade for display purposes
            display_quest_name = self.quest_name
            if self.quest_type == "Crusade":
                display_quest_name = f"🏛️ | {self.quest_name}"
            
            # Post to forum channel
            forum_post = await self.post_to_forum(interaction.guild, patrol_id)
            print(f"Debug: Forum post result: {forum_post}")
            
            if forum_post:
                # Update the sheet with forum thread info
                await self.update_sheet_with_forum_info(patrol_id, forum_post)
                
                # Create simple confirmation embed with quest image as thumbnail
                confirmation_embed = discord.Embed(
                    title="✅ Quest Created Successfully!",
                    description=f"**{display_quest_name}**\nhas been posted to <#{forum_post.id}>",
                    color=0x00ff00  # Green color for success
                )
                
                # Add quest image as thumbnail
                guild_settings = self.quest_cog.get_guild_settings(self.guild_id)
                image_url = self.quest_image or guild_settings.get("default_quest_image", "")
                if image_url:
                    confirmation_embed.set_thumbnail(url=image_url)
                
                # Remove the original quest maker embed
                try:
                    if self.original_interaction:
                        await self.original_interaction.delete_original_response()
                except Exception as edit_error:
                    print(f"Debug: Could not delete original interaction: {edit_error}")
                
                # Remove the template loading message if it exists
                try:
                    if self.template_interaction:
                        await self.template_interaction.delete_original_response()
                except Exception as template_error:
                    print(f"Could not delete template interaction: {template_error}")
                
                # Remove the image upload message if it exists
                try:
                    if self.image_upload_interaction:
                        await self.image_upload_interaction.delete_original_response()
                except Exception as image_error:
                    print(f"Could not delete image upload interaction: {image_error}")
                
                # Remove the emoji picker message if it exists
                try:
                    if self.emoji_picker_interaction:
                        await self.emoji_picker_interaction.delete_original_response()
                except Exception as emoji_error:
                    print(f"Could not delete emoji picker interaction: {emoji_error}")
                
                # Clean up any open ImageUploadView by stopping all views with timeout
                self.stop()
                
                await interaction.edit_original_response(content=None, embed=confirmation_embed)
            else:
                # Create simple failure confirmation embed with quest image as thumbnail
                confirmation_embed = discord.Embed(
                    title="⚠️ Quest Partially Created",
                    description=f"**{display_quest_name}** was saved but forum posting failed.",
                    color=0xffa500  # Orange color for warning
                )
                
                # Add quest image as thumbnail
                guild_settings = self.quest_cog.get_guild_settings(self.guild_id)
                image_url = self.quest_image or guild_settings.get("default_quest_image", "")
                if image_url:
                    confirmation_embed.set_thumbnail(url=image_url)
                
                # Remove the original quest maker embed
                try:
                    if self.original_interaction:
                        await self.original_interaction.delete_original_response()
                except Exception as edit_error:
                    print(f"Debug: Could not delete original interaction: {edit_error}")
                
                # Remove the image upload message if it exists
                try:
                    if self.image_upload_interaction:
                        await self.image_upload_interaction.delete_original_response()
                except Exception as image_error:
                    print(f"Could not delete image upload interaction: {image_error}")
                
                # Remove the emoji picker message if it exists
                try:
                    if self.emoji_picker_interaction:
                        await self.emoji_picker_interaction.delete_original_response()
                except Exception as emoji_error:
                    print(f"Could not delete emoji picker interaction: {emoji_error}")
                
                # Clean up any open ImageUploadView by stopping all views with timeout
                self.stop()
                
                await interaction.edit_original_response(content=None, embed=confirmation_embed)
                
        except Exception as e:
            print(f"Debug: Error creating quest: {e}")
            print(f"Debug: Exception type: {type(e)}")
            import traceback
            traceback.print_exc()
            
            error_embed = discord.Embed(
                title="❌ Quest Creation Failed", 
                description=f"Error: {str(e)}", 
                color=0xff0000
            )
            await interaction.edit_original_response(content=None, embed=error_embed)
        finally:
            # Reset the flag regardless of success or failure
            self.creating_quest = False
    
    async def post_to_forum(self, guild: discord.Guild, patrol_id: str):
        """Post the quest to the forum channel"""
        # Get forum channel from guild-specific settings
        guild_settings = self.quest_cog.get_guild_settings(guild.id)
        forum_channel_id = guild_settings.get("quest_forum_channel")
        if not forum_channel_id:
            print("❌ Forum channel not configured")
            return None
        
        forum_channel = guild.get_channel(forum_channel_id)
        if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
            print("❌ Forum channel not found or not a forum channel")
            return None
        
        # Find the game tag, Quest Started tag, and Crusade tag
        game_tag = None
        quest_started_tag = None
        crusade_tag = None
        
        for tag in forum_channel.available_tags:
            if tag.name == self.game:
                game_tag = tag
            elif tag.name == "Quest Started":
                quest_started_tag = tag
            elif tag.name == "Crusade":
                crusade_tag = tag
        
        # Create "Crusade" tag if it doesn't exist and we need it for a Crusade quest
        if self.quest_type == "Crusade" and not crusade_tag:
            try:
                crusade_tag = await forum_channel.create_tag(
                    name="Crusade",
                    emoji="🏛️",
                    moderated=False
                )
                print(f"✅ Created 'Crusade' forum tag for {forum_channel.name}")
            except discord.HTTPException as e:
                print(f"❌ Failed to create 'Crusade' forum tag: {e}")
            except Exception as e:
                print(f"❌ Error creating 'Crusade' forum tag: {e}")
        
        # Create forum post embed matching the exact format
        embed = discord.Embed(
            title="📜 New Quest Created!",
            color=0x4169E1  # Royal blue color
        )
        
        # Add quest details in the format shown
        embed.add_field(
            name=self.quest_name,
            value=self.quest_description,
            inline=False
        )
        embed.add_field(name="Quest Leader", value=self.leader.mention, inline=True)
        embed.add_field(name="Quest ID", value=f"`{self.quest_id}`", inline=True)
        embed.add_field(name="Quest Duration", value=f"{self.quest_length} minutes", inline=True)
        embed.add_field(name="Rank:", value=self.leader_info.get("rank", "Unknown"), inline=True)
        embed.add_field(name="Role:", value=self.leader_info.get("role", "Unknown"), inline=True)
        
        # Use current local time as the actual quest creation time (matching quest_tracker approach)
        embed.add_field(name="Quest Created", value=f"<t:{int(datetime.now().timestamp())}:R>", inline=True)
        
        embed.set_footer(text="⚔️ - Use 'Join Quest' button to take part!")
        
        # Set thumbnail
        if self.quest_image:
            embed.set_thumbnail(url=self.quest_image)
        
        # Add Crusade prefix to quest name if it's a Crusade for display purposes
        display_quest_name = self.quest_name
        if self.quest_type == "Crusade":
            display_quest_name = f"🏛️ | {self.quest_name}"
        
        # Create view with JoinQuestView
        join_view = JoinQuestView(patrol_id, display_quest_name, self.leader, self.leader_info.get("rank", "Unknown"), self.quest_cog)
        
        # Create the forum thread with proper title format
        try:
            # Apply tags: game tag, Quest Started tag, and Crusade tag if applicable
            applied_tags = []
            if game_tag:
                applied_tags.append(game_tag)
            if quest_started_tag:
                applied_tags.append(quest_started_tag)
            if self.quest_type == "Crusade" and crusade_tag:
                applied_tags.append(crusade_tag)
            
            # Create content with user mention and description above the embed
            content = f"{self.leader.mention}: {self.quest_description}"
            
            thread = await forum_channel.create_thread(
                name=f"{self.quest_scroll} {display_quest_name}",
                content=content,
                embed=embed,
                view=join_view,
                applied_tags=applied_tags
            )
            
            # Register the persistent view with the bot for restart survival
            self.quest_cog.bot.add_view(join_view)
            
            print(f"✅ Quest posted to forum: {thread.thread.name}")
            print(f"✅ Registered persistent view for quest {patrol_id}")
            
            # Update sheet with forum info first
            await self.update_sheet_with_forum_info(patrol_id, thread.thread)
            
            # Create and post the initial roster embed
            try:
                roster_embed = await self.quest_cog.create_roster_embed(patrol_id)
                if roster_embed:
                    roster_message = await thread.thread.send(embed=roster_embed)
                    print(f"🔗 Posted roster message with ID: {roster_message.id}")
                    
                    # Store roster message ID in the sheet
                    await self.update_roster_message_id(patrol_id, roster_message.id)
                    
            except Exception as roster_error:
                print(f"❌ Failed to post roster: {roster_error}")
            
            return thread.thread
            
        except Exception as e:
            print(f"❌ Failed to post to forum: {e}")
            return None
    
    async def update_sheet_with_forum_info(self, patrol_id: str, thread: discord.Thread):
        """Update the Google Sheet with forum thread information"""
        try:
            worksheet = await self.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                return
            
            # Find the row with this patrol ID
            all_values = await self.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            
            for i, row in enumerate(all_values):
                if len(row) > 0 and row[0] == patrol_id:
                    # Update Message ID (column 19) and Thread ID (column 20)
                    row_num = i + 1
                    await self.quest_cog.rate_limited_api_call(
                        worksheet.update,
                        f"S{row_num}:T{row_num}",  # Columns S and T (Message ID and Thread ID)
                        [[str(thread.id), str(thread.id)]]
                    )
                    break
            
            # Clear cache after update
            self.quest_cog.clear_cache("data_Patrols")
        except Exception as e:
            print(f"Error updating sheet with forum info: {e}")

    async def update_roster_message_id(self, patrol_id: str, roster_message_id: int):
        """Update the roster message ID in the Google Sheet"""
        try:
            worksheet = await self.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                return
            
            # Find the row with this patrol ID and update roster message ID
            all_values = await self.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            
            for i, row in enumerate(all_values):
                if len(row) > 0 and row[0] == patrol_id:
                    row_num = i + 1
                    # Update column U (index 20) with roster message ID
                    await self.quest_cog.rate_limited_api_call(
                        worksheet.update_cell,
                        row_num, 21,  # Column U (21st column)
                        str(roster_message_id)
                    )
                    print(f"✅ Stored Roster Message ID {roster_message_id} for quest {patrol_id}")
                    break
            
            # Clear cache after update
            self.quest_cog.clear_cache("data_Patrols")
            
        except Exception as e:
            print(f"Error updating roster message ID: {e}")
    
    async def save_quest_to_sheets(self):
        """Save the quest data to Google Sheets"""
        if not self.quest_cog.gc or not self.quest_cog.sheet:
            raise Exception("Google Sheets not configured")
        
        # Get Patrols worksheet
        worksheet = await self.quest_cog.get_worksheet_cached("Patrols")
        if not worksheet:
            raise Exception("Patrols worksheet not found")
        
        # Check for duplicate quest ID and get current data in one call
        quest_id_to_check = f"P{self.guild_id}-{self.quest_id}"
        current_values = await self.quest_cog.rate_limited_api_call(worksheet.get_all_values)
        
        for row in current_values:
            if len(row) > 0 and row[0] == quest_id_to_check:
                raise Exception(f"Quest {quest_id_to_check} already exists in the database. This may be a duplicate request.")
        
        print(f"✅ Quest ID {quest_id_to_check} is unique, proceeding with creation")
        
        # Prepare quest data matching your CSV structure
        # Format timestamp for Patrol Start Time (when quest is created)
        start_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        
        quest_data = [
            f"P{self.guild_id}-{self.quest_id}",  # Patrol ID with guild prefix for uniqueness
            self.leader.display_name,  # Patrol Leader
            str(self.leader.id),       # Patrol Leader ID
            self.leader_info.get("rank", "Member"),  # Leader Rank (safe access)
            self.quest_name,           # Patrol Name
            self.quest_description,    # Patrol Description
            self.quest_image,          # Patrol Image
            start_time,                # Patrol Start Time (timestamp when quest is created)
            "",                        # Patrol Scheduled end time
            self.quest_length,         # Patrol actual Length
            "",                        # Player ID (empty for now)
            "",                        # Player Name (empty for now)
            "",                        # Player Role (empty for now)
            "",                        # Player Rank (empty for now)
            "",                        # Quest (empty for now)
            "",                        # Fps kills (empty for now)
            "",                        # Ship kills (empty for now)
            "",                        # Time in Service (empty for now)
            "",                        # Message ID (will be set after forum post)
            "",                        # Thread ID (will be set after forum post)
            "",                        # Roster ID (empty for now)
            "",                        # Empty column
            "",                        # Pause Time (empty for now)
            "",                        # Total Paused Duration (empty for now)
            "",                        # Sent for review (empty for now)
            "",                        # Admin Approved (empty for now)
            "",                        # Crusades (empty for now)
            "",                        # AB: (empty)
            str(self.guild_id),        # AC: Guild ID for quest filtering
            "",                        # AD: Turret (empty for now)
            "",                        # AE: (empty)
            self.game,                 # AF: Game
            self.quest_type,           # AG: Quest Type (Quest or Crusade)
        ]
        
        # Add detailed debugging
        print(f"🔄 Attempting to save quest with {len(quest_data)} columns")
        print(f"📝 Quest data preview: {quest_data[:3]}...")
        print(f"📊 Worksheet title: {worksheet.title}")
        
        # Use the current_values we already fetched to determine next row (optimization!)
        print(f"📊 Current sheet has {len(current_values)} rows")
        
        # Find next available row using the same logic as quest joining
        next_available_row = None
        
        # Look for the first completely empty row
        for i, row in enumerate(current_values[1:], start=2):  # Start from row 2 (skip header)
            # Check if the row is completely empty or has only empty cells
            if not row or all(cell.strip() == "" for cell in row):
                next_available_row = i
                break
        
        # If no empty row found, use the next row after the last row with data
        if next_available_row is None:
            next_available_row = len(current_values) + 1
        
        print(f"🔄 Using row {next_available_row} for quest creation")

        # Use manual range update for consistency with quest joining
        range_name = f"A{next_available_row}:AG{next_available_row}"

        try:
            result = await self.quest_cog.rate_limited_api_call(
                worksheet.update,
                range_name,
                [quest_data]
            )
            print(f"✅ Manual update result: {result}")
        except Exception as update_error:
            print(f"❌ Manual update failed: {update_error}")
            print(f"📋 Error type: {type(update_error)}")
            
            # Try append_row as fallback
            print(f"🔄 Trying append_row as fallback...")
            try:
                fallback_result = await self.quest_cog.rate_limited_api_call(worksheet.append_row, quest_data)
                print(f"✅ Fallback append_row result: {fallback_result}")
            except Exception as fallback_error:
                print(f"❌ Fallback also failed: {fallback_error}")
                raise update_error  # Re-raise original error
        
        # Clear relevant caches
        self.quest_cog.clear_cache("data_Patrols")
        
        # Verify the quest was saved (optimized - reduced wait time)
        print(f"🔍 Verifying quest {self.quest_id} was saved...")
        await asyncio.sleep(0.5)  # Reduced from 2 seconds to 0.5 seconds for speed
        
        verification_data = await self.quest_cog.rate_limited_api_call(worksheet.get_all_values)
        quest_id_to_verify = f"P{self.guild_id}-{self.quest_id}"
        
        found = False
        for row in verification_data:
            if len(row) > 0 and row[0] == quest_id_to_verify:
                found = True
                print(f"✅ Verified: Quest {quest_id_to_verify} found in database")
                break
        
        if not found:
            print(f"❌ Warning: Quest {quest_id_to_verify} not found in verification check")
            # Don't raise exception for verification failure - quest likely saved successfully
            print(f"⚠️ Continuing anyway - quest likely saved successfully")
        
        print(f"✅ Quest {self.quest_id} saved to Google Sheets")
        return f"P{self.guild_id}-{self.quest_id}"  # Return the Patrol ID with guild prefix for forum posting
    
    @discord.ui.button(label="Save Template", style=discord.ButtonStyle.secondary, emoji="💾", row=1)
    async def save_template(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.quest_name or not self.quest_description:
            await interaction.response.send_message("❌ Please set both Quest Name and Description before saving template.", ephemeral=True)
            return
        
        modal = SaveTemplateModal(self)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Load Template", style=discord.ButtonStyle.secondary, emoji="📂", row=1)
    async def load_template(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Get templates for this guild
        templates = self.get_guild_templates()
        if not templates:
            await interaction.response.send_message("❌ No templates found for this server.", ephemeral=True)
            return
        
        # Filter templates by the current game (already selected in slash command)
        game_templates = {}
        for template_name, template_data in templates.items():
            if template_data.get('game') == self.game:
                game_templates[template_name] = template_data
        
        if not game_templates:
            await interaction.response.send_message(f"❌ No templates found for {self.game}.", ephemeral=True)
            return
        
        # Create template selection view for this game directly
        template_view = TemplateSelectView(self, game_templates)
        
        # Create embed showing templates for the current game
        template_embed = discord.Embed(
            title=f"📂 Load {self.game} Template",
            description=f"Choose from {len(game_templates)} available template{'s' if len(game_templates) != 1 else ''}:",
            color=0x0099ff
        )
        
        await interaction.response.send_message(embed=template_embed, view=template_view, ephemeral=True)
    
    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="⚙️", row=1)
    async def manage_quest(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user has admin permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only administrators can access template management.", ephemeral=True)
            return
        
        # Create a template management view
        manage_view = TemplateManageView(self)
        
        # Create embed for template management
        manage_embed = discord.Embed(
            title="⚙️ Template Management",
            description="Manage quest templates for this server:",
            color=0x0099ff
        )
        
        # Get template count
        templates = self.get_guild_templates()
        template_count = len(templates)
        
        manage_embed.add_field(
            name="Server Templates", 
            value=f"{template_count} template{'s' if template_count != 1 else ''} available", 
            inline=True
        )
        manage_embed.add_field(
            name="Current Game", 
            value=self.game, 
            inline=True
        )
        
        await interaction.response.send_message(embed=manage_embed, view=manage_view, ephemeral=True)

    def get_guild_templates(self):
        """Get all templates for the current guild"""
        templates_file = f"templates_guild_{self.guild_id}.json"
        if os.path.exists(templates_file):
            with open(templates_file, 'r') as f:
                return json.load(f)
        return {}
    
    def save_guild_template(self, template_name: str):
        """Save a template for the current guild"""
        templates_file = f"templates_guild_{self.guild_id}.json"
        templates = self.get_guild_templates()
        
        templates[template_name] = {
            "name": self.quest_name,
            "description": self.quest_description,
            "image": self.quest_image,
            "length": self.quest_length,
            "scroll": self.quest_scroll,
            "game": self.game
        }
        
        with open(templates_file, 'w') as f:
            json.dump(templates, f, indent=2)
    
    def load_guild_template(self, template_name: str):
        """Load a template for the current guild"""
        templates = self.get_guild_templates()
        if template_name in templates:
            template = templates[template_name]
            self.quest_name = template["name"]
            self.quest_description = template["description"]
            self.quest_image = template["image"]
            self.quest_length = template["length"]
            self.quest_scroll = template.get("scroll", "📜")  # Default to scroll if not in template
            self.game = template.get("game", self.game)  # Load game from template, fallback to current game
            return True
        return False

class QuestNameModal(discord.ui.Modal, title="Set Quest Name"):
    def __init__(self, view: QuestMakerView):
        super().__init__()
        self.view = view
    
    quest_name = discord.ui.TextInput(
        label="Quest Name",
        placeholder="Enter the quest name...",
        max_length=100
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        self.view.quest_name = self.quest_name.value
        embed = self.view.create_embed()
        await interaction.response.edit_message(embed=embed, view=self.view)

class QuestDescriptionModal(discord.ui.Modal, title="Set Quest Description"):
    def __init__(self, view: QuestMakerView):
        super().__init__()
        self.view = view
    
    quest_description = discord.ui.TextInput(
        label="Quest Description",
        placeholder="Enter the quest description...",
        style=discord.TextStyle.paragraph,
        max_length=1000
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        self.view.quest_description = self.quest_description.value
        embed = self.view.create_embed()
        await interaction.response.edit_message(embed=embed, view=self.view)

class QuestImageModal(discord.ui.Modal, title="Set Quest Image"):
    def __init__(self, view: QuestMakerView):
        super().__init__()
        self.view = view
    
    quest_image = discord.ui.TextInput(
        label="Image URL",
        placeholder="1. Upload image to Discord 2. Right-click → Copy Link 3. Paste here",
        required=False,
        max_length=500,
        style=discord.TextStyle.paragraph
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        self.view.quest_image = self.quest_image.value
        embed = self.view.create_embed()
        await interaction.response.edit_message(embed=embed, view=self.view)

class ImageUploadView(discord.ui.View):
    def __init__(self, quest_view: QuestMakerView):
        super().__init__(timeout=60)
        self.quest_view = quest_view
    
    @discord.ui.button(label="Set Image URL", style=discord.ButtonStyle.primary, emoji="🔗")
    async def set_image_url(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = QuestImageModal(self.quest_view)
        await interaction.response.send_modal(modal)

class QuestLengthModal(discord.ui.Modal, title="Set Quest Length"):
    def __init__(self, view: QuestMakerView):
        super().__init__()
        self.view = view
    
    quest_length = discord.ui.TextInput(
        label="Quest Length (minutes)",
        placeholder="Enter the quest length in minutes (e.g., 120)...",
        max_length=4
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            length = int(self.quest_length.value)
            if length <= 0:
                raise ValueError("Length must be positive")
            self.view.quest_length = length
            embed = self.view.create_embed()
            await interaction.response.edit_message(embed=embed, view=self.view)
        except ValueError:
            await interaction.response.send_message("❌ Please enter a valid positive number for quest length.", ephemeral=True)

class QuestScrollModal(discord.ui.Modal, title="Set Quest Scroll"):
    def __init__(self, view: QuestMakerView):
        super().__init__()
        self.view = view
    
    quest_scroll = discord.ui.TextInput(
        label="Quest Scroll Emoji",
        placeholder="Enter an emoji for the quest (e.g., 📜, ⚔️, 🏰)...",
        max_length=10
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        self.view.quest_scroll = self.quest_scroll.value.strip() or "📜"
        embed = self.view.create_embed()
        await interaction.response.edit_message(embed=embed, view=self.view)

class QuestScrollModal(discord.ui.Modal, title="Set Quest Scroll Emoji"):
    def __init__(self, view: QuestMakerView):
        super().__init__()
        self.view = view
    
    quest_scroll = discord.ui.TextInput(
        label="Quest Scroll Emoji",
        placeholder="Click any emoji in Discord to paste here, or type an emoji...",
        max_length=100,  # Allow for custom Discord emojis which are longer
        default=None
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        emoji_input = self.quest_scroll.value.strip()
        
        # If empty, use default
        if not emoji_input:
            self.view.quest_scroll = "📜"
        else:
            # Accept whatever they input - Discord will handle validation
            self.view.quest_scroll = emoji_input
        
        embed = self.view.create_embed()
        await interaction.response.edit_message(embed=embed, view=self.view)

class EmojiSelectView(discord.ui.View):
    def __init__(self, quest_view: QuestMakerView):
        super().__init__(timeout=60)
        self.quest_view = quest_view
        
        # Create dropdown with emoji options
        emoji_options = [
            # Scrolls & Documents
            discord.SelectOption(label="Classic Scroll", emoji="📜", value="📜", description="Traditional quest scroll"),
            discord.SelectOption(label="Quest Log", emoji="📋", value="📋", description="Adventure logbook"),
            discord.SelectOption(label="Document", emoji="📑", value="📑", description="Quest document"),
            discord.SelectOption(label="Parchment", emoji="📃", value="📃", description="Ancient parchment"),
            
            # Fantasy & Adventure  
            discord.SelectOption(label="Crossed Swords", emoji="⚔️", value="⚔️", description="Battle quest"),
            discord.SelectOption(label="Shield", emoji="🛡️", value="🛡️", description="Defense mission"),
            discord.SelectOption(label="Castle", emoji="🏰", value="🏰", description="Castle expedition"),
            discord.SelectOption(label="Dagger", emoji="🗡️", value="🗡️", description="Stealth mission"),
            discord.SelectOption(label="Bow", emoji="🏹", value="🏹", description="Archery quest"),
            discord.SelectOption(label="Magic Wand", emoji="🪄", value="🪄", description="Magic quest"),
            discord.SelectOption(label="Gem", emoji="💎", value="💎", description="Treasure hunt"),
            discord.SelectOption(label="Crown", emoji="👑", value="👑", description="Royal quest"),
            
            # Magic & Mystical
            discord.SelectOption(label="Crystal Ball", emoji="🔮", value="🔮", description="Mystical quest"),
            discord.SelectOption(label="Sparkles", emoji="✨", value="✨", description="Magical adventure"),
            discord.SelectOption(label="Star", emoji="🌟", value="🌟", description="Legendary quest"),
            discord.SelectOption(label="Golden Star", emoji="⭐", value="⭐", description="Epic adventure"),
            
            # Adventure Symbols
            discord.SelectOption(label="Treasure Map", emoji="🗺️", value="🗺️", description="Exploration quest"),
            discord.SelectOption(label="Compass", emoji="🧭", value="🧭", description="Navigation mission"),
            discord.SelectOption(label="Fire", emoji="🔥", value="🔥", description="Intense quest"),
            discord.SelectOption(label="Lightning", emoji="⚡", value="⚡", description="Fast-paced mission"),
            
            # Special Symbols
            discord.SelectOption(label="Crossed Flags", emoji="🎌", value="🎌", description="Guild event"),
            discord.SelectOption(label="Trophy", emoji="🏆", value="🏆", description="Competition quest"),
            discord.SelectOption(label="Target", emoji="🎯", value="🎯", description="Precision mission"),
            discord.SelectOption(label="Rocket", emoji="🚀", value="🚀", description="Launch quest")
        ]
        
        self.add_item(EmojiSelect(emoji_options, quest_view))

class EmojiSelect(discord.ui.Select):
    def __init__(self, options, quest_view: QuestMakerView):
        super().__init__(placeholder="Choose a quest scroll emoji...", options=options, min_values=1, max_values=1)
        self.quest_view = quest_view
    
    async def callback(self, interaction: discord.Interaction):
        selected_emoji = self.values[0]
        
        # Update the quest scroll emoji
        self.quest_view.quest_scroll = selected_emoji
        
        # Update the main quest maker embed
        embed = self.quest_view.create_embed()
        await self.quest_view.original_interaction.edit_original_response(embed=embed, view=self.quest_view)
        
        # Send confirmation and close picker
        await interaction.response.edit_message(
            content=f"✅ Quest scroll emoji set to {selected_emoji}",
            embed=None,
            view=None
        )

class EmojiModalView(discord.ui.View):
    def __init__(self, quest_view: QuestMakerView):
        super().__init__(timeout=60)
        self.quest_view = quest_view
    
    @discord.ui.button(label="Open Emoji Input", style=discord.ButtonStyle.primary, emoji="📝")
    async def open_emoji_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = QuestScrollModal(self.quest_view)
        await interaction.response.send_modal(modal)

class TemplateManageView(discord.ui.View):
    def __init__(self, quest_view: QuestMakerView):
        super().__init__(timeout=60)
        self.quest_view = quest_view
    
    @discord.ui.button(label="View Templates", style=discord.ButtonStyle.primary, emoji="👁️")
    async def view_templates(self, interaction: discord.Interaction, button: discord.ui.Button):
        templates = self.quest_view.get_guild_templates()
        
        if not templates:
            await interaction.response.edit_message(
                content="📂 No templates found for this server.",
                embed=None,
                view=TemplateManageView(self.quest_view)
            )
            return
        
        # Create template list view
        template_list_view = TemplateListView(self.quest_view, templates)
        
        # Create embed showing all templates
        list_embed = discord.Embed(
            title="📂 Server Templates",
            description=f"Found {len(templates)} template{'s' if len(templates) != 1 else ''}:",
            color=0x0099ff
        )
        
        # Group templates by game
        games = {}
        for template_name, template_data in templates.items():
            game = template_data.get('game', 'Unknown')
            if game not in games:
                games[game] = []
            games[game].append(template_name)
        
        # Add fields for each game
        for game, template_names in games.items():
            template_list = "\n".join([f"• {name}" for name in template_names])
            list_embed.add_field(
                name=f"🎮 {game}",
                value=template_list,
                inline=False
            )
        
        await interaction.response.edit_message(embed=list_embed, view=template_list_view)
    
    @discord.ui.button(label="Delete Templates", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_templates(self, interaction: discord.Interaction, button: discord.ui.Button):
        templates = self.quest_view.get_guild_templates()
        
        if not templates:
            await interaction.response.send_message("📂 No templates to delete.", ephemeral=True)
            return
        
        # Create template deletion view
        delete_view = TemplateDeleteView(self.quest_view, templates)
        
        # Create embed for template deletion
        delete_embed = discord.Embed(
            title="🗑️ Delete Templates",
            description="Select templates to delete from the dropdown below:",
            color=0xff0000
        )
        delete_embed.add_field(
            name="⚠️ Warning",
            value="Deleted templates cannot be recovered!",
            inline=False
        )
        
        await interaction.response.edit_message(embed=delete_embed, view=delete_view)

class TemplateListView(discord.ui.View):
    def __init__(self, quest_view: QuestMakerView, templates: dict):
        super().__init__(timeout=60)
        self.quest_view = quest_view
        self.templates = templates
        
        # Add dropdown to preview templates
        template_options = []
        for template_name, template_data in templates.items():
            game = template_data.get('game', 'Unknown')
            description = template_data.get('name', 'No description')[:50]
            template_options.append(discord.SelectOption(
                label=template_name,
                value=template_name,
                description=f"{game}: {description}",
                emoji="📜"
            ))
        
        if template_options:
            self.add_item(TemplatePreviewSelect(template_options, quest_view, templates))
    
    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def back_to_manage(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Go back to template management
        manage_view = TemplateManageView(self.quest_view)
        manage_embed = discord.Embed(
            title="⚙️ Template Management",
            description="Manage quest templates for this server:",
            color=0x0099ff
        )
        template_count = len(self.templates)
        manage_embed.add_field(
            name="Server Templates", 
            value=f"{template_count} template{'s' if template_count != 1 else ''} available", 
            inline=True
        )
        await interaction.response.edit_message(embed=manage_embed, view=manage_view)

class TemplatePreviewSelect(discord.ui.Select):
    def __init__(self, options, quest_view: QuestMakerView, templates: dict):
        super().__init__(placeholder="Choose a template to preview...", options=options)
        self.quest_view = quest_view
        self.templates = templates
    
    async def callback(self, interaction: discord.Interaction):
        template_name = self.values[0]
        template_data = self.templates[template_name]
        
        # Create preview embed
        preview_embed = discord.Embed(
            title=f"📜 Template Preview: {template_name}",
            description="Template details:",
            color=0x0099ff
        )
        
        preview_embed.add_field(name="Game", value=template_data.get('game', 'Unknown'), inline=True)
        preview_embed.add_field(name="Quest Name", value=template_data.get('name', '*Not set*'), inline=True)
        preview_embed.add_field(name="Length", value=f"{template_data.get('length', 60)} minutes", inline=True)
        preview_embed.add_field(name="Scroll Emoji", value=template_data.get('scroll', '📜'), inline=True)
        preview_embed.add_field(name="Description", value=template_data.get('description', '*Not set*'), inline=False)
        
        # Show image if available
        if template_data.get('image'):
            preview_embed.set_image(url=template_data['image'])
        
        await interaction.response.send_message(embed=preview_embed, ephemeral=True)

class TemplateDeleteView(discord.ui.View):
    def __init__(self, quest_view: QuestMakerView, templates: dict):
        super().__init__(timeout=60)
        self.quest_view = quest_view
        self.templates = templates
        
        # Add dropdown to select templates for deletion
        template_options = []
        for template_name, template_data in templates.items():
            game = template_data.get('game', 'Unknown')
            template_options.append(discord.SelectOption(
                label=template_name,
                value=template_name,
                description=f"Game: {game}",
                emoji="🗑️"
            ))
        
        if template_options:
            self.add_item(TemplateDeleteSelect(template_options, quest_view))
    
    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def back_to_manage(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Go back to template management
        manage_view = TemplateManageView(self.quest_view)
        manage_embed = discord.Embed(
            title="⚙️ Template Management",
            description="Manage quest templates for this server:",
            color=0x0099ff
        )
        template_count = len(self.templates)
        manage_embed.add_field(
            name="Server Templates", 
            value=f"{template_count} template{'s' if template_count != 1 else ''} available", 
            inline=True
        )
        await interaction.response.edit_message(embed=manage_embed, view=manage_view)

class TemplateDeleteSelect(discord.ui.Select):
    def __init__(self, options, quest_view: QuestMakerView):
        super().__init__(placeholder="Choose templates to delete...", options=options, min_values=1, max_values=min(len(options), 25))
        self.quest_view = quest_view
    
    async def callback(self, interaction: discord.Interaction):
        selected_templates = self.values
        
        # Create confirmation view
        confirm_view = TemplateDeleteConfirmView(self.quest_view, selected_templates)
        
        # Create confirmation embed
        confirm_embed = discord.Embed(
            title="⚠️ Confirm Template Deletion",
            description=f"Are you sure you want to delete {len(selected_templates)} template{'s' if len(selected_templates) != 1 else ''}?",
            color=0xff0000
        )
        
        template_list = "\n".join([f"• {name}" for name in selected_templates])
        confirm_embed.add_field(
            name="Templates to Delete:",
            value=template_list,
            inline=False
        )
        confirm_embed.add_field(
            name="⚠️ Warning",
            value="This action cannot be undone!",
            inline=False
        )
        
        await interaction.response.edit_message(embed=confirm_embed, view=confirm_view)

class TemplateDeleteConfirmView(discord.ui.View):
    def __init__(self, quest_view: QuestMakerView, template_names: list):
        super().__init__(timeout=30)
        self.quest_view = quest_view
        self.template_names = template_names
    
    @discord.ui.button(label="Yes, Delete", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Delete the selected templates
        templates = self.quest_view.get_guild_templates()
        
        for template_name in self.template_names:
            if template_name in templates:
                del templates[template_name]
        
        # Save updated templates
        templates_file = f"templates_guild_{self.quest_view.guild_id}.json"
        with open(templates_file, 'w') as f:
            json.dump(templates, f, indent=2)
        
        await interaction.response.edit_message(
            content=f"🗑️ Successfully deleted {len(self.template_names)} template{'s' if len(self.template_names) != 1 else ''}.",
            embed=None,
            view=None
        )
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Go back to template management
        manage_view = TemplateManageView(self.quest_view)
        manage_embed = discord.Embed(
            title="⚙️ Template Management",
            description="Manage quest templates for this server:",
            color=0x0099ff
        )
        await interaction.response.edit_message(embed=manage_embed, view=manage_view)

class SaveTemplateModal(discord.ui.Modal, title="Save Template"):
    def __init__(self, view: QuestMakerView):
        super().__init__()
        self.view = view
    
    template_name = discord.ui.TextInput(
        label="Template Name",
        placeholder="Enter a name for this template...",
        max_length=50
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        template_name = self.template_name.value.strip()
        if not template_name:
            await interaction.response.send_message("❌ Please enter a valid template name.", ephemeral=True)
            return
        
        self.view.save_guild_template(template_name)
        await interaction.response.send_message(f"✅ Template '{template_name}' saved successfully!", ephemeral=True)

class GameSelectView(discord.ui.View):
    def __init__(self, quest_view: QuestMakerView):
        super().__init__(timeout=60)
        self.quest_view = quest_view
        
        # Get all templates and extract unique games
        templates = quest_view.get_guild_templates()
        games = set()
        for template_data in templates.values():
            games.add(template_data.get('game', 'Unknown'))
        
        # Create select menu with games
        if games:
            options = []
            for game in sorted(games):
                options.append(discord.SelectOption(
                    label=game,
                    description=f"Load templates for {game}"
                ))
            
            if options:
                self.add_item(GameSelect(options, quest_view, templates))

class GameSelect(discord.ui.Select):
    def __init__(self, options, quest_view: QuestMakerView, templates: dict):
        super().__init__(placeholder="Choose a game first...", options=options)
        self.quest_view = quest_view
        self.templates = templates
    
    async def callback(self, interaction: discord.Interaction):
        selected_game = self.values[0]
        
        # Filter templates by selected game
        game_templates = {}
        for template_name, template_data in self.templates.items():
            if template_data.get('game') == selected_game:
                game_templates[template_name] = template_data
        
        if not game_templates:
            await interaction.response.edit_message(
                content=f"❌ No templates found for {selected_game}.", 
                embed=None, 
                view=None
            )
            return
        
        # Create template selection view for this game
        template_view = TemplateSelectView(self.quest_view, game_templates)
        
        # Update embed to show game-specific template selection
        template_embed = discord.Embed(
            title=f"📂 Select a {selected_game} template:",
            description=f"Choose from {len(game_templates)} available template{'s' if len(game_templates) != 1 else ''}",
            color=0x0099ff
        )
        
        await interaction.response.edit_message(embed=template_embed, view=template_view)

class TemplateSelectView(discord.ui.View):
    def __init__(self, quest_view: QuestMakerView, templates: dict):
        super().__init__(timeout=60)
        self.quest_view = quest_view
        self.templates = templates
        
        # Add select menu with templates
        options = []
        for template_name in templates.keys():
            # Get template description if available, otherwise use quest name
            template_data = templates[template_name]
            description = template_data.get('name', 'No description')
            if len(description) > 50:  # Discord select option description limit
                description = description[:47] + "..."
            
            options.append(discord.SelectOption(
                label=template_name,
                description=description
            ))
        
        if options:
            self.add_item(TemplateSelect(options, quest_view))

class TemplateSelect(discord.ui.Select):
    def __init__(self, options, quest_view: QuestMakerView):
        super().__init__(placeholder="Choose a template to load...", options=options)
        self.quest_view = quest_view
    
    async def callback(self, interaction: discord.Interaction):
        template_name = self.values[0]
        
        try:
            # Defer the interaction first to avoid timeout
            await interaction.response.defer()
            
            if self.quest_view.load_guild_template(template_name):
                # Store this interaction so we can delete it later when quest is created
                self.quest_view.template_interaction = interaction
                # Update the original Quest Maker message
                await self.quest_view.update_original_message()
                # Edit the template selection message to show success
                await interaction.edit_original_response(content="📂 Template loaded successfully!", embed=None, view=None)
            else:
                await interaction.edit_original_response(content="❌ Failed to load template.", embed=None, view=None)
        except Exception as e:
            print(f"Error in template callback: {e}")
            try:
                await interaction.followup.send(f"❌ Error loading template: {e}", ephemeral=True)
            except:
                pass

class JoinQuestView(discord.ui.View):
    # Class-level lock to prevent race conditions during join operations
    _join_lock = asyncio.Lock()
    
    def __init__(self, patrol_id: str, patrol_name: str, patrol_leader: discord.Member, leader_rank: str, quest_cog):
        super().__init__(timeout=None)  # Persistent view
        self.patrol_id = patrol_id
        self.patrol_name = patrol_name
        self.patrol_leader = patrol_leader
        self.leader_rank = leader_rank
        self.quest_cog = quest_cog
        self.processing_users = set()  # Track users currently processing join requests
        
        # Create the join button with unique custom_id
        join_button = discord.ui.Button(
            label="Join Quest",
            style=discord.ButtonStyle.success,
            emoji="🚀",
            custom_id=f"join_quest_{patrol_id}"
        )
        join_button.callback = self.join_quest
        self.add_item(join_button)
        
        # Create the manage quest button with unique custom_id
        manage_button = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            emoji="⚙️",
            custom_id=f"manage_quest_{patrol_id}"
        )
        manage_button.callback = self.manage_quest
        self.add_item(manage_button)

    async def join_quest(self, interaction: discord.Interaction):
        try:
            # Check if this user is already processing a join request
            if interaction.user.id in self.processing_users:
                await interaction.response.send_message("⏳ Your join request is already being processed. Please wait...", ephemeral=True)
                return
            
            # Add user to processing set
            self.processing_users.add(interaction.user.id)
            
            # Defer the response - this allows multiple users to join simultaneously
            await interaction.response.defer(ephemeral=True)
            
            # Look up member data from Member Log tab (outside lock for performance)
            member_log_worksheet = await self.quest_cog.get_worksheet_cached("Member Log")
            member_data = {
                "role": "Member",
                "rank": "Member"
            }
            
            if member_log_worksheet:
                try:
                    member_log_values = await self.quest_cog.rate_limited_api_call(member_log_worksheet.get_all_values)
                    # Look for the user's Discord ID in the Member Log
                    for row in member_log_values[1:]:  # Skip header row
                        # Check both column A (User ID) and column B (Discord ID) for the user ID
                        if ((len(row) > 0 and str(interaction.user.id) == str(row[0])) or 
                            (len(row) > 1 and str(interaction.user.id) == str(row[1]))):
                            # Member Log columns: [C:Rank, D:Role]
                            if len(row) > 2:
                                member_data["rank"] = row[2] if row[2] else "Member"  # C: Rank
                            if len(row) > 3:
                                member_data["role"] = row[3] if row[3] else "Member"  # D: Role
                            break
                except Exception as e:
                    print(f"Error reading Member Log: {e}")
            
            # Look up rank icon from Ranks sheet (outside lock for performance)
            rank_icon_url = None
            ranks_worksheet = await self.quest_cog.get_worksheet_cached("Ranks")
            if ranks_worksheet:
                try:
                    print(f"🔍 Looking up rank icon for rank: {member_data['rank']}")
                    ranks_values = await self.quest_cog.rate_limited_api_call(ranks_worksheet.get_all_values)
                    for row in ranks_values[1:]:  # Skip header row
                        if len(row) > 0 and row[0] == member_data["rank"]:  # Match rank name in column A
                            if len(row) > 2 and row[2]:  # Get rank icon from column C (Rank Icon)
                                rank_icon_url = row[2].strip()  # Remove whitespace
                                print(f"✅ Found rank icon URL: '{rank_icon_url}'")
                                
                                # Validate and convert URL format
                                if not rank_icon_url.startswith(('http://', 'https://')):
                                    print(f"⚠️ Invalid URL format - expected http/https URL but got: '{rank_icon_url}'")
                                    rank_icon_url = None
                                    break
                                
                                # Convert Google Drive share link to direct image URL if needed
                                if "drive.google.com" in rank_icon_url and "/file/d/" in rank_icon_url:
                                    try:
                                        file_id = rank_icon_url.split("/file/d/")[1].split("/")[0]
                                        rank_icon_url = f"https://drive.google.com/uc?export=view&id={file_id}"
                                        print(f"📝 Converted to direct link: {rank_icon_url}")
                                    except Exception as convert_error:
                                        print(f"❌ Failed to convert Google Drive URL: {convert_error}")
                                        rank_icon_url = None
                                        break
                                
                                # Final validation
                                if rank_icon_url and len(rank_icon_url) > 2000:  # Discord URL limit
                                    print(f"⚠️ URL too long ({len(rank_icon_url)} chars): {rank_icon_url[:100]}...")
                                    rank_icon_url = None
                                
                                break
                    if not rank_icon_url:
                        print(f"⚠️ No valid rank icon found for rank: {member_data['rank']}")
                except Exception as e:
                    print(f"Error reading Ranks sheet: {e}")
                    rank_icon_url = None
            else:
                print(f"❌ Could not access Ranks worksheet")
            
            # Use class-level lock to prevent race conditions when multiple users join simultaneously
            # This critical section includes: checking existence, finding available row, and writing data
            async with self._join_lock:
                # Check if user is already in this quest
                worksheet = await self.quest_cog.get_worksheet_cached("Patrols")
                if not worksheet:
                    self.processing_users.discard(interaction.user.id)
                    await interaction.followup.send("❌ Could not access the quest database.", ephemeral=True)
                    return
                
                # Get all values to check for existing participation and get quest data
                all_values = await self.quest_cog.rate_limited_api_call(worksheet.get_all_values)
                user_already_joined = False
                
                for row in all_values:
                    # Check if user is already in this quest by looking at Player ID column (K, index 10)
                    if (len(row) > 10 and row[0] == self.patrol_id and row[10] == str(interaction.user.id)):
                        user_already_joined = True
                        break
                
                if user_already_joined:
                    self.processing_users.discard(interaction.user.id)
                    # Extract clean quest ID from patrol_id (format: P{guild_id}-{quest_id})
                    clean_quest_id = self.patrol_id.split('-')[-1] if '-' in self.patrol_id else self.patrol_id
                    await interaction.followup.send(f"❌ You're already participating in quest `{clean_quest_id}`!", ephemeral=True)
                    return
                
                # Get the original quest row to copy data
                original_quest_data = None
                for row in all_values:
                    if len(row) > 0 and row[0] == self.patrol_id:
                        original_quest_data = row
                        break
                
                if not original_quest_data:
                    self.processing_users.discard(interaction.user.id)
                    # Extract clean quest ID from patrol_id (format: P{guild_id}-{quest_id})
                    clean_quest_id = self.patrol_id.split('-')[-1] if '-' in self.patrol_id else self.patrol_id
                    await interaction.followup.send(f"❌ Could not find quest `{clean_quest_id}` in the database.", ephemeral=True)
                    return
                
                # Check if quest is completed or cancelled
                quest_completed = original_quest_data[30] if len(original_quest_data) > 30 else ""  # Column AE: Quest Completed
                quest_cancelled = original_quest_data[29] if len(original_quest_data) > 29 else ""  # Column AD: Quest Cancelled
                
                if quest_completed:
                    self.processing_users.discard(interaction.user.id)
                    clean_quest_id = self.patrol_id.split('-')[-1] if '-' in self.patrol_id else self.patrol_id
                    await interaction.followup.send(f"❌ This quest has already been completed and is no longer accepting new members.", ephemeral=True)
                    return
                
                if quest_cancelled:
                    self.processing_users.discard(interaction.user.id)
                    clean_quest_id = self.patrol_id.split('-')[-1] if '-' in self.patrol_id else self.patrol_id
                    await interaction.followup.send(f"❌ This quest has been cancelled and is no longer accepting new members.", ephemeral=True)
                    return
                
                # Validate that the quest belongs to the same guild
                quest_guild_id = None
                if len(original_quest_data) > 29:  # Guild ID should be at index 29 (AD column)
                    quest_guild_id = original_quest_data[29]
                
                if quest_guild_id and str(interaction.guild.id) != str(quest_guild_id):
                    self.processing_users.discard(interaction.user.id)
                    await interaction.followup.send(f"❌ This quest belongs to a different server. You can only join quests from your current server.", ephemeral=True)
                    return
                
                # Get quest type from the original quest data to determine point assignment
                quest_type = "Quest"  # Default fallback
                if len(original_quest_data) > 32:  # AG column (index 32) contains quest type
                    quest_type = original_quest_data[32]
                
                # Assign points based on quest type: 1 point for Quest (Column O) or 1 point for Crusade (Column AA)
                quest_points = "1" if quest_type == "Quest" else "❓"
                crusade_points = "1" if quest_type == "Crusade" else "❓"
                
                # Create new row for the joining member
                join_time = datetime.now()
                
                # Prepare row data for the new participant
                participant_row_data = [
                self.patrol_id,                              # A: Patrol ID (same as original)
                original_quest_data[1] if len(original_quest_data) > 1 else "❓",  # B: Quest Leader Name
                original_quest_data[2] if len(original_quest_data) > 2 else "❓",  # C: Quest Leader ID
                original_quest_data[3] if len(original_quest_data) > 3 else "❓",  # D: Leader Rank
                original_quest_data[4] if len(original_quest_data) > 4 else "❓",  # E: Quest Name
                original_quest_data[5] if len(original_quest_data) > 5 else "❓",  # F: Quest Description
                original_quest_data[6] if len(original_quest_data) > 6 else "❓",  # G: Quest Image
                original_quest_data[7] if len(original_quest_data) > 7 else "❓",  # H: Quest Start Time
                original_quest_data[8] if len(original_quest_data) > 8 else "❓",  # I: Quest Scheduled end time
                "❓",                                          # J: Quest actual end time
                str(interaction.user.id),                    # K: Player ID
                interaction.user.display_name,               # L: Player Name
                member_data["role"],                         # M: Player Role
                member_data["rank"],                         # N: Player Rank
                quest_points,                                # O: Quest Points (1 if Quest, ❓ if Crusade)
                "❓",                                          # P: Ground Kills (reserved)
                "❓",                                          # Q: Pilot Kills (reserved)
                "❓",                                          # R: Time in Service
                original_quest_data[18] if len(original_quest_data) > 18 else "❓",  # S: Message ID
                original_quest_data[19] if len(original_quest_data) > 19 else "❓",  # T: Thread ID
                original_quest_data[20] if len(original_quest_data) > 20 else "❓",  # U: Roster ID
                join_time.strftime('%Y-%m-%d %H:%M:%S'),     # V: Join Time
                "",                                          # W: Pause Time (empty for participants)
                "",                                          # X: Total Paused Duration (empty for participants)
                "",                                          # Y: Sent for review (empty for participants)
                "",                                          # Z: Admin Approved (empty for participants)
                crusade_points,                              # AA: Crusade Points (1 if Crusade, ❓ if Quest)
                "",                                          # AB: Turret Kills (empty)
                str(interaction.guild.id),                   # AC: Guild ID (always use current guild)
                "",                                          # AD: Turret (empty for participants)
                "",                                          # AE: (empty)
                original_quest_data[31] if len(original_quest_data) > 31 else "❓",  # AF: Game
                quest_type,                                  # AG: Quest Type (same as original)
                ]
                
                # Find next available row using the same logic as quest creation
                print(f"🔄 Adding participant with {len(participant_row_data)} columns")
                print(f"📊 Current sheet has {len(all_values)} rows")
                
                next_available_row = None
                
                # Look for the first completely empty row
                for i, row in enumerate(all_values[1:], start=2):  # Start from row 2 (skip header)
                    # Check if the row is completely empty or has only empty cells
                    if not row or all(cell.strip() == "" for cell in row):
                        next_available_row = i
                        break
                
                # If no empty row found, use the next row after the last row with data
                if next_available_row is None:
                    next_available_row = len(all_values) + 1
                
                print(f"🔄 Using row {next_available_row} for participant joining")

                # Use manual range update for consistency with quest creation
                range_name = f"A{next_available_row}:AG{next_available_row}"

                try:
                    await self.quest_cog.rate_limited_api_call(
                        worksheet.update,
                        range_name,
                        [participant_row_data]
                    )
                    print(f"✅ Successfully added participant to quest {self.patrol_id} at row {next_available_row}")
                except Exception as update_error:
                    print(f"❌ Manual update failed: {update_error}")
                    print(f"📋 Error type: {type(update_error)}")
                    
                    # Try append_row as fallback
                    print(f"🔄 Trying append_row as fallback...")
                    try:
                        fallback_result = await self.quest_cog.rate_limited_api_call(worksheet.append_row, participant_row_data)
                        print(f"✅ Fallback append_row result: {fallback_result}")
                    except Exception as fallback_error:
                        print(f"❌ Fallback also failed: {fallback_error}")
                        raise update_error  # Re-raise original error
            
            # End of critical section - lock is released here
            
            # Create simple success embed
            embed = discord.Embed(
                title="✅ Successfully Joined Quest!",
                description=f"You've joined **{self.patrol_name}**",
                color=0x00ff00
            )
            
            # Set rank icon as author icon if available and valid
            if rank_icon_url and rank_icon_url.startswith(('http://', 'https://')):
                try:
                    embed.set_author(name=f"{member_data['rank']}", icon_url=rank_icon_url)
                    print(f"✅ Set author icon for join embed: {rank_icon_url}")
                except Exception as e:
                    print(f"❌ Failed to set author icon: {e}")
            
            # Set quest image as thumbnail if available
            if len(original_quest_data) > 6 and original_quest_data[6] and original_quest_data[6] != "❓":
                embed.set_thumbnail(url=original_quest_data[6])
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
            # Send a notification card to the thread about the new member
            notification_embed = discord.Embed(
                description=f"{interaction.user.mention} joined **{self.patrol_name}**",
                color=0x0099ff
            )
            
            # Add user's Discord avatar as thumbnail
            notification_embed.set_thumbnail(url=interaction.user.display_avatar.url)
            
            # Add rank information with icon if available and valid
            if rank_icon_url and rank_icon_url.startswith(('http://', 'https://')):
                try:
                    notification_embed.add_field(name="Rank", value=member_data["rank"], inline=True)
                    notification_embed.set_author(name=f"{member_data['rank']}", icon_url=rank_icon_url)
                    print(f"✅ Set author icon for notification embed: {rank_icon_url}")
                except Exception as e:
                    print(f"❌ Failed to set notification author icon: {e}")
                    notification_embed.add_field(name="Rank", value=member_data["rank"], inline=True)
            else:
                notification_embed.add_field(name="Rank", value=member_data["rank"], inline=True)
                print(f"⚠️ No valid rank icon URL for notification embed")
            
            # Always add role as second field inline with rank
            notification_embed.add_field(name="Role", value=member_data["role"], inline=True)
            
            # Set footer with the "New Quest Member" text
            notification_embed.set_footer(text="🆕 New Quest Member")
            
            await interaction.channel.send(embed=notification_embed)
            
            # Update the roster embed
            await self.update_roster_embed()
            
        except Exception as e:
            print(f"Error joining quest: {e}")
            try:
                await interaction.followup.send(f"❌ Error joining quest: {e}", ephemeral=True)
            except:
                pass
        finally:
            # Always remove user from processing set regardless of success or failure
            self.processing_users.discard(interaction.user.id)
    
    async def update_roster_embed(self):
        """Update the roster embed after someone joins"""
        try:
            # Get the roster message ID and thread ID from the sheet
            worksheet = await self.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                return
            
            all_values = await self.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            roster_message_id = None
            thread_id = None
            
            # Find the quest row and get IDs
            for row in all_values:
                if len(row) > 0 and row[0] == self.patrol_id:
                    if len(row) > 20:  # U: Roster Message ID
                        roster_message_id = row[20] if row[20] else None
                    if len(row) > 19:  # T: Thread ID  
                        thread_id = row[19] if row[19] else None
                    break
            
            if roster_message_id and thread_id:
                # Get the thread and roster message
                guild = self.patrol_leader.guild
                target_thread = guild.get_channel(int(thread_id))
                
                if not target_thread:
                    target_thread = await guild.fetch_channel(int(thread_id))
                
                if target_thread and isinstance(target_thread, discord.Thread):
                    try:
                        roster_message = await target_thread.fetch_message(int(roster_message_id))
                        
                        # Generate updated roster embed
                        updated_roster_embed = await self.quest_cog.create_roster_embed(self.patrol_id)
                        if updated_roster_embed:
                            await roster_message.edit(embed=updated_roster_embed)
                            print(f"✅ Updated roster for quest {self.patrol_id}")
                    except discord.NotFound:
                        print(f"❌ Roster message {roster_message_id} not found in thread {thread_id}")
                    except Exception as msg_error:
                        print(f"❌ Error updating roster message: {msg_error}")
        except Exception as e:
            print(f"Error updating roster: {e}")
    
    async def manage_quest(self, interaction: discord.Interaction):
        # Check if user is quest leader or admin
        is_leader = interaction.user.id == self.patrol_leader.id
        is_admin = interaction.user.guild_permissions.administrator
        
        if not (is_leader or is_admin):
            await interaction.response.send_message("❌ Only the quest leader or administrators can manage this quest.", ephemeral=True)
            return
        
        # Create quest management view
        manage_view = ActiveQuestManageView(self)
        
        # Create management embed
        manage_embed = discord.Embed(
            title="⚙️ Quest Management",
            description=f"Manage **{self.patrol_name}**",
            color=0x0099ff
        )
        manage_embed.add_field(name="Quest ID", value=f"`{self.patrol_id}`", inline=True)
        manage_embed.add_field(name="Quest Leader", value=self.patrol_leader.mention, inline=True)
        manage_embed.add_field(name="Your Role", value="Quest Leader" if is_leader else "Administrator", inline=True)
        
        await interaction.response.send_message(embed=manage_embed, view=manage_view, ephemeral=True)

class ActiveQuestManageView(discord.ui.View):
    def __init__(self, join_quest_view: "JoinQuestView"):
        super().__init__(timeout=60)
        self.join_quest_view = join_quest_view
    
    @discord.ui.button(label="Complete Quest", style=discord.ButtonStyle.success, emoji="✅")
    async def complete_quest(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if all players have recorded points first
        points_check_result = await self.check_all_points_recorded()
        
        if not points_check_result['all_recorded']:
            # Some players haven't recorded points - show helpful message first
            missing_count = len(points_check_result.get('missing_players', []))
            await interaction.response.send_message(
                f"📊 **Points Recording Required**\n\n"
                f"Before completing the quest, {missing_count} player(s) still need their points recorded.\n"
                f"The Record Points interface will open below to complete this step.",
                ephemeral=True
            )
            
            # Now show the Record Quest Points interface
            await self.show_quest_points_interface(interaction)
            return
        
        # All points recorded - complete the quest directly (no confirmation needed)
        try:
            worksheet = await self.join_quest_view.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                await interaction.response.edit_message(content="❌ Failed to access quest database.", embed=None, view=None)
                return
            
            # Find the quest row
            all_values = await self.join_quest_view.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            quest_row_index = None
            
            for i, row in enumerate(all_values):
                if len(row) > 0 and row[0] == self.join_quest_view.patrol_id:
                    quest_row_index = i + 1  # Google Sheets is 1-indexed
                    break
            
            if quest_row_index is None:
                await interaction.response.edit_message(content="❌ Quest not found in database.", embed=None, view=None)
                return
            
            # Update quest status to completed
            completion_time = datetime.now().isoformat()
            await self.join_quest_view.quest_cog.rate_limited_api_call(
                worksheet.update_cell, 
                quest_row_index, 8, "Quest Completed"  # Column H: Status
            )
            await self.join_quest_view.quest_cog.rate_limited_api_call(
                worksheet.update_cell, 
                quest_row_index, 11, completion_time  # Column K: Completion Time
            )
            
            print(f"✅ Quest {self.join_quest_view.patrol_id} completed at {completion_time}")
            
            # Update the thread title to show completion
            thread = interaction.channel
            if isinstance(thread, discord.Thread):
                # Apply the "Quest Completed" tag to the thread
                available_tags = thread.parent.available_tags if hasattr(thread.parent, 'available_tags') else []
                quest_completed_tag = None
                
                for tag in available_tags:
                    if tag.name.lower() == "quest completed":
                        quest_completed_tag = tag
                        break
                
                if quest_completed_tag:
                    try:
                        current_tags = list(thread.applied_tags) if thread.applied_tags else []
                        if quest_completed_tag not in current_tags:
                            current_tags.append(quest_completed_tag)
                            await thread.edit(applied_tags=current_tags)
                            print(f"✅ Applied 'Quest Completed' tag to thread {thread.id}")
                    except Exception as e:
                        print(f"Failed to apply quest completed tag: {e}")
            
            # Create completion embed using the same format as Record Points but for completion
            # Get quest details and all player data with proper rank/role system
            member_log_worksheet = await self.join_quest_view.quest_cog.get_worksheet_cached("Member Log")
            
            # Get Member Log data for rank lookups
            member_log_data = {}
            if member_log_worksheet:
                try:
                    member_log_values = await self.join_quest_view.quest_cog.rate_limited_api_call(member_log_worksheet.get_all_values)
                    for row in member_log_values[1:]:  # Skip header row
                        if len(row) > 2:
                            # Store Discord ID mapping to rank
                            if len(row) > 1 and row[1]:
                                member_log_data[str(row[1])] = row[2] if len(row) > 2 and row[2] else "Member"
                except Exception as e:
                    print(f"Error getting member log data: {e}")
            
            # Get quest details from database using correct columns
            quest_name = "Unknown Quest"
            quest_description = ""
            quest_image = ""
            quest_game = ""
            leader_name = ""
            quest_start_time = None
            
            # Find quest details and leader rank from database
            leader_rank = ""
            leader_role = ""
            leader_id = ""
            quest_emoji = "🎉"  # Default emoji
            quest_type = "Quest"  # Default quest type
            for row in all_values:
                if len(row) > 0 and row[0] == self.join_quest_view.patrol_id:
                    leader_name = row[1] if len(row) > 1 else ""  # Column B: Patrol Leader
                    quest_game = row[2] if len(row) > 2 else ""  # Column C: Game
                    leader_rank = row[3] if len(row) > 3 else ""  # Column D: Leader Rank
                    quest_name = row[4] if len(row) > 4 else "Unknown Quest"  # Column E: Patrol Name
                    quest_description = row[5] if len(row) > 5 else ""  # Column F: Patrol Description
                    quest_image = row[6] if len(row) > 6 else ""  # Column G: Image
                    quest_start_time = row[9] if len(row) > 9 else None  # Column J: Start time
                    leader_id = row[10] if len(row) > 10 else ""  # Column K: Leader ID for Member Log lookup
                    quest_type = row[32] if len(row) > 32 else "Quest"  # Column AG: Quest Type
                    
                    # Try to extract quest emoji from thread name (if available)
                    # The quest emoji is typically stored in the thread title
                    if hasattr(self.join_quest_view, 'quest_cog') and self.join_quest_view.quest_cog:
                        try:
                            # Check if we can get the forum thread to extract emoji
                            thread_id = row[19] if len(row) > 19 else ""  # Column T: Thread ID
                            if thread_id:
                                # For now, use default emoji, but this could be enhanced to pull from thread
                                quest_emoji = "📜"  # Default quest emoji
                        except:
                            pass
                    break
            
            # Get leader role from Member Log using leader ID
            if leader_id and member_log_worksheet:
                try:
                    member_log_values = await self.join_quest_view.quest_cog.rate_limited_api_call(member_log_worksheet.get_all_values)
                    for row in member_log_values[1:]:  # Skip header row
                        if len(row) > 0 and str(row[0]) == str(leader_id):  # Column A: Discord ID
                            leader_role = row[3] if len(row) > 3 else ""  # Column D: Role
                            break
                except Exception as e:
                    print(f"Error getting leader role from Member Log: {e}")
            
            # Calculate quest duration
            duration_text = "Unknown"
            if quest_start_time:
                try:
                    if isinstance(quest_start_time, str):
                        start_time = datetime.fromisoformat(quest_start_time.replace('Z', '+00:00'))
                    else:
                        start_time = quest_start_time
                    end_time = datetime.now()
                    duration = end_time - start_time
                    hours = int(duration.total_seconds() // 3600)
                    minutes = int((duration.total_seconds() % 3600) // 60)
                    if hours > 0:
                        duration_text = f"{hours}h {minutes}m"
                    else:
                        duration_text = f"{minutes}m"
                except:
                    duration_text = "Unknown"
            
            # Get all quest participants with their points
            completed_quest_players = []
            try:
                for row in all_values:
                    if (len(row) > 0 and row[0] == self.join_quest_view.patrol_id and 
                        len(row) > 11 and row[11]):  # Has player name
                        
                        player_id = row[10] if len(row) > 10 else ""
                        player_name = row[11] if len(row) > 11 else ""
                        
                        if player_id and player_name:
                            # Get rank and role from the correct columns
                            player_role = row[12] if len(row) > 12 else ""  # Column M: Player Role
                            player_rank = row[13] if len(row) > 13 else ""  # Column N: Player Rank
                            
                            # Use rank if available, otherwise use role, otherwise default to "Member"
                            if player_rank and player_rank.strip():
                                display_rank = player_rank
                            elif player_role and player_role.strip():
                                display_rank = player_role
                            else:
                                display_rank = "Member"
                            
                            # Calculate time in quest from Join Quest TS (Column V)
                            join_timestamp = row[21] if len(row) > 21 else ""  # Column V: Join Quest TS
                            time_in_quest = "Unknown"
                            if join_timestamp:
                                try:
                                    join_time = datetime.fromisoformat(join_timestamp.replace('Z', '+00:00'))
                                    current_time = datetime.now()
                                    time_diff = current_time - join_time
                                    time_minutes = int(time_diff.total_seconds() // 60)
                                    if time_minutes < 60:
                                        time_in_quest = f"{time_minutes}m"
                                    else:
                                        time_hours = time_minutes // 60
                                        time_mins = time_minutes % 60
                                        time_in_quest = f"{time_hours}h {time_mins}m"
                                except:
                                    time_in_quest = "Unknown"
                            
                            # Get player rank and all 5 point categories from correct columns
                            player_rank = row[13] if len(row) > 13 else "❓"     # Column N: Player Rank
                            quest_points = row[14] if len(row) > 14 else "❓"    # Column O: Quest Points
                            ground_kills = row[15] if len(row) > 15 else "❓"    # Column P: Ground Kills
                            pilot_kills = row[16] if len(row) > 16 else "❓"     # Column Q: Pilot Kills
                            crusade_points = row[26] if len(row) > 26 else "❓"  # Column AA: Crusades
                            griefer_kills = row[27] if len(row) > 27 else "❓"   # Column AB: Griefer
                            
                            completed_quest_players.append({
                                'player_name': player_name,
                                'player_id': player_id,
                                'player_rank': player_rank,  # Use rank from Google Sheets column N
                                'player_role': row[12] if len(row) > 12 else "",  # Column M: Player Role
                                'time_in_quest': time_in_quest,
                                'quest_points': quest_points,
                                'ground_kills': ground_kills,
                                'pilot_kills': pilot_kills,
                                'crusade_points': crusade_points,
                                'griefer_kills': griefer_kills
                            })
            except Exception as e:
                print(f"Error getting quest participants: {e}")
            
            # Format leader display with rank and role on separate lines
            leader_display = f"**Leader:** {leader_name}"
            if leader_rank and leader_rank.strip() and leader_role and leader_role.strip():
                leader_display += f"\n**Rank:** {leader_rank} | **Role:** {leader_role}"
            elif leader_rank and leader_rank.strip():
                leader_display += f"\n**Rank:** {leader_rank}"
            elif leader_role and leader_role.strip():
                leader_display += f"\n**Role:** {leader_role}"
            
            # Extract just the 4-digit quest number from patrol_id (e.g., P533127850409328670-1082 -> 1082)
            quest_number = self.join_quest_view.patrol_id.split('-')[-1] if '-' in self.join_quest_view.patrol_id else self.join_quest_view.patrol_id
            
            # Add Crusade prefix to quest name if it's a Crusade
            display_quest_name = quest_name
            if quest_type == "Crusade":
                display_quest_name = f"🏛️ | {quest_name}"
            
            # Create the simplified completion embed
            completed_embed = discord.Embed(
                title=f"{quest_emoji} Quest Completed Successfully!",
                description=f"**{display_quest_name}** has been completed!\n\n{leader_display}",
                color=0x00ff00
            )
            
            # Add quest image if available
            if quest_image and quest_image.startswith(('http://', 'https://')):
                completed_embed.set_image(url=quest_image)
            
            # Add footer with completion info
            completed_embed.set_footer(text=f"✅ Quest Completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | ID: {quest_number}")
            
            # Send as a new message that STAYS VISIBLE (no auto-delete)
            await interaction.response.send_message(embed=completed_embed)
            
            # Update forum thread tags - remove "Quest Started" and add "Quest Completed"
            await self.update_forum_thread_with_completed_tag(interaction)
            
            # Update Google Sheets with completion timestamp and send to admin review
            await self.send_quest_for_admin_review(display_quest_name, leader_name, leader_rank, leader_role, completed_quest_players, quest_image)
            
        except Exception as e:
            print(f"Error completing quest: {e}")
            await interaction.response.edit_message(content=f"❌ Error completing quest: {str(e)}", embed=None, view=None)

    async def check_all_points_recorded(self):
        """Check if all quest participants have recorded points"""
        try:
            # Get quest data from Google Sheets
            worksheet = await self.join_quest_view.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                return {'all_recorded': True, 'missing_players': [], 'error': 'Could not access database'}
            
            # Find all players in this quest
            all_values = await self.join_quest_view.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            quest_players = []
            missing_players = []
            
            for i, row in enumerate(all_values[1:], start=2):  # Skip header, start from row 2
                if len(row) > 0 and row[0] == self.join_quest_view.patrol_id:  # Column A: Patrol ID
                    player_id = row[10] if len(row) > 10 else ""  # Column K: Player ID
                    player_name = row[11] if len(row) > 11 else ""  # Column L: Player Name
                    
                    if player_id and player_name:  # Skip leader row or empty player data
                        # Check if points have been recorded (look for non-zero values in point columns)
                        player_rank = row[13] if len(row) > 13 else ""      # Column N: Player Rank
                        quest_points = row[14] if len(row) > 14 else ""     # Column O: Quest Points
                        ground_kills = row[15] if len(row) > 15 else ""     # Column P: Ground Kills
                        pilot_kills = row[16] if len(row) > 16 else ""      # Column Q: Pilot Kills
                        crusade_points = row[26] if len(row) > 26 else ""   # Column AA: Crusades
                        griefer_kills = row[27] if len(row) > 27 else ""    # Column AB: Griefer
                        
                        # More strict validation - check for actual valid kill stats (not auto-assigned quest/crusade points)
                        def has_valid_kill_stats(value):
                            if not value or not str(value).strip():
                                return False
                            
                            value_str = str(value).strip()
                            
                            # Check for placeholder emojis/text
                            if value_str in ["❓", "?", "TBD", "N/A", "n/a", "None", "null"]:
                                return False
                            
                            # Check if it's a Discord ID (typically 17-19 digits)
                            if value_str.isdigit() and len(value_str) >= 15:
                                return False
                            
                            try:
                                num_val = float(value_str)
                                # Valid kill stats should be reasonable numbers (0-10000)
                                return 0 <= num_val <= 10000
                            except (ValueError, TypeError):
                                return False
                        
                        # Check if kill stats have been recorded (excluding auto-assigned quest/crusade points)
                        # Points are considered "recorded" if at least one kill stat has been entered
                        kill_stats_recorded = any([
                            has_valid_kill_stats(ground_kills),
                            has_valid_kill_stats(pilot_kills),
                            has_valid_kill_stats(griefer_kills)
                        ])
                        
                        print(f"ActiveQuestManageView - Player {player_name}: QP={quest_points}, GK={ground_kills}, PK={pilot_kills}, CP={crusade_points}, GrK={griefer_kills} -> Recorded: {kill_stats_recorded}")
                        
                        quest_players.append({
                            'player_name': player_name,
                            'player_id': player_id,
                            'points_recorded': kill_stats_recorded
                        })
                        
                        if not kill_stats_recorded:
                            missing_players.append(player_name)
            
            all_recorded = len(missing_players) == 0
            print(f"ActiveQuestManageView - Points check: {len(quest_players)} players, {len(missing_players)} missing points")
            print(f"ActiveQuestManageView - Missing players: {missing_players}")
            
            return {
                'all_recorded': all_recorded,
                'missing_players': missing_players,
                'total_players': len(quest_players),
                'quest_players': quest_players
            }
            
        except Exception as e:
            print(f"Error checking points recorded: {e}")
            return {'all_recorded': True, 'missing_players': [], 'error': str(e)}

    @discord.ui.button(label="Record Points", style=discord.ButtonStyle.secondary, emoji="📊")
    async def record_points(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Show quest points recording interface
        await self.show_quest_points_interface(interaction)

    @discord.ui.button(label="Manage Participants", style=discord.ButtonStyle.secondary, emoji="👥")
    async def manage_participants(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show participant management interface with remove options"""
        try:
            # Check if user is admin or quest leader
            is_admin = interaction.user.guild_permissions.administrator
            is_leader = interaction.user.id == self.join_quest_view.patrol_leader.id
            
            if not is_admin and not is_leader:
                await interaction.response.send_message("❌ Only quest leaders and administrators can manage participants.", ephemeral=True)
                return
            
            await interaction.response.defer(ephemeral=True)
            
            # Get quest participants from the Patrols sheet
            worksheet = await self.join_quest_view.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                await interaction.followup.send("❌ Could not access the quest database.", ephemeral=True)
                return
            
            all_values = await self.join_quest_view.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            participant_data = []
            
            print(f"🔍 Looking for participants in quest: {self.join_quest_view.patrol_id}")
            print(f"📊 Total rows in sheet: {len(all_values)}")
            
            for i, row in enumerate(all_values[1:], start=2):  # Skip header, start from row 2
                if len(row) > 0 and row[0] == self.join_quest_view.patrol_id:
                    player_id = row[10] if len(row) > 10 else ""     # K: Player ID
                    player_name = row[11] if len(row) > 11 else ""   # L: Player Name
                    player_role = row[12] if len(row) > 12 else ""   # M: Player Role
                    player_rank = row[13] if len(row) > 13 else ""   # N: Player Rank
                    
                    print(f"📝 Found quest row {i}: ID={player_id}, Name={player_name}, Role={player_role}, Rank={player_rank}")
                    
                    # Skip the quest leader row (they have different data structure)
                    if player_id and player_name and player_id != "❓" and player_id != str(self.join_quest_view.patrol_leader.id):
                        # Format as [player_name, player_rank, player_role, row_index] for ParticipantManagementView
                        participant_data.append([player_name, player_rank, player_role, i])  # Add row index for deletion
                        print(f"✅ Added participant: {player_name} ({player_rank}) - {player_role}")
            
            print(f"👥 Total participants found: {len(participant_data)}")
            
            if not participant_data:
                await interaction.followup.send("❌ No participants found for this quest.", ephemeral=True)
                return
            
            # Create participant management view with properly formatted data
            participant_view = ParticipantManagementView(self, participant_data)
            
            # Create embed showing participants
            embed = discord.Embed(
                title="👥 Manage Quest Participants",
                description=f"**Quest:** {self.join_quest_view.patrol_name}\n\n"
                           f"Select a participant to remove from the quest:",
                color=0x3498db
            )
            
            participant_list = ""
            for i, participant in enumerate(participant_data, 1):
                player_name = participant[0]
                player_rank = participant[1] if participant[1] else "Unknown"
                player_role = participant[2] if participant[2] else "Unknown"
                participant_list += f"**{i}.** {player_name} ({player_rank}) - {player_role}\n"
            
            embed.add_field(name=f"Participants ({len(participant_data)})", value=participant_list, inline=False)
            embed.set_footer(text="⚠️ Removing a participant will permanently delete their quest data!")
            
            await interaction.followup.send(embed=embed, view=participant_view, ephemeral=True)
            
        except Exception as e:
            print(f"❌ Error in manage_participants: {e}")
            print(f"📋 Error type: {type(e)}")
            import traceback
            traceback.print_exc()
            
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(f"❌ Error managing participants: {str(e)}", ephemeral=True)
                else:
                    await interaction.response.send_message(f"❌ Error managing participants: {str(e)}", ephemeral=True)
            except:
                print("❌ Failed to send error message to user")
    
    @discord.ui.button(label="Cancel Quest", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel_quest(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Create confirmation view
        confirm_view = QuestCancelConfirmView(self.join_quest_view)
        
        # Create confirmation embed
        confirm_embed = discord.Embed(
            title="⚠️ Cancel Quest",
            description=f"Are you sure you want to cancel **{self.join_quest_view.patrol_name}**?",
            color=0xff0000
        )
        confirm_embed.add_field(
            name="What happens when cancelled:",
            value="• Quest will be marked as 'Quest Cancelled'\n• Cancellation timestamp will be recorded\n• Participants will see the quest is cancelled",
            inline=False
        )
        confirm_embed.add_field(
            name="⚠️ Warning",
            value="This action cannot be undone!",
            inline=False
        )
        
        await interaction.response.edit_message(embed=confirm_embed, view=confirm_view)

    async def show_quest_points_interface(self, interaction: discord.Interaction):
        """Show the quest points recording interface"""
        try:
            # Check if interaction has already been responded to
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            
            # Get quest data from Google Sheets
            worksheet = await self.join_quest_view.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                error_msg = "❌ Could not access the quest database."
                if interaction.response.is_done():
                    await interaction.followup.send(error_msg, ephemeral=True)
                else:
                    await interaction.response.send_message(error_msg, ephemeral=True)
                return
            
            # Get Member Log data for rank lookups
            member_log_worksheet = await self.join_quest_view.quest_cog.get_worksheet_cached("Member Log")
            member_log_data = {}
            if member_log_worksheet:
                try:
                    member_log_values = await self.join_quest_view.quest_cog.rate_limited_api_call(member_log_worksheet.get_all_values)
                    for row in member_log_values[1:]:  # Skip header row
                        if len(row) > 2:
                            # Store Discord ID mapping to rank
                            if len(row) > 1 and row[1]:
                                member_log_data[str(row[1])] = row[2] if len(row) > 2 and row[2] else "Member"
                except Exception as e:
                    print(f"Error reading Member Log: {e}")
            
            # Find all players in this quest
            all_values = await self.join_quest_view.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            quest_players = []
            
            for i, row in enumerate(all_values[1:], start=2):  # Skip header, start from row 2
                if len(row) > 0 and row[0] == self.join_quest_view.patrol_id:  # Column A: Patrol ID
                    player_id = row[10] if len(row) > 10 else ""  # Column K: Player ID
                    player_name = row[11] if len(row) > 11 else ""  # Column L: Player Name
                    
                    if player_id and player_name:  # Skip leader row or empty player data
                        # Get rank and role from the correct columns
                        player_role = row[12] if len(row) > 12 else ""  # Column M: Player Role
                        player_rank = row[13] if len(row) > 13 else ""  # Column N: Player Rank
                        
                        # Use rank if available, otherwise use role, otherwise default to "Member"
                        if player_rank and player_rank.strip():
                            display_rank = player_rank
                        elif player_role and player_role.strip():
                            display_rank = player_role
                        else:
                            display_rank = "Member"
                            
                        join_time_str = row[21] if len(row) > 21 else ""  # Column V: Join Time
                        
                        # Calculate time in quest
                        time_in_quest = "Unknown"
                        if join_time_str:
                            try:
                                from datetime import datetime
                                join_time = datetime.strptime(join_time_str, '%Y-%m-%d %H:%M:%S')
                                current_time = datetime.now()
                                time_diff = current_time - join_time
                                
                                # Format time difference
                                total_minutes = int(time_diff.total_seconds() / 60)
                                hours = total_minutes // 60
                                minutes = total_minutes % 60
                                
                                if hours > 0:
                                    time_in_quest = f"{hours}h {minutes}m"
                                else:
                                    time_in_quest = f"{minutes}m"
                            except:
                                time_in_quest = "Unknown"
                        
                        # Check if kill stats have been recorded (excluding auto-assigned quest/crusade points)
                        # Quest/Crusade points are auto-assigned on join, so we need to check if kill stats are recorded
                        quest_points = row[14] if len(row) > 14 else ""  # Column O: Quest Points (auto-assigned)
                        ground_kills = row[15] if len(row) > 15 else ""  # Column P: Ground Kills
                        pilot_kills = row[16] if len(row) > 16 else ""   # Column Q: Pilot Kills
                        crusade_points = row[26] if len(row) > 26 else "" # Column AA: Crusade Points (auto-assigned)
                        griefer_kills = row[27] if len(row) > 27 else ""  # Column AB: Griefer Kills (index 27)
                        
                        # Check if kill stats have been recorded (excluding auto-assigned quest/crusade points)
                        # Points are considered "recorded" if at least one kill stat has been entered
                        kill_stats_recorded = any([
                            ground_kills and ground_kills != "❓" and ground_kills != "0" and ground_kills.strip(),
                            pilot_kills and pilot_kills != "❓" and pilot_kills != "0" and pilot_kills.strip(),
                            griefer_kills and griefer_kills != "❓" and griefer_kills != "0" and griefer_kills.strip()
                        ])
                        
                        quest_players.append({
                            'row': i,
                            'player_id': player_id,
                            'player_name': player_name,
                            'player_rank': player_rank,
                            'player_role': player_role,
                            'time_in_quest': time_in_quest,
                            'points_recorded': kill_stats_recorded,
                            'current_quest_points': quest_points,
                            'current_ground_kills': ground_kills,
                            'current_pilot_kills': pilot_kills,
                            'current_crusade_points': crusade_points,
                            'current_griefer_kills': griefer_kills
                        })
            
            if not quest_players:
                error_msg = "❌ No players found in this quest to record points for."
                if interaction.response.is_done():
                    await interaction.followup.send(error_msg, ephemeral=True)
                else:
                    await interaction.response.send_message(error_msg, ephemeral=True)
                return
            
            # Create quest points recording view
            quest_points_view = QuestPointsRecordingView(self.join_quest_view.patrol_id, quest_players, self.join_quest_view.quest_cog)
            quest_points_view.set_original_interaction(interaction, self.join_quest_view.patrol_name)
            
            embed = discord.Embed(
                title="📊 Record Quest Points",
                description=f"Select a player to record their quest performance:\n\n**Quest:** {self.join_quest_view.patrol_name}",
                color=0x0099ff
            )
            
            # Add player information
            if quest_players:
                # Limit to 10 players for display to avoid embed limits
                display_players = quest_players[:10]
                
                for i, player in enumerate(display_players):
                    status_icon = "✅" if player['points_recorded'] else "⏳"
                    
                    # Create points display
                    if player['points_recorded']:
                        # Show recorded points with compact formatting
                        points_display = []
                        if player['current_quest_points'] and player['current_quest_points'] != "❓" and player['current_quest_points'].strip():
                            points_display.append(f"📜 {player['current_quest_points']}")
                        if player['current_ground_kills'] and player['current_ground_kills'] != "❓" and player['current_ground_kills'].strip():
                            points_display.append(f"🔫 {player['current_ground_kills']}")
                        if player['current_pilot_kills'] and player['current_pilot_kills'] != "❓" and player['current_pilot_kills'].strip():
                            points_display.append(f"🚀 {player['current_pilot_kills']}")
                        if player['current_crusade_points'] and player['current_crusade_points'] != "❓" and player['current_crusade_points'].strip():
                            points_display.append(f"🏛️ {player['current_crusade_points']}")
                        if player['current_griefer_kills'] and player['current_griefer_kills'] != "❓" and player['current_griefer_kills'].strip():
                            points_display.append(f"💀 {player['current_griefer_kills']}")
                        
                        points_text = " | ".join(points_display) if points_display else "No points recorded"
                        
                        embed.add_field(
                            name=f"{i+1}. {player['player_name']} {status_icon}",
                            value=f"**Rank:** {player['player_rank']} | **Role:** {player['player_role']}\n**Time:** {player['time_in_quest']}\n**Points:** {points_text}",
                            inline=True
                        )
                    else:
                        # Show awaiting status
                        embed.add_field(
                            name=f"{i+1}. {player['player_name']} {status_icon}",
                            value=f"**Rank:** {player['player_rank']} | **Role:** {player['player_role']}\n**Time:** {player['time_in_quest']}\n**Status:** Awaiting recording",
                            inline=True
                        )
                
                # Add spacing if odd number of players (for better layout)
                if len(display_players) % 2 == 1:
                    embed.add_field(name="\u200b", value="\u200b", inline=True)
                
                # Add footer with additional info if there are more players
                if len(quest_players) > 10:
                    embed.set_footer(text=f"Showing 10 of {len(quest_players)} players. Use dropdown to select any player.")
                else:
                    embed.set_footer(text="✅ = Points recorded | ⏳ = Awaiting recording | 📜 Quest 🔫 Ground 🚀 Pilot 🏛️ Crusade 💀 Turret")
            
            # Send the quest points interface
            if interaction.response.is_done():
                # Interaction was already deferred, use followup
                response_message = await interaction.followup.send(embed=embed, view=quest_points_view, ephemeral=True)
            else:
                # Interaction hasn't been responded to yet, use response
                await interaction.response.send_message(embed=embed, view=quest_points_view, ephemeral=True)
                response_message = await interaction.original_response()
            
            quest_points_view.set_message(response_message)
            
        except Exception as e:
            print(f"Error showing quest points interface: {e}")
            # Handle error response based on interaction state
            error_msg = f"❌ Error loading quest data: {str(e)}"
            if interaction.response.is_done():
                await interaction.followup.send(error_msg, ephemeral=True)
            else:
                await interaction.response.send_message(error_msg, ephemeral=True)

class ParticipantManagementView(discord.ui.View):
    def __init__(self, quest_manage_view: "ActiveQuestManageView", participant_data: list):
        super().__init__(timeout=60)
        self.quest_manage_view = quest_manage_view
        self.participant_data = participant_data
        self.add_participant_dropdown()
    
    def add_participant_dropdown(self):
        # Create dropdown options from participant data
        try:
            options = []
            for i, participant in enumerate(self.participant_data):
                # participant format: [player_name, player_rank, player_role, row_index]
                player_name = participant[0] if len(participant) > 0 else f"Participant {i+1}"
                player_rank = participant[1] if len(participant) > 1 else "Unknown"
                player_role = participant[2] if len(participant) > 2 else "Unknown"
                
                # Create display label
                display_label = f"{player_name} ({player_rank})"
                if len(display_label) > 100:  # Discord limit
                    display_label = display_label[:97] + "..."
                
                # Create description
                description = f"Role: {player_role}"
                if len(description) > 100:  # Discord limit
                    description = description[:97] + "..."
                
                options.append(discord.SelectOption(
                    label=display_label,
                    description=description,
                    value=str(i)  # Use index as value
                ))
            
            if options:
                select = ParticipantSelect(options, self)
                self.add_item(select)
                print(f"✅ Added dropdown with {len(options)} participant options")
            else:
                print("⚠️ No options to add to dropdown")
                
        except Exception as e:
            print(f"❌ Error creating participant dropdown: {e}")
            import traceback
            traceback.print_exc()
    
    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="⬅️")
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Return to the main quest management view"""
        try:
            # Re-create the main quest management view
            new_view = ActiveQuestManageView(self.quest_manage_view.join_quest_view)
            
            # Get the original embed from the quest manage view
            original_embed = self.quest_manage_view.join_quest_view.embed
            
            await interaction.response.edit_message(embed=original_embed, view=new_view)
        except Exception as e:
            print(f"Error in back button: {e}")
            await interaction.response.send_message("❌ Error returning to quest management.", ephemeral=True)

class ParticipantSelect(discord.ui.Select):
    def __init__(self, options: list, parent_view: ParticipantManagementView):
        super().__init__(
            placeholder="Select a participant to remove...",
            options=options,
            min_values=1,
            max_values=1
        )
        self.parent_view = parent_view
    
    async def callback(self, interaction: discord.Interaction):
        """Handle participant selection for removal"""
        try:
            selected_index = int(self.values[0])
            selected_participant = self.parent_view.participant_data[selected_index]
            
            # Create confirmation view
            confirm_view = ParticipantRemoveConfirmView(
                self.parent_view.quest_manage_view,
                selected_participant,
                selected_index
            )
            
            # Create confirmation embed
            player_name = selected_participant[0] if len(selected_participant) > 0 else "Unknown"
            player_rank = selected_participant[1] if len(selected_participant) > 1 else "Unknown"
            player_role = selected_participant[2] if len(selected_participant) > 2 else "Unknown"
            
            embed = discord.Embed(
                title="⚠️ Confirm Participant Removal",
                description=f"Are you sure you want to remove this participant from the quest?",
                color=0xff6b6b
            )
            embed.add_field(name="Player Name", value=player_name, inline=True)
            embed.add_field(name="Rank", value=player_rank, inline=True)
            embed.add_field(name="Role", value=player_role, inline=True)
            embed.add_field(
                name="⚠️ Warning", 
                value="This action cannot be undone. All patrol data for this participant will be permanently removed.",
                inline=False
            )
            
            await interaction.response.edit_message(embed=embed, view=confirm_view)
            
        except Exception as e:
            print(f"Error in participant selection: {e}")
            await interaction.response.send_message("❌ Error processing selection.", ephemeral=True)

class ParticipantRemoveConfirmView(discord.ui.View):
    def __init__(self, quest_manage_view: "ActiveQuestManageView", participant_data: list, participant_index: int):
        super().__init__(timeout=30)
        self.quest_manage_view = quest_manage_view
        self.participant_data = participant_data
        self.participant_index = participant_index
    
    @discord.ui.button(label="Confirm Removal", style=discord.ButtonStyle.danger, emoji="❌")
    async def confirm_removal(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Confirm and execute participant removal"""
        try:
            # Get the quest data
            join_quest_view = self.quest_manage_view.join_quest_view
            worksheet = join_quest_view.worksheet
            
            # Get the actual row index from the participant data
            # participant_data format: [player_name, player_rank, player_role, row_index]
            participant_row = self.participant_data[3]  # The row index we stored earlier
            
            # Delete the entire row
            await join_quest_view.quest_cog.rate_limited_api_call(worksheet.delete_rows, participant_row)
            
            player_name = self.participant_data[0] if len(self.participant_data) > 0 else "Unknown"
            
            # Create success embed
            embed = discord.Embed(
                title="✅ Participant Removed",
                description=f"**{player_name}** has been successfully removed from the quest.",
                color=0x28a745
            )
            embed.add_field(
                name="Next Steps",
                value="The quest participant list has been updated. All patrol data for this participant has been removed.",
                inline=False
            )
            
            # Return to main quest management view
            new_view = ActiveQuestManageView(join_quest_view)
            
            await interaction.response.edit_message(embed=embed, view=new_view)
            
        except Exception as e:
            print(f"Error removing participant: {e}")
            error_msg = f"❌ Error removing participant: {str(e)}"
            
            if interaction.response.is_done():
                await interaction.followup.send(error_msg, ephemeral=True)
            else:
                await interaction.response.send_message(error_msg, ephemeral=True)
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel_removal(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel the removal and return to participant management"""
        try:
            # Get fresh participant data
            join_quest_view = self.quest_manage_view.join_quest_view
            worksheet = join_quest_view.worksheet
            
            # Re-fetch participant data
            all_values = await join_quest_view.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            participant_data = []
            
            for i, row in enumerate(all_values[1:], start=2):  # Skip header, start from row 2
                if len(row) > 0 and row[0] == join_quest_view.patrol_id:
                    player_id = row[10] if len(row) > 10 else ""     # K: Player ID
                    player_name = row[11] if len(row) > 11 else ""   # L: Player Name
                    player_role = row[12] if len(row) > 12 else ""   # M: Player Role
                    player_rank = row[13] if len(row) > 13 else ""   # N: Player Rank
                    
                    # Skip the quest leader row (they have different data structure)
                    if player_id and player_name and player_id != "❓" and player_id != str(join_quest_view.patrol_leader.id):
                        participant_data.append([player_name, player_rank, player_role, i])
            
            # Return to participant management
            participant_view = ParticipantManagementView(self.quest_manage_view, participant_data)
            
            # Create participant management embed
            embed = discord.Embed(
                title="👥 Manage Quest Participants",
                description="Select a participant to remove from the quest.",
                color=0x3498db
            )
            
            if participant_data:
                participant_list = []
                for i, participant in enumerate(participant_data):
                    player_name = participant[0] if len(participant) > 0 else f"Participant {i+1}"
                    player_rank = participant[1] if len(participant) > 1 else "Unknown"
                    player_role = participant[2] if len(participant) > 2 else "Unknown"
                    participant_list.append(f"**{player_name}** ({player_rank}) - {player_role}")
                
                embed.add_field(
                    name=f"Current Participants ({len(participant_data)})",
                    value="\n".join(participant_list[:10]) + ("\n..." if len(participant_list) > 10 else ""),
                    inline=False
                )
            else:
                embed.add_field(
                    name="No Participants",
                    value="No participants found in this quest.",
                    inline=False
                )
            
            await interaction.response.edit_message(embed=embed, view=participant_view)
            
        except Exception as e:
            print(f"Error canceling removal: {e}")
            await interaction.response.send_message("❌ Error returning to participant management.", ephemeral=True)

class QuestCompleteConfirmView(discord.ui.View):
    def __init__(self, join_quest_view: "JoinQuestView"):
        super().__init__(timeout=30)
        self.join_quest_view = join_quest_view

    async def check_all_points_recorded(self):
        """Check if all quest participants have recorded points"""
        try:
            # Get quest data from Google Sheets
            worksheet = await self.join_quest_view.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                return {'all_recorded': True, 'missing_players': [], 'error': 'Could not access database'}
            
            # Find all players in this quest
            all_values = await self.join_quest_view.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            quest_players = []
            missing_players = []
            
            for i, row in enumerate(all_values[1:], start=2):  # Skip header, start from row 2
                if len(row) > 0 and row[0] == self.join_quest_view.patrol_id:  # Column A: Patrol ID
                    player_id = row[10] if len(row) > 10 else ""  # Column K: Player ID
                    player_name = row[11] if len(row) > 11 else ""  # Column L: Player Name
                    
                    if player_id and player_name:  # Skip leader row or empty player data
                        # Check if kill stats have been recorded (not just quest/crusade points which are auto-assigned)
                        # Quest/Crusade points are auto-assigned on join, so we need to check if kill stats are recorded
                        quest_points = row[14] if len(row) > 14 else "0"  # Column O: Quest Points (auto-assigned)
                        ground_kills = row[15] if len(row) > 15 else "0"  # Column P: Ground Kills
                        pilot_kills = row[16] if len(row) > 16 else "0"  # Column Q: Pilot Kills
                        crusade_points = row[26] if len(row) > 26 else "0"  # Column AA: Crusades (auto-assigned)
                        griefer_kills = row[27] if len(row) > 27 else "0"  # Column AB: Griefer
                        
                        # Check if kill stats have been recorded (excluding auto-assigned quest/crusade points)
                        # Points are considered "recorded" if at least one kill stat has been entered
                        kill_stats_recorded = any([
                            ground_kills and ground_kills != "0" and ground_kills != "❓" and ground_kills.strip(),
                            pilot_kills and pilot_kills != "0" and pilot_kills != "❓" and pilot_kills.strip(),
                            griefer_kills and griefer_kills != "0" and griefer_kills != "❓" and griefer_kills.strip()
                        ])
                        
                        print(f"ActiveQuestManageView - Player {player_name}: QP={quest_points}, GK={ground_kills}, PK={pilot_kills}, CP={crusade_points}, GrK={griefer_kills} -> Recorded: {kill_stats_recorded}")
                        
                        quest_players.append({
                            'player_name': player_name,
                            'player_id': player_id,
                            'points_recorded': kill_stats_recorded
                        })
                        
                        if not kill_stats_recorded:
                            missing_players.append(player_name)
            
            all_recorded = len(missing_players) == 0
            print(f"Points check: {len(quest_players)} players, {len(missing_players)} missing points")
            print(f"Missing players: {missing_players}")
            
            return {
                'all_recorded': all_recorded,
                'missing_players': missing_players,
                'total_players': len(quest_players),
                'quest_players': quest_players
            }
            
        except Exception as e:
            print(f"Error checking points recorded: {e}")
            return {'all_recorded': True, 'missing_players': [], 'error': str(e)}
    
    @discord.ui.button(label="Yes, Complete Quest", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # MANDATORY POINTS CHECK - Re-verify that all players have recorded points
            points_check = await self.check_all_points_recorded()
            
            if not points_check['all_recorded']:
                # Points are still missing - block completion
                missing_count = len(points_check['missing_players'])
                embed = discord.Embed(
                    title="❌ Cannot Complete Quest",
                    description=f"**{missing_count} player(s) still haven't recorded their points.**\n\nQuest completion requires all participants to record their points first.",
                    color=0xff0000
                )
                
                if missing_count <= 5:
                    missing_list = "\n".join([f"• {name}" for name in points_check['missing_players']])
                    embed.add_field(name="Missing Points:", value=missing_list, inline=False)
                
                embed.add_field(
                    name="Required Action:",
                    value="Use the **'Record Points'** button to record points for all participants before completing the quest.",
                    inline=False
                )
                
                # Return to the manage view
                manage_view = ActiveQuestManageView(self.join_quest_view)
                await interaction.response.edit_message(embed=embed, view=manage_view)
                return
            
            # All points recorded - proceed with completion
            worksheet = await self.join_quest_view.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                await interaction.response.edit_message(content="❌ Failed to access quest database.", embed=None, view=None)
                return
            
            # Find the quest row
            all_values = await self.join_quest_view.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            quest_row_index = None
            
            for i, row in enumerate(all_values):
                if len(row) > 0 and row[0] == self.join_quest_view.patrol_id:
                    quest_row_index = i + 1  # +1 because sheet rows are 1-indexed
                    break
            
            if quest_row_index:
                # Update column AE (Quest Completed) with timestamp
                completion_timestamp = datetime.now().isoformat()
                
                # Column AE is the 31st column (A=1, B=2, ..., AE=31)
                await self.join_quest_view.quest_cog.rate_limited_api_call(
                    worksheet.update_cell, quest_row_index, 31, completion_timestamp
                )
                
                print(f"✅ Quest {self.join_quest_view.patrol_id} completed at {completion_timestamp}")
                
                # Update the forum thread with Quest Completed tag
                await self.update_forum_thread_with_completed_tag(interaction)
                
                await interaction.response.edit_message(
                    content=f"✅ Quest **{self.join_quest_view.patrol_name}** has been marked as completed successfully!",
                    embed=None,
                    view=None
                )
            else:
                await interaction.response.edit_message(
                    content="❌ Quest not found in database.",
                    embed=None,
                    view=None
                )
                
        except Exception as e:
            print(f"Error completing quest: {e}")
            await interaction.response.edit_message(
                content=f"❌ Error completing quest: {e}",
                embed=None,
                view=None
            )
    
    async def update_forum_thread_with_completed_tag(self, interaction: discord.Interaction):
        try:
            # Get the forum channel and find the Quest Completed tag
            if isinstance(interaction.channel, discord.Thread) and isinstance(interaction.channel.parent, discord.ForumChannel):
                forum_channel = interaction.channel.parent
                thread = interaction.channel
                
                # Find the Quest Completed tag
                completed_tag = None
                for tag in forum_channel.available_tags:
                    if tag.name == "Quest Completed":
                        completed_tag = tag
                        break
                
                if completed_tag:
                    # Get current tags and add the completed tag
                    current_tags = list(thread.applied_tags)
                    
                    # Remove Quest Started tag if present and add Quest Completed
                    quest_started_tag = None
                    for tag in forum_channel.available_tags:
                        if tag.name == "Quest Started":
                            quest_started_tag = tag
                            break
                    
                    if quest_started_tag in current_tags:
                        current_tags.remove(quest_started_tag)
                    
                    current_tags.append(completed_tag)
                    
                    # Apply the updated tags
                    await thread.edit(applied_tags=current_tags)
                    print(f"✅ Applied 'Quest Completed' tag to thread {thread.id}")
                else:
                    print("⚠️ 'Quest Completed' tag not found in forum channel")
        except Exception as e:
            print(f"Error updating forum thread tags: {e}")
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Go back to quest management
        manage_view = ActiveQuestManageView(self.join_quest_view)
        manage_embed = discord.Embed(
            title="⚙️ Quest Management",
            description=f"Manage **{self.join_quest_view.patrol_name}**",
            color=0x0099ff
        )
        await interaction.response.edit_message(embed=manage_embed, view=manage_view)

    async def send_quest_for_admin_review(self, quest_name, leader_name, leader_rank, leader_role, completed_quest_players, quest_image):
        """Send completed quest data to admin review channel and update completion timestamp"""
        try:
            # Update Google Sheets with completion timestamp (Column AE)
            worksheet = await self.join_quest_view.quest_cog.get_worksheet_cached("Patrols")
            if worksheet:
                all_values = await self.join_quest_view.quest_cog.rate_limited_api_call(worksheet.get_all_values)
                
                for i, row in enumerate(all_values):
                    if len(row) > 0 and row[0] == self.join_quest_view.patrol_id:
                        quest_row_index = i + 1  # +1 because sheet rows are 1-indexed
                        
                        # Update column AE (Quest Completed) with timestamp
                        completion_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        
                        # Column AE is the 31st column (A=1, B=2, ..., AE=31)
                        await self.join_quest_view.quest_cog.rate_limited_api_call(
                            worksheet.update,
                            f"AE{quest_row_index}",
                            [[completion_timestamp]]
                        )
                        print(f"✅ Updated Quest Completed timestamp for {self.join_quest_view.patrol_id}")
                        break
            
            # Send to review channels in ALL guilds
            for guild in self.join_quest_view.quest_cog.bot.guilds:
                guild_settings = self.join_quest_view.quest_cog.get_guild_settings(guild.id)
                review_channel_id = guild_settings.get("quest_review_channel")
                
                if review_channel_id:
                    review_channel = self.join_quest_view.quest_cog.bot.get_channel(review_channel_id)
                    
                    if review_channel:
                        # Get quest number for display
                        quest_number = self.join_quest_view.patrol_id.split('-')[-1] if '-' in self.join_quest_view.patrol_id else self.join_quest_view.patrol_id
                        
                        # Get quest description from the first message in the thread
                        quest_description = "Quest completed - awaiting admin review"
                        quest_thread = self.join_quest_view.quest_cog.bot.get_channel(int(self.join_quest_view.patrol_id.split('-')[-1]))
                        if quest_thread:
                            try:
                                first_message = None
                                async for message in quest_thread.history(limit=50, oldest_first=True):
                                    if message.embeds:
                                        first_message = message
                                        break
                                
                                if first_message and first_message.embeds:
                                    embed = first_message.embeds[0]
                                    if embed.description:
                                        quest_description = embed.description
                            except Exception as e:
                                print(f"Could not retrieve quest description: {e}")
                        
                        # Create comprehensive admin review embed
                        review_embed = discord.Embed(
                            title="🎯 Quest Results Review",
                            description=f"**{quest_name}**\n{quest_description}",
                            color=0x00ff00
                        )
                        
                        # Add quest leader information
                        leader_info = f"**{leader_name}**"
                        if leader_rank and leader_role:
                            leader_info += f"\n{leader_rank} | {leader_role}"
                        elif leader_rank:
                            leader_info += f"\n{leader_rank}"
                        elif leader_role:
                            leader_info += f"\n{leader_role}"
                        
                        review_embed.add_field(name="🎖️ Quest Leader", value=leader_info, inline=True)
                        
                        # Calculate totals
                        total_participants = len(completed_quest_players)
                        total_quest_points = 0
                        total_ground_kills = 0
                        total_pilot_kills = 0
                        total_crusade_points = 0
                        total_griefer_kills = 0
                        
                        # Add participant roster with scoring
                    if completed_quest_players:
                        roster_chunks = []
                        current_chunk = ""
                        
                        for i, player in enumerate(completed_quest_players, 1):
                            # Calculate individual totals
                            try:
                                quest_pts = int(player['quest_points']) if player['quest_points'] and player['quest_points'] != "❓" and player['quest_points'].strip() and player['quest_points'].isdigit() else 0
                                ground_kills = int(player['ground_kills']) if player['ground_kills'] and player['ground_kills'] != "❓" and player['ground_kills'].strip() and player['ground_kills'].isdigit() else 0
                                pilot_kills = int(player['pilot_kills']) if player['pilot_kills'] and player['pilot_kills'] != "❓" and player['pilot_kills'].strip() and player['pilot_kills'].isdigit() else 0
                                crusade_pts = int(player['crusade_points']) if player['crusade_points'] and player['crusade_points'] != "❓" and player['crusade_points'].strip() and player['crusade_points'].isdigit() else 0
                                griefer_kills = int(player['griefer_kills']) if player['griefer_kills'] and player['griefer_kills'] != "❓" and player['griefer_kills'].strip() and player['griefer_kills'].isdigit() else 0
                                
                                total_quest_points += quest_pts
                                total_ground_kills += ground_kills
                                total_pilot_kills += pilot_kills
                                total_crusade_points += crusade_pts
                                total_griefer_kills += griefer_kills
                            except:
                                pass
                            
                            # Format player line
                            player_rank = player.get('player_rank', 'Unknown')
                            player_role = player.get('player_role', 'Unknown')
                            
                            player_line = f"**{i}. {player['player_name']}** - Rank: {player_rank}"
                            if player_role and player_role != 'Unknown':
                                player_line += f" | Role: {player_role}"
                            player_line += f"\n📜 {player['quest_points']} | 🏛️ {player['crusade_points']} | 🔫 {player['ground_kills']} | 🚀 {player['pilot_kills']} | 💀 {player['griefer_kills']}\n\n"
                            
                            # Check if adding this player would exceed Discord's field limit
                            if len(current_chunk + player_line) > 1000:
                                roster_chunks.append(current_chunk)
                                current_chunk = player_line
                            else:
                                current_chunk += player_line
                        
                        if current_chunk:
                            roster_chunks.append(current_chunk)
                        
                        # Add roster fields
                        for i, chunk in enumerate(roster_chunks):
                            field_name = "👥 Quest Roster & Points" if i == 0 else f"👥 Roster (continued {i+1})"
                            review_embed.add_field(name=field_name, value=chunk.strip(), inline=False)
                        
                        # Add summary totals
                        review_embed.add_field(
                            name="📊 Summary",
                            value=f"**Total Participants:** {total_participants}\n"
                                  f"**Total Quest Points:** {total_quest_points}\n"
                                  f"**Total Crusade Points:** {total_crusade_points}\n"
                                  f"**Total Ground Kills:** {total_ground_kills}\n"
                                  f"**Total Pilot Kills:** {total_pilot_kills}\n"
                                  f"**Total Turret Kills:** {total_griefer_kills}",
                            inline=True
                        )
                    else:
                        review_embed.add_field(name="👥 Quest Roster", value="No participants found", inline=False)
                    
                    # Add quest image if available
                    if quest_image and quest_image.startswith(('http://', 'https://')):
                        review_embed.set_thumbnail(url=quest_image)
                    
                    # Add footer
                    review_embed.set_footer(text="Admins: Click a button below to review this quest")
                    
                    # Create review view with approve/adjust buttons
                    review_view = QuestReviewView(self.join_quest_view.patrol_id, quest_name, self.join_quest_view.quest_cog)
                    
                    # Send to admin review channel
                    await review_channel.send(embed=review_embed, view=review_view)
                    print(f"✅ Sent quest {self.join_quest_view.patrol_id} for admin review")
                else:
                    print("⚠️ Quest review channel not found")
            else:
                print("⚠️ No quest review channel configured")
                
        except Exception as e:
            print(f"Error sending quest for admin review: {e}")

class QuestCancelConfirmView(discord.ui.View):
    def __init__(self, join_quest_view: "JoinQuestView"):
        super().__init__(timeout=30)
        self.join_quest_view = join_quest_view
    
    @discord.ui.button(label="Yes, Cancel Quest", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # Update the quest in the Patrols sheet with cancellation info
            worksheet = await self.join_quest_view.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                await interaction.response.edit_message(content="❌ Failed to access quest database.", embed=None, view=None)
                return
            
            # Find the quest row
            all_values = await self.join_quest_view.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            quest_row_index = None
            
            for i, row in enumerate(all_values):
                if len(row) > 0 and row[0] == self.join_quest_view.patrol_id:
                    quest_row_index = i + 1  # +1 because sheet rows are 1-indexed
                    break
            
            if quest_row_index:
                # Update column AD (Quest Cancelled) with timestamp
                cancellation_timestamp = datetime.now().isoformat()
                
                # Column AD is the 30th column (A=1, B=2, ..., AD=30)
                await self.join_quest_view.quest_cog.rate_limited_api_call(
                    worksheet.update_cell, quest_row_index, 30, cancellation_timestamp
                )
                
                print(f"✅ Quest {self.join_quest_view.patrol_id} cancelled at {cancellation_timestamp}")
                
                # Update the forum thread with Quest Cancelled tag
                await self.update_forum_thread_with_cancelled_tag(interaction)
                
                await interaction.response.edit_message(
                    content=f"❌ Quest **{self.join_quest_view.patrol_name}** has been cancelled successfully.",
                    embed=None,
                    view=None
                )
            else:
                await interaction.response.edit_message(
                    content="❌ Quest not found in database.",
                    embed=None,
                    view=None
                )
                
        except Exception as e:
            print(f"Error cancelling quest: {e}")
            await interaction.response.edit_message(
                content=f"❌ Error cancelling quest: {e}",
                embed=None,
                view=None
            )
    
    async def update_forum_thread_with_cancelled_tag(self, interaction: discord.Interaction):
        try:
            # Get the forum channel and find the Quest Cancelled tag
            if isinstance(interaction.channel, discord.Thread) and isinstance(interaction.channel.parent, discord.ForumChannel):
                forum_channel = interaction.channel.parent
                thread = interaction.channel
                
                # Find the Quest Cancelled tag
                cancelled_tag = None
                for tag in forum_channel.available_tags:
                    if tag.name == "Quest Cancelled":
                        cancelled_tag = tag
                        break
                
                if cancelled_tag:
                    # Get current tags and add the cancelled tag
                    current_tags = list(thread.applied_tags)
                    
                    # Remove Quest Started and Quest Completed tags if present
                    quest_started_tag = None
                    quest_completed_tag = None
                    for tag in forum_channel.available_tags:
                        if tag.name == "Quest Started":
                            quest_started_tag = tag
                        elif tag.name == "Quest Completed":
                            quest_completed_tag = tag
                    
                    if quest_started_tag in current_tags:
                        current_tags.remove(quest_started_tag)
                        print(f"🔄 Removed 'Quest Started' tag from thread {thread.id}")
                    
                    if quest_completed_tag in current_tags:
                        current_tags.remove(quest_completed_tag)
                        print(f"🔄 Removed 'Quest Completed' tag from thread {thread.id}")
                    
                    current_tags.append(cancelled_tag)
                    
                    # Apply the updated tags
                    await thread.edit(applied_tags=current_tags)
                    print(f"✅ Applied 'Quest Cancelled' tag to thread {thread.id}")
                else:
                    print("⚠️ 'Quest Cancelled' tag not found in forum channel")
        except Exception as e:
            print(f"Error updating forum thread tags: {e}")
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Go back to quest management
        manage_view = ActiveQuestManageView(self.join_quest_view)
        manage_embed = discord.Embed(
            title="⚙️ Quest Management",
            description=f"Manage **{self.join_quest_view.patrol_name}**",
            color=0x0099ff
        )
        await interaction.response.edit_message(embed=manage_embed, view=manage_view)

class StartQuest(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Load quest system settings - now guild-specific
        self.settings_dir = "guild_settings"
        # Create settings directory if it doesn't exist
        if not os.path.exists(self.settings_dir):
            os.makedirs(self.settings_dir)
        
        # Settings will be loaded per-guild when needed
        self.guild_settings = {}  # Cache for guild settings
        
        # Quest ID counters are now guild-specific (no global counter needed)
        
        # Google Sheets setup
        self.gc = None
        self.sheet = None
        self.SHEET_URL = "https://docs.google.com/spreadsheets/d/12OiRHpEALj1hzXRxaXgBOWjHtmUT5hg2ztxIgr4J4y8"
        self.setup_google_sheets()
        
        # Sophisticated cache system
        self.data_cache = {}
        self.cache_timestamps = {}
        self.worksheet_cache = {}
        self.worksheet_cache_timestamps = {}
        self.cache_duration = 300  # 5 minutes cache
        self.last_api_call = 0
        self.api_call_delay = 0.5  # Reduced from 1.0 to 0.5 seconds for faster quest creation
        
        # Register persistent views
        self.register_persistent_views()
        
        print("✅ Quest system initialized with cache and Google Sheets")
    
    def setup_google_sheets(self):
        """Setup Google Sheets connection"""
        try:
            self.gc = get_google_credentials()
            if self.gc:
                self.sheet = self.gc.open_by_url(self.SHEET_URL)
                print("✅ Google Sheets integration ready")
            else:
                print("⚠️ Google Sheets disabled - no credentials available")
        except Exception as e:
            print(f"❌ Google Sheets setup failed: {e}")
    
    def register_persistent_views(self):
        """Register persistent views that survive bot restarts"""
        async def setup_views():
            if not self.gc or not self.sheet:
                return
            
            try:
                # Get all active quests from the sheet
                worksheet = await self.get_worksheet_cached("Patrols")
                if not worksheet:
                    return
                
                all_values = await self.rate_limited_api_call(worksheet.get_all_values)
                processed_quests = set()
                
                for row in all_values[1:]:  # Skip header
                    if len(row) > 0:
                        patrol_id = row[0]
                        if patrol_id and patrol_id not in processed_quests:
                            # Check if quest is active (not completed or cancelled)
                            quest_completed = row[30] if len(row) > 30 else ""  # Column AE: Quest Completed
                            quest_cancelled = row[29] if len(row) > 29 else ""  # Column AD: Quest Cancelled
                            
                            # Skip completed or cancelled quests
                            if quest_completed or quest_cancelled:
                                print(f"⚠️ Skipping quest {patrol_id}: quest is completed or cancelled")
                                processed_quests.add(patrol_id)
                                continue
                            
                            # Get quest info for JoinQuestView
                            quest_name = row[4] if len(row) > 4 else "Unknown Quest"
                            # Get quest type and create display name with prefix for Crusade quests
                            quest_type = row[32] if len(row) > 32 else "Quest"  # AG: Quest Type
                            if quest_type == "Crusade":
                                display_quest_name = f"🏛️ | {quest_name}"
                            else:
                                display_quest_name = quest_name
                            leader_id = row[2] if len(row) > 2 else None
                            leader_rank = row[3] if len(row) > 3 else "Unknown"
                            
                            # Validate leader_id is a valid Discord ID (numeric)
                            if leader_id and str(leader_id).strip() and str(leader_id).isdigit():
                                try:
                                    leader_id_int = int(leader_id)
                                    # Try to get the leader member object
                                    leader_found = False
                                    for guild in self.bot.guilds:
                                        leader = guild.get_member(leader_id_int)
                                        if leader:
                                            # Register JoinQuestView for this quest
                                            join_view = JoinQuestView(patrol_id, display_quest_name, leader, leader_rank, self)
                                            self.bot.add_view(join_view)
                                            leader_found = True
                                            break
                                    
                                    if not leader_found:
                                        print(f"⚠️ Leader with ID {leader_id} not found in any guild for quest {patrol_id}")
                                        
                                except ValueError as e:
                                    print(f"⚠️ Invalid leader ID '{leader_id}' for quest {patrol_id}: {e}")
                                except Exception as e:
                                    print(f"⚠️ Failed to register view for quest {patrol_id}: {e}")
                            else:
                                if leader_id and str(leader_id).strip():
                                    # Check if it's placeholder text or invalid data
                                    leader_id_str = str(leader_id).strip()
                                    if "grab" in leader_id_str.lower() or "discord id" in leader_id_str.lower() or len(leader_id_str) > 20:
                                        print(f"⚠️ Skipping quest {patrol_id}: leader_id contains placeholder text - '{leader_id_str[:50]}...'")
                                    else:
                                        print(f"⚠️ Skipping quest {patrol_id}: leader_id '{leader_id}' is not a valid Discord ID (should be numeric)")
                                else:
                                    print(f"⚠️ Skipping quest {patrol_id}: no leader_id found or empty")
                            
                            processed_quests.add(patrol_id)
                
                print(f"✅ Registered persistent views for {len(processed_quests)} active quests")
                
            except Exception as e:
                print(f"❌ Failed to register persistent views: {e}")
        
        # Schedule the setup to run after bot is ready
        async def delayed_setup():
            await asyncio.sleep(2)  # Wait 2 seconds for bot to be fully ready
            await setup_views()
            print(f"✅ Registered persistent views for active quests")
        
        asyncio.create_task(delayed_setup())

    async def create_roster_embed(self, quest_id: str):
        """Create a roster embed showing all quest members"""
        try:
            worksheet = await self.get_worksheet_cached("Patrols")
            if not worksheet:
                return None
            
            # Load Member Log data for batch lookups
            member_log_worksheet = await self.get_worksheet_cached("Member Log")
            member_log_values = []
            if member_log_worksheet:
                try:
                    member_log_values = await self.rate_limited_api_call(member_log_worksheet.get_all_values)
                except Exception as e:
                    print(f"Could not load Member Log: {e}")
                
            all_values = await self.rate_limited_api_call(worksheet.get_all_values)
            quest_members = []
            quest_name = "Unknown Quest"
            display_quest_name = "Unknown Quest"
            
            # Get all members for this quest
            for row in all_values[1:]:  # Skip header
                if len(row) > 0 and row[0] == quest_id:
                    if len(row) > 4:
                        quest_name = row[4]  # E: Quest Name
                        # Get quest type and create display name with prefix for Crusade quests
                        quest_type = row[32] if len(row) > 32 else "Quest"  # AG: Quest Type
                        if quest_type == "Crusade":
                            display_quest_name = f"🏛️ | {quest_name}"
                        else:
                            display_quest_name = quest_name
                    
                    # Get member info: [K:Player ID, L:Player Name, M:Role, N:Rank, V:Join Time]
                    player_id = row[10] if len(row) > 10 else "❓"  # K: Player ID  
                    player_name = row[11] if len(row) > 11 else "❓"  # L: Player Name
                    role = row[12] if len(row) > 12 else "Member"  # M: Role
                    rank = row[13] if len(row) > 13 else "Member"  # N: Rank
                    join_time_str = row[21] if len(row) > 21 else "❓"  # V: Join Time
                    
                    # Only include rows that have a player ID (actual members)
                    if player_id and player_name and player_id != "❓":
                        # Parse join time
                        try:
                            if join_time_str and join_time_str != "❓":
                                join_time_dt = datetime.strptime(join_time_str, '%Y-%m-%d %H:%M:%S')
                                join_time = int(join_time_dt.timestamp())
                            else:
                                join_time = int(datetime.now().timestamp())
                        except:
                            join_time = int(datetime.now().timestamp())
                        
                        quest_members.append({
                            'name': player_name,
                            'rank': rank,
                            'role': role,
                            'join_time': join_time,
                            'player_id': player_id
                        })
            
            # Create roster embed
            roster_embed = discord.Embed(
                title="📋 Quest Roster",
                description=f"**{display_quest_name}**",
                color=0x3498db
            )
            
            if quest_members:
                # Sort members by join time (earliest first)
                quest_members.sort(key=lambda x: x['join_time'])
                
                # Create member list
                member_list = ""
                for i, member in enumerate(quest_members, 1):
                    member_list += f"**{i}.** {member['name']}\n"
                    member_list += f"└ **Rank:** {member['rank']} | **Role:** {member['role']}\n"
                    member_list += f"└ **Joined:** <t:{member['join_time']}:R>\n\n"
                
                # Add members field
                roster_embed.add_field(
                    name="Members",
                    value=member_list[:1024],  # Discord field limit
                    inline=False
                )
                
                # Add summary field
                roster_embed.add_field(
                    name="Total Members",
                    value=f"{len(quest_members)} | Last Updated: <t:{int(datetime.now().timestamp())}:R>",
                    inline=False
                )
            else:
                roster_embed.add_field(
                    name="Members",
                    value="No players have joined this quest yet.",
                    inline=False
                )
            
            return roster_embed
            
        except Exception as e:
            print(f"Error creating roster embed: {e}")
            return None
    
    async def rate_limited_api_call(self, func, *args, **kwargs):
        """Rate limited wrapper for Google Sheets API calls"""
        current_time = time.time()
        time_since_last = current_time - self.last_api_call
        
        if time_since_last < self.api_call_delay:
            await asyncio.sleep(self.api_call_delay - time_since_last)
        
        try:
            result = func(*args, **kwargs)
            self.last_api_call = time.time()
            return result
        except Exception as e:
            print(f"❌ Google Sheets API error: {e}")
            raise e
    
    def get_cached_data(self, cache_key: str):
        """Get cached data if valid, otherwise return None"""
        if cache_key in self.data_cache and cache_key in self.cache_timestamps:
            cache_time = self.cache_timestamps[cache_key]
            if (time.time() - cache_time) < self.cache_duration:
                print(f"📋 Using cached data for: {cache_key}")
                return self.data_cache[cache_key]
        return None
    
    def set_cached_data(self, cache_key: str, data):
        """Store data in cache with timestamp"""
        self.data_cache[cache_key] = data
        self.cache_timestamps[cache_key] = time.time()
        print(f"📋 Cached data for: {cache_key}")
    
    def clear_cache(self, pattern: str = None):
        """Clear cache entries matching pattern"""
        if pattern:
            keys_to_remove = [key for key in self.data_cache.keys() if pattern in key]
            for key in keys_to_remove:
                if key in self.data_cache:
                    del self.data_cache[key]
                if key in self.cache_timestamps:
                    del self.cache_timestamps[key]
            print(f"📋 Cleared {len(keys_to_remove)} cache entries matching: {pattern}")
        else:
            self.data_cache.clear()
            self.cache_timestamps.clear()
            print("📋 Cleared all cache entries")
    
    async def get_worksheet_cached(self, worksheet_name: str):
        """Get worksheet with caching"""
        cache_key = f"worksheet_{worksheet_name}"
        
        # Check worksheet cache
        if cache_key in self.worksheet_cache and cache_key in self.worksheet_cache_timestamps:
            cache_time = self.worksheet_cache_timestamps[cache_key]
            if (time.time() - cache_time) < self.cache_duration:
                return self.worksheet_cache[cache_key]
        
        # Fetch fresh worksheet
        if not self.gc or not self.sheet:
            return None
        
        try:
            worksheet = await self.rate_limited_api_call(self.sheet.worksheet, worksheet_name)
            self.worksheet_cache[cache_key] = worksheet
            self.worksheet_cache_timestamps[cache_key] = time.time()
            return worksheet
        except Exception as e:
            print(f"❌ Failed to get worksheet {worksheet_name}: {e}")
            return None
    
    async def get_worksheet_data_cached(self, worksheet_name: str):
        """Get worksheet data with caching"""
        cache_key = f"data_{worksheet_name}"
        
        # Check cache first
        cached_data = self.get_cached_data(cache_key)
        if cached_data is not None:
            return cached_data
        
        # Fetch fresh data
        worksheet = await self.get_worksheet_cached(worksheet_name)
        if not worksheet:
            return []
        
        try:
            data = await self.rate_limited_api_call(worksheet.get_all_values)
            self.set_cached_data(cache_key, data)
            return data
        except Exception as e:
            print(f"❌ Failed to get data from {worksheet_name}: {e}")
            return []
    
    async def lookup_member_info(self, member_id: str):
        """Lookup member rank and role from Member Log"""
        cache_key = f"member_{member_id}"
        
        # Check cache first
        cached_info = self.get_cached_data(cache_key)
        if cached_info is not None:
            return cached_info
        
        # Fetch from Member Log
        member_log_data = await self.get_worksheet_data_cached("Member Log")
        if not member_log_data:
            return {"rank": "Member", "role": "Member"}
        
        # Look for member in the data
        for row in member_log_data[1:]:  # Skip header
            if len(row) >= 3:  # Ensure we have enough columns
                # Check both User ID (column A) and Discord ID (column B)
                if (len(row) > 0 and str(member_id) == str(row[0])) or \
                   (len(row) > 1 and str(member_id) == str(row[1])):
                    member_info = {
                        "rank": row[2] if len(row) > 2 and row[2] else "Member",
                        "role": row[3] if len(row) > 3 and row[3] else "Member"
                    }
                    # Cache the result
                    self.set_cached_data(cache_key, member_info)
                    return member_info
        
        # Default if not found
        default_info = {"rank": "Member", "role": "Member"}
        self.set_cached_data(cache_key, default_info)
        return default_info
    
    def load_quest_id_counter(self, guild_id: int = None):
        """Load the next quest ID counter for a specific guild or global"""
        if guild_id:
            counter_file = f"quest_id_counter_guild_{guild_id}.json"
        else:
            counter_file = "quest_id_counter.json"
        
        if os.path.exists(counter_file):
            with open(counter_file, 'r') as f:
                data = json.load(f)
                return data.get("next_id", 1000)
        return 1000
    
    def save_quest_id_counter(self, guild_id: int = None):
        """Save the quest ID counter for a specific guild or global"""
        if guild_id:
            counter_file = f"quest_id_counter_guild_{guild_id}.json"
            # Load guild-specific counter
            guild_counter = self.load_quest_id_counter(guild_id)
            with open(counter_file, 'w') as f:
                json.dump({"next_id": guild_counter + 1}, f, indent=2)
        else:
            counter_file = "quest_id_counter.json"
            with open(counter_file, 'w') as f:
                json.dump({"next_id": self.next_quest_id}, f, indent=2)
    
    def get_next_quest_id(self, guild_id: int):
        """Get the next quest ID for a specific guild and increment counter"""
        current_counter = self.load_quest_id_counter(guild_id)
        quest_id = current_counter
        
        # Save the incremented counter
        counter_file = f"quest_id_counter_guild_{guild_id}.json"
        with open(counter_file, 'w') as f:
            json.dump({"next_id": current_counter + 1}, f, indent=2)
        
        return quest_id
    
    async def game_autocomplete(self, interaction: discord.Interaction, current: str):
        """Autocomplete for game selection based on forum tags"""
        choices = []
        
        # Get quest forum channel from guild-specific settings
        guild_settings = self.get_guild_settings(interaction.guild.id)
        forum_channel_id = guild_settings.get("quest_forum_channel")
        if not forum_channel_id:
            return [app_commands.Choice(name="Configure quest forum channel first", value="none")]
        
        # Get the forum channel
        forum_channel = interaction.guild.get_channel(forum_channel_id)
        if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
            return [app_commands.Choice(name="Quest forum channel not found", value="none")]
        
        # Excluded tags (these are quest status/type tags, not game tags)
        excluded_tags = {"Quest Completed", "Quest Started", "Recorded", "Quest Cancelled", "Crusade"}
        
        # Get available tags
        for tag in forum_channel.available_tags:
            if tag.name not in excluded_tags:
                if current.lower() in tag.name.lower():
                    choices.append(app_commands.Choice(name=tag.name, value=tag.name))
        
        # Limit to 25 choices (Discord limit)
        return choices[:25]
    
    def load_settings(self, guild_id: int):
        """Load quest system settings from guild-specific file"""
        settings_file = os.path.join(self.settings_dir, f"quest_settings_{guild_id}.json")
        
        if os.path.exists(settings_file):
            with open(settings_file, 'r') as f:
                settings = json.load(f)
                self.guild_settings[guild_id] = settings
                return settings
        
        # Default settings for new guilds
        default_settings = {
            "quest_review_channel": None, 
            "quest_forum_channel": None,
            "default_quest_image": "https://i.imgur.com/TZWyG45.png",
            "quest_role_id": None  # Role ID for users who can use the quest system
        }
        self.guild_settings[guild_id] = default_settings
        return default_settings
    
    def save_settings(self, guild_id: int):
        """Save quest system settings to guild-specific file"""
        if guild_id not in self.guild_settings:
            return
            
        settings_file = os.path.join(self.settings_dir, f"quest_settings_{guild_id}.json")
        with open(settings_file, 'w') as f:
            json.dump(self.guild_settings[guild_id], f, indent=2)
    
    def get_guild_settings(self, guild_id: int):
        """Get settings for a specific guild, loading them if not cached"""
        if guild_id not in self.guild_settings:
            return self.load_settings(guild_id)
        return self.guild_settings[guild_id]

    @app_commands.command(name="start_quest", description="Start a new quest")
    @app_commands.describe(
        game="Select the game for this quest",
        quest_type="Select whether this is a Quest or Crusade",
        leader="Who will be the quest leader? (Admin/Special Role only)"
    )
    @app_commands.choices(quest_type=[
        app_commands.Choice(name="⚔️ Quest", value="Quest"),
        app_commands.Choice(name="🏛️ Crusade", value="Crusade")
    ])
    async def start_quest(
        self, 
        interaction: discord.Interaction,
        game: str,
        quest_type: str,
        leader: Optional[discord.Member] = None
    ):
        """Start a new quest with specified leader and game"""
        await interaction.response.defer(ephemeral=True)
        
        # Check quest system permissions
        guild_settings = self.get_guild_settings(interaction.guild.id)
        quest_role_id = guild_settings.get("quest_role_id")
        
        # Check if user has permission to use quest system
        user_can_use_quests = False
        
        # Always allow administrators
        if interaction.user.guild_permissions.administrator:
            user_can_use_quests = True
        # Check for specific quest role if configured
        elif quest_role_id:
            user_roles = [role.id for role in interaction.user.roles]
            if quest_role_id in user_roles:
                user_can_use_quests = True
        # If no quest role is configured, allow everyone
        elif quest_role_id is None:
            user_can_use_quests = True
        
        if not user_can_use_quests:
            await interaction.followup.send("❌ You don't have permission to use the quest system.", ephemeral=True)
            return
        
        # Determine the actual quest leader
        if leader:
            # Only admins can choose a different leader
            if not interaction.user.guild_permissions.administrator:
                await interaction.followup.send("❌ Only administrators can choose a different quest leader.", ephemeral=True)
                return
            quest_leader = leader
        else:
            # Default: user becomes the quest leader
            quest_leader = interaction.user
        
        # Lookup leader info from Member Log
        leader_info = await self.lookup_member_info(str(quest_leader.id))
        
        quest_id = self.get_next_quest_id(interaction.guild.id)
        view = QuestMakerView(quest_leader, game, quest_id, interaction.guild.id, self, leader_info, quest_type)
        view.original_interaction = interaction  # Store the original interaction
        embed = view.create_embed()
        
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # Add autocomplete to the start_quest command
    start_quest.autocomplete('game')(game_autocomplete)

    @app_commands.command(name="set_quest_system", description="Configure quest system channels and settings")
    @app_commands.describe(
        quest_review_channel="Channel for quest reviews",
        quest_forum_channel="Forum channel for quests",
        default_image="Default image URL for new quests",
        quest_role="Role that can use the quest system (leave empty to allow everyone)"
    )
    async def set_quest_system(
        self,
        interaction: discord.Interaction,
        quest_review_channel: Optional[discord.TextChannel] = None,
        quest_forum_channel: Optional[discord.ForumChannel] = None,
        default_image: Optional[str] = None,
        quest_role: Optional[discord.Role] = None
    ):
        """Configure the quest system channels and default settings"""
        # Check permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You need **Administrator** permissions to configure quest settings.", ephemeral=True)
            return
        
        guild_id = interaction.guild.id
        
        # Get current settings for this guild
        guild_settings = self.get_guild_settings(guild_id)
        
        # Update settings with new values
        if quest_review_channel:
            guild_settings["quest_review_channel"] = quest_review_channel.id
        if quest_forum_channel:
            guild_settings["quest_forum_channel"] = quest_forum_channel.id
        if default_image:
            guild_settings["default_quest_image"] = default_image
        if quest_role:
            guild_settings["quest_role_id"] = quest_role.id
        
        # Save settings for this guild
        self.save_settings(guild_id)
        
        embed = discord.Embed(title="Quest System Configuration", color=0x00ff00)
        
        if quest_review_channel:
            embed.add_field(name="Quest Review Channel", value=quest_review_channel.mention, inline=False)
        if quest_forum_channel:
            embed.add_field(name="Quest Forum Channel", value=quest_forum_channel.mention, inline=False)
        if quest_role:
            embed.add_field(name="Quest System Role", value=quest_role.mention, inline=False)
        if default_image:
            embed.add_field(name="Default Quest Image", value=f"[Click to view]({default_image})", inline=False)
        
        # Always show the current default image as thumbnail
        current_default_image = guild_settings.get("default_quest_image", "https://i.imgur.com/TZWyG45.png")
        embed.set_thumbnail(url=current_default_image)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="fix_guild_ids", description="Fix missing Guild IDs in patrol records")
    async def fix_guild_ids(self, interaction: discord.Interaction):
        """Fix missing Guild IDs in patrol records by extracting them from Patrol IDs"""
        
        # Check permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You need **Administrator** permissions to use this command.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            await interaction.followup.send("🔍 Checking for missing Guild IDs...")
            
            # Get all patrol data
            worksheet = self.gc.open_by_url(self.sheets_url).worksheet('Patrols')
            all_values = await self.rate_limited_api_call(worksheet.get_all_values)
            
            if not all_values:
                await interaction.followup.send("❌ No data found in Patrols sheet")
                return
            
            data_rows = all_values[1:]  # Skip header
            guild_id_col_index = 28  # AC column (0-indexed)
            patrol_id_col_index = 0  # A column (Patrol ID)
            
            missing_guild_id_rows = []
            
            # Check each row for missing Guild ID
            for row_index, row in enumerate(data_rows, start=2):  # Start at row 2 (after header)
                # Ensure row has enough columns
                while len(row) <= guild_id_col_index:
                    row.append("")
                
                patrol_id = row[patrol_id_col_index].strip() if patrol_id_col_index < len(row) else ""
                guild_id = row[guild_id_col_index].strip() if guild_id_col_index < len(row) else ""
                
                # Check if Guild ID is missing and we have a valid Patrol ID
                if not guild_id and patrol_id.startswith("P") and "-" in patrol_id:
                    try:
                        # Extract guild ID from patrol ID format: P{guild_id}-{quest_number}
                        extracted_guild_id = patrol_id.split("-")[0][1:]  # Remove 'P' prefix
                        if extracted_guild_id.isdigit() and len(extracted_guild_id) > 10:  # Valid Discord ID length
                            missing_guild_id_rows.append({
                                'row_number': row_index,
                                'patrol_id': patrol_id,
                                'extracted_guild_id': extracted_guild_id
                            })
                    except Exception as e:
                        print(f"Could not extract Guild ID from Patrol ID '{patrol_id}': {e}")
            
            if not missing_guild_id_rows:
                await interaction.followup.send("✅ No missing Guild IDs found!")
                return
            
            await interaction.followup.send(f"🔧 Found {len(missing_guild_id_rows)} rows with missing Guild IDs. Fixing them now...")
            
            # Update each row
            fixed_count = 0
            for entry in missing_guild_id_rows:
                try:
                    row_number = entry['row_number']
                    guild_id = entry['extracted_guild_id']
                    patrol_id = entry['patrol_id']
                    
                    # Update the Guild ID column
                    cell_range = f"AC{row_number}"
                    await self.rate_limited_api_call(
                        worksheet.update,
                        cell_range,
                        [[guild_id]]
                    )
                    
                    print(f"✅ Fixed row {row_number}: {patrol_id} -> Guild ID: {guild_id}")
                    fixed_count += 1
                    
                except Exception as e:
                    print(f"❌ Failed to fix row {entry['row_number']}: {e}")
            
            # Clear cache to ensure fresh data
            self.cache.clear()
            
            await interaction.followup.send(f"🎉 Successfully fixed {fixed_count} out of {len(missing_guild_id_rows)} missing Guild IDs!")
            
        except Exception as e:
            await interaction.followup.send(f"❌ Error fixing Guild IDs: {e}")
            print(f"Error in fix_guild_ids: {e}")
            import traceback
            traceback.print_exc()

    @app_commands.command(name="clean_placeholder_quests", description="Remove quest entries with placeholder data")
    async def clean_placeholder_quests(self, interaction: discord.Interaction):
        """Clean up quest entries that have placeholder text instead of real data"""
        # Only allow administrators
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only administrators can use this command.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Get the worksheet
            worksheet = await self.get_worksheet_cached("Patrols")
            if not worksheet:
                await interaction.followup.send("❌ Could not access the quest database.", ephemeral=True)
                return
            
            # Get all data
            all_values = await self.rate_limited_api_call(worksheet.get_all_values)
            
            if not all_values:
                await interaction.followup.send("❌ No data found in Patrols sheet")
                return
            
            placeholder_rows = []
            
            # Check each row for placeholder data
            for row_index, row in enumerate(all_values[1:], start=2):  # Skip header, start at row 2
                if len(row) < 12:  # Need at least enough columns to check leader ID
                    continue
                
                patrol_id = row[0] if len(row) > 0 else ""
                patrol_name = row[4] if len(row) > 4 else ""
                leader_id = row[10] if len(row) > 10 else ""  # Column K: Leader ID
                
                # Check for placeholder text in leader_id
                if leader_id and str(leader_id).strip():
                    leader_id_str = str(leader_id).strip()
                    # Look for placeholder indicators
                    placeholder_indicators = ["grab", "discord id", "patrol", "we will", "user", "placeholder"]
                    
                    if any(indicator in leader_id_str.lower() for indicator in placeholder_indicators) or len(leader_id_str) > 20:
                        placeholder_rows.append({
                            'row_number': row_index,
                            'patrol_id': patrol_id,
                            'patrol_name': patrol_name[:50] + "..." if len(patrol_name) > 50 else patrol_name,
                            'leader_id': leader_id_str[:50] + "..." if len(leader_id_str) > 50 else leader_id_str
                        })
            
            if not placeholder_rows:
                await interaction.followup.send("✅ No placeholder quest entries found!")
                return
            
            # Show what will be deleted and ask for confirmation
            delete_list = "\n".join([f"• Row {entry['row_number']}: {entry['patrol_name']} (ID: {entry['patrol_id']})" 
                                   for entry in placeholder_rows[:10]])  # Limit display to first 10
            
            if len(placeholder_rows) > 10:
                delete_list += f"\n... and {len(placeholder_rows) - 10} more rows"
            
            confirmation_msg = f"🗑️ **Found {len(placeholder_rows)} placeholder quest entries to delete:**\n\n{delete_list}\n\n**⚠️ This action cannot be undone!**\n\nWould you like to proceed with deletion?"
            
            # Create confirmation view
            confirm_view = PlaceholderCleanupConfirmView(self, placeholder_rows)
            
            await interaction.followup.send(confirmation_msg, view=confirm_view, ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(f"❌ Error scanning for placeholder quests: {e}")
            print(f"Error in clean_placeholder_quests: {e}")
            import traceback
            traceback.print_exc()

class PlaceholderCleanupConfirmView(discord.ui.View):
    def __init__(self, quest_cog, placeholder_rows):
        super().__init__(timeout=60)
        self.quest_cog = quest_cog
        self.placeholder_rows = placeholder_rows
    
    @discord.ui.button(label="Yes, Delete Them", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        try:
            worksheet = await self.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                await interaction.followup.send("❌ Could not access the quest database.", ephemeral=True)
                return
            
            deleted_count = 0
            
            # Delete rows in reverse order to avoid row number shifting
            for entry in reversed(self.placeholder_rows):
                try:
                    row_number = entry['row_number']
                    await self.quest_cog.rate_limited_api_call(worksheet.delete_rows, row_number)
                    print(f"✅ Deleted placeholder quest at row {row_number}: {entry['patrol_name']}")
                    deleted_count += 1
                except Exception as e:
                    print(f"❌ Failed to delete row {entry['row_number']}: {e}")
            
            # Clear cache to ensure fresh data
            self.quest_cog.clear_cache()
            
            await interaction.followup.send(f"🎉 Successfully deleted {deleted_count} out of {len(self.placeholder_rows)} placeholder quest entries!")
            
        except Exception as e:
            await interaction.followup.send(f"❌ Error deleting placeholder quests: {e}")
            print(f"Error in confirm_delete: {e}")
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Cleanup cancelled. No data was deleted.", view=None)

class QuestPointsRecordingView(discord.ui.View):
    def __init__(self, patrol_id: str, quest_players: list, quest_cog):
        super().__init__(timeout=300)
        self.patrol_id = patrol_id
        self.quest_players = quest_players
        self.quest_cog = quest_cog
        self.original_interaction = None
        self.quest_name = None
        self.message = None  # Store the actual message object
        
        # Create player selection dropdown with minimal, safe content
        options = []
        
        # Always add "Set Equal Points for All" option at the top as the primary option
        try:
            bulk_option = discord.SelectOption(
                label="📊 Set Points for All",
                value="all_players",
                description="Apply same points to all players"
            )
            options.append(bulk_option)
            print(f"Added bulk option successfully")
        except Exception as e:
            print(f"Error creating bulk option: {e}")
            return
        
        # Add individual players with extremely conservative limits
        # Reduce to absolute minimum to isolate the issue
        max_players = min(5, len(quest_players))  # Start with just 5 players max
        print(f"Creating minimal dropdown: {len(quest_players)} total players, limiting to {max_players}")
        
        for i, player in enumerate(quest_players[:max_players]):
            try:
                # Ultra-conservative approach - minimal text
                player_name = str(player.get('player_name', f'Player {i}'))[:15]  # Very short names
                
                # Create minimal option
                option = discord.SelectOption(
                    label=f"{player_name}",  # No emoji, minimal text
                    value=f"p{i}",  # Very short value
                    description=f"Record for {player_name}"[:25]  # Very short description
                )
                options.append(option)
                print(f"Added player option {i}: '{player_name}' -> 'p{i}'")
                
            except Exception as e:
                print(f"Error creating option for player {i}: {e}")
                break  # Stop on any error
        
        print(f"Total dropdown options created: {len(options)}")
        
        if len(options) < 2:  # Must have at least bulk option + 1 player
            print("ERROR: Not enough valid options created")
            return
        
        # Only create dropdown if we have valid options
        if len(options) >= 2 and len(options) <= 25:  # At least bulk + 1 player, max 25 total
            try:
                print(f"Attempting to create initial dropdown with {len(options)} options")
                select = PlayerSelectionDropdown(options, self.patrol_id, self.quest_players, self.quest_cog, self)
                self.add_item(select)
                print(f"✅ Successfully added initial dropdown with {len(options)} options")
            except Exception as e:
                print(f"❌ Error creating initial dropdown: {e}")
                # Don't add any dropdown if creation fails
        else:
            print(f"❌ Invalid initial dropdown options count: {len(options)} (must be 2-25)")
    
    def set_original_interaction(self, interaction, quest_name):
        """Store the original interaction for later refreshing"""
        self.original_interaction = interaction
        self.quest_name = quest_name
    
    def set_message(self, message):
        """Store the message object for direct editing"""
        self.message = message
    
    async def refresh_embed(self):
        """Refresh the embed to show updated point recording status"""
        if not self.original_interaction:
            return
        
        try:
            # Re-fetch quest data to get updated points status
            worksheet = await self.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                return
            
            # Get Member Log data for rank lookups
            member_log_worksheet = await self.quest_cog.get_worksheet_cached("Member Log")
            member_log_data = {}
            if member_log_worksheet:
                try:
                    member_log_values = await self.quest_cog.rate_limited_api_call(member_log_worksheet.get_all_values)
                    for row in member_log_values[1:]:  # Skip header row
                        if len(row) > 2:
                            # Store Discord ID mapping to rank
                            if len(row) > 1 and row[1]:
                                member_log_data[str(row[1])] = row[2] if len(row) > 2 and row[2] else "Member"
                except Exception as e:
                    print(f"Error reading Member Log: {e}")
            
            # Find all players in this quest with updated status
            all_values = await self.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            updated_quest_players = []
            
            for i, row in enumerate(all_values[1:], start=2):  # Skip header, start from row 2
                if len(row) > 0 and row[0] == self.patrol_id:  # Column A: Patrol ID
                    player_id = row[10] if len(row) > 10 else ""  # Column K: Player ID
                    player_name = row[11] if len(row) > 11 else ""  # Column L: Player Name
                    
                    if player_id and player_name:  # Skip leader row or empty player data
                        # Get rank and role from the correct columns (Patrols tab)
                        player_role = row[12] if len(row) > 12 else ""  # Column M: Player Role
                        player_rank = row[13] if len(row) > 13 else ""  # Column N: Player Rank
                        join_time_str = row[21] if len(row) > 21 else ""  # Column V: Join Time
                        
                        # Calculate time in quest
                        time_in_quest = "Unknown"
                        if join_time_str:
                            try:
                                from datetime import datetime
                                join_time = datetime.strptime(join_time_str, '%Y-%m-%d %H:%M:%S')
                                current_time = datetime.now()
                                time_diff = current_time - join_time
                                
                                # Format time difference
                                total_minutes = int(time_diff.total_seconds() / 60)
                                hours = total_minutes // 60
                                minutes = total_minutes % 60
                                
                                if hours > 0:
                                    time_in_quest = f"{hours}h {minutes}m"
                                else:
                                    time_in_quest = f"{minutes}m"
                            except:
                                time_in_quest = "Unknown"
                        
                        # Check if kill stats have been recorded (excluding auto-assigned quest/crusade points)
                        quest_points = row[14] if len(row) > 14 else ""  # Column O: Quest Points (auto-assigned)
                        ground_kills = row[15] if len(row) > 15 else ""  # Column P: Ground Kills
                        pilot_kills = row[16] if len(row) > 16 else ""   # Column Q: Pilot Kills
                        crusade_points = row[26] if len(row) > 26 else "" # Column AA: Crusade Points (auto-assigned)
                        griefer_kills = row[27] if len(row) > 27 else ""  # Column AB: Griefer Kills (index 27)
                        
                        # Check if kill stats have been recorded (excluding auto-assigned quest/crusade points)
                        kill_stats_recorded = any([
                            ground_kills and ground_kills != "❓" and ground_kills != "0" and ground_kills.strip(),
                            pilot_kills and pilot_kills != "❓" and pilot_kills != "0" and pilot_kills.strip(),
                            griefer_kills and griefer_kills != "❓" and griefer_kills != "0" and griefer_kills.strip()
                        ])
                        
                        updated_quest_players.append({
                            'row': i,
                            'player_id': player_id,
                            'player_name': player_name,
                            'player_rank': player_rank,
                            'player_role': player_role,
                            'time_in_quest': time_in_quest,
                            'points_recorded': kill_stats_recorded,
                            'quest_points': quest_points,
                            'ground_kills': ground_kills,
                            'pilot_kills': pilot_kills,
                            'crusade_points': crusade_points,
                            'griefer_kills': griefer_kills,
                            'current_quest_points': quest_points,
                            'current_ground_kills': ground_kills,
                            'current_pilot_kills': pilot_kills,
                            'current_crusade_points': crusade_points,
                            'current_griefer_kills': griefer_kills
                        })
            
            # Update the quest_players list with new status
            self.quest_players = updated_quest_players
            
            # Create updated embed
            embed = discord.Embed(
                title="📊 Record Quest Points",
                description=f"Select a player to record their quest performance:\n\n**Quest:** {self.quest_name}",
                color=0x0099ff
            )
            
            # Add player information with updated status
            if updated_quest_players:
                # Limit to 10 players for display to avoid embed limits
                display_players = updated_quest_players[:10]
                
                for i, player in enumerate(display_players):
                    status_icon = "✅" if player['points_recorded'] else "⏳"
                    
                    # Create points display
                    if player['points_recorded']:
                        # Show recorded points with compact formatting
                        points_display = []
                        if player['quest_points'] and player['quest_points'] != "❓" and player['quest_points'].strip():
                            points_display.append(f"📜 {player['quest_points']}")
                        if player['ground_kills'] and player['ground_kills'] != "❓" and player['ground_kills'].strip():
                            points_display.append(f"🔫 {player['ground_kills']}")
                        if player['pilot_kills'] and player['pilot_kills'] != "❓" and player['pilot_kills'].strip():
                            points_display.append(f"🚀 {player['pilot_kills']}")
                        if player['crusade_points'] and player['crusade_points'] != "❓" and player['crusade_points'].strip():
                            points_display.append(f"🏛️ {player['crusade_points']}")
                        if player['griefer_kills'] and player['griefer_kills'] != "❓" and player['griefer_kills'].strip():
                            points_display.append(f"💀 {player['griefer_kills']}")
                        
                        points_text = " | ".join(points_display) if points_display else "No points recorded"
                        
                        embed.add_field(
                            name=f"{i+1}. {player['player_name']} {status_icon}",
                            value=f"**Rank:** {player['player_rank']} | **Role:** {player['player_role']}\n**Time:** {player['time_in_quest']}\n**Points:** {points_text}",
                            inline=True
                        )
                    else:
                        # Show awaiting status
                        embed.add_field(
                            name=f"{i+1}. {player['player_name']} {status_icon}",
                            value=f"**Rank:** {player['player_rank']} | **Role:** {player['player_role']}\n**Time:** {player['time_in_quest']}\n**Status:** Awaiting recording",
                            inline=True
                        )
                
                # Add spacing if odd number of players (for better layout)
                if len(display_players) % 2 == 1:
                    embed.add_field(name="\u200b", value="\u200b", inline=True)
                
                # Add footer with additional info if there are more players
                if len(updated_quest_players) > 10:
                    embed.set_footer(text=f"Showing 10 of {len(updated_quest_players)} players. Use dropdown to select any player.")
                else:
                    embed.set_footer(text="✅ = Points recorded | ⏳ = Awaiting recording | 📜 Quest 🔫 Ground 🚀 Pilot 🏛️ Crusade 💀 Griefer")
            
            # Update the existing dropdown options in place instead of recreating the view
            # Find the existing dropdown component
            existing_dropdown = None
            for item in self.children:
                if isinstance(item, PlayerSelectionDropdown):
                    existing_dropdown = item
                    break
            
            if existing_dropdown:
                print("Updating existing dropdown options in place")
                try:
                    # Update the quest_players data in the existing dropdown
                    existing_dropdown.update_quest_players(updated_quest_players)
                    
                    # Try to edit the message with the same view (Discord should handle the component state)
                    if self.message:
                        print("Attempting to edit message with updated embed (keeping existing dropdown)")
                        await self.message.edit(embed=embed, view=self)
                        print("Successfully edited message with updated embed")
                    else:
                        print("Attempting to edit original response with updated embed")
                        await self.original_interaction.edit_original_response(embed=embed, view=self)
                        print("Successfully edited original response with updated embed")
                        
                except discord.HTTPException as e:
                    print(f"Discord HTTP error during in-place update: {e}")
                    if "Must be between 1 and 40 in length" in str(e):
                        print("Component error - falling back to embed-only update")
                        try:
                            if self.message:
                                await self.message.edit(embed=embed, view=None)
                            else:
                                await self.original_interaction.edit_original_response(embed=embed, view=None)
                            print("Successfully updated embed without components")
                        except Exception as fallback_error:
                            print(f"Fallback edit failed: {fallback_error}")
                    else:
                        raise e
            else:
                print("No existing dropdown found, updating embed only")
                try:
                    if self.message:
                        await self.message.edit(embed=embed)
                    else:
                        await self.original_interaction.edit_original_response(embed=embed)
                    print("Successfully updated embed without dropdown")
                except Exception as e:
                    print(f"Failed to update embed: {e}")
            
        except Exception as e:
            print(f"Error refreshing quest points embed: {e}")

    @discord.ui.button(label="Complete Quest", style=discord.ButtonStyle.success, emoji="✅", row=4)
    async def complete_quest(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Complete the quest directly from the points recording interface"""
        try:
            # Disable the button immediately to prevent double-clicks
            button.disabled = True
            
            # Defer the response immediately to prevent timeout
            await interaction.response.defer(ephemeral=True)
            
            # First check if all players have recorded points
            worksheet = await self.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                await interaction.followup.send("❌ Failed to access quest database.", ephemeral=True)
                return
            
            # Get quest data to check points
            all_values = await self.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            quest_players = []
            missing_players = []
            
            for i, row in enumerate(all_values[1:], start=2):  # Skip header
                if len(row) > 0 and row[0] == self.patrol_id:  # Column A: Patrol ID
                    player_id = row[10] if len(row) > 10 else ""  # Column K: Player ID
                    player_name = row[11] if len(row) > 11 else ""  # Column L: Player Name
                    
                    if player_id and player_name:  # Skip leader row or empty player data
                        # Check if kill stats have been recorded (excluding auto-assigned quest/crusade points)
                        # Updated column indices based on our AG column structure
                        quest_points = row[14] if len(row) > 14 else ""      # Column O: Quest Points (auto-assigned)
                        ground_kills = row[15] if len(row) > 15 else ""      # Column P: Ground Kills  
                        pilot_kills = row[16] if len(row) > 16 else ""       # Column Q: Pilot Kills
                        crusade_points = row[26] if len(row) > 26 else ""    # Column AA: Crusade Points (auto-assigned)
                        griefer_kills = row[27] if len(row) > 27 else ""     # Column AB: Griefer Kills
                        
                        # More strict validation - check for actual valid kill stats (not auto-assigned points)
                        def has_valid_kill_stats(value):
                            if not value or not str(value).strip():
                                return False
                            
                            value_str = str(value).strip()
                            
                            # Check for placeholder emojis/text
                            if value_str in ["❓", "?", "TBD", "N/A", "n/a", "None", "null"]:
                                return False
                            
                            # Check if it's a Discord ID (typically 17-19 digits)
                            if value_str.isdigit() and len(value_str) >= 15:
                                return False
                            
                            try:
                                num_val = float(value_str)
                                # Valid kill stats should be reasonable numbers (0-10000)
                                return 0 <= num_val <= 10000
                            except (ValueError, TypeError):
                                return False
                        
                        # Check if kill stats have been recorded (excluding auto-assigned quest/crusade points)
                        kill_stats_recorded = any([
                            has_valid_kill_stats(ground_kills),
                            has_valid_kill_stats(pilot_kills),
                            has_valid_kill_stats(griefer_kills)
                        ])
                        
                        quest_players.append({
                            'player_name': player_name,
                            'points_recorded': kill_stats_recorded
                        })
                        
                        if not kill_stats_recorded:
                            missing_players.append(player_name)
            
            # Check if all players have recorded points
            if len(missing_players) > 0:
                await interaction.followup.send(
                    f"❌ **Cannot complete quest yet**\n\n{len(missing_players)} player(s) still need to record points: {', '.join(missing_players)}\n\nPlease record points for all players before completing the quest.", 
                    ephemeral=True
                )
                return
            
            # All points recorded - proceed with quest completion
            quest_row_index = None
            for i, row in enumerate(all_values):
                if len(row) > 0 and row[0] == self.patrol_id:
                    quest_row_index = i + 1  # Google Sheets is 1-indexed
                    break
            
            if quest_row_index is None:
                await interaction.followup.send("❌ Quest not found in database.", ephemeral=True)
                return
            
            # Update quest status to completed
            from datetime import datetime
            completion_time = datetime.now().isoformat()
            await self.quest_cog.rate_limited_api_call(
                worksheet.update_cell, 
                quest_row_index, 8, "Quest Completed"  # Column H: Status
            )
            await self.quest_cog.rate_limited_api_call(
                worksheet.update_cell, 
                quest_row_index, 11, completion_time  # Column K: Completion Time
            )
            
            print(f"✅ Quest {self.patrol_id} completed at {completion_time}")
            
            # Update the thread title to show completion
            thread = interaction.channel
            if isinstance(thread, discord.Thread):
                # Apply the "Quest Completed" tag to the thread
                available_tags = thread.parent.available_tags if hasattr(thread.parent, 'available_tags') else []
                quest_completed_tag = None
                
                for tag in available_tags:
                    if tag.name.lower() == "quest completed":
                        quest_completed_tag = tag
                        break
                
                if quest_completed_tag:
                    try:
                        current_tags = list(thread.applied_tags) if thread.applied_tags else []
                        if quest_completed_tag not in current_tags:
                            current_tags.append(quest_completed_tag)
                            await thread.edit(applied_tags=current_tags)
                            print(f"✅ Applied 'Quest Completed' tag to thread {thread.id}")
                    except Exception as e:
                        print(f"Failed to apply quest completed tag: {e}")
            
            # Create completion embed using the same format as Record Points but for completion
            # Get quest details and all player data with proper rank/role system
            worksheet = await self.quest_cog.get_worksheet_cached("Patrols")
            member_log_worksheet = await self.quest_cog.get_worksheet_cached("Member Log")
            
            # Get Member Log data for rank lookups
            member_log_data = {}
            if member_log_worksheet:
                try:
                    member_log_values = await self.quest_cog.rate_limited_api_call(member_log_worksheet.get_all_values)
                    for row in member_log_values[1:]:  # Skip header row
                        if len(row) > 2:
                            # Store Discord ID mapping to rank
                            if len(row) > 1 and row[1]:
                                member_log_data[str(row[1])] = row[2] if len(row) > 2 and row[2] else "Member"
                except Exception as e:
                    print(f"Error getting member log data: {e}")
            
            # Get quest details and leader rank/role from database using correct columns
            quest_name = "Unknown Quest"
            quest_description = ""
            quest_image = ""
            quest_game = ""
            leader_name = ""
            leader_rank = ""
            leader_role = ""
            quest_start_time = None
            
            quest_emoji = "🎉"  # Default emoji
            leader_id = ""
            quest_type = "Quest"  # Default quest type
            for row in all_values:
                if len(row) > 0 and row[0] == self.patrol_id:
                    leader_name = row[1] if len(row) > 1 else ""  # Column B: Patrol Leader
                    quest_game = row[2] if len(row) > 2 else ""  # Column C: Game
                    leader_rank = row[3] if len(row) > 3 else ""  # Column D: Leader Rank
                    quest_name = row[4] if len(row) > 4 else "Unknown Quest"  # Column E: Patrol Name
                    quest_description = row[5] if len(row) > 5 else ""  # Column F: Patrol Description
                    quest_image = row[6] if len(row) > 6 else ""  # Column G: Image
                    quest_start_time = row[9] if len(row) > 9 else None  # Column J: Start time
                    leader_id = row[10] if len(row) > 10 else ""  # Column K: Leader ID for Member Log lookup
                    quest_type = row[32] if len(row) > 32 else "Quest"  # Column AG: Quest Type
                    
                    # Try to extract quest emoji from thread name (if available)
                    try:
                        thread_id = row[19] if len(row) > 19 else ""  # Column T: Thread ID
                        if thread_id:
                            quest_emoji = "📜"  # Default quest emoji
                    except:
                        pass
                    break
            
            # Get leader role from Member Log using leader ID
            if leader_id:
                try:
                    member_log_worksheet = await self.quest_cog.get_worksheet_cached("Member Log")
                    if member_log_worksheet:
                        member_log_values = await self.quest_cog.rate_limited_api_call(member_log_worksheet.get_all_values)
                        for row in member_log_values[1:]:  # Skip header row
                            if len(row) > 0 and str(row[0]) == str(leader_id):  # Column A: Discord ID
                                leader_role = row[3] if len(row) > 3 else ""  # Column D: Role
                                break
                except Exception as e:
                    print(f"Error getting leader role from Member Log: {e}")
            
            # Calculate quest duration
            duration_text = "Unknown"
            if quest_start_time:
                try:
                    from datetime import datetime
                    start_time = datetime.fromisoformat(quest_start_time.replace('Z', '+00:00'))
                    end_time = datetime.now()
                    duration = end_time - start_time
                    hours = int(duration.total_seconds() // 3600)
                    minutes = int((duration.total_seconds() % 3600) // 60)
                    if hours > 0:
                        duration_text = f"{hours}h {minutes}m"
                    else:
                        duration_text = f"{minutes}m"
                except:
                    duration_text = "Unknown"
            
            # Get all quest participants with their points
            completed_quest_players = []
            for row in all_values:
                if (len(row) > 0 and row[0] == self.patrol_id and 
                    len(row) > 11 and row[11]):  # Has player name
                    
                    player_id = row[10] if len(row) > 10 else ""
                    player_name = row[11] if len(row) > 11 else ""
                    
                    if player_id and player_name:
                        # Get rank and role from the correct columns
                        player_role = row[12] if len(row) > 12 else ""  # Column M: Player Role
                        player_rank = row[13] if len(row) > 13 else ""  # Column N: Player Rank
                        
                        # Use rank if available, otherwise use role, otherwise default to "Member"
                        if player_rank and player_rank.strip():
                            display_rank = player_rank
                        elif player_role and player_role.strip():
                            display_rank = player_role
                        else:
                            display_rank = "Member"
                        
                        # Calculate time in quest from Join Quest TS (Column V)
                        join_timestamp = row[21] if len(row) > 21 else ""  # Column V: Join Quest TS
                        time_in_quest = "Unknown"
                        if join_timestamp:
                            try:
                                join_time = datetime.fromisoformat(join_timestamp.replace('Z', '+00:00'))
                                current_time = datetime.now()
                                time_diff = current_time - join_time
                                time_minutes = int(time_diff.total_seconds() // 60)
                                if time_minutes < 60:
                                    time_in_quest = f"{time_minutes}m"
                                else:
                                    time_hours = time_minutes // 60
                                    time_mins = time_minutes % 60
                                    time_in_quest = f"{time_hours}h {time_mins}m"
                            except:
                                time_in_quest = "Unknown"
                        
                        # Get player rank and all 5 point categories from correct columns
                        player_rank = row[13] if len(row) > 13 else "❓"     # Column N: Player Rank
                        quest_points = row[14] if len(row) > 14 else "❓"    # Column O: Quest Points
                        ground_kills = row[15] if len(row) > 15 else "❓"    # Column P: Ground Kills
                        pilot_kills = row[16] if len(row) > 16 else "❓"     # Column Q: Pilot Kills
                        crusade_points = row[26] if len(row) > 26 else "❓"  # Column AA: Crusades
                        griefer_kills = row[27] if len(row) > 27 else "❓"   # Column AB: Griefer
                        
                        completed_quest_players.append({
                            'player_name': player_name,
                            'player_id': player_id,
                            'player_rank': player_rank,  # Use rank from Google Sheets column N
                            'player_role': player_role,
                            'time_in_quest': time_in_quest,
                            'quest_points': quest_points,
                            'ground_kills': ground_kills,
                            'pilot_kills': pilot_kills,
                            'crusade_points': crusade_points,
                            'griefer_kills': griefer_kills
                        })
            
            # Format leader display with rank and role on separate lines
            leader_display = f"**Leader:** {leader_name}"
            if leader_rank and leader_rank.strip() and leader_role and leader_role.strip():
                leader_display += f"\n**Rank:** {leader_rank} | **Role:** {leader_role}"
            elif leader_rank and leader_rank.strip():
                leader_display += f"\n**Rank:** {leader_rank}"
            elif leader_role and leader_role.strip():
                leader_display += f"\n**Role:** {leader_role}"
            
            # Extract just the 4-digit quest number from patrol_id (e.g., P533127850409328670-1082 -> 1082)
            quest_number = self.patrol_id.split('-')[-1] if '-' in self.patrol_id else self.patrol_id
            
            # Add Crusade prefix to quest name if it's a Crusade
            display_quest_name = quest_name
            if quest_type == "Crusade":
                display_quest_name = f"🏛️ | {quest_name}"
            
            # Create the simplified completion embed
            completed_embed = discord.Embed(
                title=f"{quest_emoji} Quest Completed Successfully!",
                description=f"**{display_quest_name}** has been completed!\n\n{leader_display}",
                color=0x00ff00
            )
            
            # Add quest image if available
            if quest_image and quest_image.startswith(('http://', 'https://')):
                completed_embed.set_image(url=quest_image)
            
            # Add footer with completion info
            completed_embed.set_footer(text=f"✅ Quest Completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | ID: {quest_number}")
            
            # Send as a new message that STAYS VISIBLE (no auto-delete)
            await interaction.followup.send(embed=completed_embed)
            
            # Get thread ID from the quest data for forum tag update
            thread_id = None
            for row in all_values:
                if len(row) > 0 and row[0] == self.patrol_id:
                    thread_id = row[19] if len(row) > 19 else ""  # Column T: Thread ID
                    break
            
            # Update forum thread tags - remove "Quest Started" and add "Quest Completed"
            if thread_id:
                await self.update_forum_thread_tags_completed(thread_id)
            
            # Update Google Sheets with completion timestamp and send to admin review (run async to avoid interaction timeout)
            asyncio.create_task(self.send_quest_for_admin_review(display_quest_name, leader_name, leader_rank, leader_role, completed_quest_players, quest_image))
            
        except Exception as e:
            print(f"Error completing quest from points interface: {e}")
            try:
                # Only try to send error message if interaction is still valid
                await interaction.followup.send(f"❌ Error completing quest: {str(e)}", ephemeral=True)
            except discord.NotFound:
                print(f"Interaction expired while trying to send error message")
            except Exception as followup_error:
                print(f"Failed to send error message: {followup_error}")
            except discord.NotFound:
                print(f"Interaction expired while trying to send error message")
            except Exception as followup_error:
                print(f"Failed to send error message: {followup_error}")

    async def send_quest_for_admin_review(self, quest_name, leader_name, leader_rank, leader_role, completed_quest_players, quest_image):
        """Send completed quest data to admin review channel and update completion timestamp"""
        try:
            # Update Google Sheets with completion timestamp (Column AE)
            worksheet = await self.quest_cog.get_worksheet_cached("Patrols")
            if worksheet:
                all_values = await self.quest_cog.rate_limited_api_call(worksheet.get_all_values)
                
                for i, row in enumerate(all_values):
                    if len(row) > 0 and row[0] == self.patrol_id:
                        quest_row_index = i + 1  # +1 because sheet rows are 1-indexed
                        
                        # Update column AE (Quest Completed) with timestamp
                        completion_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        
                        # Column AE is the 31st column (A=1, B=2, ..., AE=31)
                        await self.quest_cog.rate_limited_api_call(
                            worksheet.update,
                            f"AE{quest_row_index}",
                            [[completion_timestamp]]
                        )
                        print(f"✅ Updated Quest Completed timestamp for {self.patrol_id}")
                        break
            
            # Send to review channels in ALL guilds
            for guild in self.quest_cog.bot.guilds:
                guild_settings = self.quest_cog.get_guild_settings(guild.id)
                review_channel_id = guild_settings.get("quest_review_channel")
                
                if review_channel_id:
                    review_channel = self.quest_cog.bot.get_channel(review_channel_id)
                    
                    if review_channel:
                        # Get quest description from the first message in the thread
                        quest_description = "Quest completed - awaiting admin review"
                        quest_thread = self.quest_cog.bot.get_channel(int(self.patrol_id.split('-')[-1]))
                        if quest_thread:
                            try:
                                first_message = None
                                async for message in quest_thread.history(limit=50, oldest_first=True):
                                    if message.embeds:
                                        first_message = message
                                        break
                                
                                if first_message and first_message.embeds:
                                    embed = first_message.embeds[0]
                                    if embed.description:
                                        quest_description = embed.description
                            except Exception as e:
                                print(f"Could not retrieve quest description: {e}")
                        
                        # Create comprehensive admin review embed
                        review_embed = discord.Embed(
                            title="🎯 Quest Results Review",
                            description=f"**{quest_name}**\n{quest_description}",
                            color=0x00ff00
                        )
                        
                        # Add quest leader information
                        leader_info = f"**{leader_name}**"
                        if leader_rank and leader_role:
                            leader_info += f"\n{leader_rank} | {leader_role}"
                        elif leader_rank:
                            leader_info += f"\n{leader_rank}"
                        elif leader_role:
                            leader_info += f"\n{leader_role}"
                        
                        review_embed.add_field(name="🎖️ Quest Leader", value=leader_info, inline=True)
                        
                        # Add detailed participant roster (limited to fit in embed)
                        if completed_quest_players:
                            participant_roster = ""
                            for i, player in enumerate(completed_quest_players[:20]):  # Limit to 20 for embed size
                                points_summary = []
                                if player['quest_points'] and player['quest_points'] != "❓":
                                    points_summary.append(f"📜{player['quest_points']}")
                                if player['ground_kills'] and player['ground_kills'] != "❓":
                                    points_summary.append(f"🔫{player['ground_kills']}")
                                if player['pilot_kills'] and player['pilot_kills'] != "❓":
                                    points_summary.append(f"🚀{player['pilot_kills']}")
                                if player['crusade_points'] and player['crusade_points'] != "❓":
                                    points_summary.append(f"🏛️{player['crusade_points']}")
                                if player['griefer_kills'] and player['griefer_kills'] != "❓":
                                    points_summary.append(f"💀{player['griefer_kills']}")
                                
                                points_text = " ".join(points_summary) if points_summary else "No scoring"
                                
                                # Add rank and role info
                                rank_role_info = ""
                                if player.get('player_rank') and player['player_rank'] != "❓":
                                    rank_role_info += f"**Rank:** {player['player_rank']}"
                                if player.get('player_role') and player['player_role'] != "❓" and player['player_role'] != "":
                                    if rank_role_info:
                                        rank_role_info += f" | **Role:** {player['player_role']}"
                                    else:
                                        rank_role_info += f"**Role:** {player['player_role']}"
                                
                                if rank_role_info:
                                    participant_roster += f"`{i+1:2}.` <@{player['player_id']}> - {rank_role_info}\n    ⏱️ {player['time_in_quest']} | {points_text}\n"
                                else:
                                    participant_roster += f"`{i+1:2}.` <@{player['player_id']}> ({player['time_in_quest']}) - {points_text}\n"
                            
                            if len(completed_quest_players) > 20:
                                participant_roster += f"*... and {len(completed_quest_players) - 20} more participants*"
                            
                            review_embed.add_field(name="👥 Participant Roster", value=participant_roster, inline=False)
                        
                        # Add quest image if available
                        if quest_image and quest_image.startswith(('http://', 'https://')):
                            review_embed.set_thumbnail(url=quest_image)
                        
                        # Add footer
                        quest_number = self.patrol_id.split('-')[-1] if '-' in self.patrol_id else self.patrol_id
                        review_embed.set_footer(text=f"Quest ID: {quest_number} | Review and approve quest results below")
                        
                        # Create review view with approve/edit buttons
                        review_view = QuestReviewView(self.patrol_id, quest_name, self.quest_cog)
                        
                        # Send to admin review channel
                        await review_channel.send(embed=review_embed, view=review_view)
                        print(f"✅ Sent quest {self.patrol_id} to guild {guild.name} review channel")
                    else:
                        print(f"⚠️ Quest review channel not found for guild {guild.name}")
                else:
                    print(f"⚠️ No quest review channel configured for guild {guild.name}")
                
        except Exception as e:
            print(f"Error sending quest for admin review: {e}")

    async def update_forum_thread_tags_completed(self, thread_id: str):
        """Update forum thread tags when quest is completed - remove Quest Started and add Quest Completed"""
        try:
            # Search through ALL guilds to find the one with this forum thread
            forum_channel = None
            guild = None
            thread = None
            
            for g in self.quest_cog.bot.guilds:
                guild_settings = self.quest_cog.get_guild_settings(g.id)
                forum_channel_id = guild_settings.get("quest_forum_channel")
                
                if forum_channel_id:
                    fc = g.get_channel(forum_channel_id)
                    if fc and isinstance(fc, discord.ForumChannel):
                        t = fc.get_thread(int(thread_id))
                        if t:
                            # Found the thread in this guild's forum
                            guild = g
                            forum_channel = fc
                            thread = t
                            print(f"📍 Found thread {thread_id} in guild {guild.name} forum channel")
                            break
            
            if not forum_channel or not thread:
                print(f"⚠️ Thread {thread_id} not found in any guild's forum channels")
                return
            
            # Find Quest Started and Quest Completed tags
            quest_started_tag = None
            quest_completed_tag = None
            
            for tag in forum_channel.available_tags:
                if tag.name == "Quest Started":
                    quest_started_tag = tag
                elif tag.name == "Quest Completed":
                    quest_completed_tag = tag
            
            if quest_completed_tag:
                # Get current tags and update them
                current_tags = list(thread.applied_tags)
                
                # Remove Quest Started tag if present
                if quest_started_tag and quest_started_tag in current_tags:
                    current_tags.remove(quest_started_tag)
                    print(f"🔄 Removed 'Quest Started' tag from thread {thread.id}")
                
                # Add Quest Completed tag if not already present
                if quest_completed_tag not in current_tags:
                    current_tags.append(quest_completed_tag)
                
                # Apply the updated tags
                await thread.edit(applied_tags=current_tags)
                print(f"✅ Applied 'Quest Completed' tag to thread {thread.id}")
            else:
                print("⚠️ 'Quest Completed' tag not found in forum channel")
                
        except Exception as e:
            print(f"Error updating forum thread tags: {e}")

class PlayerSelectionDropdown(discord.ui.Select):
    def __init__(self, options, patrol_id: str, quest_players: list, quest_cog, parent_view=None):
        # Use minimal placeholder to reduce component size
        placeholder = "Select player or set all..."
        
        print(f"Creating PlayerSelectionDropdown with {len(options)} options, placeholder: '{placeholder}' ({len(placeholder)} chars)")
        
        # Ensure we don't exceed Discord limits
        if len(options) > 25:
            print(f"WARNING: Too many options ({len(options)}), truncating to 25")
            options = options[:25]
        
        super().__init__(placeholder=placeholder, options=options)
        self.patrol_id = patrol_id
        self.quest_players = quest_players
        self.quest_cog = quest_cog
        self.parent_view = parent_view
    
    def update_quest_players(self, new_quest_players):
        """Update the quest players data without recreating the dropdown"""
        print(f"Updating dropdown quest_players data: {len(new_quest_players)} players")
        self.quest_players = new_quest_players
        # Also update parent view if it exists
        if self.parent_view and hasattr(self.parent_view, 'quest_players'):
            self.parent_view.quest_players = new_quest_players
    
    async def callback(self, interaction: discord.Interaction):
        selected_value = self.values[0]
        
        # Check if "Set Equal Points for All" was selected
        if selected_value == "all_players":
            # Show bulk quest points recording modal
            modal = BulkQuestPointsModal(self.patrol_id, self.quest_players, self.quest_cog, self.parent_view)
            await interaction.response.send_modal(modal)
        else:
            # Handle individual player selection - simplified approach
            if selected_value.startswith("p") and selected_value[1:].isdigit():
                # Extract the player index from the simplified value (p0, p1, etc.)
                selected_index = int(selected_value[1:])
            else:
                # Fallback to integer parsing
                selected_index = int(selected_value)
            
            selected_player = self.quest_players[selected_index]
            
            # Show quest points recording modal
            modal = QuestPointsModal(self.patrol_id, selected_player, self.quest_cog, self.parent_view)
            await interaction.response.send_modal(modal)

class QuestPointsModal(discord.ui.Modal):
    def __init__(self, patrol_id: str, player_data: dict, quest_cog, parent_view=None):
        super().__init__(title=f"Record Points - {player_data['player_name']}")
        self.patrol_id = patrol_id
        self.player_data = player_data
        self.quest_cog = quest_cog
        self.parent_view = parent_view
        
        print(f"Creating QuestPointsModal for {player_data['player_name']}")
        print(f"Player data keys: {list(player_data.keys())}")
        
        # Add input fields for kill stats only (Quest/Crusade points are auto-assigned based on quest type)
        try:
            self.ground_kills = discord.ui.TextInput(
                label="Ground Kills",
                placeholder="Enter ground kills (numbers only)",
                default=self.get_current_value(player_data, ['ground_kills', 'current_ground_kills']),
                required=False,
                max_length=10
            )
            
            self.pilot_kills = discord.ui.TextInput(
                label="Pilot Kills",
                placeholder="Enter pilot kills (numbers only)",
                default=self.get_current_value(player_data, ['pilot_kills', 'current_pilot_kills']),
                required=False,
                max_length=10
            )
            
            self.griefer_kills = discord.ui.TextInput(
                label="Turret Kills",
                placeholder="Enter turret kills (numbers only)",
                default=self.get_current_value(player_data, ['griefer_kills', 'current_griefer_kills']),
                required=False,
                max_length=10
            )
            
            # Add all inputs to the modal
            self.add_item(self.ground_kills)
            self.add_item(self.pilot_kills)
            self.add_item(self.griefer_kills)
            
        except Exception as e:
            print(f"Error creating modal fields: {e}")
            # Create basic fields with default values if there's an error
            self.ground_kills = discord.ui.TextInput(label="Ground Kills", default="0", required=False, max_length=10)
            self.pilot_kills = discord.ui.TextInput(label="Pilot Kills", default="0", required=False, max_length=10)
            self.griefer_kills = discord.ui.TextInput(label="Turret Kills", default="0", required=False, max_length=10)
            
            self.add_item(self.ground_kills)
            self.add_item(self.pilot_kills)
            self.add_item(self.griefer_kills)
            self.add_item(self.ground_kills)
            self.add_item(self.pilot_kills)
            self.add_item(self.crusade_points)
            self.add_item(self.griefer_kills)

    def get_current_value(self, player_data, key_names):
        """Get current value from player data, trying multiple possible key names"""
        for key in key_names:
            value = player_data.get(key)
            if value and value != "❓" and str(value).strip():
                print(f"Found value '{value}' for key '{key}' in player data")
                return str(value)
        print(f"No valid value found for keys {key_names} in player data, using default '0'")
        print(f"Available keys in player_data: {list(player_data.keys())}")
        return "0"
    
    async def _delete_message_after_delay(self, message, delay_seconds):
        """Delete a message after a specified delay"""
        try:
            await asyncio.sleep(delay_seconds)
            await message.delete()
        except Exception as e:
            print(f"Could not delete success message: {e}")
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Defer the interaction immediately to prevent timeout
            await interaction.response.defer(ephemeral=True)
            
            # Validate and convert inputs (Quest/Crusade points are auto-assigned on join)
            try:
                ground_kills_val = int(self.ground_kills.value) if self.ground_kills.value.strip() else 0
                pilot_kills_val = int(self.pilot_kills.value) if self.pilot_kills.value.strip() else 0
                griefer_kills_val = int(self.griefer_kills.value) if self.griefer_kills.value.strip() else 0
            except ValueError:
                await interaction.followup.send("❌ All values must be valid numbers!", ephemeral=True)
                return
            
            # Update Google Sheet
            worksheet = await self.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                await interaction.followup.send("❌ Could not access the quest database.", ephemeral=True)
                return
            
            row_num = self.player_data['row']
            
            # Update stat columns (Quest/Crusade points are auto-assigned on join)
            # Column P (16): Ground Kills, Column Q (17): Pilot Kills, Column AB (28): Turret Kills
            await self.quest_cog.rate_limited_api_call(
                worksheet.update_cell,
                row_num, 16,  # Column P: Ground Kills
                str(ground_kills_val)
            )
            await self.quest_cog.rate_limited_api_call(
                worksheet.update_cell,
                row_num, 17,  # Column Q: Pilot Kills
                str(pilot_kills_val)
            )
            await self.quest_cog.rate_limited_api_call(
                worksheet.update_cell,
                row_num, 28,  # Column AB: Turret Kills
                str(griefer_kills_val)
            )
            
            # Clear relevant caches
            self.quest_cog.clear_cache("data_Patrols")
            
            # Refresh the parent view embed to show updated status (this will include success indication)
            if self.parent_view:
                try:
                    await self.parent_view.refresh_embed()
                    # Send a brief success notification that will auto-delete after 5 seconds
                    success_msg = await interaction.followup.send(
                        f"✅ Points recorded for **{self.player_data['player_name']}**", 
                        ephemeral=False
                    )
                    # Delete the message after 5 seconds
                    asyncio.create_task(self._delete_message_after_delay(success_msg, 5))
                except Exception as refresh_error:
                    print(f"Error refreshing embed after recording points: {refresh_error}")
                    # Fallback to detailed success message if refresh fails
                    success_message = f"✅ **Quest Stats Recorded**\n\n"
                    success_message += f"**Player:** {self.player_data['player_name']}\n"
                    success_message += f" Ground Kills: {ground_kills_val}\n"
                    success_message += f"🚀 Pilot Kills: {pilot_kills_val}\n"
                    success_message += f"💀 Turret Kills: {griefer_kills_val}\n"
                    success_message += f"\n*Quest/Crusade points are auto-assigned when joining*"
                    await interaction.followup.send(success_message, ephemeral=True)
            else:
                # No parent view to refresh, send detailed success message
                success_message = f"✅ **Quest Stats Recorded**\n\n"
                success_message += f"**Player:** {self.player_data['player_name']}\n"
                success_message += f"🔫 Ground Kills: {ground_kills_val}\n"
                success_message += f"🚀 Pilot Kills: {pilot_kills_val}\n"
                success_message += f"💀 Turret Kills: {griefer_kills_val}\n"
                success_message += f"\n*Quest/Crusade points are auto-assigned when joining*"
                await interaction.followup.send(success_message, ephemeral=True)
            
        except Exception as e:
            print(f"❌ Error recording quest points: {e}")
            # Use followup if interaction was already deferred
            try:
                await interaction.followup.send("❌ Failed to record quest points. Please try again.", ephemeral=True)
            except:
                # If followup also fails, the interaction might have expired completely
                print(f"Failed to send error message - interaction may have expired")

class BulkQuestPointsModal(discord.ui.Modal):
    def __init__(self, patrol_id: str, quest_players: list, quest_cog, parent_view=None):
        super().__init__(title=f"Set Equal Stats for All Players")
        self.patrol_id = patrol_id
        self.quest_players = quest_players
        self.quest_cog = quest_cog
        self.parent_view = parent_view
        
        # Create input fields for kill stats only (Quest/Crusade points are auto-assigned based on quest type)
        self.ground_kills = discord.ui.TextInput(
            label="Ground Kills (for each player)",
            placeholder="Enter ground kills for each player...",
            default="0",
            required=False,
            max_length=10
        )
        
        self.pilot_kills = discord.ui.TextInput(
            label="Pilot Kills (for each player)",
            placeholder="Enter pilot kills for each player...",
            default="0",
            required=False,
            max_length=10
        )
        
        self.griefer_kills = discord.ui.TextInput(
            label="Turret Kills (for each player)",
            placeholder="Enter turret kills for each player...",
            default="0",
            required=False,
            max_length=10
        )
        
        # Add all inputs to the modal
        self.add_item(self.ground_kills)
        self.add_item(self.pilot_kills)
        self.add_item(self.griefer_kills)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Defer the interaction immediately to prevent timeout
            await interaction.response.defer(ephemeral=True)
            
            # Validate and convert inputs (Quest/Crusade points are auto-assigned based on quest type)
            try:
                ground_kills_val = int(self.ground_kills.value) if self.ground_kills.value.strip() else 0
                pilot_kills_val = int(self.pilot_kills.value) if self.pilot_kills.value.strip() else 0
                griefer_kills_val = int(self.griefer_kills.value) if self.griefer_kills.value.strip() else 0
            except ValueError:
                await interaction.followup.send("❌ All values must be valid numbers!", ephemeral=True)
                return
            
            # Update Google Sheet for all players
            worksheet = await self.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                await interaction.followup.send("❌ Could not access the quest database.", ephemeral=True)
                return
            
            # Get quest type from database first to determine point assignment
            all_values = await self.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            quest_type = "Quest"  # Default fallback
            
            # Find quest type from the patrol row
            for row in all_values:
                if len(row) > 0 and row[0] == self.patrol_id:
                    # Check if quest type is stored in a column (we'll need to add this to quest creation)
                    # For now, assume Quest type and we'll implement this in the next step
                    break
            
            updated_count = 0
            for player in self.quest_players:
                try:
                    row_num = player['row']
                    
                    # Update stat columns for this player (Quest/Crusade points are auto-assigned)
                    # Column P (16): Ground Kills, Column Q (17): Pilot Kills, Column AB (28): Turret Kills
                    await self.quest_cog.rate_limited_api_call(
                        worksheet.update_cell,
                        row_num, 16,  # Column P: Ground Kills
                        str(ground_kills_val)
                    )
                    await self.quest_cog.rate_limited_api_call(
                        worksheet.update_cell,
                        row_num, 17,  # Column Q: Pilot Kills
                        str(pilot_kills_val)
                    )
                    await self.quest_cog.rate_limited_api_call(
                        worksheet.update_cell,
                        row_num, 28,  # Column AB: Turret Kills
                        str(griefer_kills_val)
                    )
                    
                    updated_count += 1
                    
                except Exception as e:
                    print(f"Error updating points for {player['player_name']}: {e}")
            
            # Clear relevant caches
            self.quest_cog.clear_cache("data_Patrols")
            
            # Refresh the parent view embed to show updated status (this will include success indication)
            if self.parent_view:
                try:
                    await self.parent_view.refresh_embed()
                    # Send a brief success notification that will auto-delete after 5 seconds
                    success_msg = await interaction.followup.send(
                        f"✅ Points recorded for **{updated_count}/{len(self.quest_players)}** players", 
                        ephemeral=False
                    )
                    # Delete the message after 5 seconds
                    asyncio.create_task(self._delete_message_after_delay(success_msg, 5))
                except Exception as refresh_error:
                    print(f"Error refreshing embed after bulk recording points: {refresh_error}")
                    # Fallback to detailed success message if refresh fails
                    success_message = f"✅ **Quest Stats Recorded for All Players**\n\n"
                    success_message += f"**Players Updated:** {updated_count}/{len(self.quest_players)}\n"
                    success_message += f" Ground Kills: {ground_kills_val} (each)\n"
                    success_message += f"🚀 Pilot Kills: {pilot_kills_val} (each)\n"
                    success_message += f"💀 Turret Kills: {griefer_kills_val} (each)\n"
                    success_message += f"\n*Quest/Crusade points are auto-assigned when joining*"
                    await interaction.followup.send(success_message, ephemeral=True)
            else:
                # No parent view to refresh, send detailed success message
                success_message = f"✅ **Quest Stats Recorded for All Players**\n\n"
                success_message += f"**Players Updated:** {updated_count}/{len(self.quest_players)}\n"
                success_message += f" Ground Kills: {ground_kills_val} (each)\n"
                success_message += f"🚀 Pilot Kills: {pilot_kills_val} (each)\n"
                success_message += f"💀 Turret Kills: {griefer_kills_val} (each)\n"
                success_message += f"\n*Quest/Crusade points are auto-assigned when joining*"
                await interaction.followup.send(success_message, ephemeral=True)
            
        except Exception as e:
            print(f"❌ Error recording bulk quest points: {e}")
            # Use followup if interaction was already deferred
            try:
                await interaction.followup.send("❌ Failed to record quest points. Please try again.", ephemeral=True)
            except:
                # If followup also fails, the interaction might have expired completely
                print(f"Failed to send error message - interaction may have expired")

    async def _delete_message_after_delay(self, message, delay_seconds):
        """Delete a message after a specified delay"""
        try:
            await asyncio.sleep(delay_seconds)
            await message.delete()
        except Exception as e:
            print(f"Could not delete success message: {e}")

class QuestReviewView(discord.ui.View):
    def __init__(self, patrol_id: str, quest_name: str, quest_cog):
        super().__init__(timeout=None)  # No timeout for admin reviews
        self.patrol_id = patrol_id
        self.quest_name = quest_name
        self.quest_cog = quest_cog
    
    @discord.ui.button(label="Approve Quest Results", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_quest(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Approve the quest and mark it as recorded"""
        # Check if user is admin
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need Administrator permissions to approve quests.", ephemeral=True)
            return
        
        try:
            await interaction.response.defer(ephemeral=True)
            
            # Update Google Sheets with admin approval (Column Z)
            worksheet = await self.quest_cog.get_worksheet_cached("Patrols")
            if worksheet:
                all_values = await self.quest_cog.rate_limited_api_call(worksheet.get_all_values)
                
                for i, row in enumerate(all_values):
                    if len(row) > 0 and row[0] == self.patrol_id:
                        quest_row_index = i + 1  # +1 because sheet rows are 1-indexed
                        
                        # Update column Z (Admin Approved) with approver's name
                        approver_name = interaction.user.display_name
                        
                        # Column Z is the 26th column (A=1, B=2, ..., Z=26)
                        await self.quest_cog.rate_limited_api_call(
                            worksheet.update,
                            f"Z{quest_row_index}",
                            [[approver_name]]
                        )
                        print(f"✅ Quest {self.patrol_id} approved by {approver_name}")
                        break
            
            # Update forum thread with "Recorded" tag
            await self.update_forum_thread_with_recorded_tag()
            
            # Update the embed to show approval
            original_embed = interaction.message.embeds[0]
            approved_embed = discord.Embed(
                title="✅ Quest Approved",
                description=original_embed.description,
                color=0x00ff00  # Green color for approved
            )
            
            # Copy fields from original embed
            for field in original_embed.fields:
                approved_embed.add_field(name=field.name, value=field.value, inline=field.inline)
            
            # Add approval info
            approved_embed.add_field(name="✅ Approved by", value=interaction.user.mention, inline=True)
            approved_embed.add_field(name="⏲️ Approved", value=f"<t:{int(datetime.now().timestamp())}:R>", inline=True)
            approved_embed.set_footer(text="Quest has been approved and marked complete")
            
            # Disable buttons
            for item in self.children:
                item.disabled = True
            
            await interaction.message.edit(embed=approved_embed, view=self)
            await interaction.followup.send("✅ Quest has been approved!", ephemeral=True)
            
        except Exception as e:
            print(f"Error approving quest: {e}")
            await interaction.followup.send(f"❌ Error approving quest: {str(e)}", ephemeral=True)
    
    @discord.ui.button(label="Edit Results", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def edit_results(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Edit quest results - jump to quest thread"""
        # Check if user is admin
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need Administrator permissions to edit quest results.", ephemeral=True)
            return
        
        try:
            await interaction.response.defer(ephemeral=True)
            
            # Get Thread ID from Google Sheets column S
            worksheet = await self.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                await interaction.followup.send("❌ Unable to access quest database.", ephemeral=True)
                return
            
            all_values = await self.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            thread_id = None
            
            for row in all_values:
                if len(row) > 0 and row[0] == self.patrol_id:
                    thread_id = row[18] if len(row) > 18 else None  # Column S: Thread ID (row[18] = column S)
                    break
            
            if not thread_id or not thread_id.strip():
                await interaction.followup.send("❌ No thread ID found for this quest.", ephemeral=True)
                return
            
            # Verify the thread exists and get its name
            thread_link = f"<#{thread_id}>"
            thread_name = "Quest Thread"
            
            try:
                thread = interaction.guild.get_channel(int(thread_id))
                if thread:
                    thread_name = thread.name
                else:
                    # Try getting thread from forum channel
                    guild_settings = self.quest_cog.get_guild_settings(interaction.guild.id)
                    forum_channel_id = guild_settings.get("quest_forum_channel")
                    if forum_channel_id:
                        forum_channel = interaction.guild.get_channel(forum_channel_id)
                        if forum_channel and isinstance(forum_channel, discord.ForumChannel):
                            forum_thread = forum_channel.get_thread(int(thread_id))
                            if forum_thread:
                                thread_name = forum_thread.name
            except (ValueError, TypeError):
                thread_link = "Invalid Thread ID"
            
            # Create simple embed with editing instructions and channel link
            edit_embed = discord.Embed(
                title="✏️ Edit Quest Results",
                description=f"**Quest:** {self.quest_name}\n"
                           f"**Thread:** {thread_name}\n\n"
                           f"**To edit quest results:**\n"
                           f"1. Go to the quest thread: {thread_link}\n"
                           f"2. Use the 'Record Quest Points' button to update player scores\n"
                           f"3. Return here to approve the updated results",
                color=0x3498db  # Blue color
            )
            
            # Add debug info in footer
            edit_embed.set_footer(text=f"Thread ID: {thread_id} | Patrol ID: {self.patrol_id}")
            
            await interaction.followup.send(embed=edit_embed, ephemeral=True)
            
        except Exception as e:
            print(f"Error creating edit instructions: {e}")
            await interaction.response.send_message("❌ Error creating edit instructions.", ephemeral=True)
    
    async def update_forum_thread_with_recorded_tag(self):
        """Update forum thread tags to add 'Recorded' tag"""
        try:
            # Get Thread ID from Google Sheets column S
            worksheet = await self.quest_cog.get_worksheet_cached("Patrols")
            if not worksheet:
                print(f"⚠️ Cannot access worksheet for quest {self.patrol_id}")
                return
            
            all_values = await self.quest_cog.rate_limited_api_call(worksheet.get_all_values)
            thread_id = None
            
            for row in all_values:
                if len(row) > 0 and row[0] == self.patrol_id:
                    thread_id = row[18] if len(row) > 18 else None  # Column S: Thread ID (row[18] = column S)
                    break
            
            if not thread_id or not thread_id.strip():
                print(f"⚠️ No thread ID found for quest {self.patrol_id}")
                return
            
            # Get guild settings to find forum channel
            # First try to get guild from the first bot guild (fallback)
            guild = None
            for bot_guild in self.quest_cog.bot.guilds:
                guild_settings = self.quest_cog.get_guild_settings(bot_guild.id)
                if guild_settings.get("quest_forum_channel"):
                    guild = bot_guild
                    break
            
            if not guild:
                print("⚠️ No guild with quest forum channel found")
                return
            
            guild_settings = self.quest_cog.get_guild_settings(guild.id)
            forum_channel_id = guild_settings.get("quest_forum_channel")
            
            if not forum_channel_id:
                print("⚠️ No forum channel configured")
                return
            
            # Get the forum channel
            forum_channel = guild.get_channel(forum_channel_id)
            
            if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
                print(f"⚠️ Forum channel {forum_channel_id} not found or not a forum")
                return
            
            # Get the thread
            thread = forum_channel.get_thread(int(thread_id))
            if not thread:
                print(f"⚠️ Thread {thread_id} not found in forum")
                return
            
            # Find the Recorded tag
            recorded_tag = None
            for tag in forum_channel.available_tags:
                if tag.name == "Recorded":
                    recorded_tag = tag
                    break
            
            if recorded_tag:
                # Get current tags and add Recorded tag
                current_tags = list(thread.applied_tags)
                
                if recorded_tag not in current_tags:
                    current_tags.append(recorded_tag)
                
                # Apply the updated tags
                await thread.edit(applied_tags=current_tags)
                print(f"✅ Applied 'Recorded' tag to thread {thread.id}")
            else:
                print("⚠️ 'Recorded' tag not found in forum channel")
                
        except Exception as e:
            print(f"Error updating forum thread with recorded tag: {e}")
            print(f"Debug: patrol_id={self.patrol_id}")
            print(f"Debug: thread_id={thread_id if 'thread_id' in locals() else 'Not retrieved'}")

class QuestChangeRequestModal(discord.ui.Modal, title="Request Quest Changes"):
    def __init__(self, patrol_id: str, quest_name: str, quest_cog):
        super().__init__()
        self.patrol_id = patrol_id
        self.quest_name = quest_name
        self.quest_cog = quest_cog
    
    change_reason = discord.ui.TextInput(
        label="Change Request",
        placeholder="Explain what changes are needed for this quest...",
        style=discord.TextStyle.paragraph,
        max_length=1000
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Update the review message with change request
            change_embed = discord.Embed(
                title="📝 Changes Requested",
                description=f"**{self.quest_name}** requires changes before approval.\n\n**Requested by:** {interaction.user.display_name}",
                color=0xffa500
            )
            
            change_embed.add_field(
                name="Change Request",
                value=self.change_reason.value,
                inline=False
            )
            
            quest_number = self.patrol_id.split('-')[-1] if '-' in self.patrol_id else self.patrol_id
            change_embed.set_footer(text=f"Quest ID: {quest_number} | Status: Changes Requested")
            
            # Create a new review view for resubmission
            review_view = QuestReviewView(self.patrol_id, self.quest_name, self.quest_cog)
            
            await interaction.response.edit_message(embed=change_embed, view=review_view)
            
        except Exception as e:
            print(f"Error processing change request: {e}")
            await interaction.response.send_message(f"❌ Error processing change request: {str(e)}", ephemeral=True)

async def setup(bot):
    await bot.add_cog(StartQuest(bot))
