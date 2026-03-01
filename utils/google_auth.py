# utils/google_auth.py
"""
Google Sheets utilities for cloud deployment + local development.

- Credentials:
  - Cloud: GOOGLE_CREDENTIALS_JSON env var (service account JSON as a string)
  - Local: google_credentials.json file in project root (or path override)

- Spreadsheet:
  - Always opened by SPREADSHEET_ID env var (prevents cross-bot interference)

This version:
- Cached gspread client (authorize once)
- Cached Spreadsheet (open once per process)
- Cached Worksheets (reuse worksheet objects)
- Retry/backoff for 429/5xx transient errors
- Fixes startup deadlock (uses RLock + no nested lock acquisition)
- Optional async wrappers (use asyncio.to_thread to avoid heartbeat blocked)
"""

from __future__ import annotations

import os
import json
import time
import threading
import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import gspread
from google.oauth2.service_account import Credentials

BASE_DIR = Path(__file__).resolve().parent.parent  # project root
DEFAULT_CREDS_FILE = BASE_DIR / "google_credentials.json"

# ----------------------------
# Retry / Backoff
# ----------------------------
MAX_RETRIES = 6
BASE_SLEEP = 0.8  # seconds (exponential backoff, capped)


def _is_retryable_error(e: Exception) -> bool:
    msg = str(e)
    return (
        "[429]" in msg
        or "Quota exceeded" in msg
        or "rateLimitExceeded" in msg
        or "[500]" in msg
        or "[503]" in msg
        or "internal error" in msg.lower()
    )


def safe_call(fn: Callable[[], Any], *, label: str = "sheets_call") -> Any:
    """
    Sync retry wrapper. NOTE: uses time.sleep(), so don't call it from the event loop
    for long chains of retries. Prefer open_spreadsheet_async/open_worksheet_async.
    """
    last: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as e:
            last = e
            if not _is_retryable_error(e):
                raise
            sleep_s = min(10.0, BASE_SLEEP * (2 ** (attempt - 1)))
            print(f"[WARN] {label} failed (attempt {attempt}/{MAX_RETRIES}): {e} -> sleeping {sleep_s:.1f}s")
            time.sleep(sleep_s)
    raise last  # type: ignore


# ----------------------------
# Cached global state
# ----------------------------
# RLock prevents deadlock if something ever re-enters while holding the lock.
_lock = threading.RLock()

_cached_client: Optional[gspread.Client] = None
_cached_sheet: Optional[gspread.Spreadsheet] = None
_cached_sheet_id: str = ""
_cached_ws: Dict[str, gspread.Worksheet] = {}


def reset_spreadsheet_cache() -> None:
    """Clears cached spreadsheet + worksheet objects (forces reopen on next access)."""
    global _cached_sheet, _cached_sheet_id, _cached_ws
    with _lock:
        _cached_sheet = None
        _cached_sheet_id = ""
        _cached_ws = {}


def _build_client_from_env_or_file() -> Optional[gspread.Client]:
    """
    Creates a NEW authorized gspread client.
    This function does NOT cache; caching is handled by get_google_client().
    """
    try:
        creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]

        # Cloud deployment: JSON stored in an env var
        if creds_json:
            print("[OK] Using Google credentials from environment variable (GOOGLE_CREDENTIALS_JSON)")
            credentials_info = json.loads(creds_json)
            credentials = Credentials.from_service_account_info(credentials_info, scopes=scopes)
            return gspread.authorize(credentials)

        # Local development: JSON file in project root
        if DEFAULT_CREDS_FILE.exists():
            print(f"[OK] Using Google credentials from local file: {DEFAULT_CREDS_FILE}")
            credentials = Credentials.from_service_account_file(str(DEFAULT_CREDS_FILE), scopes=scopes)
            return gspread.authorize(credentials)

        print("[WARN] No Google credentials found - Google Sheets disabled")
        print("   For cloud: Set GOOGLE_CREDENTIALS_JSON environment variable")
        print(f"   For local: Place google_credentials.json in project root: {DEFAULT_CREDS_FILE}")
        return None

    except json.JSONDecodeError as e:
        print(f"[ERROR] Invalid JSON in GOOGLE_CREDENTIALS_JSON: {e}")
        return None
    except Exception as e:
        print(f"[ERROR] Google Sheets setup failed: {e}")
        return None


def get_google_client() -> Optional[gspread.Client]:
    """
    Returns a cached authorized gspread client (authorize once per process).
    """
    global _cached_client
    with _lock:
        if _cached_client is not None:
            return _cached_client

        client = _build_client_from_env_or_file()
        _cached_client = client
        return _cached_client


def open_spreadsheet(spreadsheet_id: str | None = None) -> Optional[gspread.Spreadsheet]:
    """
    Opens and returns a cached gspread Spreadsheet object.

    - Uses SPREADSHEET_ID from environment by default.
    - Accepts an explicit spreadsheet_id for rare cases.
    """
    global _cached_sheet, _cached_sheet_id, _cached_ws

    sid = (spreadsheet_id or os.getenv("SPREADSHEET_ID", "")).strip()
    if not sid:
        print("[ERROR] SPREADSHEET_ID is missing. Add it to your .env / environment variables.")
        return None

    # IMPORTANT: get client OUTSIDE the sheet lock to avoid nested lock deadlocks.
    client = get_google_client()
    if not client:
        return None

    with _lock:
        # If we already opened this sheet, reuse it
        if _cached_sheet is not None and _cached_sheet_id == sid:
            return _cached_sheet

        # New sheet id or first open -> reset worksheet cache
        _cached_sheet_id = sid
        _cached_ws = {}

    # Do the network open outside the lock so we don't block other threads
    try:
        ss = safe_call(lambda: client.open_by_key(sid), label=f"open_spreadsheet({sid[:6]}...)")
        with _lock:
            _cached_sheet = ss
        print(f"[OK] Opened spreadsheet: {ss.title}")
        return ss
    except Exception as e:
        with _lock:
            _cached_sheet = None
        print(f"[ERROR] Failed to open spreadsheet by SPREADSHEET_ID ({sid[:6]}...): {e}")
        return None


def open_worksheet(title: str, spreadsheet_id: str | None = None) -> Optional[gspread.Worksheet]:
    """
    Convenience helper to open a worksheet by name — cached by tab name.
    """
    global _cached_ws

    key = (title or "").strip()
    if not key:
        return None

    # quick cache hit
    with _lock:
        if key in _cached_ws:
            return _cached_ws[key]

    ss = open_spreadsheet(spreadsheet_id=spreadsheet_id)
    if not ss:
        return None

    try:
        ws = safe_call(lambda: ss.worksheet(key), label=f"worksheet({key})")
        with _lock:
            _cached_ws[key] = ws
        return ws
    except Exception as e:
        print(f"[ERROR] Failed to open worksheet '{title}': {e}")
        return None


# ----------------------------
# Async wrappers (recommended for cogs)
# ----------------------------
async def open_spreadsheet_async(spreadsheet_id: str | None = None) -> Optional[gspread.Spreadsheet]:
    # Run blocking gspread work off the event loop to prevent heartbeat blocked.
    return await asyncio.to_thread(open_spreadsheet, spreadsheet_id)


async def open_worksheet_async(title: str, spreadsheet_id: str | None = None) -> Optional[gspread.Worksheet]:
    return await asyncio.to_thread(open_worksheet, title, spreadsheet_id)


# Backwards compatibility alias
def get_google_credentials():
    """
    DEPRECATED: Use get_google_client() instead.
    Kept for backwards compatibility with existing cogs.
    """
    return get_google_client()
