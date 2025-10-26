# 🚀 Patrol System with Join Functionality - Compl### 📋 Member Log Requirements:

For the automatic lookup to work, your "Member Log" tab should have these columns:
- **Column A**: Name
- **Column B**: Discord ID (must match exactly)
- **Column C**: Rank
- **Column D**: Role
- **Column E**: Quest/Crusades
- **Column F**: FPS Kills
- **Column G**: Time in Service
- **Column H**: Ship Kills
- **Column I**: Hosted Quest/Crusades

### 📊 Patrols Tab Column Structure:

When members join patrols, data is written to these columns:
- **Columns A-J**: Patrol information (copied for each participant)
- **Column K**: Player ID (Discord ID of joining member)
- **Column L**: Player Name (Display name of joining member)
- **Column M**: Role (from Member Log column D)
- **Column N**: Quest/Crusades (from Member Log column E)
- **Column O**: FPS Kills (RESERVED - for patrol points, left empty on join)
- **Column P**: Ship Kills (RESERVED - for patrol points, left empty on join)
- **Column Q**: Hosted Quest/Crusades (RESERVED - for patrol points, left empty on join)
- **Column R**: Time in Service (from Member Log column G)te Guide

## Overview
The patrol system now includes full functionality for creating patrols, managing templates, and allowing members to join active patrols through interactive buttons.

## Features Implemented ✅

### 1. Patrol Creation & Management
- **`/start_quest` command**: Create new patrols with comprehensive details
- **Interactive Patrol Editor**: Similar to embed maker with live preview
- **Google Sheets Integration**: Automatic logging to your OFS-Bot spreadsheet
- **Template System**: Save and reuse patrol configurations

### 2. Join Patrol Functionality (NEW!)
- **Join Button**: Added to all public patrol announcements
- **Automatic Tracking**: Creates individual participant rows in Google Sheets
- **Duplicate Prevention**: Won't allow same user to join twice
- **Live Notifications**: Sends confirmation when someone joins

## How It Works

### Creating a Patrol:
1. Use `/start_quest` command
2. Fill out patrol details in the interactive editor
3. Use templates to save time (similar to embed templates)
4. Click "Finalize Patrol" to start

### When a Patrol is Created:
1. **Private Confirmation**: Leader gets detailed patrol info with ID
2. **Public Announcement**: Channel gets patrol announcement with join button
3. **Google Sheets Entry**: New row created in "Patrols" worksheet
4. **Persistent Button**: Join button stays active even after bot restarts

### Joining a Patrol:
1. **Click Join Button**: Any member can click the green "Join Patrol" button
2. **Automatic Member Lookup**: Bot searches "Member Log" tab for the player's Discord ID
3. **Data Population**: Automatically fills in member data from Member Log:
   - Role (from Member Log)
   - Quest/Crusades count
   - FPS Kills
   - Ship Kills
   - Hosted Quest/Crusades count
   - Time in Service
4. **Sheet Update**: Creates new participant row with patrol data + member data
5. **Confirmation**: User gets success message showing their role and stats, channel gets notification

## Google Sheets Structure

### Member Log Integration:
- **Automatic Lookup**: When members join patrols, their data is automatically pulled from "Member Log" tab
- **Required Columns in Member Log**: Name, Discord ID, Role, Quest/Crusades, FPS Kills, Ship Kills, Hosted Quest/Crusades, Time in Service
- **Data Validation**: If member not found in Member Log, defaults to "Member" role with empty stats

### Master Patrol Row (Created by Leader):
- All patrol details (name, description, leader, etc.)
- Start time, scheduled end time
- Unique Patrol ID
- Extended columns for member tracking data

### Participant Rows (Created by Join Button):
- Same patrol data as master row
- Individual member information from Member Log
- Join timestamp
- Complete member statistics automatically populated
- Links back to original patrol via ID

## Template System

### Patrol Templates:
- Save frequently used patrol configurations
- Server-specific storage (each Discord server has own templates)
- Smart overwrite warnings
- Easy loading with preview updates

### Template Features:
- **Save**: Store current patrol setup as template
- **Load**: Quick-apply saved configurations
- **Delete**: Remove unwanted templates
- **Smart Warnings**: Prevents accidental overwrites

## Commands Reference

### `/start_quest`
- **Purpose**: Create a new patrol/quest
- **Parameters**: 
  - `patrol_leader`: The Discord user who will lead the patrol
- **Features**: Interactive editor, templates, live preview, automatic rank lookup
- **Automatic Data**: Leader's rank is automatically pulled from Member Log column C

### `/update_quest` (Future Enhancement)
- **Purpose**: Update existing patrol data
- **Usage**: Use with Patrol ID from Google Sheets

## Technical Implementation

### Persistent Views:
- Join buttons survive bot restarts
- No timeout on join functionality
- Proper error handling for all scenarios

### Google Sheets Integration:
- Real-time updates to your spreadsheet
- Automatic worksheet detection
- Robust error handling and reconnection

### Error Prevention:
- Duplicate join protection
- Invalid patrol ID handling
- Network failure recovery
- User feedback for all actions

## Usage Examples

### Example Patrol Creation Flow:
1. Admin uses `/start_quest @Leader`
2. Bot automatically looks up Leader's rank from Member Log
3. Opens patrol editor with Leader info pre-filled
4. Admin fills out "Border Patrol Alpha" details
5. Saves as template for future use
6. Finalizes patrol with auto-populated leader rank
7. Public announcement appears with join button

### Example Join Flow:
1. Member sees patrol announcement
2. Clicks "Join Patrol" button
3. Bot looks up member in "Member Log" tab using Discord ID
4. Finds: Role="Sergeant", Quest/Crusades="15", FPS Kills="45", etc.
5. Creates new row with patrol info + member stats
6. Gets confirmation: "✅ Successfully joined Border Patrol Alpha! Role: Sergeant"
7. Channel sees: "🎉 @Member (Sergeant) has joined the patrol!"
8. Google Sheets gets new participant row with complete member data

## Benefits

### For Leaders:
- Easy patrol creation with templates
- Automatic tracking and logging
- Professional announcements
- Clear participant management

### For Members:
- Simple one-click joining
- Clear feedback and confirmation
- No manual tracking needed

### For Administrators:
- Complete patrol history in Google Sheets
- Participant tracking for each patrol
- Template management for consistency
- Integration with existing bot systems

## Next Steps (Future Enhancements)

1. **Patrol Management Commands**:
   - View current patrol participants
   - End patrol early
   - Update patrol status

2. **Advanced Analytics**:
   - Patrol participation statistics
   - Member activity tracking
   - Popular patrol types

3. **Notifications**:
   - Patrol reminders
   - Scheduled end notifications
   - Participant messaging

## Configuration Notes

- Ensure your Google Sheets API is properly configured
- "Patrols" worksheet must exist in your spreadsheet
- **"Member Log" worksheet must exist** with columns: Name (A), Discord ID (B), Rank (C), Role (D), Quest/Crusades (E), FPS Kills (F), Time in Service (G), Ship Kills (H), Hosted Quest/Crusades (I)
- Bot needs appropriate permissions for buttons and embeds
- Consider role restrictions for `/start_quest` command
- **Discord IDs in Member Log column B must match exactly** for automatic data lookup
- **Leader ranks are automatically pulled from Member Log column C**
- **Patrols tab will use columns A-R** for complete member tracking
- **Columns O, P, Q are RESERVED** for patrol-specific points and will be left empty when members join (can be updated later via `/update_quest`)

---
**Status**: ✅ Fully Implemented and Operational
**Last Updated**: Current Session
**Compatibility**: Discord.py 2.x, Python 3.13+
