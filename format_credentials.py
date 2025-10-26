#!/usr/bin/env python3
"""
Helper script to format Google credentials for environment variables
"""
import json

def format_credentials_for_env():
    """Read google_credentials.json and format it for environment variable"""
    try:
        with open('google_credentials.json', 'r') as f:
            credentials = json.load(f)
        
        # Convert to compact JSON string (no extra whitespace)
        credentials_json = json.dumps(credentials, separators=(',', ':'))
        
        print("=" * 60)
        print("🔑 GOOGLE_CREDENTIALS_JSON Environment Variable Value:")
        print("=" * 60)
        print(credentials_json)
        print("=" * 60)
        print("\n✅ Copy the above line and paste it as the value for")
        print("   GOOGLE_CREDENTIALS_JSON in your hosting platform\n")
        
    except FileNotFoundError:
        print("❌ google_credentials.json not found in current directory")
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in google_credentials.json: {e}")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    format_credentials_for_env()
