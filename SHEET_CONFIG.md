# Configure Your OFS Sheet

To set up your hardcoded Google Sheet URL:

1. **Open** `cogs/quest_tracker.py`
2. **Find line 16** that says:
   ```python
   self.SHEET_URL = "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID_HERE"
   ```
3. **Replace** `YOUR_SHEET_ID_HERE` with your actual Google Sheet ID from the URL

## How to get your Sheet ID:

From your Google Sheet URL:
```
https://docs.google.com/spreadsheets/d/1ABC123def456GHI789jkl/edit#gid=0
```

The Sheet ID is the long string between `/d/` and `/edit`:
```
1ABC123def456GHI789jkl
```

So your line should look like:
```python
self.SHEET_URL = "https://docs.google.com/spreadsheets/d/1ABC123def456GHI789jkl"
```

## Tab Name
The tab name is already set to "Patrols" - change this in line 17 if your tab has a different name.

## After making changes:
- Save the file
- Restart the bot
- Test with `/start_quest`
