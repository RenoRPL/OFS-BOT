# OFS Bot - Discord Quest Management Bot

A comprehensive Discord bot for managing quests, patrols, and member tracking with Google Sheets integration.

## Features

- 🎯 Quest creation and management
- 👥 Participant tracking and management
- 📊 Google Sheets integration for data persistence
- 🏛️ Crusade quest support with special indicators
- 📝 Forum post creation with automatic tagging
- 👤 Member rank and role tracking

## Local Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/RenoRPL/OFS-BOT.git
   cd OFS-BOT
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Create environment file:**
   Copy `.env.example` to `.env` and fill in your values:
   ```bash
   cp .env.example .env
   ```

4. **Set up Google Sheets:**
   - Place your `google_credentials.json` file in the project root, OR
   - Set the `GOOGLE_CREDENTIALS_JSON` environment variable with the JSON content

5. **Run the bot:**
   ```bash
   python bot.py
   ```

## Cloud Deployment

### Environment Variables Required:

- `DISCORD_TOKEN`: Your Discord bot token
- `GOOGLE_CREDENTIALS_JSON`: Google service account credentials as JSON string

### Deploy to Heroku:

1. Create a new Heroku app
2. Connect to your GitHub repository
3. Set environment variables in Heroku dashboard
4. Deploy from GitHub

### Deploy to Railway:

1. Connect your GitHub repository to Railway
2. Set environment variables in Railway dashboard
3. Deploy automatically

### Deploy to Render:

1. Connect your GitHub repository to Render
2. Set environment variables in Render dashboard
3. Use start command: `python bot.py`

## Google Sheets Setup

1. Create a Google Cloud Project
2. Enable Google Sheets API and Google Drive API
3. Create a service account and download credentials
4. Share your Google Sheets with the service account email
5. Use the sheet URL in your bot configuration

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## Support

For issues and questions, please open an issue on GitHub.
