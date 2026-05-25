# -*- coding: utf-8 -*-
import os
import discord
from discord.ext import commands, tasks
import gspread
from google.oauth2.service_account import Credentials
import json
import asyncio
from datetime import datetime
import traceback
from utils.google_auth import get_google_credentials, open_spreadsheet

class MemberTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # LAZY: sheet is opened on first use (in a thread), not at startup
        self.gc = None
        self.sheet = None
        self.MEMBER_LOG_WORKSHEET = "Discord Member Log"

        # Read ENV to check if we're in staging
        self.env = os.getenv("ENV", "production").strip().lower()
        self.target_guild_id = int(os.getenv("OFS_MEMBER_SCAN_GUILD_ID", "1385700546434039928"))

        # Start the periodic task ONLY if not in staging mode
        if self.env != "staging":
            self.member_scan_task.start()
            print("[INFO] Member tracker background task enabled (production mode)")
        else:
            print("[STAGING] Member tracker background task DISABLED - use manual scan only")
    
    def _ensure_sheet(self):
        """Lazy-open spreadsheet on first use (runs in a thread, never at startup)."""
        if self.sheet is None:
            self.sheet = open_spreadsheet()
        return self.sheet

    def init_google_sheets(self):
        """DEPRECATED: kept for compat. Use _ensure_sheet() instead."""
        self._ensure_sheet()
    
    def get_worksheet(self, worksheet_name: str):
        """Get or create a worksheet"""
        try:
            if not self._ensure_sheet():
                return None

            # Try to get existing worksheet
            try:
                worksheet = self.sheet.worksheet(worksheet_name)
                return worksheet
            except gspread.exceptions.WorksheetNotFound:
                # Create new worksheet if it doesn't exist
                worksheet = self.sheet.add_worksheet(title=worksheet_name, rows=1000, cols=20)
                
                # Add headers
                headers = [
                    "User ID", "Username", "Display Name", "Guild ID", "Guild Name",
                    "Roles", "Role IDs", "Role Count", "Is Bot", "Account Created", "Joined Guild",
                    "Avatar URL", "Status", "Last Updated", "Nickname", "Premium Since",
                    "Permissions", "Top Role", "Top Role Color", "Top Role Position", "Mutual Guilds"
                ]
                worksheet.append_row(headers)
                print(f"✅ Created new worksheet: {worksheet_name}")
                return worksheet
                
        except Exception as e:
            print(f"❌ Error accessing worksheet {worksheet_name}: {e}")
            return None
    
    @tasks.loop(minutes=5)  # Optimized: Every 5 minutes (safe with current member count)
    async def member_scan_task(self):
        """Periodic task to scan all members every 5 minutes"""
        try:
            print("🔍 Starting periodic member scan...")
            await self.scan_all_members()
            print("✅ Periodic member scan completed")
        except Exception as e:
            print(f"❌ Error in periodic member scan: {e}")
            traceback.print_exc()
    
    @member_scan_task.before_loop
    async def before_member_scan(self):
        """Wait until bot is ready before starting the task"""
        await self.bot.wait_until_ready()
        print("🚀 Member tracker task started - will run every 5 minutes")
    
    async def scan_all_members(self):
        """Scan all members across all guilds and update the spreadsheet"""
        if not self._ensure_sheet():
            print("❌ Google Sheets not initialized")
            return

        # Check if members intent is enabled
        if not self.bot.intents.members:
            print("❌ Members intent not enabled. Enable it in Discord Developer Portal and bot.py")
            return

        worksheet = self.get_worksheet(self.MEMBER_LOG_WORKSHEET)
        if not worksheet:
            print("❌ Could not access Member Log worksheet")
            return
        
        try:
            # Get current timestamp
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Clear existing data (keep headers)
            worksheet.clear()
            headers = [
                "User ID", "Username", "Display Name", "Guild ID", "Guild Name",
                "Roles", "Role IDs", "Role Count", "Is Bot", "Account Created", "Joined Guild",
                "Avatar URL", "Status", "Last Updated", "Nickname", "Premium Since",
                "Permissions", "Top Role", "Top Role Color", "Top Role Position", "Mutual Guilds"
            ]
            worksheet.append_row(headers)
            
            all_member_data = []
            
            # Scan only the production OFS community guild. The bot may also be in
            # test/staging Discord servers with duplicate role names but different
            # role IDs; those must not contaminate the production Discord Member Log.
            guild = self.bot.get_guild(self.target_guild_id)
            if guild is None:
                print(f"❌ Target guild {self.target_guild_id} not found. Available guilds: "
                      f"{[(g.name, g.id) for g in self.bot.guilds]}")
                return

            print(f"📊 Scanning target guild: {guild.name} ({guild.id})")
            
            # Get all members in the target guild
            members = guild.members
            
            for member in members:
                    try:
                        # Get member roles (excluding @everyone). Keep both display names and
                        # stable Discord role IDs so Sheets/App Script can resolve mutable
                        # banner names by immutable role identity.
                        member_roles = [role for role in member.roles if role.name != "@everyone"]
                        role_names = [role.name for role in member_roles]
                        role_ids = [str(role.id) for role in member_roles]
                        roles_str = ", ".join(role_names) if role_names else "No roles"
                        role_ids_str = ", ".join(role_ids) if role_ids else ""
                        
                        # Get top role info
                        top_role = member.top_role
                        top_role_color = str(top_role.color) if top_role.color != discord.Color.default() else "Default"
                        
                        # Get member permissions (summarized)
                        perms = member.guild_permissions
                        key_perms = []
                        if perms.administrator:
                            key_perms.append("Administrator")
                        if perms.manage_guild:
                            key_perms.append("Manage Server")
                        if perms.manage_roles:
                            key_perms.append("Manage Roles")
                        if perms.manage_channels:
                            key_perms.append("Manage Channels")
                        if perms.moderate_members:
                            key_perms.append("Moderate Members")
                        
                        permissions_str = ", ".join(key_perms) if key_perms else "Standard"
                        
                        # Count mutual guilds with the bot
                        mutual_guilds = len([g for g in self.bot.guilds if member in g.members])
                        
                        # Prepare member data
                        member_data = [
                            str(member.id),  # User ID
                            member.name,  # Username
                            member.display_name,  # Display Name
                            str(guild.id),  # Guild ID
                            guild.name,  # Guild Name
                            roles_str,  # Roles
                            role_ids_str,  # Role IDs
                            len(member_roles),  # Role Count
                            "Yes" if member.bot else "No",  # Is Bot
                            member.created_at.strftime("%Y-%m-%d %H:%M:%S"),  # Account Created
                            member.joined_at.strftime("%Y-%m-%d %H:%M:%S") if member.joined_at else "Unknown",  # Joined Guild
                            str(member.avatar.url) if member.avatar else "No Avatar",  # Avatar URL
                            str(member.status).title(),  # Status
                            current_time,  # Last Updated
                            member.nick if member.nick else "No Nickname",  # Nickname
                            member.premium_since.strftime("%Y-%m-%d %H:%M:%S") if member.premium_since else "Not Premium",  # Premium Since
                            permissions_str,  # Permissions
                            top_role.name,  # Top Role
                            top_role_color,  # Top Role Color
                            top_role.position,  # Top Role Position
                            mutual_guilds  # Mutual Guilds
                        ]
                        
                        all_member_data.append(member_data)
                        
                    except Exception as e:
                        print(f"❌ Error processing member {member.name}: {e}")
                        continue
            
            # Batch update the worksheet
            if all_member_data:
                # Split into chunks to avoid API limits
                chunk_size = 100
                for i in range(0, len(all_member_data), chunk_size):
                    chunk = all_member_data[i:i + chunk_size]
                    worksheet.append_rows(chunk)
                    print(f"📝 Updated {len(chunk)} member records (batch {i//chunk_size + 1})")
                    
                    # Small delay to avoid rate limits
                    await asyncio.sleep(1)
                
                print(f"✅ Successfully updated {len(all_member_data)} total member records")
            else:
                print("⚠️ No member data found")
                
        except Exception as e:
            print(f"❌ Error in scan_all_members: {e}")
            traceback.print_exc()
    
    @commands.command(name="scan_members")
    @commands.has_permissions(administrator=True)
    async def manual_member_scan(self, ctx):
        """Manually trigger a member scan (Admin only)"""
        await ctx.send("🔍 Starting member scan... This may take a few minutes.")
        
        try:
            await self.scan_all_members()
            await ctx.send("✅ Member scan completed successfully!")
        except Exception as e:
            await ctx.send(f"❌ Error during member scan: {e}")
            print(f"❌ Manual member scan error: {e}")
            traceback.print_exc()
    
    @commands.command(name="scan_frequency")
    @commands.has_permissions(administrator=True)
    async def scan_frequency(self, ctx, minutes: int = None):
        """Change or view the member scan frequency (Admin only)"""
        if minutes is None:
            # Show current frequency
            current_minutes = self.member_scan_task.minutes
            embed = discord.Embed(
                title="📊 Member Scan Frequency",
                color=0x00ff00,
                timestamp=datetime.now()
            )
            embed.add_field(
                name="🔄 Current Frequency",
                value=f"Every **{current_minutes}** minutes",
                inline=False
            )
            embed.add_field(
                name="⚡ Recommended Frequencies",
                value="• **1 minute** - Real-time (high API usage)\n"
                      "• **2 minutes** - Very frequent (moderate API usage)\n"
                      "• **5 minutes** - Frequent (low API usage) ⭐\n"
                      "• **10 minutes** - Standard (very low API usage)\n"
                      "• **15 minutes** - Conservative (minimal API usage)",
                inline=False
            )
            embed.add_field(
                name="📈 API Usage Estimate",
                value=f"Current: ~{int(60/current_minutes * 4)} API calls/hour\n"
                      f"Limit: 300 requests/minute",
                inline=False
            )
            embed.set_footer(text="Use !scan_frequency <minutes> to change")
            await ctx.send(embed=embed)
            return
        
        # Validate frequency
        if minutes < 1:
            await ctx.send("❌ Frequency must be at least 1 minute.")
            return
        elif minutes > 60:
            await ctx.send("❌ Frequency cannot exceed 60 minutes.")
            return
        
        # Calculate API usage
        api_calls_per_hour = int(60/minutes * 4)
        if api_calls_per_hour > 250:
            await ctx.send(f"⚠️ Warning: {minutes} minutes would use ~{api_calls_per_hour} API calls/hour. This might hit rate limits.")
            return
        
        # Update frequency
        self.member_scan_task.change_interval(minutes=minutes)
        
        embed = discord.Embed(
            title="✅ Scan Frequency Updated",
            description=f"Member scanning frequency changed to **every {minutes} minutes**",
            color=0x00ff00,
            timestamp=datetime.now()
        )
        embed.add_field(
            name="📊 Estimated API Usage",
            value=f"~{api_calls_per_hour} API calls per hour",
            inline=True
        )
        embed.add_field(
            name="⏰ Next Scan",
            value=f"In {minutes} minutes",
            inline=True
        )
        await ctx.send(embed=embed)
        print(f"📊 Scan frequency changed to {minutes} minutes by {ctx.author}")

    @commands.command(name="member_stats")
    @commands.has_permissions(administrator=True)
    async def member_stats(self, ctx):
        """Show member statistics across all guilds"""
        try:
            total_members = 0
            total_bots = 0
            guild_stats = []
            
            guild = self.bot.get_guild(self.target_guild_id)
            if guild is None:
                await ctx.send(f"❌ Target guild {self.target_guild_id} not found. The member scanner is scoped to the production OFS guild.")
                return

            members = len(guild.members)
            bots = len([m for m in guild.members if m.bot])
            humans = members - bots
            
            guild_stats.append({
                'name': guild.name,
                'id': guild.id,
                'total': members,
                'humans': humans,
                'bots': bots
            })
            
            total_members += members
            total_bots += bots
            
            embed = discord.Embed(
                title="📊 Member Statistics",
                color=0x00ff00,
                timestamp=datetime.now()
            )
            
            embed.add_field(
                name="🌍 Overall Stats",
                value=f"**Total Members:** {total_members}\n"
                      f"**Humans:** {total_members - total_bots}\n"
                      f"**Bots:** {total_bots}\n"
                      f"**Scanner Guild:** {self.target_guild_id}",
                inline=False
            )
            
            # Add per-guild stats
            guild_info = ""
            for stats in guild_stats:
                guild_info += f"**{stats['name']}** (`{stats['id']}`)\n"
                guild_info += f"Total: {stats['total']} | Humans: {stats['humans']} | Bots: {stats['bots']}\n\n"
            
            if guild_info:
                embed.add_field(
                    name="🏰 Per-Guild Breakdown",
                    value=guild_info.strip(),
                    inline=False
                )
            
            embed.set_footer(text="Next automatic scan in <10 minutes")
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            await ctx.send(f"❌ Error generating member stats: {e}")
            print(f"❌ Member stats error: {e}")
    
    def cog_unload(self):
        """Clean up when cog is unloaded"""
        if self.member_scan_task.is_running():
            self.member_scan_task.cancel()
            print("🛑 Member tracker task stopped")

async def setup(bot):
    await bot.add_cog(MemberTracker(bot))
