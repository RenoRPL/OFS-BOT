# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
from discord import app_commands
import gspread
from google.oauth2.service_account import Credentials
import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional, List
import asyncio

class QuestStatusView(discord.ui.View):
    def __init__(self, quest_id: str, quest_tracker, patrol_leader_id: str):
        super().__init__(timeout=300)
        self.quest_id = quest_id
        self.quest_tracker = quest_tracker
        self.patrol_leader_id = patrol_leader_id
    
    @discord.ui.button(label="Mark Complete", style=discord.ButtonStyle.success, emoji="✅")
    async def mark_completed(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user is admin or quest leader
        is_admin = interaction.user.guild_permissions.administrator
        is_quest_leader = str(interaction.user.id) == self.patrol_leader_id
        
        if not is_admin and not is_quest_leader:
            await interaction.response.send_message("❌ You need Administrator permissions or be the Quest Leader to complete the quest.", ephemeral=True)
            return
        
        # Check if quest points have been recorded first
        if not await self.check_quest_points_recorded():
            # Show quest completion workflow view
            view = QuestCompletionWorkflowView(self.quest_id, self.quest_tracker, self.patrol_leader_id)
            
            embed = discord.Embed(
                title="✅ Complete Quest",
                description="Before marking this quest as complete, you need to record quest points for all participants.",
                color=0x0099ff
            )
            embed.add_field(
                name="Required Steps:",
                value="1️⃣ Record quest points for all participants\n2️⃣ Submit quest for review\n3️⃣ Wait for admin approval",
                inline=False
            )
            embed.set_footer(text="Click 'Record Quest Points' to begin the completion process")
            
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            # Quest points already recorded, proceed with review submission
            await self.submit_for_review(interaction)
    
    @discord.ui.button(label="Mark Cancelled", style=discord.ButtonStyle.secondary, emoji="❌")
    async def mark_cancelled(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_status(interaction, "cancelled", "❌ Quest Cancelled")
    
    @discord.ui.button(label="Record Quest Points", style=discord.ButtonStyle.secondary, emoji="🪶")
    async def mark_recorded(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Show quest points recording interface
        await self.show_quest_points_interface(interaction)
    
    def update_button_visibility(self, current_status: str):
        """Update button visibility based on current quest status"""
        # Get all buttons
        pending_btn = None
        completed_btn = None
        cancelled_btn = None
        recorded_btn = None
        start_btn = None
        
        for item in self.children:
            if hasattr(item, 'label'):
                if item.label == "Mark Complete":
                    completed_btn = item
                elif item.label == "Mark Cancelled":
                    cancelled_btn = item
                elif item.label == "Record Quest Points":
                    recorded_btn = item
        
        # Reset all buttons to enabled first
        for item in self.children:
            if hasattr(item, 'disabled'):
                item.disabled = False
        
        # Set visibility based on current status
        if current_status == "pending":
            # When pending: show Start, Completed, Cancelled, Recorded buttons
            # Hide Pending button
            if pending_btn:
                pending_btn.disabled = True
        elif current_status == "started":
            # When started: show Pending, Completed, Cancelled buttons
            # Hide Start and Recorded buttons  
            if start_btn:
                start_btn.disabled = True
            if recorded_btn:
                recorded_btn.disabled = True
        elif current_status in ["completed", "cancelled"]:
            # When completed/cancelled: show only Recorded button
            # Hide all other buttons
            if pending_btn:
                pending_btn.disabled = True
            if completed_btn:
                completed_btn.disabled = True
            if cancelled_btn:
                cancelled_btn.disabled = True
            if start_btn:
                start_btn.disabled = True
        elif current_status == "recorded":
            # When recorded: disable all buttons
            for item in self.children:
                if hasattr(item, 'disabled'):
                    item.disabled = True
    
    async def update_status(self, interaction: discord.Interaction, status: str, status_name: str, update_start_time: bool = False):
        # Check if user is admin or quest leader
        is_admin = interaction.user.guild_permissions.administrator
        is_quest_leader = str(interaction.user.id) == self.patrol_leader_id
        
        if not is_admin and not is_quest_leader:
            await interaction.response.send_message("❌ You need Administrator permissions or be the Quest Leader to update quest status.", ephemeral=True)
            return
        
        try:
            await interaction.response.defer(ephemeral=True)
            
            # Get the quest forum channel
            forum_channel_id = self.quest_tracker.get_quest_forum_channel(interaction.guild.id)
            if not forum_channel_id:
                await self.quest_tracker.send_auto_close_ephemeral(interaction, "❌ Quest forum not configured.")
                return
            
            forum_channel = interaction.guild.get_channel(forum_channel_id)
            if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
                await self.quest_tracker.send_auto_close_ephemeral(interaction, "❌ Quest forum channel not found.")
                return
            
            # Find the current thread
            current_thread = interaction.channel
            if not isinstance(current_thread, discord.Thread):
                await self.quest_tracker.send_auto_close_ephemeral(interaction, "❌ This command must be used in a quest thread.")
                return
            
            # Map status to tag name (without emojis)
            status_map = {
                "pending": "Quest Pending",
                "completed": "Quest Completed",
                "cancelled": "Quest Cancelled",
                "recorded": "Recorded",
                "started": "Quest Started"
            }
            
            target_tag_name = status_map.get(status)
            target_tag = self.quest_tracker.get_quest_tag_by_name(forum_channel, target_tag_name)
            
            if not target_tag:
                await self.quest_tracker.send_auto_close_ephemeral(interaction, f"❌ Status tag `{target_tag_name}` not found in forum channel.\n\n**Solution:** Use `/setup_quest_tags` to create the required tags first.", 5)
                return
            
            # Handle different tag removal logic based on status
            if status == "recorded":
                # For "recorded", only remove active status tags (Quest Started, Quest Pending)
                # Keep completion tags (Quest Completed, Quest Cancelled)
                tags_to_remove = [
                    self.quest_tracker.get_quest_tag_by_name(forum_channel, "Quest Started"),
                    self.quest_tracker.get_quest_tag_by_name(forum_channel, "Quest Pending")
                ]
                # Filter out None values and the tags we want to remove
                current_tags = [tag for tag in current_thread.applied_tags if tag not in tags_to_remove]
                new_tags = current_tags + [target_tag]
            else:
                # For other statuses, remove all quest status tags and add the new one
                all_quest_tags = [
                    self.quest_tracker.get_quest_tag_by_name(forum_channel, "Quest Started"),
                    self.quest_tracker.get_quest_tag_by_name(forum_channel, "Quest Pending"),
                    self.quest_tracker.get_quest_tag_by_name(forum_channel, "Quest Completed"),
                    self.quest_tracker.get_quest_tag_by_name(forum_channel, "Quest Cancelled"),
                    self.quest_tracker.get_quest_tag_by_name(forum_channel, "Recorded")
                ]
                # Filter out None values and current status tags
                current_tags = [tag for tag in current_thread.applied_tags if tag not in all_quest_tags]
                new_tags = current_tags + [target_tag]
            
            # Update the thread tags
            await current_thread.edit(applied_tags=new_tags)
            
            # Update start time in Google Sheets if requested, handle pause/resume timing, and calculate total time for completion
            completion_time_info = None  # Initialize for completion time tracking
            try:
                worksheet = self.quest_tracker.get_worksheet(self.quest_tracker.SHEET_URL, self.quest_tracker.WORKSHEET_NAME)
                if worksheet:
                    all_values = self.quest_tracker.rate_limited_get_all_values(worksheet)
                    # Find the quest row by quest ID
                    quest_row = None
                    current_row_data = None
                    for i, row in enumerate(all_values):
                        if len(row) > 0 and row[0] == self.quest_id:
                            quest_row = i + 1  # gspread uses 1-based indexing
                            current_row_data = row
                            break
                    
                    if quest_row and current_row_data:
                        if update_start_time:
                            # When starting quest (from pending), or restarting after pause
                            if len(current_row_data) > 20 and current_row_data[20]:  # Check if there was a pause time
                                # Calculate and accumulate paused duration
                                pause_time_str = current_row_data[20]  # U: Pause Time
                                total_paused_str = current_row_data[21] if len(current_row_data) > 21 else "0"  # V: Total Paused Duration
                                
                                try:
                                    pause_start = datetime.strptime(pause_time_str, '%Y-%m-%d %H:%M:%S')
                                    pause_end = datetime.now()
                                    pause_duration_minutes = (pause_end - pause_start).total_seconds() / 60
                                    
                                    # Add to total paused time
                                    total_paused_minutes = float(total_paused_str) if total_paused_str and total_paused_str != "❓" else 0.0
                                    total_paused_minutes += pause_duration_minutes
                                    
                                    # Update total paused duration
                                    await self.quest_tracker.rate_limited_update_cell(worksheet, quest_row, 22, str(round(total_paused_minutes, 2)))  # V: Total Paused Duration
                                except Exception as e:
                                    print(f"Error calculating pause duration: {e}")
                            
                            # Update start time when quest is (re)started
                            new_start_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            await self.quest_tracker.rate_limited_update_cell(worksheet, quest_row, 8, new_start_time)  # H: Quest Start Time (column 8)
                            # Clear pause time when starting/restarting
                            await self.quest_tracker.rate_limited_update_cell(worksheet, quest_row, 21, "❓")  # U: Pause Time (column 21)
                            
                            # Send confirmation about time update
                            await self.quest_tracker.send_auto_close_ephemeral(interaction, f"✅ Quest started! **{status_name}**\n⏰ Started at: <t:{int(datetime.now().timestamp())}:R>")
                            
                        elif status == "pending":
                            # Update pause time when quest is paused
                            pause_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            await self.quest_tracker.rate_limited_update_cell(worksheet, quest_row, 21, pause_time)  # U: Pause Time (column 21)
                            
                            # Send confirmation about pause
                            await self.quest_tracker.send_auto_close_ephemeral(interaction, f"✅ Quest paused! **{status_name}**\n⏸️ Paused at: <t:{int(datetime.now().timestamp())}:R>")
                            
                        elif status == "completed":
                            # Calculate total quest time when completing
                            try:
                                quest_start_str = current_row_data[7] if len(current_row_data) > 7 else "❓"  # H: Quest Start Time
                                pause_time_str = current_row_data[20] if len(current_row_data) > 20 else "❓"  # U: Pause Time
                                total_paused_str = current_row_data[21] if len(current_row_data) > 21 else "0"  # V: Total Paused Duration
                                
                                if quest_start_str:
                                    quest_start = datetime.strptime(quest_start_str, '%Y-%m-%d %H:%M:%S')
                                    quest_end = datetime.now()
                                    
                                    # Calculate total elapsed time
                                    total_elapsed = quest_end - quest_start
                                    
                                    # Add current pause session if quest is currently paused
                                    total_paused_minutes = float(total_paused_str) if total_paused_str and total_paused_str != "❓" else 0.0
                                    if pause_time_str:
                                        pause_start = datetime.strptime(pause_time_str, '%Y-%m-%d %H:%M:%S')
                                        current_pause_duration = quest_end - pause_start
                                        total_paused_minutes += current_pause_duration.total_seconds() / 60
                                    
                                    # Calculate actual quest time (excluding paused time)
                                    actual_quest_minutes = total_elapsed.total_seconds() / 60 - total_paused_minutes
                                    
                                    # Format the time nicely
                                    hours = int(actual_quest_minutes // 60)
                                    minutes = int(actual_quest_minutes % 60)
                                    if hours > 0:
                                        time_display = f"{hours}h {minutes}m"
                                    else:
                                        time_display = f"{minutes}m"
                                    
                                    # Update Google Sheets with completion data
                                    await self.quest_tracker.rate_limited_update_cell(worksheet, quest_row, 10, quest_end.strftime('%Y-%m-%d %H:%M:%S'))  # J: Patrol actual end time
                                    await self.quest_tracker.rate_limited_update_cell(worksheet, quest_row, 21, "❓")  # U: Clear pause time
                                    await self.quest_tracker.rate_limited_update_cell(worksheet, quest_row, 22, str(round(total_paused_minutes, 2)))  # V: Total Paused Duration
                                    
                                    # Store time info for the status embed
                                    completion_time_info = {
                                        'total_time': time_display,
                                        'paused_time': f"{int(total_paused_minutes)}m" if total_paused_minutes > 0 else "0m"
                                    }
                                    
                                    # Send confirmation with time info
                                    await self.quest_tracker.send_auto_close_ephemeral(interaction, f"✅ Quest completed! **{status_name}**\n⏲️ Total Quest Time: **{time_display}**\n⏸️ Total Paused Time: **{int(total_paused_minutes)}m**", 5)
                                else:
                                    completion_time_info = None
                                    await self.quest_tracker.send_auto_close_ephemeral(interaction, f"✅ Quest completed! **{status_name}** (could not calculate time - no start time found)")
                            except Exception as e:
                                print(f"Error calculating quest time: {e}")
                                completion_time_info = None
                                await self.quest_tracker.send_auto_close_ephemeral(interaction, f"✅ Quest completed! **{status_name}** (time calculation failed)")
                        else:
                            # Regular status update
                            await self.quest_tracker.send_auto_close_ephemeral(interaction, f"✅ Quest status updated to **{status_name}**")
                    else:
                        await self.quest_tracker.send_auto_close_ephemeral(interaction, f"✅ Quest status updated to **{status_name}** (could not update time in sheet)")
                else:
                    await self.quest_tracker.send_auto_close_ephemeral(interaction, f"✅ Quest status updated to **{status_name}** (could not access sheet for time update)")
            except Exception as e:
                print(f"Error updating quest times: {e}")
                await interaction.followup.send(f"✅ Quest status updated to **{status_name}** (time update failed)", ephemeral=True)
            
            # Send status update message to the thread
            status_embed = discord.Embed(
                title="📋 Quest Status Updated",
                description=f"This quest has been marked as: **{status_name}**",
                color=0x0099ff
            )
            status_embed.add_field(name="Updated by", value=interaction.user.mention, inline=True)
            
            # Add time-specific information based on status
            if status == "pending":
                status_embed.add_field(name="⏸️ Quest Paused", value=f"<t:{int(datetime.now().timestamp())}:R>", inline=True)
                status_embed.set_footer(text="Quest is now paused. Use 'Start Quest' button to resume.")
            elif update_start_time:  # Starting/restarting quest
                status_embed.add_field(name="▶️ Quest (Re)Started", value=f"<t:{int(datetime.now().timestamp())}:R>", inline=True)
                status_embed.set_footer(text="Quest timer has been updated with new start time.")
            elif status == "completed":
                status_embed.add_field(name="✅ Completed", value=f"<t:{int(datetime.now().timestamp())}:R>", inline=True)
                # Add time information if available
                if completion_time_info:
                    status_embed.add_field(name="⏲️ Total Quest Time", value=completion_time_info['total_time'], inline=True)
                    if completion_time_info['paused_time'] != "0m":
                        status_embed.add_field(name="⏸️ Total Paused Time", value=completion_time_info['paused_time'], inline=True)
                    status_embed.set_footer(text=f"Quest completed! Total time: {completion_time_info['total_time']}")
                else:
                    status_embed.set_footer(text="Quest completed successfully!")
            elif status == "cancelled":
                status_embed.add_field(name="❌ Cancelled", value=f"<t:{int(datetime.now().timestamp())}:R>", inline=True)
                status_embed.set_footer(text="Quest was cancelled.")
            elif status == "recorded":
                status_embed.add_field(name="🪶 Recorded", value=f"<t:{int(datetime.now().timestamp())}:R>", inline=True)
                status_embed.set_footer(text="Quest data has been recorded.")
            
            await interaction.channel.send(embed=status_embed)
            
            # Update button visibility based on status
            self.update_button_visibility(status)
            
        except Exception as e:
            print(f"Error updating quest status: {e}")
            await self.quest_tracker.send_auto_close_ephemeral(interaction, f"❌ Error: {str(e)}")
    
    async def show_quest_points_interface(self, interaction: discord.Interaction):
        """Show the quest points recording interface"""
        try:
            await interaction.response.defer(ephemeral=True)
            
            # Get quest data from Google Sheets with rate limit handling
            worksheet = self.quest_tracker.get_cached_worksheet(self.quest_tracker.SHEET_URL, self.quest_tracker.WORKSHEET_NAME)
            if not worksheet:
                await interaction.followup.send("❌ Could not access the Google Sheet.", ephemeral=True)
                return
            
            # Get Member Log data once to avoid multiple API calls
            member_log_worksheet = self.quest_tracker.get_cached_worksheet(self.quest_tracker.SHEET_URL, "Member Log")
            member_log_data = {}
            if member_log_worksheet:
                member_log_values = await self.quest_tracker.cached_get_all_values(member_log_worksheet, "Member Log")
                for row in member_log_values[1:]:  # Skip header row
                    if len(row) > 2:
                        # Store both User ID (column A) and Discord ID (column B) mappings
                        if len(row) > 0 and row[0]:
                            member_log_data[str(row[0])] = row[2] if row[2] else "Member"
                        if len(row) > 1 and row[1]:
                            member_log_data[str(row[1])] = row[2] if row[2] else "Member"
            
            # Find all players in this quest
            all_values = await self.quest_tracker.cached_get_all_values(worksheet, self.quest_tracker.WORKSHEET_NAME)
            quest_players = []
            
            for i, row in enumerate(all_values[1:], start=2):  # Skip header, start from row 2
                if len(row) > 0 and row[0] == self.quest_id:  # Column A: Quest ID
                    player_id = row[10] if len(row) > 10 else "❓"  # Column K: Player ID
                    player_name = row[11] if len(row) > 11 else "❓"  # Column L: Player Name
                    
                    if player_id and player_name:  # Skip leader row (empty player data)
                        player_rank = member_log_data.get(str(player_id), "Member")  # Get rank from cached Member Log
                        join_time_str = row[21] if len(row) > 21 else "❓"  # Column V: Join Time
                        
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
                        
                        # Check if quest points have been recorded (any non-empty quest point field)
                        quest_points = row[14] if len(row) > 14 else "❓"  # Column O: Quests
                        ground_kills = row[15] if len(row) > 15 else "❓"  # Column P: Ground Kills
                        pilot_kills = row[16] if len(row) > 16 else "❓"   # Column Q: Pilot Kills
                        crusade_points = row[26] if len(row) > 26 else "❓" # Column AA: Crusades (index 26 = column 27)
                        griefer_kills = row[28] if len(row) > 28 else "❓" # Column AC: Griefer Kills (index 28 = column 29)
                        
                        has_recorded_points = any([quest_points, ground_kills, pilot_kills, crusade_points, griefer_kills])
                        
                        # Debug logging for all point values
                        print(f"Debug: Player {player_name} - Quest: '{quest_points}', Crusades: '{crusade_points}', Ground: '{ground_kills}', Pilot: '{pilot_kills}', Griefer: '{griefer_kills}' (row length: {len(row)})")
                        
                        quest_players.append({
                            'row': i,
                            'player_id': player_id,
                            'player_name': player_name,
                            'player_rank': player_rank,
                            'time_in_quest': time_in_quest,
                            'points_recorded': has_recorded_points,
                            'current_quest_points': quest_points,
                            'current_crusade_points': crusade_points,
                            'current_fps_kills': ground_kills,
                            'current_ship_kills': pilot_kills,
                            'current_griefer_kills': griefer_kills
                        })
            
            if not quest_players:
                await interaction.followup.send("❌ No players found in this quest to record points for.", ephemeral=True)
                return
            
            # Create quest points recording view
            quest_points_view = QuestPointsRecordingView(self.quest_id, quest_players, self.quest_tracker)
            quest_points_view.set_interaction(interaction)
            
            embed = discord.Embed(
                title="🪶 Record Quest Points",
                description=f"Select a player to record their quest performance:\n\n**Quest ID:** `{self.quest_id}`",
                color=0x0099ff  # Imperial blue color
            )
            
            # Add detailed player information
            if quest_players:
                # Limit to 10 players for display to avoid embed limits
                display_players = quest_players[:10]
                
                for i, player in enumerate(display_players):
                    status_icon = "✅" if player['points_recorded'] else "⏳"
                    
                    # Player name field
                    embed.add_field(
                        name=f"{i+1}. {player['player_name']} {status_icon}",
                        value=f"**Rank:** {player['player_rank']}\n**Time in Quest:** {player['time_in_quest']}",
                        inline=True
                    )
                
                # Add spacing if odd number of players (for better layout)
                if len(display_players) % 2 == 1:
                    embed.add_field(name="\u200b", value="\u200b", inline=True)
                
                # Add footer with additional info if there are more players
                if len(quest_players) > 10:
                    embed.set_footer(text=f"Showing 10 of {len(quest_players)} players. Use dropdown to select any player.")
                else:
                    embed.set_footer(text="✅ = Points recorded | ⏳ = Awaiting recording")
            else:
                embed.add_field(
                    name="❌ No Players Found",
                    value="No players have joined this quest yet.",
                    inline=False
                )
            
            embed.set_footer(text="Select a player below to record their quest points")
            
            # Send the message and store the reference for refreshing
            response_message = await interaction.followup.send(embed=embed, view=quest_points_view, ephemeral=True)
            quest_points_view.set_message(response_message)
            
        except Exception as e:
            print(f"Error showing quest points interface: {e}")
            error_msg = str(e)
            
            # Handle rate limit errors specifically
            if "429" in error_msg or "Quota exceeded" in error_msg or "Read requests per minute" in error_msg:
                await interaction.followup.send(
                    "❌ **Google Sheets Rate Limit Reached**\n\n"
                    "The bot has made too many requests to Google Sheets. Please wait 1-2 minutes and try again.\n\n"
                    "💡 **Tip:** This happens when multiple users are using quest features simultaneously.",
                    ephemeral=True
                )
            else:
                await interaction.followup.send(f"❌ Error loading quest data: {error_msg}", ephemeral=True)
    
    async def check_quest_points_recorded(self):
        """Check if quest points have been recorded for this quest"""
        try:
            worksheet = self.quest_tracker.get_cached_worksheet(self.quest_tracker.SHEET_URL, self.quest_tracker.WORKSHEET_NAME)
            if not worksheet:
                return False
            
            all_values = await self.quest_tracker.cached_get_all_values(worksheet, self.quest_tracker.WORKSHEET_NAME)
            
            # Find all players in this quest
            for i, row in enumerate(all_values[1:], start=2):  # Skip header
                if len(row) > 0 and row[0] == self.quest_id:  # Column A: Quest ID
                    player_id = row[10] if len(row) > 10 else "❓"  # Column K: Player ID
                    player_name = row[11] if len(row) > 11 else "❓"  # Column L: Player Name
                    
                    if player_id and player_name:  # Skip leader row (empty player data)
                        # Check if quest points recorded (any non-empty quest point field)
                        quest_points = row[14] if len(row) > 14 else "❓"  # Column O: Quests
                        ground_kills = row[15] if len(row) > 15 else "❓"  # Column P: Ground Kills
                        pilot_kills = row[16] if len(row) > 16 else "❓"   # Column Q: Pilot Kills
                        crusade_points = row[26] if len(row) > 26 else "❓" # Column AA: Crusades
                        griefer_kills = row[28] if len(row) > 28 else "❓" # Column AC: Griefer Kills
                        
                        # If any player doesn't have points recorded, return False
                        if not any([quest_points, ground_kills, pilot_kills, crusade_points, griefer_kills]):
                            return False
            
            return True  # All players have points recorded
            
        except Exception as e:
            print(f"Error checking quest points: {e}")
            return False
    
    async def get_quest_roster_for_review(self):
        """Get quest roster and points data for review embed"""
        try:
            worksheet = self.quest_tracker.get_cached_worksheet(self.quest_tracker.SHEET_URL, self.quest_tracker.WORKSHEET_NAME)
            if not worksheet:
                return {'players': [], 'quest_name': 'Unknown Quest', 'quest_description': 'No description available', 'total_quest_points': 0, 'total_crusade_points': 0, 'total_fps_kills': 0, 'total_ship_kills': 0, 'total_griefer_kills': 0}
            
            all_values = await self.quest_tracker.cached_get_all_values(worksheet, self.quest_tracker.WORKSHEET_NAME)
            players = []
            total_quest_points = 0
            total_crusade_points = 0
            total_ground_kills = 0
            total_pilot_kills = 0
            total_griefer_kills = 0
            quest_name = "Unknown Quest"
            quest_description = "No description available"
            
            # Find all players in this quest
            for i, row in enumerate(all_values[1:], start=2):  # Skip header
                if len(row) > 0 and row[0] == self.quest_id:  # Column A: Quest ID
                    # Extract quest details from the first matching row
                    if len(row) > 4:
                        quest_name = row[4] if row[4] else "Unknown Quest"  # Column E: Quest Name
                    if len(row) > 5:
                        quest_description = row[5] if row[5] else "No description available"  # Column F: Quest Description
                    
                    player_id = row[10] if len(row) > 10 else "❓"  # Column K: Player ID
                    player_name = row[11] if len(row) > 11 else "❓"  # Column L: Player Name
                    
                    if player_id and player_name:  # Skip leader row (empty player data)
                        player_rank = await self.quest_tracker.get_member_rank_from_log(player_id)  # Get rank from Member Log
                        player_role = await self.quest_tracker.get_member_role_from_log(player_id)  # Get role from Member Log
                        
                        # Get quest points data
                        quest_points = row[14] if len(row) > 14 else "0"     # Column O: Quests
                        ground_kills = row[15] if len(row) > 15 else "0"     # Column P: Ground Kills
                        pilot_kills = row[16] if len(row) > 16 else "0"      # Column Q: Pilot Kills
                        crusade_points = row[26] if len(row) > 26 else "0"   # Column AA: Crusades
                        griefer_kills = row[28] if len(row) > 28 else "0"    # Column AC: Griefer Kills
                        
                        # Convert to integers for totals
                        try:
                            quest_points_int = int(quest_points) if quest_points else 0
                            crusade_points_int = int(crusade_points) if crusade_points else 0
                            ground_kills_int = int(ground_kills) if ground_kills else 0
                            pilot_kills_int = int(pilot_kills) if pilot_kills else 0
                            griefer_kills_int = int(griefer_kills) if griefer_kills else 0
                        except ValueError:
                            quest_points_int = crusade_points_int = ground_kills_int = pilot_kills_int = griefer_kills_int = 0
                        
                        players.append({
                            'name': player_name,
                            'rank': player_rank,
                            'role': player_role,
                            'quest_points': quest_points or "0",
                            'crusade_points': crusade_points or "0",
                            'fps_kills': ground_kills or "0",
                            'ship_kills': pilot_kills or "0",
                            'griefer_kills': griefer_kills or "0"
                        })
                        
                        # Add to totals
                        total_quest_points += quest_points_int
                        total_crusade_points += crusade_points_int
                        total_ground_kills += ground_kills_int
                        total_pilot_kills += pilot_kills_int
                        total_griefer_kills += griefer_kills_int
            
            return {
                'players': players,
                'quest_name': quest_name,
                'quest_description': quest_description,
                'total_quest_points': total_quest_points,
                'total_crusade_points': total_crusade_points,
                'total_fps_kills': total_ground_kills,
                'total_ship_kills': total_pilot_kills,
                'total_griefer_kills': total_griefer_kills
            }
            
        except Exception as e:
            print(f"Error getting quest roster for review: {e}")
            return {'players': [], 'quest_name': 'Unknown Quest', 'quest_description': 'No description available', 'total_quest_points': 0, 'total_crusade_points': 0, 'total_fps_kills': 0, 'total_ship_kills': 0, 'total_griefer_kills': 0}
    
    async def submit_for_review(self, interaction):
        """Submit the quest for admin review"""
        try:
            await interaction.response.defer(ephemeral=True)
            
            # Get quest review channel
            quest_review_channel_id = self.quest_tracker.get_quest_review_channel(interaction.guild.id)
            if not quest_review_channel_id:
                await interaction.followup.send("❌ Quest review channel not configured. Please contact an administrator.", ephemeral=True)
                return
            
            quest_review_channel = interaction.guild.get_channel(quest_review_channel_id)
            if not quest_review_channel:
                await interaction.followup.send("❌ Quest review channel not found. Please contact an administrator.", ephemeral=True)
                return
            
            # Update Google Sheets to mark as "Sent for review" for ALL rows with this quest ID
            worksheet = self.quest_tracker.get_cached_worksheet(self.quest_tracker.SHEET_URL, self.quest_tracker.WORKSHEET_NAME)
            updated_rows = 0
            if worksheet:
                all_values = await self.quest_tracker.cached_get_all_values(worksheet, self.quest_tracker.WORKSHEET_NAME)
                for i, row in enumerate(all_values[1:], start=2):  # Skip header
                    if len(row) > 0 and row[0] == self.quest_id:  # Column A: Quest ID
                        # Update "Sent for review" column (Y = 25) for this participant
                        await self.quest_tracker.rate_limited_update_cell(worksheet, i, 25, "SFR")  # Column Y: Sent for review
                        updated_rows += 1
                print(f"Updated {updated_rows} rows to 'SFR' status for quest {self.quest_id}")
            
            # Get quest thread for jump link
            quest_thread = interaction.channel
            quest_jump_link = f"[🔗 **Jump to Quest Thread**]({quest_thread.jump_url})" if isinstance(quest_thread, discord.Thread) else "N/A"
            
            # Get quest roster and points data
            roster_info = await self.get_quest_roster_for_review()
            
            # Create review embed
            embed = discord.Embed(
                title=f"📋 Quest Review Required: {roster_info.get('quest_name', 'Unknown Quest')}",
                description=f"**Quest ID:** `{self.quest_id}`\n**Submitted by:** {interaction.user.mention}\n\n{quest_jump_link}",
                color=0xffaa00  # Orange color for review
            )
            
            # Add quest description if available
            quest_desc = roster_info.get('quest_description', 'No description available')
            if quest_desc and quest_desc != 'No description available':
                embed.add_field(name="📜 Quest Description", value=quest_desc[:1024], inline=False)  # Discord field limit
            
            # Add quest details
            embed.add_field(name="📊 Status", value="✅ Quest Completed\n🪶 Points Recorded", inline=True)
            embed.add_field(name="⏲️ Submitted", value=f"<t:{int(datetime.now().timestamp())}:R>", inline=True)
            
            # Add roster information
            if roster_info['players']:
                # Split roster into chunks if too long
                roster_chunks = []
                current_chunk = ""
                
                for i, player in enumerate(roster_info['players'], 1):
                    player_line = f"**{i}. {player['name']}** - Rank: {player['rank']} **|** Role: {player['role']}\n"
                    player_line += f"📜 {player['quest_points']} | 🏛️ {player['crusade_points']} | 🔫 {player['fps_kills']} | 🚀 {player['ship_kills']} | 💀 {player['griefer_kills']}\n\n"
                    
                    if len(current_chunk + player_line) > 1000:  # Discord field limit
                        roster_chunks.append(current_chunk)
                        current_chunk = player_line
                    else:
                        current_chunk += player_line
                
                if current_chunk:
                    roster_chunks.append(current_chunk)
                
                # Add roster fields
                for i, chunk in enumerate(roster_chunks):
                    field_name = "👥 Quest Roster & Points" if i == 0 else f"👥 Roster (continued {i+1})"
                    embed.add_field(name=field_name, value=chunk.strip(), inline=False)
                
                # Add summary
                embed.add_field(
                    name="📊 Summary",
                    value=f"**Total Participants:** {len(roster_info['players'])}\n"
                          f"**Total Quest Points:** {roster_info['total_quest_points']}\n"
                          f"**Total Crusade Points:** {roster_info['total_crusade_points']}\n"
                          f"**Total Ground Kills:** {roster_info['total_fps_kills']}\n"
                          f"**Total Pilot Kills:** {roster_info['total_ship_kills']}\n"
                          f"**Total Griefer Kills:** {roster_info['total_griefer_kills']}",
                    inline=True
                )
            else:
                embed.add_field(name="👥 Quest Roster", value="No participants found", inline=False)
            
            embed.set_footer(text="Admins: Click a button below to review this quest")
            
            # Create review buttons
            view = QuestReviewView(self.quest_id, self.quest_tracker)
            
            # Send to review channel and store the message reference
            review_message = await quest_review_channel.send(embed=embed, view=view)
            self.quest_tracker.store_quest_review_message(self.quest_id, review_message)
            
            # Update quest status to completed
            await self.update_status(interaction, "completed", "✅ Quest Completed")
            
            # Update forum tags: Add "Quest Completed" and remove "Quest Started"
            try:
                quest_thread = interaction.channel
                if isinstance(quest_thread, discord.Thread):
                    forum_channel = quest_thread.parent
                    if isinstance(forum_channel, discord.ForumChannel):
                        # Get the "Quest Completed" and "Quest Started" tags
                        completed_tag = self.quest_tracker.get_quest_tag_by_name(forum_channel, "Quest Completed")
                        started_tag = self.quest_tracker.get_quest_tag_by_name(forum_channel, "Quest Started")
                        
                        if completed_tag:
                            # Remove "Quest Started" tag and add "Quest Completed" tag
                            current_tags = [tag for tag in quest_thread.applied_tags if tag != started_tag]
                            if completed_tag not in current_tags:
                                current_tags.append(completed_tag)
                            
                            await quest_thread.edit(applied_tags=current_tags)
                            print(f"Updated forum tags for quest {self.quest_id}: Added 'Quest Completed', removed 'Quest Started'")
                        else:
                            print(f"Warning: 'Quest Completed' tag not found for quest {self.quest_id}")
            except Exception as e:
                print(f"Error updating forum tags for quest {self.quest_id}: {e}")
            
            await interaction.followup.send("✅ Quest has been submitted for admin review!", ephemeral=True)
            
        except Exception as e:
            print(f"Error submitting quest for review: {e}")
            await interaction.followup.send(f"❌ Error submitting quest for review: {str(e)}", ephemeral=True)

class QuestCompletionWorkflowView(discord.ui.View):
    def __init__(self, quest_id: str, quest_tracker, patrol_leader_id: str):
        super().__init__(timeout=300)
        self.quest_id = quest_id
        self.quest_tracker = quest_tracker
        self.patrol_leader_id = patrol_leader_id
    
    @discord.ui.button(label="Record Quest Points", style=discord.ButtonStyle.primary, emoji="🪶")
    async def record_points(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Use the existing quest points recording functionality
        quest_controls = QuestStatusView(self.quest_id, self.quest_tracker, self.patrol_leader_id)
        await quest_controls.show_quest_points_interface(interaction)
    
    @discord.ui.button(label="Submit for Review", style=discord.ButtonStyle.success, emoji="📤")
    async def submit_review(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if quest points are recorded before allowing submission
        quest_controls = QuestStatusView(self.quest_id, self.quest_tracker, self.patrol_leader_id)
        if not await quest_controls.check_quest_points_recorded():
            await interaction.response.send_message("❌ You must record quest points for all participants before submitting for review.", ephemeral=True)
            return
        
        # Submit for review
        await quest_controls.submit_for_review(interaction)

class QuestReviewView(discord.ui.View):
    def __init__(self, quest_id: str, quest_tracker):
        super().__init__(timeout=None)  # Persistent view
        self.quest_id = quest_id
        self.quest_tracker = quest_tracker
        
        # Set unique custom_id for each button based on quest_id
        for item in self.children:
            if hasattr(item, 'label'):
                if item.label == "Approve Quest Results":
                    item.custom_id = f"approve_quest_{quest_id}"
                elif item.label == "Edit Results":
                    item.custom_id = f"edit_results_{quest_id}"
    
    @discord.ui.button(label="Approve Quest Results", style=discord.ButtonStyle.success, emoji="✅")
    async def approve_quest(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user is admin
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need Administrator permissions to approve quests.", ephemeral=True)
            return
        
        try:
            await interaction.response.defer(ephemeral=True)
            
            # Update Google Sheets to mark as approved for ALL rows with this quest ID
            worksheet = self.quest_tracker.get_cached_worksheet(self.quest_tracker.SHEET_URL, self.quest_tracker.WORKSHEET_NAME)
            updated_rows = 0
            if worksheet:
                all_values = await self.quest_tracker.cached_get_all_values(worksheet, self.quest_tracker.WORKSHEET_NAME)
                for i, row in enumerate(all_values[1:], start=2):  # Skip header
                    if len(row) > 0 and row[0] == self.quest_id:  # Column A: Quest ID
                        # Ensure "Sent for review" column (Y = 25) is marked
                        await self.quest_tracker.rate_limited_update_cell(worksheet, i, 25, "SFR")  # Column Y: Sent for review
                        # Update "Admin Approved" column (Z = 26)
                        await self.quest_tracker.rate_limited_update_cell(worksheet, i, 26, "Approved")  # Column Z: Admin Approved
                        updated_rows += 1
                print(f"Updated {updated_rows} rows to 'SFR' and 'Approved' status for quest {self.quest_id}")
            
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
            
            # Ensure forum tags are correct: "Quest Completed" should already be set from submission
            # This is just a safety check to ensure the tag is still there
            try:
                # Find the quest thread using the quest ID
                quest_thread = None
                for guild in interaction.client.guilds:
                    for channel in guild.channels:
                        if isinstance(channel, discord.ForumChannel):
                            for thread in channel.threads:
                                if thread.name and self.quest_id in thread.name:
                                    quest_thread = thread
                                    break
                            if quest_thread:
                                break
                    if quest_thread:
                        break
                
                if quest_thread:
                    forum_channel = quest_thread.parent
                    completed_tag = self.quest_tracker.get_quest_tag_by_name(forum_channel, "Quest Completed")
                    
                    if completed_tag and completed_tag not in quest_thread.applied_tags:
                        # Add "Quest Completed" tag if somehow missing
                        current_tags = list(quest_thread.applied_tags)
                        current_tags.append(completed_tag)
                        await quest_thread.edit(applied_tags=current_tags)
                        print(f"Added missing 'Quest Completed' tag to approved quest {self.quest_id}")
                    else:
                        print(f"Quest {self.quest_id} already has correct 'Quest Completed' tag")
                else:
                    print(f"Warning: Could not find quest thread for {self.quest_id} to verify tags")
            except Exception as e:
                print(f"Error verifying forum tags for approved quest {self.quest_id}: {e}")
            
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
        # Check if user is admin
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need Administrator permissions to edit quest results.", ephemeral=True)
            return
        
        await interaction.response.send_message(
            "📝 **Edit Quest Results**\n\n"
            "To edit quest results:\n"
            "1. Go to the original quest thread\n"
            "2. Use the 'Record Quest Points' button to update player scores\n"
            "3. Return here to approve the updated results\n\n"
            f"**Quest ID:** `{self.quest_id}`",
            ephemeral=True
        )

class JoinQuestView(discord.ui.View):
    def __init__(self, patrol_id: str, patrol_name: str, patrol_leader: discord.Member, leader_rank: str, quest_tracker):
        super().__init__(timeout=None)  # Persistent view
        self.patrol_id = patrol_id
        self.patrol_name = patrol_name
        self.patrol_leader = patrol_leader
        self.leader_rank = leader_rank
        self.quest_tracker = quest_tracker
        self.processing_users = set()  # Track users currently processing join requests
        
        # Set unique custom_id for each button based on quest_id
        for item in self.children:
            if hasattr(item, 'label'):
                if item.label == "Join Quest":
                    item.custom_id = f"join_quest_{patrol_id}"
                elif item.label == "Manage Quest":
                    item.custom_id = f"manage_quest_{patrol_id}"
    
    @discord.ui.button(label="Join Quest", style=discord.ButtonStyle.success, emoji="🚀")
    async def join_quest(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # Check if this user is already processing a join request
            if interaction.user.id in self.processing_users:
                await interaction.response.send_message("⏳ Your join request is already being processed. Please wait...", ephemeral=True)
                return
            
            # Add user to processing set
            self.processing_users.add(interaction.user.id)
            
            # Defer the response - this allows multiple users to join simultaneously
            await interaction.response.defer(ephemeral=True)
            
            # Check if user is already in this quest
            worksheet = self.quest_tracker.get_worksheet(self.quest_tracker.SHEET_URL, self.quest_tracker.WORKSHEET_NAME)
            if not worksheet:
                self.processing_users.discard(interaction.user.id)
                await interaction.followup.send("❌ Could not access the OFS Google Sheet.", ephemeral=True)
                return
            
            # Get all values to check for existing participation
            all_values = await self.quest_tracker.cached_get_all_values(worksheet, self.quest_tracker.WORKSHEET_NAME)
            user_already_joined = False
            
            for row in all_values:
                # Check if user is already in this quest by looking at Player ID column (K, index 10)
                if (len(row) > 10 and row[0] == self.patrol_id and row[10] == str(interaction.user.id)):
                    user_already_joined = True
                    break
            
            if user_already_joined:
                self.processing_users.discard(interaction.user.id)
                await interaction.followup.send(f"❌ You're already participating in quest `{self.patrol_id}`!", ephemeral=True)
                return
            
            # Get the original quest row to copy data
            original_quest_data = None
            for row in all_values:
                if len(row) > 0 and row[0] == self.patrol_id:
                    original_quest_data = row
                    break
            
            if not original_quest_data:
                self.processing_users.discard(interaction.user.id)
                await interaction.followup.send(f"❌ Could not find quest `{self.patrol_id}` in the sheet.", ephemeral=True)
                return
            
            # Look up member data from Member Log tab
            member_log_worksheet = self.quest_tracker.get_worksheet(self.quest_tracker.SHEET_URL, "Member Log")
            member_data = {
                "role": "Member",
                "rank": "Member",  # Add rank field
                "quest_crusades": "❓",
                "fps_kills": "❓",
                "ship_kills": "❓",
                "hosted_quest": "❓",
                "time_in_service": "❓"
            }
            
            if member_log_worksheet:
                try:
                    member_log_values = await self.quest_tracker.cached_get_all_values(member_log_worksheet, "Member Log")
                    # Look for the user's Discord ID in the Member Log - check both columns A and B
                    for row in member_log_values[1:]:  # Skip header row
                        # Check both column A (User ID) and column B (Discord ID) for the user ID
                        if ((len(row) > 0 and str(interaction.user.id) == str(row[0])) or 
                            (len(row) > 1 and str(interaction.user.id) == str(row[1]))):
                            # Member Log columns: [A:User ID, B:Discord ID, C:Rank, D:Role, E:Quest/Crusades, F:FPS Kills, G:Time in Service, H:Ship Kills, I:Hosted Quest/Crusades]
                            if len(row) > 2:
                                member_data["rank"] = row[2] if row[2] else "Member"  # C: Rank
                            if len(row) > 3:
                                member_data["role"] = row[3] if row[3] else "Member"  # D: Role
                            if len(row) > 4:
                                member_data["quest_crusades"] = row[4] if row[4] else "❓"  # E: Quest/Crusades
                            if len(row) > 5:
                                member_data["fps_kills"] = row[5] if row[5] else "❓"  # F: FPS Kills
                            if len(row) > 6:
                                member_data["time_in_service"] = row[6] if row[6] else "❓"  # G: Time in Service
                            if len(row) > 7:
                                member_data["ship_kills"] = row[7] if row[7] else "❓"  # H: Ship Kills
                            if len(row) > 8:
                                member_data["hosted_quest"] = row[8] if row[8] else "❓"  # I: Hosted Quest/Crusades
                            break
                except Exception as e:
                    print(f"Error reading Member Log: {e}")
            
            # Look up rank icon from Ranks sheet
            rank_icon_url = None
            ranks_worksheet = self.quest_tracker.get_worksheet(self.quest_tracker.SHEET_URL, "Ranks")
            if ranks_worksheet:
                try:
                    print(f"🔍 Looking up rank icon for rank: {member_data['rank']}")
                    ranks_values = await self.quest_tracker.cached_get_all_values(ranks_worksheet, "Ranks")
                    for row in ranks_values[1:]:  # Skip header row
                        if len(row) > 0 and row[0] == member_data["rank"]:  # Match rank name in column A
                            if len(row) > 2 and row[2]:  # Get rank icon from column C (Rank Icon)
                                rank_icon_url = row[2].strip()  # Remove whitespace
                                print(f"✅ Found rank icon URL: '{rank_icon_url}'")
                                
                                # Validate and convert URL format
                                if not rank_icon_url.startswith(('http://', 'https://')):
                                    print(f"⚠️ Invalid URL format - expected http/https URL but got: '{rank_icon_url}'")
                                    print(f"   💡 Hint: Make sure the Ranks sheet column B contains full image URLs like:")
                                    print(f"   💡 https://example.com/image.png or https://cdn.discordapp.com/attachments/...")
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
            
            # Create new row for the joining member
            join_time = datetime.now()
            
            # Prepare row data for the new participant with correct column mapping
            participant_row_data = [
                self.patrol_id,                              # A: Patrol ID (same as original)
                original_quest_data[1] if len(original_quest_data) > 1 else "❓",  # B: Quest Leader Name (copy from original)
                original_quest_data[2] if len(original_quest_data) > 2 else "❓",  # C: Quest Leader ID (copy from original)
                original_quest_data[3] if len(original_quest_data) > 3 else "❓",  # D: Leader Rank (copy from original)
                original_quest_data[4] if len(original_quest_data) > 4 else "❓",  # E: Quest Name (copy from original)
                original_quest_data[5] if len(original_quest_data) > 5 else "❓",  # F: Quest Description (copy from original)
                original_quest_data[6] if len(original_quest_data) > 6 else "❓",  # G: Quest Image (copy from original)
                original_quest_data[7] if len(original_quest_data) > 7 else "❓",  # H: Quest Start Time (copy from original)
                original_quest_data[8] if len(original_quest_data) > 8 else "❓",  # I: Quest Scheduled end time (copy from original)
                "❓",                                          # J: Quest actual end time (to be filled later)
                str(interaction.user.id),                    # K: Player ID (participant Discord ID)
                interaction.user.display_name,               # L: Player Name (participant name)
                member_data["role"],                         # M: Player Role (from Member Log)
                member_data["rank"],                         # N: Player Rank (from Member Log)
                "❓",                                          # O: Quests (RESERVED - leave empty for quest points)
                "❓",                                          # P: Ground Kills (RESERVED - leave empty for quest points)
                "❓",                                          # Q: Pilot Kills (RESERVED - leave empty for quest points)
                member_data["time_in_service"],              # R: Time in Service (from Member Log column G)
                original_quest_data[18] if len(original_quest_data) > 18 else "❓",  # S: Message ID (copy from original)
                original_quest_data[19] if len(original_quest_data) > 19 else "❓",  # T: Thread ID (copy from original)
                original_quest_data[20] if len(original_quest_data) > 20 else "❓",  # U: Roster ID (copy from original)
                join_time.strftime('%Y-%m-%d %H:%M:%S'),     # V: Join Time (member join timestamp)
                "❓",                                          # W: Reserved
                "❓",                                          # X: Reserved
                "❓",                                          # Y: Reserved
                "❓",                                          # Z: Reserved
                "❓",                                          # AA: Crusades (RESERVED - leave empty for quest points)
                original_quest_data[27] if len(original_quest_data) > 27 else "❓",  # AB: Game (copy from original)
                "❓"                                           # AC: Griefer Kills (RESERVED - leave empty for quest points)
            ]
            
            print(f"📊 Adding member {interaction.user.display_name} with data:")
            print(f"  📤 Player ID (K): {interaction.user.id}")
            print(f"  📤 Player Name (L): {interaction.user.display_name}")
            print(f"  👔 Player Role (M): {member_data['role']}")
            print(f"  📜 Quest/Crusades (N): BLANK - will be filled when recording quest points")
            print(f"  🔫 Ground Kills (P): BLANK - will be filled when recording quest points")
            print(f"  🚀 Pilot Kills (Q): BLANK - will be filled when recording quest points")
            print(f"  🔗 Message ID (S): {original_quest_data[18] if len(original_quest_data) > 18 else 'None'}")
            print(f"  🔗 Thread ID (T): {original_quest_data[19] if len(original_quest_data) > 19 else 'None'}")
            print(f"  🔗 Roster ID (U): {original_quest_data[20] if len(original_quest_data) > 20 else 'None'}")
            print(f"  ⏰ Join Time (V): {join_time.strftime('%Y-%m-%d %H:%M:%S')}")
            
            # Find next empty row and append data
            next_row = len(all_values) + 1
            range_name = f"A{next_row}:AC{next_row}"  # Extended to include all columns (A-AC)
            await self.quest_tracker.rate_limited_update(worksheet, range_name, [participant_row_data])
            
            # Create success embed
            embed = discord.Embed(
                title="✅ Successfully Joined Quest!",
                description=f"You've joined **{self.patrol_name}**",
                color=0x00ff00
            )
            embed.add_field(name="Quest ID", value=f"`{self.patrol_id}`", inline=True)
            embed.add_field(name="Quest Leader", value=f"{self.patrol_leader.mention}\n**Rank:** {self.leader_rank}", inline=True)
            embed.add_field(name="Your Rank", value=member_data["rank"], inline=True)
            embed.add_field(name="Your Role", value=member_data["role"], inline=True)
            embed.add_field(name="Joined", value=f"<t:{int(join_time.timestamp())}:R>", inline=True)
            
            # Add member stats if available
            if member_data["quest_crusades"]:
                embed.add_field(name="Quest/Crusades", value=member_data["quest_crusades"], inline=True)
            if member_data["time_in_service"]:
                embed.add_field(name="Time in Service", value=member_data["time_in_service"], inline=True)
            
            # Set rank icon as author icon if available and valid
            if rank_icon_url and rank_icon_url.startswith(('http://', 'https://')):
                try:
                    embed.set_author(name=f"{member_data['rank']}", icon_url=rank_icon_url)
                    print(f"✅ Set author icon for join embed: {rank_icon_url}")
                except Exception as e:
                    print(f"❌ Failed to set author icon: {e}")
            else:
                print(f"⚠️ No valid rank icon URL for join embed")
            
            if len(original_quest_data) > 6 and original_quest_data[6]:
                embed.set_thumbnail(url=original_quest_data[6])
            
            embed.set_footer(text=f"Your data has been pulled from Member Log and logged in row {next_row}")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
            # Optional: Send a notification to the thread about the new member
            notification_embed = discord.Embed(
                description=f"{interaction.user.mention} joined **{self.patrol_name}**",
                color=0x0099ff
            )
            
            # Add user's Discord avatar as thumbnail
            notification_embed.set_thumbnail(url=interaction.user.display_avatar.url)
            
            # Add rank information with icon if available and valid
            if rank_icon_url and rank_icon_url.startswith(('http://', 'https://')):
                try:
                    rank_display = f"{member_data['rank']}"
                    notification_embed.add_field(name="Rank", value=rank_display, inline=True)
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
            notification_embed.set_footer(text="➕ New Quest Member")
            
            await interaction.channel.send(embed=notification_embed)
            
            # Update the roster embed
            try:
                print(f"📝 Starting roster update for quest {self.patrol_id}")
                # Get the roster message ID from the sheet
                worksheet = self.quest_tracker.get_worksheet(self.quest_tracker.SHEET_URL, self.quest_tracker.WORKSHEET_NAME)
                if worksheet:
                    print(f"✅ Got worksheet for roster update")
                    all_values = await self.quest_tracker.rate_limited_get_all_values(worksheet)
                    roster_message_id = None
                    thread_id = None
                    
                    # Find the quest row and get Roster Message ID and Thread ID
                    quest_found = False
                    for row in all_values[1:]:
                        if len(row) > 0 and row[0] == self.patrol_id:
                            quest_found = True
                            print(f"🔍 Found quest row for {self.patrol_id}, row length: {len(row)}")
                            
                            # Show all relevant IDs for debugging
                            message_id_col = row[18] if len(row) > 18 else "❓"  # S: Message ID
                            thread_id_col = row[19] if len(row) > 19 else "❓"   # T: Thread ID
                            roster_id_col = row[20] if len(row) > 20 else "❓"   # U: Roster ID
                            
                            print(f"📊 All stored IDs for quest {self.patrol_id}:")
                            print(f"  🔗 Message ID (S/19): {message_id_col}")
                            print(f"  🔗 Thread ID (T/20): {thread_id_col}")
                            print(f"  🔗 Roster ID (U/21): {roster_id_col}")
                            
                            if len(row) > 20:  # Roster Message ID in column U (index 20)
                                roster_message_id = row[20] if row[20] else None
                                print(f"🔍 Using Roster Message ID: {roster_message_id}")
                            if len(row) > 19:  # Thread ID in column T (index 19)  
                                thread_id = row[19] if row[19] else None
                                print(f"🔍 Using Thread ID: {thread_id}")
                            break
                    
                    if not quest_found:
                        print(f"❌ Quest {self.patrol_id} not found in sheet")
                    
                    if roster_message_id and thread_id:
                        print(f"📜 Found both IDs - Roster: {roster_message_id}, Thread: {thread_id}")
                        try:
                            # Get the thread and roster message
                            print(f"🔍 Looking for thread with ID: {thread_id}")
                            target_thread = interaction.guild.get_channel(int(thread_id))
                            
                            if not target_thread:
                                print(f"❌ Thread not found with get_channel, trying fetch_channel...")
                                try:
                                    target_thread = await interaction.guild.fetch_channel(int(thread_id))
                                except Exception as fetch_error:
                                    print(f"❌ Failed to fetch channel: {fetch_error}")
                            
                            if target_thread and isinstance(target_thread, discord.Thread):
                                print(f"✅ Found target thread: {target_thread.name} (ID: {target_thread.id})")
                                try:
                                    roster_message = await target_thread.fetch_message(int(roster_message_id))
                                    print(f"✅ Found roster message: {roster_message.id}")
                                    
                                    # Generate updated roster embed
                                    updated_roster_embed = await self.quest_tracker.create_roster_embed(self.patrol_id)
                                    if updated_roster_embed:
                                        await roster_message.edit(embed=updated_roster_embed)
                                        print(f"✅ Updated roster for quest {self.patrol_id}")
                                    else:
                                        print(f"❌ Failed to create updated roster embed for quest {self.patrol_id}")
                                except discord.NotFound:
                                    print(f"❌ Roster message {roster_message_id} not found in thread {thread_id}")
                                except Exception as msg_error:
                                    print(f"❌ Error fetching roster message: {msg_error}")
                            else:
                                print(f"❌ Target thread not found or not a thread: {thread_id}")
                                print(f"   Thread object type: {type(target_thread)}")
                                if target_thread:
                                    print(f"   Channel name: {target_thread.name}")
                                    print(f"   Channel type: {target_thread.type}")
                        except Exception as e:
                            print(f"Error updating roster message: {e}")
                            import traceback
                            traceback.print_exc()
                    else:
                        print(f"⚠️ Missing IDs - Roster: {roster_message_id}, Thread: {thread_id}")
                        print(f"⚠️ No roster message ID ({roster_message_id}) or thread ID ({thread_id}) found for quest {self.patrol_id}")
                else:
                    print(f"❌ Failed to get worksheet for roster update")
            except Exception as e:
                print(f"Error updating roster: {e}")
            
            # Remove user from processing set after successful join
            self.processing_users.discard(interaction.user.id)
            
        except Exception as e:
            print(f"Error joining quest: {e}")
            # Remove user from processing set on error
            self.processing_users.discard(interaction.user.id)
            
            error_msg = str(e)
            try:
                # Handle rate limit errors specifically
                if "429" in error_msg or "Quota exceeded" in error_msg or "Read requests per minute" in error_msg:
                    await interaction.followup.send(
                        "⚠️ **Google Sheets Rate Limit Reached**\n\n"
                        "Too many users are joining quests simultaneously. Please wait 30 seconds and try again.\n\n"
                        "💡 **Tip:** The system has built-in retry logic, so this should work on the next attempt!",
                        ephemeral=True
                    )
                elif "Failed to access Google Sheets after maximum retries" in error_msg:
                    await interaction.followup.send(
                        "⚠️ **Connection Issues**\n\n"
                        "Unable to connect to Google Sheets after multiple attempts. Please try again in a minute.\n\n"
                        "If this continues, contact an admin.",
                        ephemeral=True
                    )
                else:
                    await interaction.followup.send(f"❌ An error occurred: {error_msg}", ephemeral=True)
            except:
                pass

    @discord.ui.button(label="Manage Quest", style=discord.ButtonStyle.secondary, emoji="⚙️", row=1)
    async def manage_quest(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user is admin or quest leader
        is_admin = interaction.user.guild_permissions.administrator
        is_quest_leader = str(interaction.user.id) == str(self.patrol_leader.id)
        
        if not is_admin and not is_quest_leader:
            await interaction.response.send_message("❌ You need Administrator permissions or be the Quest Leader to manage this quest.", ephemeral=True)
            return
        
        # Create the quest management view
        status_view = QuestStatusView(self.patrol_id, self.quest_tracker, str(self.patrol_leader.id))
        status_embed = discord.Embed(
            title="⚙️ Quest Management",
            description="**Administrators & Quest Leader:** Use the buttons below to update quest status",
            color=0x666666
        )
        status_embed.add_field(name="Quest ID", value=f"`{self.patrol_id}`", inline=True)
        status_embed.add_field(name="Quest Leader", value=f"{self.patrol_leader.mention}\n**Rank:** {self.leader_rank}", inline=True)
        status_embed.add_field(name="Quest Name", value=self.patrol_name, inline=True)
        status_embed.set_footer(text="Status updates will be logged and visible to all members")
        
        await interaction.response.send_message(embed=status_embed, view=status_view, ephemeral=True)

class PatrolDraft:
    def __init__(self):
        self.patrol_name = "❓"
        self.patrol_description = "❓"
        self.patrol_image = "❓"
        self.scheduled_duration = "❓"
        self.game = "❓"
        self.loaded_template_name = None
    
    def to_dict(self):
        return {
            "patrol_name": self.patrol_name,
            "patrol_description": self.patrol_description,
            "patrol_image": self.patrol_image,
            "scheduled_duration": self.scheduled_duration,
            "game": self.game
        }
    
    def from_dict(self, data):
        self.patrol_name = data.get("patrol_name", "❓")
        self.patrol_description = data.get("patrol_description", "❓")
        self.patrol_image = data.get("patrol_image", "❓")
        self.scheduled_duration = data.get("scheduled_duration", "❓")

class QuestNameModal(discord.ui.Modal, title='Edit Quest Name'):
    def __init__(self, draft: PatrolDraft, view):
        super().__init__()
        self.draft = draft
        self.view = view
        self.quest_name.default = draft.patrol_name
    
    quest_name = discord.ui.TextInput(
        label='Quest Name',
        placeholder='Enter the name of your quest...',
        required=True,
        max_length=100
    )

    async def on_submit(self, interaction: discord.Interaction):
        self.draft.patrol_name = self.quest_name.value
        await self.view.update_preview(interaction)

class QuestDescriptionModal(discord.ui.Modal, title='Edit Quest Description'):
    def __init__(self, draft: PatrolDraft, view):
        super().__init__()
        self.draft = draft
        self.view = view
        self.quest_description.default = draft.patrol_description
    
    quest_description = discord.ui.TextInput(
        label='Quest Description',
        placeholder='Describe what this quest will do...',
        style=discord.TextStyle.long,
        required=False,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        self.draft.patrol_description = self.quest_description.value
        await self.view.update_preview(interaction)

class QuestImageModal(discord.ui.Modal, title='Edit Quest Image'):
    def __init__(self, draft: PatrolDraft, view):
        super().__init__()
        self.draft = draft
        self.view = view
        self.quest_image.default = draft.patrol_image
    
    quest_image = discord.ui.TextInput(
        label='Quest Image URL',
        placeholder='https://example.com/image.png',
        required=False,
        max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction):
        self.draft.patrol_image = self.quest_image.value
        await self.view.update_preview(interaction)

class QuestDurationModal(discord.ui.Modal, title='Edit Quest Duration'):
    def __init__(self, draft: PatrolDraft, view):
        super().__init__()
        self.draft = draft
        self.view = view
        self.scheduled_duration.default = draft.scheduled_duration
    
    scheduled_duration = discord.ui.TextInput(
        label='Scheduled Duration (minutes)',
        placeholder='60',
        required=False,
        max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        self.draft.scheduled_duration = self.scheduled_duration.value
        await self.view.update_preview(interaction)

class QuestTemplateNameModal(discord.ui.Modal, title='Save Quest Template'):
    def __init__(self, draft: PatrolDraft, view, guild_id: int):
        super().__init__()
        self.draft = draft
        self.view = view
        self.guild_id = guild_id
    
    template_name = discord.ui.TextInput(
        label='Template Name',
        placeholder='Enter a name for this quest template...',
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            template_name = self.template_name.value
            
            # Load existing templates
            templates_file = "patrol_templates.json"
            templates = {}
            if os.path.exists(templates_file):
                with open(templates_file, 'r') as f:
                    templates = json.load(f)
            
            # Initialize guild templates if not exists
            guild_key = str(self.guild_id)
            if guild_key not in templates:
                templates[guild_key] = {}
            
            # Check if template already exists
            template_exists = template_name in templates[guild_key]
            
            if template_exists:
                # Show warning with confirm/cancel buttons
                warning_embed = discord.Embed(
                    title="⚠️ Template Already Exists",
                    description=f"A template named **{template_name}** already exists.\n\nDo you want to overwrite it?",
                    color=0xff9900
                )
                
                confirm_view = TemplateOverwriteView(template_name, self.draft, self.guild_id)
                await interaction.response.send_message(embed=warning_embed, view=confirm_view, ephemeral=True)
            else:
                # Save new template
                templates[guild_key][template_name] = self.draft.to_dict()
                with open(templates_file, 'w') as f:
                    json.dump(templates, f, indent=2)
                
                embed = discord.Embed(
                    title="✅ Template Saved",
                    description=f"Quest template **{template_name}** has been saved!",
                    color=0x00ff00
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                
        except Exception as e:
            await interaction.response.send_message(f"❌ Error saving template: {str(e)}", ephemeral=True)

class QuestPointsRecordingView(discord.ui.View):
    def __init__(self, quest_id: str, quest_players: list, quest_tracker):
        super().__init__(timeout=300)
        self.quest_id = quest_id
        self.quest_players = quest_players
        self.quest_tracker = quest_tracker
        self.original_interaction = None  # Store the original interaction for refreshing
        self.message = None  # Store the message reference
        
        # Create player selection dropdown
        options = []
        
        # Add "Set Equal Points for All" option at the top if there are multiple players
        if len(quest_players) > 1:
            options.append(discord.SelectOption(
                label="📜 Set Equal Points for All Players",
                value="all_players",
                description=f"Apply the same quest points to all {len(quest_players)} players",
                emoji="📜"
            ))
        
        for i, player in enumerate(quest_players[:24]):  # Limit to 24 to account for the "all players" option
            options.append(discord.SelectOption(
                label=player['player_name'],
                value=str(i),
                description=f"Record quest points for {player['player_name']}"
            ))
        
        if options:
            select = PlayerSelectionDropdown(options, self.quest_id, self.quest_players, self.quest_tracker, self)
            self.add_item(select)
    
    def set_interaction(self, interaction):
        """Store the original interaction for later refreshing"""
        self.original_interaction = interaction
    
    def set_message(self, message):
        """Store the message reference for later editing"""
        self.message = message
    
    async def refresh_interface_silently(self):
        """Refresh the interface after quest points are recorded without creating duplicates"""
        try:
            # Re-fetch updated quest data from Google Sheets using cached data
            worksheet = self.quest_tracker.get_cached_worksheet(self.quest_tracker.SHEET_URL, self.quest_tracker.WORKSHEET_NAME)
            if not worksheet:
                return
            
            # Get Member Log data once for all lookups using cached data
            member_log_worksheet = self.quest_tracker.get_cached_worksheet(self.quest_tracker.SHEET_URL, "Member Log")
            member_log_values = []
            if member_log_worksheet:
                try:
                    member_log_values = await self.quest_tracker.cached_get_all_values(member_log_worksheet, "Member Log")
                except Exception as e:
                    print(f"⚠️ Could not load Member Log for batch optimization: {e}")
            
            # Find all players in this quest again (with updated data)
            all_values = await self.quest_tracker.cached_get_all_values(worksheet, self.quest_tracker.WORKSHEET_NAME)
            quest_players = []
            
            for i, row in enumerate(all_values[1:], start=2):  # Skip header, start from row 2
                if len(row) > 0 and row[0] == self.quest_id:  # Column A: Quest ID
                    player_id = row[10] if len(row) > 10 else "❓"  # Column K: Player ID  
                    player_name = row[11] if len(row) > 11 else "❓"  # Column L: Player Name
                    
                    if player_id and player_name:  # Skip leader row (empty player data)
                        # Use cached Member Log data to avoid individual API calls
                        if member_log_values:
                            player_rank = self.quest_tracker.get_member_rank_from_cached_data(player_id, member_log_values)
                        else:
                            player_rank = "Member"  # Fallback if Member Log couldn't be loaded
                        join_time_str = row[21] if len(row) > 21 else "❓"  # Column V: Join Time
                        
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
                        
                        # Check if quest points have been recorded (any non-empty quest point field)
                        quest_points = row[14] if len(row) > 14 else "❓"  # Column O: Quest/Crusades
                        fps_kills = row[15] if len(row) > 15 else "❓"     # Column P: FPS Kills
                        ship_kills = row[16] if len(row) > 16 else "❓"    # Column Q: Ship Kills
                        griefer_kills = row[28] if len(row) > 28 else "❓" # Column AC: Griefer Kills
                        
                        has_recorded_points = any([quest_points, fps_kills, ship_kills, griefer_kills])
                        
                        quest_players.append({
                            'row': i,
                            'player_id': player_id,
                            'player_name': player_name,
                            'player_rank': player_rank,
                            'time_in_quest': time_in_quest,
                            'points_recorded': has_recorded_points,
                            'current_quest_points': quest_points,
                            'current_fps_kills': fps_kills,
                            'current_ship_kills': ship_kills,
                            'current_griefer_kills': griefer_kills
                        })
            
            # Update the stored quest players data
            self.quest_players = quest_players
            
            # Recreate the view with updated data
            self.clear_items()
            options = []
            
            # Add "Set Equal Points for All" option at the top if there are multiple players
            if len(quest_players) > 1:
                options.append(discord.SelectOption(
                    label="📜 Set Equal Points for All Players",
                    value="all_players",
                    description=f"Apply the same quest points to all {len(quest_players)} players",
                    emoji="📜"
                ))
            
            for i, player in enumerate(quest_players[:24]):  # Limit to 24 to account for the "all players" option
                options.append(discord.SelectOption(
                    label=player['player_name'],
                    value=str(i),
                    description=f"Record quest points for {player['player_name']}"
                ))
            
            if options:
                select = PlayerSelectionDropdown(options, self.quest_id, quest_players, self.quest_tracker, self)
                self.add_item(select)
            
            # Create updated embed with the same structure as original
            embed = discord.Embed(
                title="🪶 Record Quest Points",
                description=f"Select a player to record their quest performance:\n\n**Quest ID:** `{self.quest_id}`",
                color=0x0099ff  # Imperial blue color
            )
            
            # Add detailed player information with updated status icons
            if quest_players:
                # Limit to 10 players for display to avoid embed limits
                display_players = quest_players[:10]
                
                for i, player in enumerate(display_players):
                    status_icon = "✅" if player['points_recorded'] else "⏳"
                    
                    # Player name field with updated status
                    embed.add_field(
                        name=f"{i+1}. {player['player_name']} {status_icon}",
                        value=f"**Rank:** {player['player_rank']}\n**Time in Quest:** {player['time_in_quest']}",
                        inline=True
                    )
                
                # Add spacing if odd number of players (for better layout)
                if len(display_players) % 2 == 1:
                    embed.add_field(name="\u200b", value="\u200b", inline=True)
                
                # Add footer with additional info if there are more players
                if len(quest_players) > 10:
                    embed.set_footer(text=f"Showing 10 of {len(quest_players)} players. Use dropdown to select any player.")
                else:
                    embed.set_footer(text="✅ = Points recorded | ⏳ = Awaiting recording")
            
            # Edit the existing message with updated embed and view (this should not create duplicates)
            if self.message:
                await self.message.edit(embed=embed, view=self)
            
        except Exception as e:
            print(f"Error refreshing quest points interface silently: {e}")
    
    async def refresh_interface(self, interaction):
        """Refresh the quest points interface with updated data"""
        try:
            # Re-fetch updated quest data
            worksheet = self.quest_tracker.get_cached_worksheet(self.quest_tracker.SHEET_URL, self.quest_tracker.WORKSHEET_NAME)
            if not worksheet:
                return
            
            # Get Member Log data once for all lookups (optimization to reduce API calls)
            member_log_worksheet = self.quest_tracker.get_cached_worksheet(self.quest_tracker.SHEET_URL, "Member Log")
            member_log_values = []
            if member_log_worksheet:
                try:
                    member_log_values = await self.quest_tracker.cached_get_all_values(member_log_worksheet, "Member Log")
                except Exception as e:
                    print(f"⚠️ Could not load Member Log for batch optimization: {e}")
            
            # Find all players in this quest again (with updated data)
            all_values = await self.quest_tracker.cached_get_all_values(worksheet, self.quest_tracker.WORKSHEET_NAME)
            quest_players = []
            
            for i, row in enumerate(all_values[1:], start=2):  # Skip header, start from row 2
                if len(row) > 0 and row[0] == self.quest_id:  # Column A: Quest ID
                    player_id = row[10] if len(row) > 10 else "❓"  # Column K: Player ID
                    player_name = row[11] if len(row) > 11 else "❓"  # Column L: Player Name
                    
                    if player_id and player_name:  # Skip leader row (empty player data)
                        # Use cached Member Log data to avoid individual API calls
                        if member_log_values:
                            player_rank = self.quest_tracker.get_member_rank_from_cached_data(player_id, member_log_values)
                        else:
                            player_rank = "Member"  # Fallback if Member Log couldn't be loaded
                        join_time_str = row[21] if len(row) > 21 else "❓"  # Column V: Join Time
                        
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
                        
                        # Check if quest points have been recorded (any non-empty quest point field)
                        quest_points = row[14] if len(row) > 14 else "❓"  # Column O: Quest/Crusades
                        fps_kills = row[15] if len(row) > 15 else "❓"     # Column P: FPS Kills
                        ship_kills = row[16] if len(row) > 16 else "❓"    # Column Q: Ship Kills
                        griefer_kills = row[28] if len(row) > 28 else "❓" # Column AC: Griefer Kills
                        
                        has_recorded_points = any([quest_points, fps_kills, ship_kills, griefer_kills])
                        
                        quest_players.append({
                            'row': i,
                            'player_id': player_id,
                            'player_name': player_name,
                            'player_rank': player_rank,
                            'time_in_quest': time_in_quest,
                            'points_recorded': has_recorded_points,
                            'current_quest_points': quest_points,
                            'current_fps_kills': fps_kills,
                            'current_ship_kills': ship_kills,
                            'current_griefer_kills': griefer_kills
                        })
            
            # Update the stored quest players data
            self.quest_players = quest_players
            
            # Recreate the view with updated data
            self.clear_items()
            options = []
            
            # Add "Set Equal Points for All" option at the top if there are multiple players
            if len(quest_players) > 1:
                options.append(discord.SelectOption(
                    label="📜 Set Equal Points for All Players",
                    value="all_players",
                    description=f"Apply the same quest points to all {len(quest_players)} players",
                    emoji="📜"
                ))
            
            for i, player in enumerate(quest_players[:24]):  # Limit to 24 to account for the "all players" option
                options.append(discord.SelectOption(
                    label=player['player_name'],
                    value=str(i),
                    description=f"Record quest points for {player['player_name']}"
                ))
            
            if options:
                select = PlayerSelectionDropdown(options, self.quest_id, quest_players, self.quest_tracker, self)
                self.add_item(select)
            
            # Create updated embed
            embed = discord.Embed(
                title="🪶 Record Quest Points",
                description=f"Select a player to record their quest performance:\n\n**Quest ID:** `{self.quest_id}`",
                color=0x0099ff  # Imperial blue color
            )
            
            # Add detailed player information
            if quest_players:
                # Limit to 10 players for display to avoid embed limits
                display_players = quest_players[:10]
                
                for i, player in enumerate(display_players):
                    status_icon = "✅" if player['points_recorded'] else "⏳"
                    
                    # Player name field
                    embed.add_field(
                        name=f"{i+1}. {player['player_name']} {status_icon}",
                        value=f"**Rank:** {player['player_rank']}\n**Time in Quest:** {player['time_in_quest']}",
                        inline=True
                    )
                
                # Add spacing if odd number of players (for better layout)
                if len(display_players) % 2 == 1:
                    embed.add_field(name="\u200b", value="\u200b", inline=True)
                
                # Add footer with additional info if there are more players
                if len(quest_players) > 10:
                    embed.set_footer(text=f"Showing 10 of {len(quest_players)} players. Use dropdown to select any player.")
                else:
                    embed.set_footer(text="✅ = Points recorded | ⏳ = Awaiting recording")
            else:
                embed.add_field(
                    name="❌ No Players Found",
                    value="No players have joined this quest yet.",
                    inline=False
                )
            
            # Update the original message
            await interaction.edit_original_response(embed=embed, view=self)
            
        except Exception as e:
            print(f"Error refreshing quest points interface: {e}")

class PlayerSelectionDropdown(discord.ui.Select):
    def __init__(self, options, quest_id: str, quest_players: list, quest_tracker, parent_view):
        super().__init__(placeholder="Select a player to record points for...", options=options)
        self.quest_id = quest_id
        self.quest_players = quest_players
        self.quest_tracker = quest_tracker
        self.parent_view = parent_view
    
    async def callback(self, interaction: discord.Interaction):
        selected_value = self.values[0]
        
        # Check if "Set Equal Points for All" was selected
        if selected_value == "all_players":
            # Show bulk quest points recording modal
            modal = BulkQuestPointsModal(self.quest_id, self.quest_players, self.quest_tracker, self.parent_view)
            await interaction.response.send_modal(modal)
        else:
            # Handle individual player selection
            selected_index = int(selected_value)
            selected_player = self.quest_players[selected_index]
            
            # Show quest points recording modal
            modal = QuestPointsModal(self.quest_id, selected_player, self.quest_tracker, self.parent_view)
            await interaction.response.send_modal(modal)

class QuestPointsModal(discord.ui.Modal):
    def __init__(self, quest_id: str, player_data: dict, quest_tracker, parent_view):
        super().__init__(title=f"Record Points - {player_data['player_name']}")
        self.quest_id = quest_id
        self.player_data = player_data
        self.quest_tracker = quest_tracker
        self.parent_view = parent_view
        
        # Add input fields for each stat
        self.quest_points = discord.ui.TextInput(
            label="Quests",
            placeholder="Enter quest points earned (numbers only)",
            default=str(player_data['current_quest_points']) if player_data['current_quest_points'] else "0",
            required=False,
            max_length=10
        )
        
        self.crusade_points = discord.ui.TextInput(
            label="Crusades",
            placeholder="Enter crusade points earned (numbers only)",
            default=str(player_data.get('current_crusade_points', '0')) if player_data.get('current_crusade_points') else "0",
            required=False,
            max_length=10
        )
        
        self.ground_kills = discord.ui.TextInput(
            label="Ground Kills",
            placeholder="Enter ground kills (numbers only)",
            default=str(player_data['current_fps_kills']) if player_data['current_fps_kills'] else "0",
            required=False,
            max_length=10
        )
        
        self.pilot_kills = discord.ui.TextInput(
            label="Pilot Kills",
            placeholder="Enter pilot kills (numbers only)",
            default=str(player_data['current_ship_kills']) if player_data['current_ship_kills'] else "0",
            required=False,
            max_length=10
        )
        
        self.griefer_kills = discord.ui.TextInput(
            label="Griefer Kills",
            placeholder="Enter griefer kills (numbers only)",
            default=str(player_data['current_griefer_kills']) if player_data['current_griefer_kills'] else "0",
            required=False,
            max_length=10
        )
        
        # Add all inputs to the modal
        self.add_item(self.quest_points)
        self.add_item(self.crusade_points)
        self.add_item(self.ground_kills)
        self.add_item(self.pilot_kills)
        self.add_item(self.griefer_kills)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Validate and convert inputs
            try:
                quest_points_val = int(self.quest_points.value) if self.quest_points.value.strip() else 0
                crusade_points_val = int(self.crusade_points.value) if self.crusade_points.value.strip() else 0
                ground_kills_val = int(self.ground_kills.value) if self.ground_kills.value.strip() else 0
                pilot_kills_val = int(self.pilot_kills.value) if self.pilot_kills.value.strip() else 0
                griefer_kills_val = int(self.griefer_kills.value) if self.griefer_kills.value.strip() else 0
            except ValueError:
                await interaction.response.send_message("❌ All values must be numbers only!", ephemeral=True)
                return
            
            # Update Google Sheet
            worksheet = self.quest_tracker.get_worksheet(self.quest_tracker.SHEET_URL, self.quest_tracker.WORKSHEET_NAME)
            if not worksheet:
                await interaction.response.send_message("❌ Could not access the Google Sheet.", ephemeral=True)
                return
            
            row_num = self.player_data['row']
            
            # Update the player's stats in the sheet with rate limiting
            # Column O (15): Quests, Column P (16): Ground Kills, Column Q (17): Pilot Kills, Column AA (27): Crusades, Column AC (29): Griefer Kills
            await self.quest_tracker.rate_limited_update_cell(worksheet, row_num, 15, quest_points_val)    # Column O: Quests
            await self.quest_tracker.rate_limited_update_cell(worksheet, row_num, 16, ground_kills_val)    # Column P: Ground Kills  
            await self.quest_tracker.rate_limited_update_cell(worksheet, row_num, 17, pilot_kills_val)     # Column Q: Pilot Kills
            await self.quest_tracker.rate_limited_update_cell(worksheet, row_num, 27, crusade_points_val)  # Column AA: Crusades
            await self.quest_tracker.rate_limited_update_cell(worksheet, row_num, 29, griefer_kills_val)   # Column AC: Griefer Kills
            
            # Send simple success message
            await interaction.response.send_message(
                f"✅ **Quest points recorded for {self.player_data['player_name']}**\n"
                f"📜 Quests: {quest_points_val} | 🏛️ Crusades: {crusade_points_val} | 🔫 Ground: {ground_kills_val} | "
                f"🚀 Pilot: {pilot_kills_val} | 💀 Griefer: {griefer_kills_val}",
                ephemeral=True
            )
            
            # Refresh the parent quest points interface to show updated status
            try:
                if self.parent_view:
                    await self.parent_view.refresh_interface_silently()
                    print("Successfully refreshed quest points interface")
            except Exception as e:
                print(f"Error refreshing parent interface: {e}")
            
            # Update quest review embed if it exists
            try:
                await self.quest_tracker.update_quest_review_embed(self.quest_id)
            except Exception as e:
                print(f"Error updating quest review embed: {e}")
            
        except Exception as e:
            print(f"Error recording quest points: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Error saving quest points: {str(e)}", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Error saving quest points: {str(e)}", ephemeral=True)

class BulkQuestPointsModal(discord.ui.Modal):
    def __init__(self, quest_id: str, quest_players: list, quest_tracker, parent_view):
        super().__init__(title=f"Set Equal Points for All Players")
        self.quest_id = quest_id
        self.quest_players = quest_players
        self.quest_tracker = quest_tracker
        self.parent_view = parent_view
        
        # Create input fields
        self.quest_points = discord.ui.TextInput(
            label="Quest Points (for each player)",
            placeholder="Enter quest points to give each player...",
            default="0",
            max_length=10
        )
        
        self.crusade_points = discord.ui.TextInput(
            label="Crusade Points (for each player)",
            placeholder="Enter crusade points to give each player...",
            default="0",
            required=False,
            max_length=10
        )
        
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
            label="Griefer Kills (for each player)",
            placeholder="Enter griefer kills for each player...",
            default="0",
            required=False,
            max_length=10
        )
        
        # Add all inputs to the modal
        self.add_item(self.quest_points)
        self.add_item(self.crusade_points)
        self.add_item(self.ground_kills)
        self.add_item(self.pilot_kills)
        self.add_item(self.griefer_kills)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Validate and convert inputs
            try:
                quest_points_val = int(self.quest_points.value) if self.quest_points.value.strip() else 0
                crusade_points_val = int(self.crusade_points.value) if self.crusade_points.value.strip() else 0
                ground_kills_val = int(self.ground_kills.value) if self.ground_kills.value.strip() else 0
                pilot_kills_val = int(self.pilot_kills.value) if self.pilot_kills.value.strip() else 0
                griefer_kills_val = int(self.griefer_kills.value) if self.griefer_kills.value.strip() else 0
            except ValueError:
                await interaction.response.send_message("❌ All values must be numbers only!", ephemeral=True)
                return
            
            # Get the worksheet
            worksheet = self.quest_tracker.get_cached_worksheet(self.quest_tracker.SHEET_URL, self.quest_tracker.WORKSHEET_NAME)
            if not worksheet:
                await interaction.response.send_message("❌ Could not access the Google Sheet.", ephemeral=True)
                return
            
            # Apply points to all players
            success_count = 0
            total_players = len(self.quest_players)
            
            for player in self.quest_players:
                try:
                    row_num = player['row']
                    
                    # Update the player's stats in the sheet with rate limiting
                    # Column O (15): Quests, Column P (16): Ground Kills, Column Q (17): Pilot Kills, Column AA (27): Crusades, Column AC (29): Griefer Kills
                    await self.quest_tracker.rate_limited_update_cell(worksheet, row_num, 15, quest_points_val)    # Column O: Quests
                    await self.quest_tracker.rate_limited_update_cell(worksheet, row_num, 16, ground_kills_val)    # Column P: Ground Kills  
                    await self.quest_tracker.rate_limited_update_cell(worksheet, row_num, 17, pilot_kills_val)     # Column Q: Pilot Kills
                    await self.quest_tracker.rate_limited_update_cell(worksheet, row_num, 27, crusade_points_val)  # Column AA: Crusades
                    await self.quest_tracker.rate_limited_update_cell(worksheet, row_num, 29, griefer_kills_val)   # Column AC: Griefer Kills
                    
                    success_count += 1
                except Exception as e:
                    print(f"Error recording points for player {player['player_name']}: {e}")
            
            # Send confirmation message
            if success_count == total_players:
                await interaction.response.send_message(
                    f"✅ **Successfully recorded points for all {total_players} players!**\n\n"
                    f"**Points Applied:**\n"
                    f"📜 Quest Points: {quest_points_val}\n"
                    f"🏛️ Crusade Points: {crusade_points_val}\n"
                    f"🔫 Ground Kills: {ground_kills_val}\n"
                    f"🚀 Pilot Kills: {pilot_kills_val}\n"
                    f"💀 Griefer Kills: {griefer_kills_val}",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"⚠️ **Partial Success:** Applied points to {success_count} out of {total_players} players.\n\n"
                    f"**Points Applied:**\n"
                    f"📜 Quest Points: {quest_points_val}\n"
                    f"🏛️ Crusade Points: {crusade_points_val}\n"
                    f"🔫 Ground Kills: {ground_kills_val}\n"
                    f"🚀 Pilot Kills: {pilot_kills_val}\n"
                    f"💀 Griefer Kills: {griefer_kills_val}",
                    ephemeral=True
                )
            
            # Clear cache to ensure fresh data is loaded for individual edits
            try:
                self.quest_tracker.clear_cache(self.quest_tracker.WORKSHEET_NAME)
                print(f"Cleared cache after bulk update for quest {self.quest_id}")
            except Exception as e:
                print(f"Error clearing cache after bulk update: {e}")
            
            # Refresh the parent quest points interface to show updated status
            try:
                if self.parent_view:
                    await self.parent_view.refresh_interface_silently()
                    print("Successfully refreshed quest points interface")
            except Exception as e:
                print(f"Error refreshing parent interface: {e}")
            
            # Update quest review embed if it exists
            try:
                await self.quest_tracker.update_quest_review_embed(self.quest_id)
            except Exception as e:
                print(f"Error updating quest review embed: {e}")
            
        except Exception as e:
            print(f"Error recording bulk quest points: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Error saving quest points: {str(e)}", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Error saving quest points: {str(e)}", ephemeral=True)

class TemplateOverwriteView(discord.ui.View):
    def __init__(self, template_name: str, draft: PatrolDraft, guild_id: int):
        super().__init__(timeout=30)
        self.template_name = template_name
        self.draft = draft
        self.guild_id = guild_id
    
    @discord.ui.button(label="✅ Overwrite", style=discord.ButtonStyle.danger)
    async def confirm_overwrite(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            templates_file = "patrol_templates.json"
            with open(templates_file, 'r') as f:
                templates = json.load(f)
            
            guild_key = str(self.guild_id)
            templates[guild_key][self.template_name] = self.draft.to_dict()
            
            with open(templates_file, 'w') as f:
                json.dump(templates, f, indent=2)
            
            embed = discord.Embed(
                title="✅ Template Overwritten",
                description=f"Quest template **{self.template_name}** has been updated!",
                color=0x00ff00
            )
            await interaction.response.edit_message(embed=embed, view=None)
            
        except Exception as e:
            await interaction.response.send_message(f"❌ Error overwriting template: {str(e)}", ephemeral=True)
    
    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_overwrite(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="❌ Cancelled",
            description="Template was not saved.",
            color=0x999999
        )
        await interaction.response.edit_message(embed=embed, view=None)

class PatrolEditorView(discord.ui.View):
    def __init__(self, patrol_leader: discord.Member, leader_rank: str, quest_tracker, game: str = None):
        super().__init__(timeout=300)
        self.patrol_leader = patrol_leader
        self.leader_rank = leader_rank
        self.quest_tracker = quest_tracker
        self.draft = PatrolDraft()
        # Set the game if provided
        if game:
            self.draft.game = game
    
    def create_preview_embed(self):
        embed = discord.Embed(
            title="📜 Quest Editor - Live Preview",
            color=0x0099ff
        )
        
        # Add fields based on current draft
        if self.draft.patrol_name:
            embed.add_field(name="📜 Quest Name", value=self.draft.patrol_name, inline=False)
        else:
            embed.add_field(name="📜 Quest Name", value="*Not set*", inline=False)
        
        if self.draft.patrol_description:
            embed.add_field(name="📝 Description", value=self.draft.patrol_description, inline=False)
        else:
            embed.add_field(name="📝 Description", value="*Not set*", inline=False)
        
        if self.draft.patrol_image:
            embed.add_field(name="🖼️ Image URL", value=self.draft.patrol_image, inline=False)
            embed.set_image(url=self.draft.patrol_image)
        else:
            embed.add_field(name="🖼️ Image URL", value="*Not set*", inline=False)
        
        if self.draft.scheduled_duration:
            embed.add_field(name="⏲️ Duration", value=f"{self.draft.scheduled_duration} minutes", inline=True)
        else:
            embed.add_field(name="⏲️ Duration", value="*Not set*", inline=True)
        
        embed.add_field(name="👑 Leader", value=self.patrol_leader.mention, inline=True)
        embed.add_field(name="🏅 Rank", value=self.leader_rank, inline=True)
        
        if self.draft.loaded_template_name:
            embed.set_footer(text=f"Template: {self.draft.loaded_template_name}")
        else:
            embed.set_footer(text="Use the buttons below to edit quest details")
        
        return embed
    
    async def update_preview(self, interaction: discord.Interaction):
        embed = self.create_preview_embed()
        await interaction.response.edit_message(embed=embed, view=self)
    
    # Row 1: Main editing buttons
    @discord.ui.button(label="Edit Name", emoji="📜", style=discord.ButtonStyle.primary, row=0)
    async def edit_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = QuestNameModal(self.draft, self)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Edit Description", emoji="📝", style=discord.ButtonStyle.primary, row=0)
    async def edit_description(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = QuestDescriptionModal(self.draft, self)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Edit Image", emoji="🖼️", style=discord.ButtonStyle.primary, row=0)
    async def edit_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = QuestImageModal(self.draft, self)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Edit Duration", emoji="⏲️", style=discord.ButtonStyle.primary, row=0)
    async def edit_duration(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = QuestDurationModal(self.draft, self)
        await interaction.response.send_modal(modal)
    
    # Row 2: Template management
    @discord.ui.button(label="Save Template", emoji="💾", style=discord.ButtonStyle.success, row=1)
    async def save_template(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.draft.patrol_name:
            await interaction.response.send_message("❌ Please set a quest name before saving as template.", ephemeral=True)
            return
        modal = QuestTemplateNameModal(self.draft, self, interaction.guild.id)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Load Template", emoji="📂", style=discord.ButtonStyle.secondary, row=1)
    async def load_template(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_template_selector(interaction)
    
    @discord.ui.button(label="Manage Templates", emoji="🗑️", style=discord.ButtonStyle.secondary, row=1)
    async def manage_templates(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_template_manager(interaction)
    
    # Row 3: Actions
    @discord.ui.button(label="Create Quest", emoji="🚀", style=discord.ButtonStyle.success, row=2)
    async def create_quest(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.draft.patrol_name:
            await interaction.response.send_message("❌ Please set a quest name before creating.", ephemeral=True)
            return
        
        await self.finalize_quest(interaction)
    
    @discord.ui.button(label="Clear All", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
    async def clear_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.draft = PatrolDraft()
        await self.update_preview(interaction)
    
    async def show_template_selector(self, interaction: discord.Interaction):
        """Show available templates for loading"""
        templates_file = "patrol_templates.json"
        if not os.path.exists(templates_file):
            await interaction.response.send_message("❌ No quest templates found.", ephemeral=True)
            return
        
        with open(templates_file, 'r') as f:
            templates = json.load(f)
        
        guild_key = str(interaction.guild.id)
        if guild_key not in templates or not templates[guild_key]:
            await interaction.response.send_message("❌ No quest templates found for this server.", ephemeral=True)
            return
        
        # Create dropdown with templates
        options = []
        for template_name in templates[guild_key].keys():
            options.append(discord.SelectOption(
                label=template_name,
                description="Load this quest template"
            ))
        
        if len(options) > 25:
            options = options[:25]  # Discord limit
        
        select = TemplateSelect(options, self.draft, self, templates[guild_key], interaction)
        view = discord.ui.View()
        view.add_item(select)
        
        embed = discord.Embed(
            title="📂 Load Quest Template",
            description="Select a template to load:",
            color=0x0099ff
        )
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    async def show_template_manager(self, interaction: discord.Interaction):
        """Show template management interface"""
        templates_file = "patrol_templates.json"
        if not os.path.exists(templates_file):
            await interaction.response.send_message("❌ No quest templates found.", ephemeral=True)
            return
        
        with open(templates_file, 'r') as f:
            templates = json.load(f)
        
        guild_key = str(interaction.guild.id)
        if guild_key not in templates or not templates[guild_key]:
            await interaction.response.send_message("❌ No quest templates found for this server.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="🗑️ Manage Quest Templates",
            description="Select a template to delete:",
            color=0xff9900
        )
        
        # Create dropdown for deletion
        options = []
        for template_name in templates[guild_key].keys():
            options.append(discord.SelectOption(
                label=template_name,
                description="Delete this quest template",
                emoji="🗑️"
            ))
        
        if len(options) > 25:
            options = options[:25]
        
        select = TemplateDeleteSelect(options, interaction.guild.id)
        view = discord.ui.View()
        view.add_item(select)
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    async def finalize_quest(self, interaction: discord.Interaction):
        """Create the quest in Google Sheets"""
        try:
            await interaction.response.defer(ephemeral=True)
            
            # Generate unique patrol ID
            patrol_id = f"P{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            start_time = datetime.now()
            
            # Calculate scheduled end time if duration provided
            scheduled_end = "❓"
            scheduled_end_timestamp = None
            if self.draft.scheduled_duration:
                try:
                    duration_minutes = int(self.draft.scheduled_duration)
                    scheduled_end_time = start_time + timedelta(minutes=duration_minutes)
                    scheduled_end = scheduled_end_time.strftime('%Y-%m-%d %H:%M:%S')
                    scheduled_end_timestamp = scheduled_end_time.timestamp()
                except:
                    pass
            
            # Prepare row data for new column structure with extended member data columns
            row_data = [
                patrol_id,                                # A: Patrol ID
                self.patrol_leader.display_name,          # B: Patrol Leader  
                str(self.patrol_leader.id),               # C: Patrol Leader ID
                self.leader_rank,                         # D: Leader Rank
                self.draft.patrol_name,                   # E: Patrol Name
                self.draft.patrol_description or "❓",      # F: Patrol Description
                self.draft.patrol_image or "❓",            # G: Patrol Image
                start_time.strftime('%Y-%m-%d %H:%M:%S'), # H: Patrol Start Time
                scheduled_end,                            # I: Patrol Scheduled end time
                "❓",                                       # J: Patrol actual end time (to be filled later)
                "❓",                                       # K: Player ID (empty for leader row)
                "❓",                                       # L: Player Name (empty for leader row)
                "❓",                                       # M: Role (empty for leader row)
                "❓",                                       # N: Rank (empty for leader row)
                "❓",                                       # O: Quests (empty for leader row)
                "❓",                                       # P: Ground Kills (empty for leader row)
                "❓",                                       # Q: Pilot Kills (empty for leader row)
                "❓",                                       # R: Time in Service (empty for leader row)
                "❓",                                       # S: Message ID (to be filled later)
                "❓",                                       # T: Thread ID (to be filled later)
                "❓",                                       # U: Roster ID (to be filled later)
                "❓",                                       # V: Join Time (empty for leader row)
                "❓",                                       # W: Reserved
                "❓",                                       # X: Reserved
                "❓",                                       # Y: Reserved
                "❓",                                       # Z: Reserved
                "❓",                                       # AA: Crusades (empty for leader row)
                self.draft.game or "❓",                    # AB: Game
                "❓"                                        # AC: Griefer Kills (empty for leader row)
            ]
            
            # Add to Google Sheet
            worksheet = self.quest_tracker.get_worksheet(self.quest_tracker.SHEET_URL, self.quest_tracker.WORKSHEET_NAME)
            if not worksheet:
                await interaction.followup.send("❌ Could not access the OFS Google Sheet.", ephemeral=True)
                return
            
            # Find next empty row and append data
            all_values = await self.quest_tracker.rate_limited_get_all_values(worksheet)
            next_row = len(all_values) + 1
            
            # Convert row_data to the range needed (extended to include all columns A-AC)
            range_name = f"A{next_row}:AC{next_row}"
            await self.quest_tracker.rate_limited_update(worksheet, range_name, [row_data])
            
            print(f"📊 Created initial quest row with {len(row_data)} columns (A-AC)")
            
            # Create success embed
            embed = discord.Embed(
                title="📜 Quest Created Successfully!",
                description=f"**{self.draft.patrol_name}** is ready to begin",
                color=0x00ff00
            )
            embed.add_field(name="Quest ID", value=f"`{patrol_id}`", inline=True)
            embed.add_field(name="Leader", value=self.patrol_leader.mention, inline=True)
            embed.add_field(name="Rank", value=self.leader_rank, inline=True)
            
            if self.draft.patrol_description:
                embed.add_field(name="Description", value=self.draft.patrol_description, inline=False)
            
            if self.draft.patrol_image:
                embed.set_image(url=self.draft.patrol_image)
            
            embed.add_field(name="Started", value=f"<t:{int(start_time.timestamp())}:R>", inline=True)
            if scheduled_end and scheduled_end_timestamp:
                embed.add_field(name="Scheduled End", value=f"<t:{int(scheduled_end_timestamp)}:R>", inline=True)
            
            embed.set_footer(text=f"Use /update_quest with ID: {patrol_id} to add members and data")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
            # Get the quest forum channel
            forum_channel_id = self.quest_tracker.get_quest_forum_channel(interaction.guild.id)
            if not forum_channel_id:
                await interaction.followup.send("⚠️ No quest forum channel configured! Use `/set_quest_forum` to set one up.", ephemeral=True)
                return
            
            forum_channel = interaction.guild.get_channel(forum_channel_id)
            if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
                await interaction.followup.send("❌ Quest forum channel not found or is not a forum channel!", ephemeral=True)
                return
            
            # Create forum post for the quest
            quest_thread_name = f"📜 {self.draft.patrol_name}"
            if len(quest_thread_name) > 100:  # Discord forum post title limit
                quest_thread_name = quest_thread_name[:97] + "..."
            
            # Create quest announcement embed for the forum post (start as pending)
            announcement_embed = discord.Embed(
                title="🚀 New Quest Created!",
                description=f"**{self.draft.patrol_name}**\n{self.draft.patrol_description or 'A new quest has been created and is ready to start!'}",
                color=0xffa500  # Orange for pending
            )
            announcement_embed.add_field(name="Quest Leader", value=f"{self.patrol_leader.mention}\n**Rank:** {self.leader_rank}", inline=True)
            announcement_embed.add_field(name="Quest ID", value=f"`{patrol_id}`", inline=True)
            announcement_embed.add_field(name="Created", value=f"<t:{int(start_time.timestamp())}:R>", inline=True)
            
            if self.draft.patrol_image:
                announcement_embed.set_thumbnail(url=self.draft.patrol_image)
            
            announcement_embed.set_footer(text="⏳ Quest is pending - Use 'Start Quest' button to begin!")
            
            # Create join quest view (renamed from patrol to quest)
            join_view = JoinQuestView(patrol_id, self.draft.patrol_name, self.patrol_leader, self.leader_rank, self.quest_tracker)
            
            try:
                # Get the "Quest Started" tag (quests start as started now)
                started_tag = self.quest_tracker.get_quest_tag_by_name(forum_channel, "Quest Started")
                if not started_tag:
                    # Try to create tags automatically
                    await self.quest_tracker.setup_quest_forum_tags(forum_channel)
                    started_tag = self.quest_tracker.get_quest_tag_by_name(forum_channel, "Quest Started")
                
                applied_tags = [started_tag] if started_tag else []
                
                # Add game tag if specified (must already exist as a forum tag)
                if self.draft.game:
                    game_tag = self.quest_tracker.get_quest_tag_by_name(forum_channel, self.draft.game)
                    if game_tag:
                        applied_tags.append(game_tag)
                        print(f"✅ Applied game tag: {self.draft.game}")
                    else:
                        print(f"⚠️ Game tag '{self.draft.game}' not found in forum channel")
                
                # Create the forum thread/post with the started tag
                forum_thread, forum_message = await forum_channel.create_thread(
                    name=quest_thread_name,
                    content=None,  # We'll send the embed as a separate message
                    embed=announcement_embed,
                    view=join_view,
                    applied_tags=applied_tags
                )
                
                # Update the Google Sheet with all IDs for embed tracking
                try:
                    # Find the row we just created and update columns S, T, and W (Message ID, Thread ID, Roster Message ID)
                    all_values = await self.quest_tracker.rate_limited_get_all_values(worksheet)
                    for i, row in enumerate(all_values):
                        if len(row) > 0 and row[0] == patrol_id:  # Found our patrol by ID
                            row_num = i + 1
                            # Update Message ID (column S, position 19) and Thread ID (column T, position 20)
                            await self.quest_tracker.rate_limited_update_cell(worksheet, row_num, 19, str(forum_message.id))  # S: Message ID
                            await self.quest_tracker.rate_limited_update_cell(worksheet, row_num, 20, str(forum_thread.id))  # T: Thread ID
                            print(f"✅ Updated quest {patrol_id} with Message ID: {forum_message.id}, Thread ID: {forum_thread.id}")
                            break
                except Exception as e:
                    print(f"⚠️ Failed to update Message ID and Thread ID: {e}")
                
                # Create and post the initial roster embed
                try:
                    print(f"📝 Creating initial roster embed for quest {patrol_id}")
                    roster_embed = await self.quest_tracker.create_roster_embed(patrol_id)
                    if roster_embed:
                        roster_message = await forum_thread.send(embed=roster_embed)
                        print(f"🔗 Posted roster message with ID: {roster_message.id}")
                        
                        # Store roster message ID in column U (position 21) for future updates
                        try:
                            # Refresh data and find the quest row again
                            all_values = await self.quest_tracker.rate_limited_get_all_values(worksheet)
                            roster_updated = False
                            for i, row in enumerate(all_values):
                                if len(row) > 0 and row[0] == patrol_id:
                                    row_num = i + 1
                                    print(f"🔍 Found quest row {row_num} with {len(row)} columns for roster ID update")
                                    await self.quest_tracker.rate_limited_update_cell(worksheet, row_num, 21, str(roster_message.id))  # U: Roster ID
                                    print(f"✅ Stored Roster Message ID {roster_message.id} in row {row_num}, column U (21) for quest {patrol_id}")
                                    roster_updated = True
                                    break
                            
                            if not roster_updated:
                                print(f"❌ Failed to find quest {patrol_id} row for roster ID update")
                            else:
                                # Verify all IDs are properly stored
                                try:
                                    verification_values = await self.quest_tracker.rate_limited_get_all_values(worksheet)
                                    for row in verification_values[1:]:
                                        if len(row) > 0 and row[0] == patrol_id:
                                            message_id = row[18] if len(row) > 18 else "❓"  # S: Message ID
                                            thread_id = row[19] if len(row) > 19 else "❓"   # T: Thread ID  
                                            roster_id = row[20] if len(row) > 20 else "❓"   # U: Roster Message ID
                                            print(f"🔍 VERIFICATION - Quest {patrol_id}:")
                                            print(f"  🔗 Message ID (S): {message_id}")
                                            print(f"  🔗 Thread ID (T): {thread_id}")
                                            print(f"  🔗 Roster ID (U): {roster_id}")
                                            break
                                except Exception as e:
                                    print(f"⚠️ Verification failed: {e}")
                                
                        except Exception as e:
                            print(f"⚠️ Failed to update Roster Message ID: {e}")
                            import traceback
                            traceback.print_exc()
                    else:
                        print(f"❌ Failed to create roster embed for quest {patrol_id}")
                except Exception as e:
                    print(f"⚠️ Failed to create initial roster: {e}")
                    import traceback
                    traceback.print_exc()
                
                # Send a follow-up message in the creator's channel with the forum link
                success_followup = discord.Embed(
                    title="✅ Quest Forum Post Created!",
                    description=f"Your quest has been posted in {forum_channel.mention}",
                    color=0x00ff00
                )
                success_followup.add_field(name="Forum Post", value=f"[Click here to view]({forum_message.jump_url})", inline=True)
                success_followup.add_field(name="Quest ID", value=f"`{patrol_id}`", inline=True)
                success_followup.add_field(name="Status", value="▶️ Quest Started", inline=True)
                
                await interaction.followup.send(embed=success_followup, ephemeral=True)
                
            except Exception as e:
                print(f"Error creating forum post: {e}")
                await interaction.followup.send(f"❌ Failed to create forum post: {str(e)}", ephemeral=True)
                return
            
            # Disable the view
            for item in self.children:
                item.disabled = True
            
            try:
                await interaction.edit_original_response(view=self)
            except:
                pass
            
        except Exception as e:
            print(f"Error creating patrol: {e}")
            try:
                await interaction.followup.send(f"❌ An error occurred: {str(e)}", ephemeral=True)
            except:
                pass

class TemplateSelect(discord.ui.Select):
    def __init__(self, options, draft: PatrolDraft, view, templates_data, original_interaction):
        super().__init__(placeholder="Choose a quest template...", options=options)
        self.draft = draft
        self.editor_view = view
        self.templates_data = templates_data
        self.original_interaction = original_interaction
    
    async def callback(self, interaction: discord.Interaction):
        template_name = self.values[0]
        template_data = self.templates_data[template_name]
        
        # Load template data into draft
        self.draft.from_dict(template_data)
        self.draft.loaded_template_name = template_name
        
        embed = discord.Embed(
            title="✅ Template Loaded",
            description=f"Loaded quest template: **{template_name}**",
            color=0x00ff00
        )
        
        await interaction.response.edit_message(embed=embed, view=None)
        
        # Update the main editor view using the original interaction
        try:
            updated_embed = self.editor_view.create_preview_embed()
            await self.original_interaction.edit_original_response(embed=updated_embed, view=self.editor_view)
        except Exception as e:
            print(f"Error updating preview: {e}")
            pass

class TemplateDeleteSelect(discord.ui.Select):
    def __init__(self, options, guild_id: int):
        super().__init__(placeholder="Choose a template to delete...", options=options)
        self.guild_id = guild_id
    
    async def callback(self, interaction: discord.Interaction):
        template_name = self.values[0]
        
        # Create confirmation buttons
        view = TemplateDeleteConfirmView(template_name, self.guild_id)
        
        embed = discord.Embed(
            title="⚠️ Confirm Deletion",
            description=f"Are you sure you want to delete the template **{template_name}**?\n\nThis action cannot be undone.",
            color=0xff0000
        )
        
        await interaction.response.edit_message(embed=embed, view=view)

class TemplateDeleteConfirmView(discord.ui.View):
    def __init__(self, template_name: str, guild_id: int):
        super().__init__(timeout=30)
        self.template_name = template_name
        self.guild_id = guild_id
    
    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger)
    async def confirm_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            templates_file = "patrol_templates.json"
            with open(templates_file, 'r') as f:
                templates = json.load(f)
            
            guild_key = str(self.guild_id)
            if guild_key in templates and self.template_name in templates[guild_key]:
                del templates[guild_key][self.template_name]
                
                with open(templates_file, 'w') as f:
                    json.dump(templates, f, indent=2)
                
                embed = discord.Embed(
                    title="✅ Template Deleted",
                    description=f"Quest template **{self.template_name}** has been deleted.",
                    color=0x00ff00
                )
            else:
                embed = discord.Embed(
                    title="❌ Template Not Found",
                    description=f"Could not find template **{self.template_name}**.",
                    color=0xff0000
                )
            
            await interaction.response.edit_message(embed=embed, view=None)
            
        except Exception as e:
            await interaction.response.send_message(f"❌ Error deleting template: {str(e)}", ephemeral=True)
    
    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="❌ Cancelled",
            description="Template deletion cancelled.",
            color=0x999999
        )
        await interaction.response.edit_message(embed=embed, view=None)

class QuestTracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.gc = None
        self.sheet = None
        # Hardcoded sheet configuration
        self.SHEET_URL = "https://docs.google.com/spreadsheets/d/12OiRHpEALj1hzXRxaXgBOWjHtmUT5hg2ztxIgr4J4y8"
        self.WORKSHEET_NAME = "Patrols"  # Your tab name
        self.setup_google_sheets()
        self.load_guild_settings()
        # Track review messages for updating when quest points change
        self.quest_review_messages = {}  # {quest_id: {"message": message_obj, "channel_id": channel_id}}
        # Enhanced rate limiting for API calls
        self.last_api_call = 0
        self.api_call_count = 0
        self.api_call_reset_time = 0
        self.rate_limit_queue = asyncio.Queue()
        self.is_rate_limited = False
        
        # Comprehensive caching system to reduce API calls
        self.data_cache = {}
        self.cache_timestamps = {}
        self.worksheet_cache = {}  # Cache worksheet objects to reduce get_worksheet API calls
        self.worksheet_cache_timestamps = {}
        self.cache_duration = 300  # Cache data for 5 minutes (300 seconds) - increased for better rate limiting
        print("CACHE: Initialized data caching system with 5-minute cache duration")
    
    async def rate_limited_get_all_values(self, worksheet, max_retries=3):
        """Enhanced rate-limited wrapper for get_all_values with exponential backoff"""
        retry_count = 0
        base_delay = 0.5  # Start with 500ms delay
        
        while retry_count <= max_retries:
            try:
                # Check if we're in a rate-limited state
                if self.is_rate_limited:
                    wait_time = max(60, base_delay * (2 ** retry_count))  # Exponential backoff with 60s minimum
                    print(f"⚠️ Rate limited state active, waiting {wait_time}s before API call...")
                    await asyncio.sleep(wait_time)
                    self.is_rate_limited = False
                
                # Add progressive delay between API calls
                current_time = asyncio.get_event_loop().time()
                time_since_last_call = current_time - self.last_api_call
                min_delay = min(0.5, base_delay * (2 ** retry_count))  # Progressive delay
                
                if time_since_last_call < min_delay:
                    await asyncio.sleep(min_delay - time_since_last_call)
                
                # Track API calls per minute
                current_time = asyncio.get_event_loop().time()
                if current_time - self.api_call_reset_time > 60:
                    self.api_call_count = 0
                    self.api_call_reset_time = current_time
                
                self.api_call_count += 1
                self.last_api_call = current_time
                
                # If we're approaching the limit, add extra delay
                if self.api_call_count > 80:  # Conservative limit (Google allows 100)
                    print(f"⚠️ Approaching API limit ({self.api_call_count}/100), adding extra delay...")
                    await asyncio.sleep(2)
                elif self.api_call_count % 20 == 0:  # Log every 20 calls
                    print(f"📊 API Usage: {self.api_call_count}/100 calls this minute")
                
                return worksheet.get_all_values()
                
            except Exception as e:
                error_msg = str(e)
                retry_count += 1
                
                if "429" in error_msg or "Quota exceeded" in error_msg or "Read requests per minute" in error_msg:
                    self.is_rate_limited = True
                    wait_time = 60 + (base_delay * (2 ** retry_count))  # 60s + exponential backoff
                    
                    if retry_count <= max_retries:
                        print(f"⚠️ Rate limit hit (attempt {retry_count}/{max_retries + 1}), waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        print(f"❌ Max retries exceeded for rate limit error")
                        raise Exception("Google Sheets API rate limit exceeded. Please try again in a few minutes.")
                else:
                    # Non-rate-limit error, re-raise immediately
                    raise e
        
        # If we get here, all retries failed
        raise Exception("Failed to access Google Sheets after maximum retries")
    
    async def rate_limited_update(self, worksheet, range_name, values, max_retries=3):
        """Rate-limited wrapper for worksheet updates"""
        retry_count = 0
        base_delay = 0.5
        
        while retry_count <= max_retries:
            try:
                # Same rate limiting logic as get_all_values
                if self.is_rate_limited:
                    wait_time = max(60, base_delay * (2 ** retry_count))
                    print(f"⚠️ Rate limited state active, waiting {wait_time}s before update...")
                    await asyncio.sleep(wait_time)
                    self.is_rate_limited = False
                
                current_time = asyncio.get_event_loop().time()
                time_since_last_call = current_time - self.last_api_call
                min_delay = min(0.5, base_delay * (2 ** retry_count))
                
                if time_since_last_call < min_delay:
                    await asyncio.sleep(min_delay - time_since_last_call)
                
                # Track API calls
                if current_time - self.api_call_reset_time > 60:
                    self.api_call_count = 0
                    self.api_call_reset_time = current_time
                
                self.api_call_count += 1
                self.last_api_call = current_time
                
                if self.api_call_count > 80:
                    await asyncio.sleep(2)
                
                # Clear cache after successful update to maintain data consistency
                self.clear_cache(self.WORKSHEET_NAME)
                
                return worksheet.update(range_name, values)
                
            except Exception as e:
                error_msg = str(e)
                retry_count += 1
                
                if "429" in error_msg or "Quota exceeded" in error_msg:
                    self.is_rate_limited = True
                    wait_time = 60 + (base_delay * (2 ** retry_count))
                    
                    if retry_count <= max_retries:
                        print(f"⚠️ Update rate limit hit (attempt {retry_count}/{max_retries + 1}), waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        raise Exception("Google Sheets API rate limit exceeded during update. Please try again in a few minutes.")
                else:
                    raise e
        
        raise Exception("Failed to update Google Sheets after maximum retries")
    
    async def rate_limited_update_cell(self, worksheet, row, col, value, max_retries=3):
        """Rate-limited wrapper for individual cell updates"""
        retry_count = 0
        base_delay = 0.3  # Shorter delay for single cell updates
        
        while retry_count <= max_retries:
            try:
                if self.is_rate_limited:
                    wait_time = max(30, base_delay * (2 ** retry_count))
                    await asyncio.sleep(wait_time)
                    self.is_rate_limited = False
                
                current_time = asyncio.get_event_loop().time()
                time_since_last_call = current_time - self.last_api_call
                min_delay = base_delay * (1 + retry_count * 0.5)  # Progressive delay
                
                if time_since_last_call < min_delay:
                    await asyncio.sleep(min_delay - time_since_last_call)
                
                self.last_api_call = current_time
                self.api_call_count += 1
                
                result = worksheet.update_cell(row, col, value)
                # Invalidate cache since data was modified
                self.clear_cache(self.WORKSHEET_NAME)
                return result
                
            except Exception as e:
                error_msg = str(e)
                retry_count += 1
                
                if "429" in error_msg or "Quota exceeded" in error_msg:
                    self.is_rate_limited = True
                    wait_time = 30 + (base_delay * (2 ** retry_count))
                    
                    if retry_count <= max_retries:
                        print(f"⚠️ Cell update rate limit hit, waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        raise Exception("Google Sheets API rate limit exceeded during cell update.")
                else:
                    raise e
        
        raise Exception("Failed to update cell after maximum retries")
    
    def get_rate_limit_status(self):
        """Get current rate limiting status for debugging"""
        current_time = asyncio.get_event_loop().time()
        minutes_since_reset = (current_time - self.api_call_reset_time) / 60
        return {
            "api_calls_this_minute": self.api_call_count,
            "minutes_since_reset": round(minutes_since_reset, 2),
            "is_rate_limited": self.is_rate_limited,
            "seconds_since_last_call": round(current_time - self.last_api_call, 2)
        }
    
    def is_cache_valid(self, cache_key: str) -> bool:
        """Check if cached data is still valid based on cache duration"""
        if cache_key not in self.cache_timestamps:
            return False
        
        import time
        current_time = time.time()
        cache_time = self.cache_timestamps[cache_key]
        
        is_valid = (current_time - cache_time) < self.cache_duration
        if not is_valid:
            print(f"CACHE: Cache expired for key: {cache_key}")
        return is_valid
    
    def get_cached_data(self, cache_key: str):
        """Get cached data if valid, otherwise return None"""
        if self.is_cache_valid(cache_key):
            print(f"CACHE: Using cached data for: {cache_key}")
            return self.data_cache[cache_key]
        
        # Clean up expired cache entry
        if cache_key in self.data_cache:
            del self.data_cache[cache_key]
        if cache_key in self.cache_timestamps:
            del self.cache_timestamps[cache_key]
        
        return None
    
    def set_cached_data(self, cache_key: str, data):
        """Store data in cache with current timestamp"""
        import time
        self.data_cache[cache_key] = data
        self.cache_timestamps[cache_key] = time.time()
        print(f"CACHE: Stored data for: {cache_key}")
    
    def clear_cache(self, pattern: str = None):
        """Clear cache entries. If pattern provided, only clear matching keys"""
        if pattern:
            keys_to_remove = [key for key in self.data_cache.keys() if pattern in key]
            for key in keys_to_remove:
                if key in self.data_cache:
                    del self.data_cache[key]
                if key in self.cache_timestamps:
                    del self.cache_timestamps[key]
            print(f"CACHE: Cleared {len(keys_to_remove)} cache entries matching pattern: {pattern}")
        else:
            self.data_cache.clear()
            self.cache_timestamps.clear()
            print("CACHE: Cleared all cache entries")

    def get_cached_worksheet(self, sheet_url: str, worksheet_name: str):
        """Get worksheet object with caching to reduce API calls"""
        cache_key = f"ws_{worksheet_name}"
        current_time = time.time()
        
        # Check if we have cached worksheet and it's still valid
        if cache_key in self.worksheet_cache and cache_key in self.worksheet_cache_timestamps:
            cache_time = self.worksheet_cache_timestamps[cache_key]
            if (current_time - cache_time) < self.cache_duration:
                return self.worksheet_cache[cache_key]
        
        # If not in cache or expired, fetch fresh worksheet and cache it
        try:
            worksheet = self.get_worksheet(sheet_url, worksheet_name)
            if worksheet:
                self.worksheet_cache[cache_key] = worksheet
                self.worksheet_cache_timestamps[cache_key] = current_time
            return worksheet
        except Exception as e:
            # If we have expired cached worksheet, use it as fallback
            if cache_key in self.worksheet_cache:
                print(f"CACHE: Using expired worksheet cache as fallback for: {worksheet_name}")
                return self.worksheet_cache[cache_key]
            raise e

    async def cached_get_all_values(self, worksheet, worksheet_name: str):
        """Get worksheet data with caching to dramatically reduce API calls"""
        cache_key = f"worksheet_{worksheet_name}"
        
        # Try to get from cache first
        cached_data = self.get_cached_data(cache_key)
        if cached_data is not None:
            return cached_data
        
        # If not in cache, fetch from API and cache it
        print(f"CACHE: Fetching fresh data for worksheet: {worksheet_name}")
        try:
            fresh_data = await self.rate_limited_get_all_values(worksheet)
            self.set_cached_data(cache_key, fresh_data)
            return fresh_data
        except Exception as e:
            print(f"❌ Failed to fetch worksheet data: {e}")
            # If we have expired cached data, use it as fallback
            if cache_key in self.data_cache:
                print(f"CACHE: Using expired cache as fallback for: {worksheet_name}")
                return self.data_cache[cache_key]
            raise e
    
    async def pre_cache_common_worksheets(self):
        """Pre-load commonly used worksheets into cache to reduce API calls"""
        try:
            print("CACHE: Pre-caching commonly used worksheets...")
            worksheets_to_cache = [
                (self.WORKSHEET_NAME, "Patrols"),
                ("Member Log", "Member Log"),
                ("Ranks", "Ranks")
            ]
            
            for worksheet_name, cache_name in worksheets_to_cache:
                try:
                    worksheet = self.get_cached_worksheet(self.SHEET_URL, worksheet_name)
                    if worksheet:
                        await self.cached_get_all_values(worksheet, cache_name)
                        print(f"✅ Pre-cached worksheet and data: {cache_name}")
                except Exception as e:
                    print(f"⚠️ Failed to pre-cache {cache_name}: {e}")
                    
            print("CACHE: Pre-caching complete")
        except Exception as e:
            print(f"❌ Error during pre-caching: {e}")
    
    async def register_persistent_views(self):
        """Re-register persistent views for active quests after bot restart"""
        try:
            print("VIEWS: Re-registering persistent views for active quests...")
            
            # Pre-cache commonly used worksheets to reduce API calls
            await self.pre_cache_common_worksheets()
            
            # Get all active quests from the sheet using cached data
            worksheet = self.get_cached_worksheet(self.SHEET_URL, self.WORKSHEET_NAME)
            if not worksheet:
                print("❌ Could not access worksheet for persistent view registration")
                return
            
            all_values = await self.cached_get_all_values(worksheet, self.WORKSHEET_NAME)
            registered_count = 0
            
            # Find all unique quests and their statuses
            active_quests = {}
            review_quests = set()
            
            for row in all_values[1:]:  # Skip header
                if len(row) > 4:  # Make sure we have enough columns
                    quest_id = row[0]  # A: Quest ID
                    quest_name = row[4] if len(row) > 4 else "Unknown Quest"  # E: Quest Name
                    leader_id = row[2] if len(row) > 2 else "❓"  # C: Leader ID
                    leader_rank = row[3] if len(row) > 3 else "Member"  # D: Leader Rank
                    status = row[9] if len(row) > 9 else "❓"  # J: Status
                    
                    if quest_id:
                        # Track quests in review status
                        if status.lower() == 'review':
                            review_quests.add(quest_id)
                        
                        # Register join views for active quests (not completed or cancelled)
                        if not status.lower() in ['completed', 'cancelled']:
                            if quest_id not in active_quests:
                                # Get leader member object
                                leader_member = None
                                if leader_id:
                                    for guild in self.bot.guilds:
                                        try:
                                            leader_member = guild.get_member(int(leader_id))
                                            if leader_member:
                                                break
                                        except (ValueError, AttributeError):
                                            continue
                                
                                if leader_member:
                                    active_quests[quest_id] = {
                                        'name': quest_name,
                                        'leader': leader_member,
                                        'leader_rank': leader_rank
                                    }
            
            # Register views for each active quest
            for quest_id, quest_data in active_quests.items():
                try:
                    # Register JoinQuestView (includes both Join Quest and Manage Quest buttons)
                    join_view = JoinQuestView(
                        quest_id, 
                        quest_data['name'], 
                        quest_data['leader'], 
                        quest_data['leader_rank'], 
                        self
                    )
                    self.bot.add_view(join_view)
                    registered_count += 1
                    print(f"✅ Registered join view for quest: {quest_id}")
                    
                except Exception as e:
                    print(f"❌ Error registering join view for quest {quest_id}: {e}")
            
            # Register review views for quests in review status
            for quest_id in review_quests:
                try:
                    review_view = QuestReviewView(quest_id, self)
                    self.bot.add_view(review_view)
                    registered_count += 1
                    print(f"✅ Registered review view for quest: {quest_id}")
                    
                except Exception as e:
                    print(f"❌ Error registering review view for quest {quest_id}: {e}")
            
            print(f"VIEWS: Successfully re-registered persistent views for {registered_count} active quests")
            
        except Exception as e:
            print(f"❌ Error in register_persistent_views: {e}")
    
    async def send_auto_close_ephemeral(self, interaction, message: str, delay: int = 3):
        """Send an ephemeral message (simplified - Discord doesn't support auto-close)"""
        try:
            await interaction.followup.send(message, ephemeral=True)
        except Exception as e:
            print(f"Error in ephemeral message: {e}")
            # Fallback to regular message
            try:
                await interaction.followup.send(message, ephemeral=True)
            except:
                pass
    
    async def create_roster_embed(self, quest_id: str):
        """Create a roster embed showing all quest members"""
        try:
            print(f"🔍 Creating roster embed for quest: {quest_id}")
            worksheet = self.get_cached_worksheet(self.SHEET_URL, self.WORKSHEET_NAME)
            if not worksheet:
                print(f"❌ Failed to get worksheet")
                return None
            
            # Load Member Log data once for all lookups using cached data
            member_log_worksheet = self.get_cached_worksheet(self.SHEET_URL, "Member Log")
            member_log_values = []
            if member_log_worksheet:
                try:
                    member_log_values = await self.cached_get_all_values(member_log_worksheet, "Member Log")
                    print(f"📊 Loaded Member Log with {len(member_log_values)} rows for batch lookups")
                except Exception as e:
                    print(f"⚠️ Could not load Member Log for batch optimization: {e}")
                
            all_values = await self.cached_get_all_values(worksheet, self.WORKSHEET_NAME)
            quest_members = []
            quest_name = "Unknown Quest"
            
            print(f"📊 Total rows in sheet: {len(all_values)}")
            
            # Get all members for this quest (exclude leader row - only count rows with player IDs)
            member_count = 0
            for row in all_values[1:]:  # Skip header
                if len(row) > 0 and row[0] == quest_id:
                    if len(row) > 4:
                        quest_name = row[4]  # E: Quest Name
                    
                    # Get member info: [K:Player ID, L:Player Name, M:Role, N:Rank, V:Join Time]
                    player_id = row[10] if len(row) > 10 else "❓"  # K: Player ID  
                    player_name = row[11] if len(row) > 11 else "❓"  # L: Player Name
                    
                    # Use cached Member Log data to avoid individual API calls
                    if member_log_values:
                        role = self.get_member_role_from_cached_data(player_id, member_log_values)
                        rank = self.get_member_rank_from_cached_data(player_id, member_log_values)
                    else:
                        role = "Member"  # Fallback if Member Log couldn't be loaded
                        rank = "Member"  # Fallback if Member Log couldn't be loaded
                    join_time_str = row[21] if len(row) > 21 else "❓"  # V: Join Time (column 22, index 21)
                    
                    # Only include rows that have a player ID (actual members, not the leader row)
                    if player_id and player_name:
                        member_count += 1
                        print(f"🔍 Found member row {member_count} for quest {quest_id}")
                        print(f"👤 Member: {player_name} (ID: {player_id}, Role: {role}, Rank: {rank}, Join Time: {join_time_str})")
                    
                        # Parse join time or use current time as fallback
                        try:
                            if join_time_str:
                                join_time_dt = datetime.strptime(join_time_str, '%Y-%m-%d %H:%M:%S')
                                join_time = int(join_time_dt.timestamp())
                            else:
                                join_time = int(datetime.now().timestamp())
                        except:
                            join_time = int(datetime.now().timestamp())
                        
                        # Rank is now stored directly in the Patrols sheet, no need to look it up!
                        print(f"✅ Using rank from Patrols sheet: {rank}")
                        
                        quest_members.append({
                            'name': player_name,
                            'rank': rank,
                            'role': role,
                            'join_time': join_time,
                            'player_id': player_id
                        })
            
            print(f"📋 Found {len(quest_members)} members for quest {quest_id}")
            
            # Create roster embed
            roster_embed = discord.Embed(
                title="📋 Quest Roster",
                description=f"**{quest_name}**",
                color=0x3498db
            )
            
            if quest_members:
                # Sort members by join time (earliest first)
                quest_members.sort(key=lambda x: x['join_time'])
                
                # Create roster fields
                member_list = ""
                for i, member in enumerate(quest_members, 1):
                    member_list += f"**{i}.** {member['name']}\n"
                    member_list += f"🏅 **Rank:** {member['rank']} | **Role:** {member['role']}\n"
                    member_list += f"⏰ **Joined:** <t:{member['join_time']}:R>\n\n"
                
                # Split into multiple fields if too long
                if len(member_list) > 1024:
                    # Split members into chunks
                    chunks = []
                    current_chunk = ""
                    for i, member in enumerate(quest_members, 1):
                        member_entry = f"**{i}.** {member['name']}\n🏅 **Rank:** {member['rank']} | **Role:** {member['role']}\n⏰ **Joined:** <t:{member['join_time']}:R>\n\n"
                        
                        if len(current_chunk + member_entry) > 1024:
                            chunks.append(current_chunk)
                            current_chunk = member_entry
                        else:
                            current_chunk += member_entry
                    
                    if current_chunk:
                        chunks.append(current_chunk)
                    
                    # Add chunks as fields
                    for i, chunk in enumerate(chunks):
                        field_name = "Members" if i == 0 else f"Members (cont. {i+1})"
                        roster_embed.add_field(name=field_name, value=chunk, inline=False)
                else:
                    roster_embed.add_field(name="Members", value=member_list, inline=False)
                
                roster_embed.set_footer(text=f"Total Members: {len(quest_members)} | Last Updated")
                roster_embed.timestamp = datetime.now()
            else:
                roster_embed.add_field(name="Members", value="*No members have joined yet*", inline=False)
                roster_embed.set_footer(text="Waiting for members to join...")
            
            return roster_embed
            
        except Exception as e:
            error_msg = str(e)
            print(f"Error creating roster embed: {e}")
            
            # Handle rate limit errors specifically
            if "429" in error_msg or "Quota exceeded" in error_msg or "Read requests per minute" in error_msg:
                print(f"⚠️ Rate limit hit while creating roster embed for quest {quest_id}")
                # Return a simple placeholder embed instead of None
                placeholder_embed = discord.Embed(
                    title="📜 Quest Roster",
                    description="*Roster temporarily unavailable due to high server activity. Will update shortly.*",
                    color=0xffaa00
                )
                placeholder_embed.set_footer(text="Refreshing roster data...")
                return placeholder_embed
            
            return None
    
    def load_guild_settings(self):
        """Load guild-specific settings like forum channel IDs"""
        self.guild_settings_file = "guild_settings.json"
        self.guild_settings = {}
        if os.path.exists(self.guild_settings_file):
            try:
                with open(self.guild_settings_file, 'r') as f:
                    self.guild_settings = json.load(f)
            except Exception as e:
                print(f"Error loading guild settings: {e}")
                self.guild_settings = {}
    
    def save_guild_settings(self):
        """Save guild settings to file"""
        try:
            with open(self.guild_settings_file, 'w') as f:
                json.dump(self.guild_settings, f, indent=2)
        except Exception as e:
            print(f"Error saving guild settings: {e}")
    
    def get_quest_forum_channel(self, guild_id: int):
        """Get the configured quest forum channel for a guild"""
        guild_key = str(guild_id)
        return self.guild_settings.get(guild_key, {}).get('quest_forum_channel_id')
    
    def set_quest_forum_channel(self, guild_id: int, channel_id: int):
        """Set the quest forum channel for a guild"""
        guild_key = str(guild_id)
        if guild_key not in self.guild_settings:
            self.guild_settings[guild_key] = {}
        self.guild_settings[guild_key]['quest_forum_channel_id'] = channel_id
        self.save_guild_settings()
    
    def get_quest_review_channel(self, guild_id: int):
        """Get the configured quest review channel for a guild"""
        guild_key = str(guild_id)
        return self.guild_settings.get(guild_key, {}).get('quest_review_channel_id')
    
    def set_quest_review_channel(self, guild_id: int, channel_id: int):
        """Set the quest review channel for a guild"""
        guild_key = str(guild_id)
        if guild_key not in self.guild_settings:
            self.guild_settings[guild_key] = {}
        self.guild_settings[guild_key]['quest_review_channel_id'] = channel_id
        self.save_guild_settings()
    
    def store_quest_review_message(self, quest_id: str, message: discord.Message):
        """Store a quest review message for later updates"""
        self.quest_review_messages[quest_id] = {
            "message": message,
            "channel_id": message.channel.id
        }
        print(f"Stored review message for quest {quest_id}")
    
    async def update_quest_review_embed(self, quest_id: str):
        """Update the quest review embed when quest points change"""
        try:
            if quest_id not in self.quest_review_messages:
                print(f"No review message found for quest {quest_id}")
                return
            
            review_data = self.quest_review_messages[quest_id]
            message = review_data["message"]
            
            # Get updated roster information using the same logic as submit_for_review
            roster_info = await self.get_quest_roster_for_review(quest_id)
            
            # Recreate the embed with updated data
            original_embed = message.embeds[0]
            updated_embed = discord.Embed(
                title="📋 Quest Review Required",
                description=original_embed.description,
                color=0xffaa00  # Orange color for review
            )
            
            # Copy status and submitted fields from original
            for field in original_embed.fields:
                if field.name in ["📊 Status", "⏲️ Submitted"]:
                    updated_embed.add_field(name=field.name, value=field.value, inline=field.inline)
            
            # Add updated roster information
            if roster_info['players']:
                # Split roster into chunks if too long
                roster_chunks = []
                current_chunk = ""
                
                for i, player in enumerate(roster_info['players'], 1):
                    player_line = f"**{i}. {player['name']}** - Rank: {player['rank']} **|** Role: {player['role']}\n"
                    player_line += f"📜 {player['quest_points']} | 🏛️ {player['crusade_points']} | 🔫 {player['fps_kills']} | 🚀 {player['ship_kills']} | 💀 {player['griefer_kills']}\n\n"
                    
                    if len(current_chunk + player_line) > 1000:  # Discord field limit
                        roster_chunks.append(current_chunk)
                        current_chunk = player_line
                    else:
                        current_chunk += player_line
                
                if current_chunk:
                    roster_chunks.append(current_chunk)
                
                # Add roster fields
                for i, chunk in enumerate(roster_chunks):
                    field_name = "👥 Quest Roster & Points" if i == 0 else f"👥 Roster (continued {i+1})"
                    updated_embed.add_field(name=field_name, value=chunk.strip(), inline=False)
                
                # Add updated summary
                updated_embed.add_field(
                    name="📊 Summary",
                    value=f"**Total Participants:** {len(roster_info['players'])}\n"
                          f"**Total Quest Points:** {roster_info['total_quest_points']}\n"
                          f"**Total FPS Kills:** {roster_info['total_fps_kills']}\n"
                          f"**Total Ship Kills:** {roster_info['total_ship_kills']}\n"
                          f"**Total Griefer Kills:** {roster_info['total_griefer_kills']}",
                    inline=True
                )
            else:
                updated_embed.add_field(name="👥 Quest Roster", value="No participants found", inline=False)
            
            updated_embed.set_footer(text="Admins: Click a button below to review this quest | 📝 Updated with latest points")
            
            # Recreate the quest review view
            review_view = QuestReviewView(quest_id, self)
            
            # Update the message with new embed and recreated view
            await message.edit(embed=updated_embed, view=review_view)
            print(f"Successfully updated review embed for quest {quest_id}")
            
        except Exception as e:
            print(f"Error updating quest review embed: {e}")
    
    async def get_quest_roster_for_review(self, quest_id: str):
        """Get quest roster and points data for review embed"""
        try:
            worksheet = self.get_worksheet(self.SHEET_URL, self.WORKSHEET_NAME)
            if not worksheet:
                return {'players': [], 'total_quest_points': 0, 'total_fps_kills': 0, 'total_ship_kills': 0, 'total_griefer_kills': 0}
            
            all_values = await self.cached_get_all_values(worksheet, self.WORKSHEET_NAME)
            players = []
            total_quest_points = 0
            total_fps_kills = 0
            total_ship_kills = 0
            total_griefer_kills = 0
            
            # Find all players in this quest
            for i, row in enumerate(all_values[1:], start=2):  # Skip header
                if len(row) > 0 and row[0] == quest_id:  # Column A: Quest ID
                    player_id = row[10] if len(row) > 10 else "❓"  # Column K: Player ID
                    player_name = row[11] if len(row) > 11 else "❓"  # Column L: Player Name
                    
                    if player_id and player_name:  # Skip leader row (empty player data)
                        player_rank = await self.get_member_rank_from_log(player_id)  # Get rank from Member Log
                        player_role = await self.get_member_role_from_log(player_id)  # Get role from Member Log
                        
                        # Get quest points data
                        quest_points = row[14] if len(row) > 14 else "0"  # Column O: Quest/Crusades
                        fps_kills = row[15] if len(row) > 15 else "0"     # Column P: FPS Kills
                        ship_kills = row[16] if len(row) > 16 else "0"    # Column Q: Ship Kills
                        griefer_kills = row[28] if len(row) > 28 else "0" # Column AC: Griefer Kills
                        
                        # Convert to integers for totals
                        try:
                            quest_points_int = int(quest_points) if quest_points else 0
                            fps_kills_int = int(fps_kills) if fps_kills else 0
                            ship_kills_int = int(ship_kills) if ship_kills else 0
                            griefer_kills_int = int(griefer_kills) if griefer_kills else 0
                        except ValueError:
                            quest_points_int = fps_kills_int = ship_kills_int = griefer_kills_int = 0
                        
                        players.append({
                            'name': player_name,
                            'rank': player_rank,
                            'role': player_role,
                            'quest_points': quest_points or "0",
                            'fps_kills': fps_kills or "0",
                            'ship_kills': ship_kills or "0",
                            'griefer_kills': griefer_kills or "0"
                        })
                        
                        # Add to totals
                        total_quest_points += quest_points_int
                        total_fps_kills += fps_kills_int
                        total_ship_kills += ship_kills_int
                        total_griefer_kills += griefer_kills_int
            
            return {
                'players': players,
                'total_quest_points': total_quest_points,
                'total_fps_kills': total_fps_kills,
                'total_ship_kills': total_ship_kills,
                'total_griefer_kills': total_griefer_kills
            }
            
        except Exception as e:
            print(f"Error getting quest roster for review: {e}")
            return {'players': [], 'total_quest_points': 0, 'total_fps_kills': 0, 'total_ship_kills': 0, 'total_griefer_kills': 0}
    
    async def setup_quest_forum_tags(self, forum_channel: discord.ForumChannel):
        """Setup quest status tags in the forum channel"""
        try:
            # Define the quest status tags we want (no emoji in name since we set emoji separately)
            desired_tags = [
                {"name": "Quest Completed", "emoji": "✅", "moderated": False},
                {"name": "Quest Started", "emoji": "▶️", "moderated": False},
                {"name": "Quest Cancelled", "emoji": "❌", "moderated": False},
                {"name": "Recorded", "emoji": "🪶", "moderated": False}
            ]
            
            # Get existing tags
            existing_tags = {tag.name: tag for tag in forum_channel.available_tags}
            
            # Create missing tags
            for tag_info in desired_tags:
                if tag_info["name"] not in existing_tags:
                    try:
                        await forum_channel.create_tag(
                            name=tag_info["name"],
                            emoji=tag_info["emoji"],
                            moderated=tag_info["moderated"]
                        )
                        print(f"✅ Created forum tag: {tag_info['emoji']} {tag_info['name']}")
                    except Exception as e:
                        print(f"❌ Failed to create tag {tag_info['name']}: {e}")
            
            return True
        except Exception as e:
            print(f"❌ Error setting up forum tags: {e}")
            return False
    
    def get_quest_tag_by_name(self, forum_channel: discord.ForumChannel, tag_name: str):
        """Get a forum tag by name (without emoji prefix)"""
        for tag in forum_channel.available_tags:
            if tag.name == tag_name:
                return tag
        return None
    
    async def get_member_rank_from_log(self, user_id: str):
        """Get member rank from Member Log sheet by Discord ID"""
        try:
            print(f"🔍 Looking up rank for user ID: {user_id}")
            member_log_worksheet = self.get_cached_worksheet(self.SHEET_URL, "Member Log")
            if not member_log_worksheet:
                print(f"❌ Could not access Member Log worksheet")
                return "Member"  # Default rank
            
            member_log_values = await self.cached_get_all_values(member_log_worksheet, "Member Log")
            print(f"📊 Member Log has {len(member_log_values)} rows")
            # Look for the user's Discord ID in the Member Log - try both column A and B
            for row in member_log_values[1:]:  # Skip header row
                # Check both column A (User ID) and column B (Discord ID) for the user ID
                if (len(row) > 0 and str(user_id) == str(row[0])) or (len(row) > 1 and str(user_id) == str(row[1])):
                    # Member Log columns: [A:Name, B:Discord ID, C:Rank, ...]
                    if len(row) > 2:
                        rank = row[2] if row[2] else "Member"
                        print(f"✅ Found user {user_id} with rank: {rank}")
                        return rank
            
            print(f"❌ User {user_id} not found in Member Log")
            return "Member"  # Default if not found
        except Exception as e:
            print(f"❌ Error getting member rank from log: {e}")
            return "Member"  # Default on error

    async def get_member_role_from_log(self, user_id: str):
        """Get member role from Member Log sheet by Discord ID"""
        try:
            print(f"🔍 Looking up role for user ID: {user_id}")
            member_log_worksheet = self.get_cached_worksheet(self.SHEET_URL, "Member Log")
            if not member_log_worksheet:
                print(f"❌ Could not access Member Log worksheet")
                return "Member"  # Default role
            
            member_log_values = await self.cached_get_all_values(member_log_worksheet, "Member Log")
            # Look for the user's Discord ID in the Member Log - check both columns A and B
            for row in member_log_values[1:]:  # Skip header row
                # Check both column A (User ID) and column B (Discord ID) for the user ID
                if ((len(row) > 0 and str(user_id) == str(row[0])) or 
                    (len(row) > 1 and str(user_id) == str(row[1]))):
                    # Member Log columns: [A:User ID, B:Discord ID, C:Rank, D:Role, ...]
                    if len(row) > 3:
                        role = row[3] if row[3] else "Member"
                        print(f"✅ Found user {user_id} with role: {role}")
                        return role
            
            print(f"❌ User {user_id} not found in Member Log")
            return "Member"  # Default if not found
        except Exception as e:
            print(f"❌ Error getting member role from log: {e}")
            return "Member"

    def get_member_rank_from_cached_data(self, user_id: str, member_log_values: list):
        """Get member rank from pre-loaded Member Log data (optimized for batch operations)"""
        try:
            # Look for the user's Discord ID in the cached Member Log data
            for row in member_log_values[1:]:  # Skip header row
                # Check both column A (User ID) and column B (Discord ID) for the user ID
                if (len(row) > 0 and str(user_id) == str(row[0])) or (len(row) > 1 and str(user_id) == str(row[1])):
                    # Member Log columns: [A:Name, B:Discord ID, C:Rank, ...]
                    if len(row) > 2:
                        rank = row[2] if row[2] else "Member"
                        return rank
            
            return "Member"  # Default if not found
        except Exception as e:
            print(f"❌ Error getting member rank from cached data: {e}")
            return "Member"  # Default on error

    def get_member_role_from_cached_data(self, user_id: str, member_log_values: list):
        """Get member role from pre-loaded Member Log data (optimized for batch operations)"""
        try:
            # Look for the user's Discord ID in the cached Member Log data
            for row in member_log_values[1:]:  # Skip header row
                # Check both column A (User ID) and column B (Discord ID) for the user ID
                if ((len(row) > 0 and str(user_id) == str(row[0])) or 
                    (len(row) > 1 and str(user_id) == str(row[1]))):
                    # Member Log columns: [A:User ID, B:Discord ID, C:Rank, D:Role, ...]
                    if len(row) > 3:
                        role = row[3] if row[3] else "Member"
                        return role
            
            return "Member"  # Default if not found
        except Exception as e:
            print(f"❌ Error getting member role from cached data: {e}")
            return "Member"
    
    def setup_google_sheets(self):
        """Initialize Google Sheets connection"""
        try:
            # Define the scope
            scope = [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"
            ]
            
            # Check if credentials file exists
            creds_file = "google_credentials.json"
            if os.path.exists(creds_file):
                creds = Credentials.from_service_account_file(creds_file, scopes=scope)
                self.gc = gspread.authorize(creds)
                print("✅ Google Sheets integration ready")
                
                # Test connection immediately
                self.test_connection()
            else:
                print("❌ Google credentials file not found. Please add 'google_credentials.json'")
                
        except Exception as e:
            print(f"❌ Error setting up Google Sheets: {e}")
    
    def test_connection(self):
        """Test the Google Sheets connection"""
        try:
            print("🔍 Testing Google Sheets connection...")
            sheet = self.gc.open_by_url(self.SHEET_URL)
            print(f"✅ Successfully opened sheet: {sheet.title}")
            
            worksheets = sheet.worksheets()
            worksheet_names = [ws.title for ws in worksheets]
            print(f"📋 Available worksheets: {worksheet_names}")
            
            if self.WORKSHEET_NAME in worksheet_names:
                print(f"✅ Found target worksheet: '{self.WORKSHEET_NAME}'")
            else:
                print(f"❌ Target worksheet '{self.WORKSHEET_NAME}' not found!")
                
        except Exception as e:
            print(f"❌ Connection test failed: {e}")
            print("⚙️ Troubleshooting tips:")
            print("   1. Make sure Google Sheets API is enabled")
            print("   2. Make sure Google Drive API is enabled")
            print(f"   3. Make sure the sheet is shared with: ofs-bot@ofs-bot.iam.gserviceaccount.com")
            print("   4. Check if the sheet URL is correct")
    
    def get_worksheet(self, sheet_url_or_key: str, worksheet_name: str = None):
        """Get a specific worksheet from Google Sheets"""
        try:
            if not self.gc:
                return None
            
            # Open the spreadsheet
            if sheet_url_or_key.startswith('http'):
                sheet = self.gc.open_by_url(sheet_url_or_key)
            else:
                sheet = self.gc.open_by_key(sheet_url_or_key)
            
            # Debug: Show available worksheets
            worksheets = sheet.worksheets()
            worksheet_names = [ws.title for ws in worksheets]
            print(f"SHEETS: Available worksheets: {worksheet_names}")
            
            # Get specific worksheet or first one
            if worksheet_name:
                print(f"SHEETS: Looking for worksheet: '{worksheet_name}'")
                worksheet = sheet.worksheet(worksheet_name)
            else:
                worksheet = sheet.sheet1
                
            return worksheet
            
        except Exception as e:
            print(f"❌ Error accessing worksheet: {e}")
            return None
    
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="set_quest_forum", description="Set the forum channel for quest announcements")
    @app_commands.describe(
        forum_channel="The forum channel where quest announcements will be posted"
    )
    async def set_quest_forum(
        self, 
        interaction: discord.Interaction,
        forum_channel: discord.ForumChannel
    ):
        # Check permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You need **Administrator** permissions to configure quest settings.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        # Save the forum channel ID
        self.set_quest_forum_channel(interaction.guild.id, forum_channel.id)
        
        # Setup quest status tags
        tags_setup = await self.setup_quest_forum_tags(forum_channel)
        
        embed = discord.Embed(
            title="✅ Quest Forum Configured",
            description=f"Quest announcements will now be posted in {forum_channel.mention}",
            color=0x00ff00
        )
        embed.add_field(name="Forum Channel", value=forum_channel.mention, inline=True)
        embed.add_field(name="Channel ID", value=f"`{forum_channel.id}`", inline=True)
        
        if tags_setup:
            embed.add_field(name="Status Tags", value="✅ Quest status tags have been configured", inline=False)
            embed.add_field(
                name="Available Tags", 
                value="▶️ Quest Started\n⏳ Quest Pending\n✅ Quest Completed\n❌ Quest Cancelled\n🪶 Recorded", 
                inline=False
            )
        else:
            embed.add_field(name="Status Tags", value="⚠️ Some tags may not have been created", inline=False)
        
        embed.set_footer(text="Each quest will create a new forum post with appropriate status tags")
        
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="set_quest_review_channel", description="Set the channel for quest review submissions")
    @app_commands.describe(
        review_channel="The channel where completed quests will be sent for admin review"
    )
    async def set_quest_review_channel_command(
        self, 
        interaction: discord.Interaction,
        review_channel: discord.TextChannel
    ):
        try:
            # Check permissions
            if not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message("You need **Administrator** permissions to configure quest settings.", ephemeral=True)
                return
            
            await interaction.response.defer(ephemeral=True)
            
            # Save the review channel ID using the helper method
            self.set_quest_review_channel(interaction.guild.id, review_channel.id)
            print(f"Successfully set quest review channel to {review_channel.id} for guild {interaction.guild.id}")
            
            embed = discord.Embed(
                title="✅ Quest Review Channel Configured",
                description=f"Completed quests will now be sent to {review_channel.mention} for admin review",
                color=0x00ff00
            )
            embed.add_field(name="Review Channel", value=review_channel.mention, inline=True)
            embed.add_field(name="Channel ID", value=f"`{review_channel.id}`", inline=True)
            embed.add_field(
                name="How it works:",
                value="1️⃣ Quest leaders mark quests complete\n2️⃣ Quest gets sent here for review\n3️⃣ Admins approve or request edits\n4️⃣ Approved quests are marked final",
                inline=False
            )
            embed.set_footer(text="Quest review workflow is now active")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            print(f"Error setting quest review channel: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Error setting quest review channel: {str(e)}", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Error setting quest review channel: {str(e)}", ephemeral=True)
    
    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="setup_quest_tags", description="Setup quest status tags in the configured forum channel")
    async def setup_quest_tags(self, interaction: discord.Interaction):
        # Check permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You need **Administrator** permissions to setup quest tags.", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        # Get the quest forum channel
        forum_channel_id = self.get_quest_forum_channel(interaction.guild.id)
        if not forum_channel_id:
            await interaction.followup.send("❌ No quest forum channel configured! Use `/set_quest_forum` first.", ephemeral=True)
            return
        
        forum_channel = interaction.guild.get_channel(forum_channel_id)
        if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
            await interaction.followup.send("❌ Quest forum channel not found or is not a forum channel!", ephemeral=True)
            return
        
        # Setup quest status tags
        tags_setup = await self.setup_quest_forum_tags(forum_channel)
        
        if tags_setup:
            # Show current tags
            tag_list = []
            for tag in forum_channel.available_tags:
                tag_list.append(f"{tag.emoji} {tag.name}" if tag.emoji else tag.name)
            
            embed = discord.Embed(
                title="✅ Quest Tags Setup Complete",
                description=f"Quest status tags have been configured in {forum_channel.mention}",
                color=0x00ff00
            )
            embed.add_field(
                name="Available Tags", 
                value="\n".join(tag_list) if tag_list else "None", 
                inline=False
            )
            embed.set_footer(text="You can now create quests with proper status tags!")
        else:
            embed = discord.Embed(
                title="⚠️ Tag Setup Issues",
                description="Some tags may not have been created properly. Check bot permissions.",
                color=0xff9900
            )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    async def game_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        """Autocomplete function for game selection using forum tags"""
        # Get the quest forum channel to access available tags
        forum_channel_id = self.get_quest_forum_channel(interaction.guild.id)
        if not forum_channel_id:
            return []
        
        forum_channel = interaction.guild.get_channel(forum_channel_id)
        if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
            return []
        
        # Get available game tags from forum (exclude quest status tags)
        excluded_tags = {"Quest Completed", "Quest Started", "Quest Cancelled", "Recorded"}
        choices = []
        
        for tag in forum_channel.available_tags:
            if tag.name not in excluded_tags and current.lower() in tag.name.lower():
                choices.append(app_commands.Choice(name=tag.name, value=tag.name))
                
                if len(choices) >= 25:  # Discord limit
                    break
        
        return choices
    
    @app_commands.command(name="start_quest", description="Launch the quest builder interface")
    @app_commands.describe(
        patrol_leader="The Discord user who is leading this patrol",
        game="The game this quest is for (select from available forum tags)"
    )
    @app_commands.autocomplete(game=game_autocomplete)
    async def start_quest(
        self, 
        interaction: discord.Interaction,
        patrol_leader: discord.Member,
        game: str
    ):
        print(f"🚨 START_QUEST CALLED! User: {interaction.user}, Leader: {patrol_leader}, Game: {game}")
        try:
            # Clear any potentially corrupted cache first
            self.clear_cache()
            print(f"🔧 DEBUG: Cache cleared before starting quest")
            
            # Defer the response immediately to prevent timeout
            await interaction.response.defer(ephemeral=True)
            print(f"🔧 DEBUG: Response deferred")
            
            print(f"🔧 DEBUG: start_quest called by {interaction.user}")
            
            # Check permissions
            if not any(role.name in ["Host", "Admin", "Moderator"] for role in interaction.user.roles):
                print(f"🔧 DEBUG: Permission denied for {interaction.user}")
                await interaction.followup.send("❌ You need the Host, Admin, or Moderator role to create quests.", ephemeral=True)
                return
            
            print(f"🔧 DEBUG: Permission check passed")
            
            if not self.gc:
                print(f"🔧 DEBUG: Google Sheets not configured")
                await interaction.followup.send("❌ Google Sheets integration not configured. Please add credentials file.", ephemeral=True)
                return
            
            print(f"🔧 DEBUG: Google Sheets check passed")
            
            # Look up leader's rank from Member Log
            leader_rank = "Member"  # Default fallback
            member_log_worksheet = self.get_worksheet(self.SHEET_URL, "Member Log")
            if member_log_worksheet:
                try:
                    member_log_values = await self.rate_limited_get_all_values(member_log_worksheet)
                    # Look for the patrol leader's Discord ID in the Member Log - check both columns A and B
                    for row in member_log_values[1:]:  # Skip header row
                        # Check both column A (User ID) and column B (Discord ID) for the leader ID
                        if ((len(row) > 0 and str(patrol_leader.id) == str(row[0])) or 
                            (len(row) > 1 and str(patrol_leader.id) == str(row[1]))):
                            # Member Log columns: [A:User ID, B:Discord ID, C:Rank, D:Role, ...]
                            if len(row) > 2:
                                leader_rank = row[2] if row[2] else "Member"  # C: Rank
                            break
                except Exception as e:
                    print(f"🔧 DEBUG: Error reading Member Log for leader rank: {e}")
            
            print(f"🔧 DEBUG: Leader rank lookup complete: {leader_rank}")
            
            # Show the patrol editor interface
            view = PatrolEditorView(patrol_leader, leader_rank, self, game)
            print(f"🔧 DEBUG: PatrolEditorView created")
            
            embed = view.create_preview_embed()
            print(f"🔧 DEBUG: Preview embed created")
            
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            print(f"🔧 DEBUG: Response sent successfully")
            
        except Exception as e:
            print(f"❌ ERROR in start_quest: {e}")
            import traceback
            traceback.print_exc()
            try:
                await interaction.followup.send(f"❌ Error starting quest: {str(e)}", ephemeral=True)
            except:
                pass

    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="update_quest", description="Update quest data in the OFS sheet")
    @app_commands.describe(
        patrol_id="The Patrol ID to update",
        player="Player to add to the quest",
        role="Role of the player",
        fps_kills="Number of FPS kills",
        ship_kills="Number of ship kills",
        quest_crusades="Quest/Crusades count",
        hosted_quest="Hosted Quest/Crusades count"
    )
    async def update_quest(
        self,
        interaction: discord.Interaction,
        patrol_id: str,
        player: Optional[discord.Member] = None,
        role: Optional[str] = None,
        fps_kills: Optional[int] = None,
        ship_kills: Optional[int] = None,
        quest_crusades: Optional[str] = None,
        hosted_quest: Optional[str] = None
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You need **Administrator** permissions to update quests.", ephemeral=True)
            return
        
        try:
            await interaction.response.defer(ephemeral=True)
            
            worksheet = self.get_worksheet(self.SHEET_URL, self.WORKSHEET_NAME)
            if not worksheet:
                await interaction.followup.send("❌ Could not access the OFS Google Sheet.", ephemeral=True)
                return
            
            # Find the row with the patrol ID
            all_values = self.rate_limited_get_all_values(worksheet)
            target_row = None
            
            for i, row in enumerate(all_values):
                if row and len(row) > 0 and row[0] == patrol_id:
                    target_row = i + 1  # gspread uses 1-based indexing
                    break
            
            if not target_row:
                await interaction.followup.send(f"❌ Patrol ID `{patrol_id}` not found in the sheet.", ephemeral=True)
                return
            
            # Update the specified fields
            updates = []
            if player:
                updates.append(f"E{target_row}")  # Player ID column
                worksheet.update(f"E{target_row}", str(player.id))
                
            if role:
                updates.append(f"F{target_row}")  # Role column
                worksheet.update(f"F{target_row}", role)
                
            if quest_crusades:
                updates.append(f"G{target_row}")  # Quest/Crusades column
                worksheet.update(f"G{target_row}", quest_crusades)
                
            if fps_kills is not None:
                updates.append(f"H{target_row}")  # FPS kills column
                worksheet.update(f"H{target_row}", str(fps_kills))
                
            if ship_kills is not None:
                updates.append(f"I{target_row}")  # Ship kills column
                worksheet.update(f"I{target_row}", str(ship_kills))
                
            if hosted_quest:
                updates.append(f"J{target_row}")  # Hosted Quest column
                worksheet.update(f"J{target_row}", hosted_quest)
            
            embed = discord.Embed(
                title="✅ Quest Updated",
                description=f"**Quest ID:** `{patrol_id}`\n**Updated cells:** {', '.join(updates)}",
                color=0x00ff00
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            print(f"Error in update_quest command: {e}")
            try:
                await interaction.followup.send(f"❌ An error occurred: {str(e)}", ephemeral=True)
            except:
                pass

    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="update_quest_status", description="Update the status tag of a quest forum post")
    @app_commands.describe(
        quest_id="The Quest ID to update status for",
        status="The new status for the quest"
    )
    @app_commands.choices(status=[
        app_commands.Choice(name="▶️ Quest Started", value="started"),
        app_commands.Choice(name="⏳ Quest Pending", value="pending"),
        app_commands.Choice(name="✅ Quest Completed", value="completed"),
        app_commands.Choice(name="❌ Quest Cancelled", value="cancelled"),
        app_commands.Choice(name="🪶 Recorded", value="recorded")
    ])
    async def update_quest_status(
        self,
        interaction: discord.Interaction,
        quest_id: str,
        status: str
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You need **Administrator** permissions to update quest status.", ephemeral=True)
            return
        
        try:
            await interaction.response.defer(ephemeral=True)
            
            # Get the quest forum channel
            forum_channel_id = self.get_quest_forum_channel(interaction.guild.id)
            if not forum_channel_id:
                await interaction.followup.send("❌ No quest forum channel configured! Use `/set_quest_forum` first.", ephemeral=True)
                return
            
            forum_channel = interaction.guild.get_channel(forum_channel_id)
            if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
                await interaction.followup.send("❌ Quest forum channel not found or is not a forum channel!", ephemeral=True)
                return
            
            # Map status choices to tag names (without emojis)
            status_map = {
                "started": "Quest Started",
                "pending": "Quest Pending", 
                "completed": "Quest Completed",
                "cancelled": "Quest Cancelled",
                "recorded": "Recorded"
            }
            
            target_tag_name = status_map.get(status)
            if not target_tag_name:
                await interaction.followup.send("❌ Invalid status selected.", ephemeral=True)
                return
            
            # Find the forum thread with the quest ID in the name
            quest_thread = None
            async for thread in forum_channel.archived_threads(limit=None):
                if quest_id.upper() in thread.name.upper():
                    quest_thread = thread
                    break
            
            # Check active threads too
            if not quest_thread:
                for thread in forum_channel.threads:
                    if quest_id.upper() in thread.name.upper():
                        quest_thread = thread
                        break
            
            if not quest_thread:
                await interaction.followup.send(f"❌ Could not find forum thread for quest ID `{quest_id}`.", ephemeral=True)
                return
            
            # Get the target tag
            target_tag = self.get_quest_tag_by_name(forum_channel, target_tag_name)
            if not target_tag:
                await interaction.followup.send(f"❌ Status tag `{target_tag_name}` not found in forum channel.\n\n**Solution:** Use `/setup_quest_tags` to create the required tags first.", ephemeral=True)
                return
            
            # Handle different tag removal logic based on status
            if status == "recorded":
                # For "recorded", only remove active status tags (Quest Started, Quest Pending)
                # Keep completion tags (Quest Completed, Quest Cancelled)
                tags_to_remove = [
                    self.get_quest_tag_by_name(forum_channel, "Quest Started"),
                    self.get_quest_tag_by_name(forum_channel, "Quest Pending")
                ]
                # Filter out None values and the tags we want to remove
                current_tags = [tag for tag in quest_thread.applied_tags if tag not in tags_to_remove]
                new_tags = current_tags + [target_tag]
            else:
                # For other statuses, remove all quest status tags and add the new one
                all_quest_tags = [
                    self.get_quest_tag_by_name(forum_channel, "Quest Started"),
                    self.get_quest_tag_by_name(forum_channel, "Quest Pending"),
                    self.get_quest_tag_by_name(forum_channel, "Quest Completed"),
                    self.get_quest_tag_by_name(forum_channel, "Quest Cancelled"),
                    self.get_quest_tag_by_name(forum_channel, "Recorded")
                ]
                # Filter out None values and current status tags
                current_tags = [tag for tag in quest_thread.applied_tags if tag not in all_quest_tags]
                new_tags = current_tags + [target_tag]
            
            # Update the thread tags
            await quest_thread.edit(applied_tags=new_tags)
            
            # Update start time in Google Sheets if starting the quest
            if status == "started":
                try:
                    worksheet = self.get_worksheet(self.SHEET_URL, self.WORKSHEET_NAME)
                    if worksheet:
                        all_values = self.rate_limited_get_all_values(worksheet)
                        # Find the quest row by quest ID
                        quest_row = None
                        for i, row in enumerate(all_values):
                            if len(row) > 0 and row[0] == quest_id:
                                quest_row = i + 1  # gspread uses 1-based indexing
                                break
                        
                        if quest_row:
                            new_start_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            worksheet.update(f"H{quest_row}", new_start_time)  # H: Quest Start Time
                except Exception as e:
                    print(f"Error updating start time in slash command: {e}")
            
            # Create success embed
            embed = discord.Embed(
                title="✅ Quest Status Updated",
                description=f"Quest `{quest_id}` status has been updated",
                color=0x00ff00
            )
            embed.add_field(name="Quest Thread", value=f"[{quest_thread.name}]({quest_thread.jump_url})", inline=False)
            embed.add_field(name="New Status", value=target_tag_name, inline=True)
            embed.add_field(name="Quest ID", value=f"`{quest_id}`", inline=True)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
            # Send status update message to the quest thread
            status_embed = discord.Embed(
                title="📋 Quest Status Updated",
                description=f"This quest has been marked as: **{target_tag_name}**",
                color=0x0099ff
            )
            status_embed.add_field(name="Updated by", value=interaction.user.mention, inline=True)
            
            # Add time-specific information based on status
            if status == "pending":
                status_embed.add_field(name="⏸️ Quest Paused", value=f"<t:{int(datetime.now().timestamp())}:R>", inline=True)
                status_embed.set_footer(text="Quest is now paused. Use 'Start Quest' button to resume.")
            elif status == "started":
                status_embed.add_field(name="▶️ Quest (Re)Started", value=f"<t:{int(datetime.now().timestamp())}:R>", inline=True)
                status_embed.set_footer(text="Quest timer has been updated with new start time.")
            elif status == "completed":
                status_embed.add_field(name="✅ Completed", value=f"<t:{int(datetime.now().timestamp())}:R>", inline=True)
                status_embed.set_footer(text="Quest completed successfully!")
            elif status == "cancelled":
                status_embed.add_field(name="❌ Cancelled", value=f"<t:{int(datetime.now().timestamp())}:R>", inline=True)
                status_embed.set_footer(text="Quest was cancelled.")
            elif status == "recorded":
                status_embed.add_field(name="🪶 Recorded", value=f"<t:{int(datetime.now().timestamp())}:R>", inline=True)
                status_embed.set_footer(text="Quest data has been recorded.")
            
            await quest_thread.send(embed=status_embed)
            
        except Exception as e:
            print(f"Error in update_quest_status command: {e}")
            try:
                await interaction.followup.send(f"❌ An error occurred: {str(e)}", ephemeral=True)
            except:
                pass

    @app_commands.default_permissions(administrator=True)
    @app_commands.command(name="list_quests", description="List all quests and their current status")
    async def list_quests(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You need **Administrator** permissions to list quests.", ephemeral=True)
            return
        
        try:
            await interaction.response.defer(ephemeral=True)
            
            # Get the quest forum channel
            forum_channel_id = self.get_quest_forum_channel(interaction.guild.id)
            if not forum_channel_id:
                await interaction.followup.send("❌ No quest forum channel configured! Use `/set_quest_forum` first.", ephemeral=True)
                return
            
            forum_channel = interaction.guild.get_channel(forum_channel_id)
            if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
                await interaction.followup.send("❌ Quest forum channel not found or is not a forum channel!", ephemeral=True)
                return
            
            # Collect all quest threads
            quest_threads = []
            
            # Get active threads
            for thread in forum_channel.threads:
                if "📜" in thread.name:  # Quest threads have this emoji
                    quest_threads.append(thread)
            
            # Get archived threads (recent ones)
            async for thread in forum_channel.archived_threads(limit=50):
                if "📜" in thread.name:
                    quest_threads.append(thread)
            
            if not quest_threads:
                await interaction.followup.send("🔍 No quest threads found in the forum channel.", ephemeral=True)
                return
            
            # Create embed with quest list
            embed = discord.Embed(
                title="📋 Quest Status Overview",
                description=f"Found {len(quest_threads)} quest(s) in {forum_channel.mention}",
                color=0x0099ff
            )
            
            # Group quests by status (using display names with emojis for UI)
            status_groups = {
                "▶️ Quest Started": [],
                "⏳ Quest Pending": [],
                "✅ Quest Completed": [],
                "❌ Quest Cancelled": [],
                "🪶 Recorded": [],
                "❓ No Status": []
            }
            
            # Map tag names (without emojis) to display names (with emojis)
            tag_to_display = {
                "Quest Started": "▶️ Quest Started",
                "Quest Pending": "⏳ Quest Pending",
                "Quest Completed": "✅ Quest Completed",
                "Quest Cancelled": "❌ Quest Cancelled",
                "Recorded": "🪶 Recorded"
            }
            
            for thread in quest_threads:
                # Get the quest status from tags
                quest_status = "❓ No Status"
                for tag in thread.applied_tags:
                    if tag.name in tag_to_display:
                        quest_status = tag_to_display[tag.name]
                        break
                
                # Extract quest ID from thread name if possible
                quest_info = {
                    "name": thread.name,
                    "url": thread.jump_url,
                    "created": thread.created_at,
                    "archived": getattr(thread, 'archived', False)
                }
                
                status_groups[quest_status].append(quest_info)
            
            # Add fields for each status group
            for status, quests in status_groups.items():
                if quests:
                    quest_list = []
                    for i, quest in enumerate(quests[:5]):  # Limit to 5 per status to avoid embed limits
                        archived_indicator = " 📦" if quest["archived"] else ""
                        quest_list.append(f"• [{quest['name'][:40]}...]({quest['url']}){archived_indicator}")
                    
                    if len(quests) > 5:
                        quest_list.append(f"... and {len(quests) - 5} more")
                    
                    embed.add_field(
                        name=f"{status} ({len(quests)})",
                        value="\n".join(quest_list) if quest_list else "None",
                        inline=False
                    )
            
            embed.set_footer(text="📦 = Archived | Use /update_quest_status to change quest status")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            print(f"Error in list_quests command: {e}")
            try:
                await interaction.followup.send(f"❌ An error occurred: {str(e)}", ephemeral=True)
            except:
                pass

async def setup(bot: commands.Bot):
    await bot.add_cog(QuestTracker(bot))