# OFS Bot

Discord bot for quest management, patrol workflows, member tracking, and Google Sheets integration.

## Features

- Quest creation and management
- Participant tracking
- Google Sheets-backed data access
- Forum post creation and tagging
- Member rank and role tracking

## Required configuration

The bot reads configuration from real environment variables and also loads a local `.env` file if present.

- `DISCORD_TOKEN`: Discord bot token
- `SPREADSHEET_ID`: Google Sheet ID used by the bot
- `GOOGLE_CREDENTIALS_JSON`: Service-account JSON as a single-line string for VPS/cloud hosting
- `BOT_NAME`: Optional display/logging name
- `ENV`: Optional, defaults to `production`
- `TEST_GUILD_ID`: Optional, used when `ENV=staging`

For local development only, you can place `google_credentials.json` in the project root instead of using `GOOGLE_CREDENTIALS_JSON`.

## Local setup

1. Clone the repository.
2. Install dependencies with `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and fill in the values.
4. Run the bot with `python bot.py`.

## GitHub repo checklist

Before pushing this project to GitHub:

1. Keep `.env` and `google_credentials.json` out of the repo.
2. Keep runtime data like guild settings, counters, and per-guild templates out of the repo unless you intentionally want seeded defaults.
3. Use GitHub or VPS environment variables for secrets instead of committing them.
4. If a token or credential was ever committed, rotate it before publishing.

## VPS deployment

1. Clone the repo onto the VPS.
2. Install Python and project dependencies.
3. Set `DISCORD_TOKEN`, `SPREADSHEET_ID`, and `GOOGLE_CREDENTIALS_JSON` in the VPS environment or process manager.
4. Start the bot with `python bot.py`.
5. Use a process manager such as `systemd`, `pm2`, or `supervisor` so the bot restarts automatically.

## Google Sheets setup

1. Create a Google Cloud project.
2. Enable Google Sheets API and Google Drive API.
3. Create a service account.
4. Share the target sheet with the service-account email.
5. Put the sheet ID in `SPREADSHEET_ID`.
