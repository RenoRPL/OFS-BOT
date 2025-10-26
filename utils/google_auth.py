"""
Google Sheets utilities for cloud deployment
"""
import os
import json
import gspread
from google.oauth2.service_account import Credentials


def get_google_credentials():
    """
    Get Google credentials from environment variables or file.
    Supports both cloud deployment (env vars) and local development (file).
    """
    try:
        # Try to get credentials from environment variable (for cloud deployment)
        creds_json = os.getenv('GOOGLE_CREDENTIALS_JSON')
        if creds_json:
            print("✅ Using Google credentials from environment variable")
            credentials_info = json.loads(creds_json)
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"
            ]
            credentials = Credentials.from_service_account_info(credentials_info, scopes=scopes)
            return gspread.authorize(credentials)
        
        # Fallback to local file (for local development)
        elif os.path.exists("google_credentials.json"):
            print("✅ Using Google credentials from local file")
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets", 
                "https://www.googleapis.com/auth/drive"
            ]
            credentials = Credentials.from_service_account_file("google_credentials.json", scopes=scopes)
            return gspread.authorize(credentials)
        
        else:
            print("⚠️ No Google credentials found - Google Sheets disabled")
            print("   For cloud: Set GOOGLE_CREDENTIALS_JSON environment variable")
            print("   For local: Place google_credentials.json file in project root")
            return None
            
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in GOOGLE_CREDENTIALS_JSON: {e}")
        return None
    except Exception as e:
        print(f"❌ Google Sheets setup failed: {e}")
        return None
