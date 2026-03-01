# cogs/rolls.py
# -*- coding: utf-8 -*-
from __future__ import annotations

print("=== LOADED rolls.py v1.4 (GAMBLE: AUTO-CLOSE + DYNAMIC TIMER + RESTART RECOVERY) ===")

import asyncio
import json
import random
import re
import time
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from utils.google_auth import open_spreadsheet, reset_spreadsheet_cache

# ----------------------------
# Sheets / tabs
# ----------------------------
ROLL_EVENTS_SHEET = "Roll_Events"
ROLL_ENTRANTS_SHEET = "Roll_Entrants"
BANK_SHEET = "Bank"
BANK_LOGS_SHEET = "The bank (logs)"
CURRENCY_SHEET = "Currency"
PERMISSIONS_SHEET = "Permissions for slash commands"  # <-- NEW

# ----------------------------
# Permissions mapping
# ----------------------------
ROLL_COMMAND = "/roll"  # matches column A in Permissions tab

# Columns in Permissions tab (as shown in your screenshot)
# J = Roll timer (seconds)
# K = Gamble Fee %
ROLL_TIMER_COL_LETTER = "J"
GAMBLE_FEE_COL_LETTER = "K"

# ----------------------------
# Config
# ----------------------------
CACHE_TTL_SECONDS = 30
RETRY_COUNT = 3
RETRY_BASE_DELAY = 2.0
REFRESH_COOLDOWN_SECONDS = 5

# Who can CREATE/CLOSE/CANCEL events
REQUIRE_ADMIN_FOR_EVENT_CREATE = False
ALLOWED_CREATOR_ROLE_NAMES: List[str] = []

# Roll settings
ROLL_MIN = 1
ROLL_MAX = 100

# JOIN BUTTON FIX:
PUBLIC_JOIN_BUTTON_CUSTOM_ID_PREFIX = "roll_join:"


# ----------------------------
# Utilities
# ----------------------------
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_utc_iso() -> str:
    return _now_utc().isoformat()


def _safe_lower(v: Any) -> str:
    return str(v).strip().lower() if v is not None else ""


def _to_int(v: Any) -> int:
    try:
        if v is None:
            return 0
        s = str(v).strip()
        if s == "":
            return 0
        return int(float(s))
    except Exception:
        return 0


def _normalize_uid_cell(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    if s.isdigit():
        return s
    s2 = s.replace(",", "").replace(" ", "")
    try:
        if "e" in s2.lower() or "." in s2:
            return str(int(float(s2)))
    except Exception:
        pass
    return re.sub(r"\D+", "", s2)


def _col_to_a1(col_idx_0: int) -> str:
    n = col_idx_0 + 1
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _is_truthy(v: Any) -> bool:
    s = _safe_lower(v)
    return s not in ("", "0", "false", "no", "off", "n")


def _parse_coin_amount(cell: str) -> Tuple[int, str]:
    """
    Parse strings like:
      "1 Silver"
      "50 Silver"
      "100 copper"
      "2 gold"
    Returns (amount_int, currency_lower)
    """
    s = str(cell or "").strip()
    if not s:
        return 0, ""
    m = re.match(r"^\s*(\d+)\s*([A-Za-z]+)\s*$", s)
    if not m:
        return 0, ""
    amt = int(m.group(1))
    cur = m.group(2).strip().lower()
    if cur.endswith("s"):
        cur = cur[:-1]
    if cur in ("g", "gold"):
        return amt, "gold"
    if cur in ("s", "silver"):
        return amt, "silver"
    if cur in ("c", "copper"):
        return amt, "copper"
    return amt, cur


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _norm_currency(cur: str) -> Optional[str]:
    c = _safe_lower(cur)
    if c in ("g", "gold"):
        return "gold"
    if c in ("s", "silver"):
        return "silver"
    if c in ("c", "copper"):
        return "copper"
    return None


def _norm_type(t: str) -> str:
    tt = _safe_lower(t)
    if tt in ("gamble", "gmb", "pot"):
        return "GAMBLE"
    return "LOOT"


def _parse_percent(s: str) -> float:
    """
    Accepts:
      "10%" -> 0.10
      "0.1" -> 0.10
      "10"  -> 0.10 (assume percent if >1)
      ""    -> 0
    """
    raw = str(s or "").strip()
    if not raw:
        return 0.0
    raw = raw.replace(" ", "")
    try:
        if raw.endswith("%"):
            return max(0.0, float(raw[:-1]) / 100.0)
        val = float(raw)
        if val > 1.0:
            return max(0.0, val / 100.0)
        return max(0.0, val)
    except Exception:
        return 0.0


def _parse_iso_dt(s: str) -> Optional[datetime]:
    try:
        if not s:
            return None
        return datetime.fromisoformat(str(s).strip())
    except Exception:
        return None


def _discord_rel_ts(dt: datetime) -> str:
    # <t:unix:R>
    try:
        unix = int(dt.timestamp())
        return f"<t:{unix}:R>"
    except Exception:
        return "—"


def _fmt_countdown_mmss(target_dt: datetime) -> str:
    """Format time remaining until target_dt as mm:ss, clamped at 00:00."""
    remaining = (target_dt - _now_utc()).total_seconds()
    if remaining <= 0:
        return "00:00"
    total_sec = int(remaining)
    m, s = divmod(total_sec, 60)
    return f"{m:02d}:{s:02d}"


# ----------------------------
# Simple caching
# ----------------------------
class CacheEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, ttl: float):
        self.value = value
        self.expires_at = time.monotonic() + ttl

    def valid(self) -> bool:
        return time.monotonic() < self.expires_at


# ----------------------------
# Retry helper (threaded gspread calls)
# ----------------------------
async def _retry_to_thread(
    func,
    *,
    name: str = "op",
    retries: int = RETRY_COUNT,
    base_delay: float = RETRY_BASE_DELAY,
):
    last = None
    for attempt in range(retries):
        try:
            return await asyncio.to_thread(func)
        except Exception as e:
            last = e
            if attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"[Rolls] {name} failed ({attempt+1}/{retries}): {type(e).__name__}: {e} -> retry {delay:.1f}s")
                await asyncio.sleep(delay)
            else:
                print(f"[Rolls] {name} failed after {retries}: {type(e).__name__}: {e}")
                raise last


# ----------------------------
# Cog
# ----------------------------
class RollsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sheet = None

        self._cache: Dict[str, CacheEntry] = {}
        self._user_locks: Dict[int, asyncio.Lock] = {}
        self._event_locks: Dict[str, asyncio.Lock] = {}

        # in-memory scheduled close tasks per event (best-effort)
        self._entry_close_tasks: Dict[str, asyncio.Task] = {}
        self._loot_close_tasks: Dict[str, asyncio.Task] = {}
        self._gambles_rescheduled: bool = False
        # Receipt cooldowns to prevent spam hitting Sheets: (user_id, event_id) -> last_ts
        self._receipt_cooldowns: Dict[Tuple[int, str], float] = {}
        # Refresh button cooldown: (user_id, event_id) -> monotonic timestamp
        self._refresh_cd_until: Dict[Tuple[int, str], float] = {}
        # Per-event snapshot cache to reduce Sheets reads: key -> snapshot dict wrapped by CacheEntry via self._cache
        # snapshot keys use prefix 'evt:' + event_id
        # TTL seconds for snapshot cache
        self._event_snapshot_ttl = 7
        
        # EVENT STATE: in-memory cache for entrants count, pot, status (reduces Sheets reads)
        # event_state[event_id] = {"entrants": int, "pot_total": int, "status": str, "closes_at": datetime, "last_update_ts": float}
        self._event_state: Dict[str, Dict[str, Any]] = {}
        
        # PENDING PUBLIC UPDATES: debounce/coalesce updates (1-2s grace period)
        # pending_public_updates[event_id] -> asyncio.Task
        self._pending_public_updates: Dict[str, asyncio.Task] = {}

        # LOOT TICKERS: per-event 10s update loop
        self._loot_ticker_tasks: Dict[str, asyncio.Task] = {}
        
        # 429 CIRCUIT BREAKER: global backoff to prevent retry storms
        # read_backoff_until: time.monotonic() timestamp when we can resume Sheets reads
        self._read_backoff_until = 0.0
        self._read_backoff_base = 5.0  # start at 5s

    # ------------- on_ready: reschedule open gamble events after restart -------------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._gambles_rescheduled:
            return
        self._gambles_rescheduled = True
        try:
            await self._reschedule_open_gambles()
            await self._reschedule_open_loots()
        except Exception as e:
            print(f"[Rolls] on_ready reschedule error: {e}")

    async def _reschedule_open_gambles(self) -> None:
        """On restart, reschedule auto-close for any OPEN GAMBLE events."""
        def _read_open_gambles_sync() -> List[Dict[str, str]]:
            ws = self._ws(ROLL_EVENTS_SHEET)
            if not ws:
                return []
            data = ws.get_all_values()
            if not data or len(data) < 2:
                return []
            headers = data[0]
            idx_eid = self._find_header_index(headers, "Event_ID")
            idx_status = self._find_header_index(headers, "Status")
            idx_type = self._find_header_index(headers, "Type")
            idx_created = self._find_header_index(headers, "Created_At")
            idx_channel = self._find_header_index(headers, "Channel_ID")
            events: List[Dict[str, str]] = []
            for row in data[1:]:
                eid = (row[idx_eid] if idx_eid != -1 and idx_eid < len(row) else "").strip()
                status = _safe_lower(row[idx_status] if idx_status != -1 and idx_status < len(row) else "")
                etype = _norm_type(row[idx_type] if idx_type != -1 and idx_type < len(row) else "")
                created = (row[idx_created] if idx_created != -1 and idx_created < len(row) else "").strip()
                channel = (row[idx_channel] if idx_channel != -1 and idx_channel < len(row) else "").strip()
                if status == "open" and etype == "GAMBLE" and eid:
                    events.append({"event_id": eid, "created_at": created, "channel_id": channel})
            return events

        open_gambles = await _retry_to_thread(_read_open_gambles_sync, name="read_open_gambles")
        timer_sec = await self.get_roll_timer_seconds()

        for ev in open_gambles:
            created_at = _parse_iso_dt(ev["created_at"])
            if not created_at:
                continue
            entry_close_at = created_at + timedelta(seconds=timer_sec)
            eid = ev["event_id"]

            # skip if task already running
            if eid in self._entry_close_tasks and not self._entry_close_tasks[eid].done():
                continue

            # find guild from channel
            guild = None
            try:
                ch = self.bot.get_channel(int(ev["channel_id"]))
                if ch:
                    guild = ch.guild
            except Exception:
                pass
            if not guild:
                continue

            task = asyncio.create_task(
                self._schedule_auto_close(guild_id=guild.id, event_id=eid, entry_close_at=entry_close_at)
            )
            self._entry_close_tasks[eid] = task
            print(f"[Rolls] Rescheduled auto-close for {eid} (closes {entry_close_at.isoformat()})")

    async def _reschedule_open_loots(self) -> None:
        """On restart, reschedule auto-close for any OPEN LOOT events."""
        def _read_open_loots_sync() -> List[Dict[str, str]]:
            ws = self._ws(ROLL_EVENTS_SHEET)
            if not ws:
                return []
            data = ws.get_all_values()
            if not data or len(data) < 2:
                return []
            headers = data[0]
            idx_eid = self._find_header_index(headers, "Event_ID")
            idx_status = self._find_header_index(headers, "Status")
            idx_type = self._find_header_index(headers, "Type")
            idx_created = self._find_header_index(headers, "Created_At")
            idx_channel = self._find_header_index(headers, "Channel_ID")
            events: List[Dict[str, str]] = []
            for row in data[1:]:
                eid = (row[idx_eid] if idx_eid != -1 and idx_eid < len(row) else "").strip()
                status = _safe_lower(row[idx_status] if idx_status != -1 and idx_status < len(row) else "")
                etype = _norm_type(row[idx_type] if idx_type != -1 and idx_type < len(row) else "")
                created = (row[idx_created] if idx_created != -1 and idx_created < len(row) else "").strip()
                channel = (row[idx_channel] if idx_channel != -1 and idx_channel < len(row) else "").strip()
                if status == "open" and etype == "LOOT" and eid:
                    events.append({"event_id": eid, "created_at": created, "channel_id": channel})
            return events

        open_loots = await _retry_to_thread(_read_open_loots_sync, name="read_open_loots")
        timer_sec = await self.get_roll_timer_seconds()

        for ev in open_loots:
            created_at = _parse_iso_dt(ev["created_at"])
            if not created_at:
                continue
            entry_close_at = created_at + timedelta(seconds=timer_sec)
            eid = ev["event_id"]

            if eid in self._loot_close_tasks and not self._loot_close_tasks[eid].done():
                continue

            guild = None
            try:
                ch = self.bot.get_channel(int(ev["channel_id"]))
                if ch:
                    guild = ch.guild
            except Exception:
                pass
            if not guild:
                continue

            task = asyncio.create_task(
                self._schedule_auto_close_loot(guild_id=guild.id, event_id=eid, entry_close_at=entry_close_at)
            )
            self._loot_close_tasks[eid] = task
            print(f"[Rolls] Rescheduled loot auto-close for {eid} (closes {entry_close_at.isoformat()})")

    # ------------- locks / cache -------------
    def _lock_user(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._user_locks:
            self._user_locks[user_id] = asyncio.Lock()
        return self._user_locks[user_id]

    def _lock_event(self, event_id: str) -> asyncio.Lock:
        eid = str(event_id or "").strip()
        if eid not in self._event_locks:
            self._event_locks[eid] = asyncio.Lock()
        return self._event_locks[eid]

    def invalidate_event_cache(self, event_id: str) -> None:
        """Remove any cached snapshot for the event."""
        key = f"evt:{str(event_id).strip()}"
        if key in self._cache:
            try:
                del self._cache[key]
            except Exception:
                pass

    async def get_event_snapshot(self, event_id: str, ttl: Optional[int] = None) -> Dict[str, Any]:
        """Return a small snapshot for an event with caching, single-flight locking, and graceful handling of rate limits.

        Snapshot includes: row_i, headers, row, status, type, created_at (datetime or None),
        buyin_currency, buyin_amount, entrant_count, pot_total, entry_close_at (datetime or None), last_fetch_ts
        On error returns dict with 'error' key describing brief reason.
        """
        eid = str(event_id).strip()
        if not eid:
            return {"error": "missing_event_id"}

        key = f"evt:{eid}"
        # use default ttl if not provided
        ttl_use = int(ttl if ttl is not None else self._event_snapshot_ttl)

        # fast path: cached
        cached = self._get_cached(key)
        if cached is not None:
            return cached

        lock = self._lock_event(eid)
        # single-flight: acquire per-event lock
        async with lock:
            # re-check cache after acquiring
            cached = self._get_cached(key)
            if cached is not None:
                return cached

            # Try to fetch minimal data: event row + entrants. Use retries via _retry_to_thread which logs
            try:
                row_i, headers, row = await _retry_to_thread(lambda: self._find_event_row_sync(eid), name="find_event_row(snapshot)")
            except Exception as e:
                msg = str(e)
                print(f"[Rolls] find_event_row(snapshot) failed: {msg}")
                # detect rate-limit-like messages
                if "429" in msg or "Quota exceeded" in msg or "rateLimitExceeded" in msg:
                    # fallback to cache if exists
                    cached_fallback = self._get_cached(key)
                    if cached_fallback is not None:
                        return cached_fallback
                    return {"error": "rate_limited"}
                # attempt to re-open spreadsheet once and retry
                try:
                    print("[Rolls] Attempting to re-open spreadsheet after failure...")
                    await _retry_to_thread(lambda: open_spreadsheet(), name="reopen_spreadsheet_once")
                    row_i, headers, row = await _retry_to_thread(lambda: self._find_event_row_sync(eid), name="find_event_row(snapshot_retry)")
                except Exception as e2:
                    print(f"[Rolls] find_event_row(snapshot) failed after reopen: {e2}")
                    cached_fallback = self._get_cached(key)
                    if cached_fallback is not None:
                        return cached_fallback
                    return {"error": "sheet_unavailable"}

            # read entrants (minimally)
            try:
                entrants = await _retry_to_thread(lambda: self._read_entrants_for_event_sync(eid), name="read_entrants(snapshot)")
            except Exception as e:
                print(f"[Rolls] read_entrants(snapshot) failed: {e}")
                entrants = []

            def gv(col: str) -> str:
                ci = self._find_header_index(headers, col)
                return (row[ci] if ci != -1 and ci < len(row) else "").strip()

            status = _safe_lower(gv("Status")) or "open"
            evt_type = _norm_type(gv("Type"))
            buyin_currency = _safe_lower(gv("BuyIn_Currency")) or "silver"
            buyin_amount = _to_int(gv("BuyIn_Amount"))
            created_at = _parse_iso_dt(gv("Created_At"))

            timer_sec = await self.get_roll_timer_seconds()
            entry_close_at = (created_at + timedelta(seconds=timer_sec)) if created_at else None

            entrant_count = len([e for e in entrants if e.get("user_id")])
            pot_total = buyin_amount * entrant_count

            snapshot = {
                "row_i": row_i,
                "headers": headers,
                "row": row,
                "status": status,
                "type": evt_type,
                "created_at": created_at,
                "buyin_currency": buyin_currency,
                "buyin_amount": buyin_amount,
                "entrant_count": entrant_count,
                "pot_total": pot_total,
                "entry_close_at": entry_close_at,
                "last_fetch_ts": time.time(),
            }

            # cache snapshot for a short TTL
            try:
                self._set_cached(key, snapshot, ttl=ttl_use)
            except Exception:
                pass

            return snapshot

    # --- 429 CIRCUIT BREAKER: backoff on rate limit ---
    def _check_read_backoff(self) -> bool:
        """True if in backoff (don't read Sheets yet). False if OK to read."""
        return time.monotonic() < self._read_backoff_until

    def _trigger_read_backoff(self) -> None:
        """Triggered on 429 error. Sets backoff window."""
        backoff_secs = max(5.0, float(self._read_backoff_base))
        if self._check_read_backoff():
            backoff_secs = min(30.0, backoff_secs * 2)
        self._read_backoff_base = backoff_secs
        # exponential backoff with jitter: cap at 30s
        self._read_backoff_until = time.monotonic() + min(30.0, backoff_secs + random.uniform(0, 2))
        print(f"[Rolls] 429 BACKOFF triggered: will skip Sheets reads for ~{min(30.0, backoff_secs):.0f}s")

    # --- EVENT STATE: in-memory tracking (entrants, pot, status) ---
    def _update_event_state(self, event_id: str, **kwargs: Any) -> None:
        """Update in-memory event state. Keys: entrants, pot_total, status, closes_at."""
        eid = str(event_id).strip()
        if eid not in self._event_state:
            self._event_state[eid] = {}
        self._event_state[eid].update(kwargs)
        self._event_state[eid]["last_update_ts"] = time.monotonic()

    def _get_event_state(self, event_id: str) -> Dict[str, Any]:
        """Get in-memory event state (or empty dict if not tracked)."""
        eid = str(event_id).strip()
        return self._event_state.get(eid, {})

    # --- DEBOUNCE: schedule one public update per event (coalesce multiple joins) ---
    async def _schedule_public_update(self, guild: discord.Guild, event_id: str, delay: float = 1.5) -> None:
        """Debounce: schedule a single public update, canceling any pending one."""
        eid = str(event_id).strip()
        
        # cancel any pending update for this event
        if eid in self._pending_public_updates:
            try:
                self._pending_public_updates[eid].cancel()
            except Exception:
                pass
        
        async def do_update():
            try:
                await asyncio.sleep(delay)
                await self._update_public_gamble_message(guild=guild, event_id=eid)
            except Exception as e:
                print(f"[Rolls] debounced public update failed for {eid}: {e}")
            finally:
                try:
                    del self._pending_public_updates[eid]
                except Exception:
                    pass
        
        task = asyncio.create_task(do_update())
        self._pending_public_updates[eid] = task

    def _stop_loot_ticker(self, event_id: str) -> None:
        eid = str(event_id).strip()
        task = self._loot_ticker_tasks.get(eid)
        if task and not task.done():
            try:
                task.cancel()
            except Exception:
                pass
        try:
            del self._loot_ticker_tasks[eid]
        except Exception:
            pass

    def _stop_loot_close_task(self, event_id: str) -> None:
        eid = str(event_id).strip()
        task = self._loot_close_tasks.get(eid)
        if task and not task.done():
            try:
                task.cancel()
            except Exception:
                pass
        try:
            del self._loot_close_tasks[eid]
        except Exception:
            pass

    def _start_loot_ticker(self, guild: discord.Guild, event_id: str) -> None:
        eid = str(event_id).strip()
        if not eid:
            return
        existing = self._loot_ticker_tasks.get(eid)
        if existing and not existing.done():
            return

        async def _run() -> None:
            try:
                while True:
                    status = await self._update_public_loot_message(guild=guild, event_id=eid)
                    if status != "open":
                        break
                    await asyncio.sleep(10)
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[Rolls] Loot ticker failed for {eid}: {e}")
            finally:
                try:
                    del self._loot_ticker_tasks[eid]
                except Exception:
                    pass

        self._loot_ticker_tasks[eid] = asyncio.create_task(_run())

    def _get_cached(self, key: str) -> Optional[Any]:
        e = self._cache.get(key)
        if e and e.valid():
            return e.value
        return None

    def _set_cached(self, key: str, val: Any, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._cache[key] = CacheEntry(val, ttl)

    # ------------- worksheet helpers -------------
    def _ensure_sheet(self):
        if self.sheet is None:
            self.sheet = open_spreadsheet()
        return self.sheet

    def _ws(self, tab_name: str):
        if not self._ensure_sheet():
            return None
        try:
            return self.sheet.worksheet(tab_name)
        except Exception as e:
            print(f"[Rolls] worksheet access failed for '{tab_name}': {e} (reopening sheet)")
            try:
                reset_spreadsheet_cache()
                self.sheet = open_spreadsheet()
                if self.sheet:
                    return self.sheet.worksheet(tab_name)
            except Exception as e2:
                print(f"[Rolls] worksheet reopen failed for '{tab_name}': {e2}")
            return None

    def _find_header_index(self, headers: List[str], name: str) -> int:
        target = name.strip().lower()
        for i, h in enumerate(headers):
            if str(h or "").strip().lower() == target:
                return i
        return -1

    def _public_message_id_column(self, headers: List[str]) -> Optional[str]:
        if self._find_header_index(headers, "Public_Message_ID") != -1:
            return "Public_Message_ID"
        if self._find_header_index(headers, "Message_ID") != -1:
            return "Message_ID"
        return None

    def _get_public_message_id(self, headers: List[str], row: List[str]) -> str:
        for col in ("Public_Message_ID", "Message_ID"):
            idx = self._find_header_index(headers, col)
            if idx != -1 and idx < len(row):
                val = str(row[idx] or "").strip()
                if val:
                    return val
        return ""

    def _headers_sync(self, tab: str) -> List[str]:
        ws = self._ws(tab)
        if not ws:
            return []
        return ws.row_values(1)

    async def headers(self, tab: str) -> List[str]:
        key = f"hdr:{tab}"
        cached = self._get_cached(key)
        if cached is not None:
            return cached
        headers = await _retry_to_thread(lambda: self._headers_sync(tab), name=f"headers({tab})")
        self._set_cached(key, headers, ttl=60)
        return headers

    # ------------- permissions (timer + fee) -------------
    def _get_command_cell_sync(self, slash_command: str, column_letter: str) -> str:
        ws = self._ws(PERMISSIONS_SHEET)
        if not ws:
            return ""
        data = ws.get_all_values()
        if not data or len(data) < 2:
            return ""
        target = (slash_command or "").strip().lower()
        col_idx = ord(column_letter.upper()) - ord("A")
        if col_idx < 0:
            return ""
        for row in data[1:]:
            cmd = (row[0] if len(row) > 0 else "").strip().lower()
            if cmd == target:
                return (row[col_idx] if len(row) > col_idx else "").strip()
        return ""

    async def get_roll_timer_seconds(self) -> int:
        # default 120 if blank/invalid
        key = "perm:roll_timer"
        cached = self._get_cached(key)
        if cached is not None:
            return int(cached)
        raw = await _retry_to_thread(lambda: self._get_command_cell_sync(ROLL_COMMAND, ROLL_TIMER_COL_LETTER), name="get_roll_timer")
        sec = _to_int(raw)
        if sec <= 0:
            sec = 120
        sec = _clamp(sec, 10, 36000)
        self._set_cached(key, sec, ttl=30)
        return sec

    async def get_gamble_fee_pct(self) -> float:
        key = "perm:gamble_fee"
        cached = self._get_cached(key)
        if cached is not None:
            return float(cached)
        raw = await _retry_to_thread(lambda: self._get_command_cell_sync(ROLL_COMMAND, GAMBLE_FEE_COL_LETTER), name="get_gamble_fee")
        pct = _parse_percent(raw)
        # clamp 0..0.95
        pct = max(0.0, min(0.95, pct))
        self._set_cached(key, pct, ttl=30)
        return pct

    # ------------- currency meta (emojis) -------------
    def _read_currency_meta_sync(self) -> Dict[str, Dict[str, str]]:
        ws = self._ws(CURRENCY_SHEET)
        if not ws:
            return {}
        data = ws.get_all_values()
        if not data or len(data) < 2:
            return {}
        out: Dict[str, Dict[str, str]] = {}
        for row in data[1:]:
            ctype = (row[0] if len(row) > 0 else "").strip()
            emoji = (row[1] if len(row) > 1 else "").strip()
            emoji_id = (row[2] if len(row) > 2 else "").strip()
            if not ctype:
                continue
            name = emoji.strip().strip(":").strip()
            render = ""
            if name and str(emoji_id).isdigit():
                render = f"<:{name}:{emoji_id}>"
            out[_safe_lower(ctype)] = {"name": ctype, "emoji": render}
        return out

    async def currency_meta(self) -> Dict[str, Dict[str, str]]:
        cached = self._get_cached("currency_meta")
        if cached is not None:
            return cached
        meta = await _retry_to_thread(self._read_currency_meta_sync, name="currency_meta")
        self._set_cached("currency_meta", meta, ttl=120)
        return meta

    async def format_currency(self, amount: int, currency: str) -> str:
        cur = _safe_lower(currency)
        label = cur.title() if cur else str(currency).strip()
        meta = await self.currency_meta()
        emoji = meta.get(cur, {}).get("emoji") if cur else ""
        if emoji:
            return f"{emoji} {amount} {label}"
        return f"{amount} {label}"

    async def format_wallet(self, wallet: Dict[str, int]) -> str:
        meta = await self.currency_meta()

        def _fmt(amount: int, cur: str) -> str:
            label = cur.title()
            emoji = meta.get(cur, {}).get("emoji") or ""
            if emoji:
                return f"{emoji} {amount} {label}"
            return f"{amount} {label}"

        return ", ".join([
            _fmt(int(wallet.get("gold", 0)), "gold"),
            _fmt(int(wallet.get("silver", 0)), "silver"),
            _fmt(int(wallet.get("copper", 0)), "copper"),
        ])

    # ------------- bank ops -------------
    def _ensure_bank_row_sync(self, user_id: str) -> Tuple[int, List[str]]:
        ws = self._ws(BANK_SHEET)
        if not ws:
            raise RuntimeError("Bank sheet not available")
        headers = ws.row_values(1)
        if not headers:
            ws.append_row(["User ID", "Gold", "Silver", "Copper", "Last_Collected"], value_input_option="RAW")
            headers = ws.row_values(1)

        idx_uid = self._find_header_index(headers, "User ID")
        if idx_uid == -1:
            raise RuntimeError("Bank sheet missing 'User ID' header")

        col = ws.col_values(idx_uid + 1)
        target = user_id.strip()
        for i, v in enumerate(col[1:], start=2):
            if str(v).strip() == target:
                return i, headers

        new_row = [""] * len(headers)
        new_row[idx_uid] = target
        for col_name in ("Gold", "Silver", "Copper"):
            ci = self._find_header_index(headers, col_name)
            if ci != -1:
                new_row[ci] = "0"
        ws.append_row(new_row, value_input_option="RAW")

        col = ws.col_values(idx_uid + 1)
        for i, v in enumerate(col[1:], start=2):
            if str(v).strip() == target:
                return i, headers

        raise RuntimeError("Failed to create bank row")

    def _read_wallet_sync(self, user_id: str) -> Dict[str, int]:
        ws = self._ws(BANK_SHEET)
        if not ws:
            return {"gold": 0, "silver": 0, "copper": 0}
        headers = ws.row_values(1)
        if not headers:
            return {"gold": 0, "silver": 0, "copper": 0}

        idx_uid = self._find_header_index(headers, "User ID")
        idx_g = self._find_header_index(headers, "Gold")
        idx_s = self._find_header_index(headers, "Silver")
        idx_c = self._find_header_index(headers, "Copper")
        if idx_uid == -1:
            return {"gold": 0, "silver": 0, "copper": 0}

        col = ws.col_values(idx_uid + 1)
        target = user_id.strip()
        row_i = None
        for i, v in enumerate(col[1:], start=2):
            if str(v).strip() == target:
                row_i = i
                break
        if not row_i:
            return {"gold": 0, "silver": 0, "copper": 0}

        row = ws.row_values(row_i)
        return {
            "gold": _to_int(row[idx_g] if idx_g != -1 and idx_g < len(row) else 0),
            "silver": _to_int(row[idx_s] if idx_s != -1 and idx_s < len(row) else 0),
            "copper": _to_int(row[idx_c] if idx_c != -1 and idx_c < len(row) else 0),
        }

    def _append_bank_log_sync(self, row: List[Any]) -> None:
        ws = self._ws(BANK_LOGS_SHEET)
        if not ws:
            return
        ws.append_row(row, value_input_option="RAW")

    def _apply_bank_delta_sync(
        self,
        user_id: str,
        delta: Dict[str, int],
        *,
        action: str,
        event_id: str,
        source: str,
        meta: Dict[str, Any],
    ) -> Dict[str, int]:
        ws = self._ws(BANK_SHEET)
        if not ws:
            raise RuntimeError("Bank sheet not available")

        row_i, headers = self._ensure_bank_row_sync(user_id)

        idx_g = self._find_header_index(headers, "Gold")
        idx_s = self._find_header_index(headers, "Silver")
        idx_c = self._find_header_index(headers, "Copper")

        wallet = self._read_wallet_sync(user_id)
        new_wallet = {
            "gold": wallet["gold"] + int(delta.get("gold", 0)),
            "silver": wallet["silver"] + int(delta.get("silver", 0)),
            "copper": wallet["copper"] + int(delta.get("copper", 0)),
        }

        for k in ("gold", "silver", "copper"):
            new_wallet[k] = _clamp(new_wallet[k], 0, 10**12)

        updates = []
        if idx_g != -1:
            updates.append({"range": f"{_col_to_a1(idx_g)}{row_i}", "values": [[str(new_wallet["gold"])]]})
        if idx_s != -1:
            updates.append({"range": f"{_col_to_a1(idx_s)}{row_i}", "values": [[str(new_wallet["silver"])]]})
        if idx_c != -1:
            updates.append({"range": f"{_col_to_a1(idx_c)}{row_i}", "values": [[str(new_wallet["copper"])]]})
        if updates:
            ws.batch_update(updates, value_input_option="RAW")

        bal_after_json = json.dumps(new_wallet, ensure_ascii=False)
        log_row = [
            str(user_id),
            _now_utc_iso(),
            str(event_id),
            str(action),
            str(int(delta.get("gold", 0))),
            str(int(delta.get("silver", 0))),
            str(int(delta.get("copper", 0))),
            bal_after_json,
            str(source),
            json.dumps(meta or {}, ensure_ascii=False),
        ]
        self._append_bank_log_sync(log_row)

        return new_wallet

    async def read_wallet(self, user_id: int) -> Dict[str, int]:
        return await _retry_to_thread(lambda: self._read_wallet_sync(str(user_id)), name="read_wallet")

    async def apply_bank_delta(
        self,
        user_id: int,
        delta: Dict[str, int],
        *,
        action: str,
        event_id: str,
        source: str,
        meta: Dict[str, Any],
    ) -> Dict[str, int]:
        return await _retry_to_thread(
            lambda: self._apply_bank_delta_sync(str(user_id), delta, action=action, event_id=event_id, source=source, meta=meta),
            name=f"apply_bank_delta({action})",
        )

    # ------------- roll events / entrants -------------
    def _find_event_row_sync(self, event_id: str) -> Tuple[int, List[str], List[str]]:
        ws = self._ws(ROLL_EVENTS_SHEET)
        if not ws:
            raise RuntimeError("Roll_Events sheet not available")
        data = ws.get_all_values()
        if not data or len(data) < 2:
            raise RuntimeError("Roll_Events sheet empty")

        headers = data[0]
        idx_eid = self._find_header_index(headers, "Event_ID")
        if idx_eid == -1:
            raise RuntimeError("Roll_Events missing Event_ID header")

        target = str(event_id).strip()
        for i, row in enumerate(data[1:], start=2):
            if (row[idx_eid] if idx_eid < len(row) else "").strip() == target:
                return i, headers, row

        raise RuntimeError(f"Event not found: {event_id}")

    def _event_write_sync(self, row_i: int, headers: List[str], updates: Dict[str, Any]) -> None:
        ws = self._ws(ROLL_EVENTS_SHEET)
        if not ws:
            raise RuntimeError("Roll_Events sheet not available")

        batch = []
        for col_name, val in updates.items():
            ci = self._find_header_index(headers, col_name)
            if ci == -1:
                continue
            batch.append({"range": f"{_col_to_a1(ci)}{row_i}", "values": [[str(val)]]})
        if batch:
            ws.batch_update(batch, value_input_option="RAW")

    def _append_event_sync(self, row: List[Any]) -> None:
        ws = self._ws(ROLL_EVENTS_SHEET)
        if not ws:
            raise RuntimeError("Roll_Events sheet not available")
        ws.append_row(row, value_input_option="RAW")

    def _append_entrant_sync(self, row: List[Any]) -> None:
        ws = self._ws(ROLL_ENTRANTS_SHEET)
        if not ws:
            raise RuntimeError("Roll_Entrants sheet not available")
        ws.append_row(row, value_input_option="RAW")

    def _read_entrants_for_event_sync(self, event_id: str) -> List[Dict[str, Any]]:
        ws = self._ws(ROLL_ENTRANTS_SHEET)
        if not ws:
            return []
        data = ws.get_all_values()
        if not data or len(data) < 2:
            return []
        headers = data[0]
        idx_eid = self._find_header_index(headers, "Event_ID")
        idx_uid = self._find_header_index(headers, "User_ID")
        idx_join = self._find_header_index(headers, "Joined_At")
        idx_cur = self._find_header_index(headers, "Paid_Currency")
        idx_amt = self._find_header_index(headers, "Paid_Amount")
        idx_roll = self._find_header_index(headers, "Roll")

        target = str(event_id).strip()
        out: List[Dict[str, Any]] = []
        for row in data[1:]:
            if idx_eid == -1 or idx_uid == -1:
                continue
            if (row[idx_eid] if idx_eid < len(row) else "").strip() != target:
                continue
            out.append(
                {
                    "user_id": _normalize_uid_cell(row[idx_uid] if idx_uid < len(row) else ""),
                    "joined_at": (row[idx_join] if idx_join != -1 and idx_join < len(row) else "").strip(),
                    "paid_currency": _safe_lower(row[idx_cur] if idx_cur != -1 and idx_cur < len(row) else ""),
                    "paid_amount": _to_int(row[idx_amt] if idx_amt != -1 and idx_amt < len(row) else 0),
                    "roll": _to_int(row[idx_roll] if idx_roll != -1 and idx_roll < len(row) else 0),
                }
            )
        return out

    def _entrant_exists_sync(self, event_id: str, user_id: str) -> bool:
        ws = self._ws(ROLL_ENTRANTS_SHEET)
        if not ws:
            return False
        data = ws.get_all_values()
        if not data or len(data) < 2:
            return False
        headers = data[0]
        idx_eid = self._find_header_index(headers, "Event_ID")
        idx_uid = self._find_header_index(headers, "User_ID")
        if idx_eid == -1 or idx_uid == -1:
            return False
        eid_t = str(event_id).strip()
        uid_t = _normalize_uid_cell(user_id)
        for row in data[1:]:
            if (row[idx_eid] if idx_eid < len(row) else "").strip() != eid_t:
                continue
            if _normalize_uid_cell(row[idx_uid] if idx_uid < len(row) else "") == uid_t:
                return True
        return False

    def _write_rolls_for_event_sync(self, event_id: str, rolls: Dict[str, int]) -> None:
        ws = self._ws(ROLL_ENTRANTS_SHEET)
        if not ws:
            raise RuntimeError("Roll_Entrants sheet not available")
        data = ws.get_all_values()
        if not data or len(data) < 2:
            return
        headers = data[0]
        idx_eid = self._find_header_index(headers, "Event_ID")
        idx_uid = self._find_header_index(headers, "User_ID")
        idx_roll = self._find_header_index(headers, "Roll")
        if idx_eid == -1 or idx_uid == -1 or idx_roll == -1:
            raise RuntimeError("Roll_Entrants missing required headers")

        eid_t = str(event_id).strip()
        batch = []
        for row_i, row in enumerate(data[1:], start=2):
            if (row[idx_eid] if idx_eid < len(row) else "").strip() != eid_t:
                continue
            uid = _normalize_uid_cell(row[idx_uid] if idx_uid < len(row) else "")
            if not uid or uid not in rolls:
                continue
            batch.append({"range": f"{_col_to_a1(idx_roll)}{row_i}", "values": [[str(int(rolls[uid]))]]})

        if batch:
            ws.batch_update(batch, value_input_option="RAW")

    # ------------- permissions / roles -------------
    def _is_admin(self, member: discord.Member) -> bool:
        perms = member.guild_permissions
        return bool(perms.administrator or perms.manage_guild)

    def _has_any_role_name(self, member: discord.Member, role_names: List[str]) -> bool:
        if not role_names:
            return False
        want = {r.strip().lower() for r in role_names if r and r.strip()}
        if not want:
            return False
        return any((role.name or "").strip().lower() in want for role in member.roles)

    def can_create_event(self, member: discord.Member) -> bool:
        if self._is_admin(member):
            return True
        if self._has_any_role_name(member, ALLOWED_CREATOR_ROLE_NAMES):
            return True
        return not REQUIRE_ADMIN_FOR_EVENT_CREATE

    def can_manage_event(self, member: discord.Member, created_by_user_id: str) -> bool:
        if self._is_admin(member):
            return True
        return _normalize_uid_cell(created_by_user_id) == str(member.id)

    # ------------- GAMBLE: live embed updater -------------
    async def _update_public_gamble_message(self, *, guild: discord.Guild, event_id: str) -> None:
        """
        Updates the public Gamble Pot embed:
          - entrants list
          - pot total
          - entry close countdown
          - disables join when entry is closed / status not OPEN
        """
        eid = str(event_id).strip()
        if not eid:
            return

        # ANTI-429: Check if we're in a read backoff window
        if self._check_read_backoff():
            # Defer update to next debounce window
            print(f"[Rolls] Deferring public update for {eid} (in backoff)")
            asyncio.create_task(self._schedule_public_update(guild=guild, event_id=eid, delay=2.0))
            return

        try:
            row_i, headers, row = await _retry_to_thread(lambda: self._find_event_row_sync(eid), name="find_event_row(update_public)")
        except Exception as e:
            print(f"[Rolls] _update_public failed to find event {eid}: {e}")
            if "429" in str(e):
                self._trigger_read_backoff()
            return

        def gv(col: str) -> str:
            ci = self._find_header_index(headers, col)
            return (row[ci] if ci != -1 and ci < len(row) else "").strip()

        if _norm_type(gv("Type")) != "GAMBLE":
            return

        channel_id = gv("Channel_ID")
        message_id = self._get_public_message_id(headers, row)
        title = gv("Title")
        status = _safe_lower(gv("Status")) or "open"
        buyin_currency = _safe_lower(gv("BuyIn_Currency")) or "silver"
        buyin_amount = _to_int(gv("BuyIn_Amount"))
        max_entrants = _to_int(gv("Max_Entrants"))

        if status != "open":
            return status

        # Compute entry_close_at dynamically from Created_At + timer (no sheet column needed)
        created_at = _parse_iso_dt(gv("Created_At"))
        timer_sec = await self.get_roll_timer_seconds()
        entry_close_at = (created_at + timedelta(seconds=timer_sec)) if created_at else None

        # ANTI-429: Use cached state instead of re-reading entrants
        cached_state = self._get_event_state(eid)
        entrant_ids: List[str] = []
        if cached_state and "entrant_count" in cached_state:
            entrant_count = int(cached_state.get("entrant_count") or 0)
            entrant_ids = list(cached_state.get("entrant_ids") or [])
            print(f"[Rolls] Using cached entrant count ({entrant_count}) for {eid}")
        else:
            # Fallback: read fresh (but check backoff first)
            try:
                entrants = await _retry_to_thread(lambda: self._read_entrants_for_event_sync(eid), name="read_entrants(update_public)")
                entrant_ids = [e["user_id"] for e in entrants if e.get("user_id")]
                entrant_count = len(entrant_ids)
                self._update_event_state(eid, entrant_count=entrant_count, entrant_ids=entrant_ids)
            except Exception as e:
                print(f"[Rolls] Failed to read entrants for {eid}: {e}")
                if "429" in str(e):
                    self._trigger_read_backoff()
                    entrant_count = cached_state.get("entrant_count", 0) if cached_state else 0
                    entrant_ids = list(cached_state.get("entrant_ids") or []) if cached_state else []
                else:
                    return

        pot_total = max(0, buyin_amount) * entrant_count

        fee_pct = await self.get_gamble_fee_pct()
        fee_amount = int(pot_total * fee_pct)
        payout_est = max(0, pot_total - fee_amount)

        # build entrants lines (mentions)
        lines: List[str] = []
        for uid in entrant_ids[:25]:
            lines.append(f"<@{uid}>")
        if entrant_ids and entrant_count > 25:
            lines.append(f"+ {entrant_count - 25} more")

        if lines:
            entrants_txt = "\n".join(lines)
        else:
            entrants_txt = f"{entrant_count} entrants" if entrant_count else "—"

        buyin_txt = await self.format_currency(buyin_amount, buyin_currency)

        # decide if entry is closed
        now = _now_utc()
        entry_closed = False
        if entry_close_at is not None and now >= entry_close_at:
            entry_closed = True

        # Update embed
        public = discord.Embed(title="🎰 Gamble Pot Event", color=0xD4AF37)
        if title:
            public.add_field(name="Title", value=title, inline=False)

        public.add_field(name="Buy-In", value=buyin_txt, inline=True)

        # status label
        status_label = "**OPEN**" if (status == "open" and not entry_closed) else ("**ENTRY CLOSED**" if status == "open" else f"**{status.upper()}**")
        public.add_field(name="Status", value=status_label, inline=True)

        if max_entrants:
            public.add_field(name="Max Entrants", value=str(max_entrants), inline=True)

        public.add_field(name="Event ID", value=f"`{eid}`", inline=False)

        # Show two countdown fields: Entry Closes In + Event Closes In (same deadline)
        if entry_close_at is not None:
            if entry_closed:
                public.add_field(name="Entry Closes In", value="**Entry Closed**", inline=True)
                public.add_field(name="Event Closes In", value="**Event Closed**", inline=True)
            else:
                countdown = _fmt_countdown_mmss(entry_close_at)
                public.add_field(name="Entry Closes In", value=f"**{countdown}**", inline=True)
                public.add_field(name="Event Closes In", value=f"**{countdown}**", inline=True)

        public.add_field(name="Entrants", value=entrants_txt, inline=False)

        if fee_pct > 0.0:
            pct_disp = f"{int(round(fee_pct * 100))}%"
            pot_txt = await self.format_currency(pot_total, buyin_currency)
            fee_txt = await self.format_currency(fee_amount, buyin_currency)
            payout_txt = await self.format_currency(payout_est, buyin_currency)
            public.add_field(name="Pot Total", value=f"**{pot_txt}**", inline=True)
            public.add_field(name="Fee", value=f"{pct_disp} (≈ {fee_txt})", inline=True)
            public.add_field(name="Est. Payout", value=f"**{payout_txt}**", inline=True)
        else:
            pot_txt = await self.format_currency(pot_total, buyin_currency)
            public.add_field(name="Pot Total", value=f"**{pot_txt}**", inline=True)

        public.set_footer(text="Winner takes the pot. Event auto-closes when timer expires.")

        # fetch message and edit
        if not channel_id or not message_id:
            return

        ch = guild.get_channel(int(channel_id))
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return
        try:
            msg = await ch.fetch_message(int(message_id))
        except Exception:
            return

        # disable join if entry closed or not open
        if status != "open" or entry_closed:
            await msg.edit(embed=public, view=PublicJoinView(self, eid, disabled=True))
        else:
            await msg.edit(embed=public, view=PublicJoinView(self, eid))

    async def _update_public_loot_message(self, *, guild: discord.Guild, event_id: str) -> str:
        """
        Updates the public Loot Roll embed:
          - entrants list + count
          - pot total + fee + 2nd place payout
          - status
        Returns the normalized status ("open"/"closed"/"canceled").
        """
        eid = str(event_id).strip()
        if not eid:
            return "closed"

        if self._check_read_backoff():
            print(f"[Rolls] Deferring loot update for {eid} (in backoff)")
            return "open"

        try:
            _row_i, headers, row = await _retry_to_thread(lambda: self._find_event_row_sync(eid), name="find_event_row(update_loot)")
        except Exception as e:
            print(f"[Rolls] _update_public_loot failed to find event {eid}: {e}")
            if "429" in str(e):
                self._trigger_read_backoff()
            return "closed"

        def gv(col: str) -> str:
            ci = self._find_header_index(headers, col)
            return (row[ci] if ci != -1 and ci < len(row) else "").strip()

        if _norm_type(gv("Type")) != "LOOT":
            return "closed"

        channel_id = gv("Channel_ID")
        message_id = self._get_public_message_id(headers, row)
        title = gv("Title")
        status = _safe_lower(gv("Status")) or "open"
        buyin_currency = _safe_lower(gv("BuyIn_Currency")) or "silver"
        buyin_amount = _to_int(gv("BuyIn_Amount"))
        max_entrants = _to_int(gv("Max_Entrants"))

        cached_state = self._get_event_state(eid)
        entrant_ids: List[str] = []
        if cached_state and "entrant_count" in cached_state:
            entrant_count = int(cached_state.get("entrant_count") or 0)
            entrant_ids = list(cached_state.get("entrant_ids") or [])
        else:
            try:
                entrants = await _retry_to_thread(lambda: self._read_entrants_for_event_sync(eid), name="read_entrants(update_loot)")
                entrant_ids = [e["user_id"] for e in entrants if e.get("user_id")]
                entrant_count = len(entrant_ids)
                self._update_event_state(eid, entrant_count=entrant_count, entrant_ids=entrant_ids)
            except Exception as e:
                print(f"[Rolls] Failed to read entrants for {eid}: {e}")
                if "429" in str(e):
                    self._trigger_read_backoff()
                    entrant_count = cached_state.get("entrant_count", 0) if cached_state else 0
                    entrant_ids = list(cached_state.get("entrant_ids") or []) if cached_state else []
                else:
                    return status

        pot_total = max(0, buyin_amount) * entrant_count
        fee_pct = await self.get_gamble_fee_pct()
        fee_amount = int(pot_total * fee_pct)
        second_payout = max(0, pot_total - fee_amount) if entrant_count >= 2 else 0

        # build entrants lines (mentions)
        lines: List[str] = []
        for uid in entrant_ids[:25]:
            lines.append(f"<@{uid}>")
        if entrant_ids and entrant_count > 25:
            lines.append(f"+ {entrant_count - 25} more")

        entrants_txt = "\n".join(lines) if lines else (f"{entrant_count} entrants" if entrant_count else "—")

        buyin_txt = await self.format_currency(buyin_amount, buyin_currency)
        pot_txt = await self.format_currency(pot_total, buyin_currency)
        fee_txt = await self.format_currency(fee_amount, buyin_currency)
        second_txt = await self.format_currency(second_payout, buyin_currency)

        embed = discord.Embed(title="🧰 Loot Roll Event", color=0xD4AF37)
        if title:
            embed.add_field(name="Title", value=title, inline=False)
        embed.add_field(name="Prize", value="In-game item (provided by host)", inline=False)
        embed.add_field(name="Buy-In", value=buyin_txt, inline=True)
        embed.add_field(name="Status", value=f"**{status.upper()}**", inline=True)
        if max_entrants:
            embed.add_field(name="Max Entrants", value=str(max_entrants), inline=True)
        embed.add_field(name="Event ID", value=f"`{eid}`", inline=False)
        embed.add_field(name="Entrants", value=entrants_txt, inline=False)
        embed.add_field(name="Entrant Count", value=str(entrant_count), inline=True)
        embed.add_field(name="Pot Total", value=f"**{pot_txt}**", inline=True)
        pct_disp = f"{int(round(fee_pct * 100))}%" if fee_pct > 0 else "0%"
        embed.add_field(name="Fee", value=f"{pct_disp} (≈ {fee_txt})", inline=True)
        embed.add_field(name="2nd Place Wins", value=f"**{second_txt}**", inline=True)
        embed.set_footer(text="1st wins loot. 2nd wins the pot (minus fee).")

        if not channel_id or not message_id:
            return status

        ch = guild.get_channel(int(channel_id))
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return status
        try:
            msg = await ch.fetch_message(int(message_id))
        except Exception:
            return status

        if status == "open":
            await msg.edit(embed=embed, view=PublicJoinView(self, eid))
        else:
            await msg.edit(embed=embed, view=None)

        return status

    async def _edit_closed_loot_message(
        self,
        *,
        guild: discord.Guild,
        channel_id: str,
        message_id: str,
        title: str,
        eid: str,
        winner_uid: str,
        winner_roll: int,
        second_uid: str,
        second_roll: int,
        pot_total: int,
        fee_pct: float,
        fee_amount: int,
        pot_paid: int,
        buyin_currency: str,
        entrants: List[str],
    ) -> None:
        if not channel_id or not message_id:
            return

        ch = guild.get_channel(int(channel_id))
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return
        try:
            msg = await ch.fetch_message(int(message_id))
        except Exception:
            return

        embed = discord.Embed(title="🧰 Loot Roll Event", color=0xD4AF37)
        if title:
            embed.add_field(name="Title", value=title, inline=False)
        embed.add_field(name="Prize", value="In-game item (provided by host)", inline=False)
        embed.add_field(name="Status", value="**CLOSED**", inline=True)
        embed.add_field(name="Event ID", value=f"`{eid}`", inline=True)

        if winner_uid:
            embed.add_field(name="Winner", value=f"<@{winner_uid}> — **{winner_roll}**", inline=False)
        else:
            embed.add_field(name="Winner", value="No entrants", inline=False)

        if second_uid and pot_paid > 0:
            payout_txt = await self.format_currency(pot_paid, buyin_currency)
            embed.add_field(
                name="Second Place",
                value=f"<@{second_uid}> — **{second_roll}** (wins {payout_txt})",
                inline=False,
            )
        else:
            embed.add_field(name="Second Place", value="— (no payout)", inline=False)

        lines = [f"<@{uid}>" for uid in entrants[:25]]
        if entrants and len(entrants) > 25:
            lines.append(f"+ {len(entrants) - 25} more")
        entrants_txt = "\n".join(lines) if lines else "—"
        embed.add_field(name="Entrants", value=entrants_txt, inline=False)
        embed.add_field(name="Entrant Count", value=str(len(entrants)), inline=True)

        pot_txt = await self.format_currency(pot_total, buyin_currency)
        fee_txt = await self.format_currency(fee_amount, buyin_currency)
        embed.add_field(name="Pot Total", value=f"**{pot_txt}**", inline=True)
        pct_disp = f"{int(round(fee_pct * 100))}%" if fee_pct > 0 else "0%"
        embed.add_field(name="Fee", value=f"{pct_disp} (≈ {fee_txt})", inline=True)

        await msg.edit(embed=embed, view=None)

    async def _schedule_auto_close_loot(self, *, guild_id: int, event_id: str, entry_close_at: datetime) -> None:
        """Background timer: sleeps until entry_close_at then auto-closes the LOOT event."""
        eid = str(event_id).strip()
        if not eid:
            return

        now = _now_utc()
        delay = (entry_close_at - now).total_seconds()
        if delay > 0:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            except Exception:
                return

        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            return

        await self._auto_close_loot_event(guild=guild, event_id=eid)

    async def _auto_close_loot_event(self, *, guild: discord.Guild, event_id: str) -> None:
        """Auto-close a LOOT event: roll, pay second, update sheet + public message."""
        eid = str(event_id).strip()
        if not eid:
            return

        async with self._lock_event(eid):
            try:
                row_i, headers, row = await _retry_to_thread(
                    lambda: self._find_event_row_sync(eid), name="find_event_row(auto_close_loot)"
                )
            except Exception as e:
                if "429" in str(e):
                    self._trigger_read_backoff()
                    print(f"[Rolls] auto_close_loot: hit 429 for {eid}, rescheduling in 5s")
                    try:
                        delay_sec = self._read_backoff_base + 2.0

                        async def _retry_later() -> None:
                            await asyncio.sleep(delay_sec)
                            await self._auto_close_loot_event(guild=guild, event_id=eid)

                        asyncio.create_task(_retry_later())
                    except Exception:
                        pass
                else:
                    print(f"[Rolls] auto_close_loot: event {eid} not found: {e}")
                return

            def gv(col: str) -> str:
                ci = self._find_header_index(headers, col)
                return (row[ci] if ci != -1 and ci < len(row) else "").strip()

            status = _safe_lower(gv("Status"))
            if status != "open":
                return

            if _norm_type(gv("Type")) != "LOOT":
                return

            title = gv("Title")
            channel_id = gv("Channel_ID")
            message_id = self._get_public_message_id(headers, row)
            buyin_currency = _safe_lower(gv("BuyIn_Currency")) or "silver"
            buyin_amount = _to_int(gv("BuyIn_Amount"))

            entrants = []
            try:
                entrants = await _retry_to_thread(
                    lambda: self._read_entrants_for_event_sync(eid), name="read_entrants(auto_close_loot)"
                )
            except Exception as e:
                if "429" in str(e):
                    self._trigger_read_backoff()
                print(f"[Rolls] auto_close_loot failed to read entrants: {e}")
                return

            entrant_ids = [e.get("user_id") for e in entrants if e.get("user_id")]

            winner_uid = ""
            winner_roll = 0
            second_uid = ""
            second_roll = 0

            if entrant_ids:
                rolls: Dict[str, int] = {uid: random.randint(ROLL_MIN, ROLL_MAX) for uid in entrant_ids}
                tiebreak_rounds = 0
                while True:
                    max_roll = max(rolls.values())
                    tied = [uid for uid, r in rolls.items() if r == max_roll]
                    if len(tied) <= 1:
                        break
                    tiebreak_rounds += 1
                    for uid in tied:
                        rolls[uid] = random.randint(ROLL_MIN, ROLL_MAX)
                    if tiebreak_rounds >= 10:
                        break

                winner_uid = max(rolls, key=lambda u: rolls[u])
                winner_roll = rolls[winner_uid]

                if len(rolls) >= 2:
                    candidates = {uid: r for uid, r in rolls.items() if uid != winner_uid}
                    second_tiebreak_rounds = 0
                    while True:
                        max_roll = max(candidates.values())
                        tied = [uid for uid, r in candidates.items() if r == max_roll]
                        if len(tied) <= 1:
                            break
                        second_tiebreak_rounds += 1
                        for uid in tied:
                            candidates[uid] = random.randint(ROLL_MIN, ROLL_MAX)
                        if second_tiebreak_rounds >= 10:
                            break
                    second_uid = max(candidates, key=lambda u: candidates[u])
                    second_roll = candidates[second_uid]

                await _retry_to_thread(lambda: self._write_rolls_for_event_sync(eid, rolls), name="write_rolls(auto_close_loot)")

            pot_total = max(0, buyin_amount) * len(entrant_ids)
            fee_pct = await self.get_gamble_fee_pct()
            fee_amount = int(pot_total * fee_pct)
            pot_paid = max(0, pot_total - fee_amount)

            if second_uid and pot_paid > 0 and buyin_currency in ("gold", "silver", "copper"):
                async with self._lock_user(int(second_uid)):
                    delta = {"gold": 0, "silver": 0, "copper": 0}
                    delta[buyin_currency] = pot_paid
                    await self.apply_bank_delta(
                        int(second_uid),
                        delta,
                        action="LOOT_SECOND_PAYOUT",
                        event_id=eid,
                        source="roll_system",
                        meta={
                            "event_type": "LOOT",
                            "pot_total": pot_total,
                            "fee_pct": fee_pct,
                            "fee_amount": fee_amount,
                            "second_place_payout": pot_paid,
                            "currency": buyin_currency,
                            "entrants": len(entrant_ids),
                            "winner_uid": winner_uid,
                            "second_uid": second_uid,
                        },
                    )

            await _retry_to_thread(
                lambda: self._event_write_sync(
                    row_i,
                    headers,
                    {
                        "Status": "CLOSED",
                        "Close_At": _now_utc_iso(),
                        "Winner_UserID": winner_uid,
                        "Winner_Roll": str(winner_roll) if winner_uid else "",
                    },
                ),
                name="close_loot_event_write",
            )

            try:
                self.invalidate_event_cache(eid)
            except Exception:
                pass

            try:
                await self._edit_closed_loot_message(
                    guild=guild,
                    channel_id=channel_id,
                    message_id=message_id,
                    title=title,
                    eid=eid,
                    winner_uid=winner_uid,
                    winner_roll=winner_roll,
                    second_uid=second_uid,
                    second_roll=second_roll,
                    pot_total=pot_total,
                    fee_pct=fee_pct,
                    fee_amount=fee_amount,
                    pot_paid=pot_paid,
                    buyin_currency=buyin_currency,
                    entrants=entrant_ids,
                )
            except Exception as e:
                print(f"[Rolls] auto_close_loot: failed to edit public message for {eid}: {e}")

            self._stop_loot_ticker(eid)
            self._stop_loot_close_task(eid)

    async def _schedule_auto_close(self, *, guild_id: int, event_id: str, entry_close_at: datetime) -> None:
        """
        Background timer: sleeps until entry_close_at then auto-closes the GAMBLE event
        (rolls dice, pays winner, updates sheet + public message).
        """
        eid = str(event_id).strip()
        if not eid:
            return

        now = _now_utc()
        delay = (entry_close_at - now).total_seconds()
        if delay > 0:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            except Exception:
                return

        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            return

        await self._auto_close_gamble_event(guild=guild, event_id=eid)

    async def _auto_close_gamble_event(self, *, guild: discord.Guild, event_id: str) -> None:
        """Auto-close a GAMBLE event: roll, pay winner, update sheet + public message."""
        eid = str(event_id).strip()
        if not eid:
            return

        async with self._lock_event(eid):
            try:
                row_i, headers, row = await _retry_to_thread(
                    lambda: self._find_event_row_sync(eid), name="find_event_row(auto_close)"
                )
            except Exception as e:
                # ANTI-429: Log and handle 429, reschedule if needed
                if "429" in str(e):
                    self._trigger_read_backoff()
                    print(f"[Rolls] auto_close: hit 429 for {eid}, rescheduling auto-close in 5s")
                    # Re-schedule this auto-close to retry after backoff
                    try:
                        delay_sec = self._read_backoff_base + 2.0

                        async def _retry_later() -> None:
                            await asyncio.sleep(delay_sec)
                            await self._auto_close_gamble_event(guild=guild, event_id=eid)

                        asyncio.create_task(_retry_later())
                    except Exception:
                        pass
                else:
                    print(f"[Rolls] auto_close: event {eid} not found: {e}")
                return

            def gv(col: str) -> str:
                ci = self._find_header_index(headers, col)
                return (row[ci] if ci != -1 and ci < len(row) else "").strip()

            status = _safe_lower(gv("Status"))
            if status != "open":
                return  # already closed/canceled

            event_type = _norm_type(gv("Type"))
            if event_type != "GAMBLE":
                return

            title = gv("Title")
            channel_id = gv("Channel_ID")
            message_id = self._get_public_message_id(headers, row)
            buyin_currency = _safe_lower(gv("BuyIn_Currency")) or "silver"
            buyin_amount = _to_int(gv("BuyIn_Amount"))

            # ANTI-429: Try cached state first, fallback to read with error handling
            cached_state = self._get_event_state(eid)
            if cached_state and "entrant_count" in cached_state:
                entrant_ids = cached_state.get("entrant_ids", [])
                entrant_count = cached_state.get("entrant_count", 0)
                print(f"[Rolls] auto_close using cached state for {eid}: {entrant_count} entrants")
                # But we still need user_ids for rolling, so fetch if we have cached count but not IDs
                if not entrant_ids:
                    try:
                        entrants = await _retry_to_thread(
                            lambda: self._read_entrants_for_event_sync(eid), name="read_entrants(auto_close)"
                        )
                        entrant_ids = [e["user_id"] for e in entrants if e.get("user_id")]
                        entrant_count = len(entrant_ids)
                        self._update_event_state(eid, entrant_ids=entrant_ids, entrant_count=entrant_count)
                    except Exception as e:
                        if "429" in str(e):
                            self._trigger_read_backoff()
                            print(f"[Rolls] auto_close hit 429, using cached entrant_ids from state")
                            entrant_ids = cached_state.get("entrant_ids", [])
                        else:
                            print(f"[Rolls] auto_close failed to read entrants: {e}")
                            return
            else:
                try:
                    entrants = await _retry_to_thread(
                        lambda: self._read_entrants_for_event_sync(eid), name="read_entrants(auto_close)"
                    )
                    entrant_ids = [e["user_id"] for e in entrants if e.get("user_id")]
                    entrant_count = len(entrant_ids)
                    self._update_event_state(eid, entrant_ids=entrant_ids, entrant_count=entrant_count)
                except Exception as e:
                    if "429" in str(e):
                        self._trigger_read_backoff()
                    print(f"[Rolls] auto_close failed to read entrants: {e}")
                    return

            entrants = [{"user_id": uid} for uid in entrant_ids]

            if entrant_count > 0 and not entrant_ids:
                print(f"[Rolls] auto_close missing entrant IDs for {eid}; retrying later")
                try:
                    delay_sec = self._read_backoff_base + 2.0

                    async def _retry_later() -> None:
                        await asyncio.sleep(delay_sec)
                        await self._auto_close_gamble_event(guild=guild, event_id=eid)

                    asyncio.create_task(_retry_later())
                except Exception:
                    pass
                return
            if not entrants:
                # No entrants — mark CLOSED, update public message
                await _retry_to_thread(
                    lambda: self._event_write_sync(row_i, headers, {
                        "Status": "CLOSED", "Close_At": _now_utc_iso(),
                        "Winner_UserID": "", "Winner_Roll": "",
                    }),
                    name="close_empty_event",
                )
                # invalidate snapshot cache — event closed
                try:
                    self.invalidate_event_cache(eid)
                except Exception:
                    pass
                try:
                    await self._edit_closed_public_message(
                        guild=guild, channel_id=channel_id, message_id=message_id,
                        title=title, eid=eid,
                        winner_uid="", winner_roll=0, rolls={},
                        pot_total=0, fee_pct=0.0, fee_amount=0, pot_paid=0,
                        buyin_currency=buyin_currency, tiebreak_rounds=0,
                        entrant_count=0,
                    )
                except Exception:
                    pass
                print(f"[Rolls] Auto-closed {eid} (no entrants)")
                return

            # Roll each entrant 1-100
            rolls: Dict[str, int] = {
                e["user_id"]: random.randint(ROLL_MIN, ROLL_MAX)
                for e in entrants if e.get("user_id")
            }
            tiebreak_rounds = 0
            while True:
                max_roll = max(rolls.values())
                tied = [uid for uid, r in rolls.items() if r == max_roll]
                if len(tied) <= 1:
                    break
                tiebreak_rounds += 1
                for uid in tied:
                    rolls[uid] = random.randint(ROLL_MIN, ROLL_MAX)
                if tiebreak_rounds >= 10:
                    break

            winner_uid = max(rolls, key=lambda u: rolls[u])
            winner_roll = rolls[winner_uid]

            # Write rolls to sheet
            await _retry_to_thread(
                lambda: self._write_rolls_for_event_sync(eid, rolls), name="write_rolls(auto_close)"
            )

            # Calculate pot and fee
            pot_total = max(0, buyin_amount) * len(entrants)
            fee_pct = await self.get_gamble_fee_pct()
            fee_amount = int(pot_total * fee_pct)
            pot_paid = max(0, pot_total - fee_amount)

            # Pay winner
            if pot_paid > 0 and buyin_currency in ("gold", "silver", "copper"):
                async with self._lock_user(int(winner_uid)):
                    delta = {"gold": 0, "silver": 0, "copper": 0}
                    delta[buyin_currency] = pot_paid
                    await self.apply_bank_delta(
                        int(winner_uid),
                        delta,
                        action="GAMBLE_POT_PAYOUT",
                        event_id=eid,
                        source="roll_system",
                        meta={
                            "event_type": "GAMBLE",
                            "pot_total": pot_total,
                            "fee_pct": fee_pct,
                            "fee_amount": fee_amount,
                            "pot_paid": pot_paid,
                            "currency": buyin_currency,
                            "entrants": len(entrants),
                        },
                    )

            # Write CLOSED status
            await _retry_to_thread(
                lambda: self._event_write_sync(row_i, headers, {
                    "Status": "CLOSED",
                    "Close_At": _now_utc_iso(),
                    "Winner_UserID": winner_uid,
                    "Winner_Roll": str(winner_roll),
                }),
                name="close_event_write(auto_close)",
            )

            # invalidate snapshot cache — event closed
            try:
                self.invalidate_event_cache(eid)
            except Exception:
                pass

            # Update public message to CLOSED embed
            try:
                await self._edit_closed_public_message(
                    guild=guild, channel_id=channel_id, message_id=message_id,
                    title=title, eid=eid,
                    winner_uid=winner_uid, winner_roll=winner_roll, rolls=rolls,
                    pot_total=pot_total, fee_pct=fee_pct, fee_amount=fee_amount,
                    pot_paid=pot_paid, buyin_currency=buyin_currency,
                    tiebreak_rounds=tiebreak_rounds, entrant_count=len(entrants),
                )
            except Exception as e:
                print(f"[Rolls] auto_close: failed to edit public message for {eid}: {e}")

            print(f"[Rolls] Auto-closed {eid} — winner <@{winner_uid}> rolled {winner_roll}, pot paid {pot_paid} {buyin_currency}")

    async def _edit_closed_public_message(
        self, *, guild: discord.Guild, channel_id: str, message_id: str,
        title: str, eid: str,
        winner_uid: str, winner_roll: int, rolls: Dict[str, int],
        pot_total: int, fee_pct: float, fee_amount: int, pot_paid: int,
        buyin_currency: str, tiebreak_rounds: int, entrant_count: int,
    ) -> None:
        """Build the CLOSED embed and edit the public message (shared by auto-close and manual close)."""
        if not channel_id or not message_id:
            return

        ch = guild.get_channel(int(channel_id))
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return
        try:
            msg = await ch.fetch_message(int(message_id))
        except Exception:
            return

        embed = discord.Embed(title="🎰 Gamble Pot Event", color=0xD4AF37)
        if title:
            embed.add_field(name="Title", value=title, inline=False)
        embed.add_field(name="Status", value="**CLOSED**", inline=True)
        embed.add_field(name="Event ID", value=f"`{eid}`", inline=True)

        if winner_uid:
            embed.add_field(name="Winner", value=f"<@{winner_uid}> — **{winner_roll}**", inline=False)
            try:
                winner_member = guild.get_member(int(winner_uid))
                if winner_member is None:
                    winner_member = await guild.fetch_member(int(winner_uid))
                if winner_member is not None:
                    embed.set_thumbnail(url=winner_member.display_avatar.url)
            except Exception:
                pass
        else:
            embed.add_field(name="Winner", value="No entrants", inline=False)

        embed.add_field(name="Entrants", value=str(entrant_count), inline=True)

        if pot_total:
            pot_txt = await self.format_currency(pot_total, buyin_currency)
            embed.add_field(name="Pot Total", value=f"**{pot_txt}**", inline=True)
        if fee_pct > 0.0:
            pct_disp = f"{int(round(fee_pct * 100))}%"
            fee_txt = await self.format_currency(fee_amount, buyin_currency)
            embed.add_field(name="Fee", value=f"{pct_disp} (≈ {fee_txt})", inline=True)
        if pot_paid:
            paid_txt = await self.format_currency(pot_paid, buyin_currency)
            embed.add_field(name="Pot Paid", value=f"**{paid_txt}**", inline=True)

        # Top rolls
        if rolls:
            top = sorted(rolls.items(), key=lambda kv: kv[1], reverse=True)[:10]
            lines = [f"<@{uid}> — {r}" for uid, r in top]
            if tiebreak_rounds > 0:
                lines.append("")
                lines.append(f"(Tie-break rounds: {tiebreak_rounds})")
            embed.add_field(name="Top Rolls", value="\n".join(lines) if lines else "—", inline=False)

        embed.add_field(name="Entry Closes In", value="**Entry Closed**", inline=True)
        embed.add_field(name="Event Closes In", value="**Event Closed**", inline=True)

        await msg.edit(embed=embed, view=PublicJoinView(self, eid, disabled=True))

    # ----------------------------
    # Slash command: /roll
    # ----------------------------
    @app_commands.command(name="roll", description="Open the roll panel (Loot Buy-In, Gamble Pot, Free Roll)")
    async def roll_command(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            await interaction.response.send_message("Member context not available.", ephemeral=True)
            return

        view = RollMainView(cog=self, owner_id=member.id, channel_id=interaction.channel_id)
        embed = await view.build_main_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ----------------------------
# Shared join logic (Loot + Gamble)
# ----------------------------
async def join_event_flow(
    cog: RollsCog,
    *,
    member: discord.Member,
    event_id: str,
    channel_id: int,
) -> Dict[str, Any]:
    eid = str(event_id).strip()
    if not eid:
        return {"content": "Missing Event ID."}

    async with cog._lock_event(eid):
        try:
            _row_i, headers, row = await _retry_to_thread(lambda: cog._find_event_row_sync(eid), name="find_event_row")
        except Exception:
            return {"content": f"Event `{eid}` not found."}

        def gv(col: str) -> str:
            ci = cog._find_header_index(headers, col)
            return (row[ci] if ci != -1 and ci < len(row) else "").strip()

        status = _safe_lower(gv("Status")) or "open"
        if status != "open":
            return {"content": f"Event `{eid}` is not open (status: {status})."}

        event_type = _norm_type(gv("Type"))
        title = gv("Title")
        max_entrants = _to_int(gv("Max_Entrants"))

        buyin_currency = _safe_lower(gv("BuyIn_Currency")) or "silver"
        buyin_amount = _to_int(gv("BuyIn_Amount"))

        # ENTRY TIMER enforcement (GAMBLE only) — computed dynamically from Created_At + timer
        if event_type == "GAMBLE":
            created_at = _parse_iso_dt(gv("Created_At"))
            if created_at is not None:
                timer_sec = await cog.get_roll_timer_seconds()
                entry_close_at = created_at + timedelta(seconds=timer_sec)
                if _now_utc() >= entry_close_at:
                    # try to update public message to remove join button
                    try:
                        if member.guild:
                            asyncio.create_task(cog._update_public_gamble_message(guild=member.guild, event_id=eid))
                    except Exception:
                        pass
                    return {"content": f"Entry for `{eid}` is closed."}

        if buyin_amount <= 0 or buyin_currency not in ("gold", "silver", "copper"):
            return {"content": f"Event `{eid}` has invalid buy-in configuration."}

        # prevent double join
        exists = await _retry_to_thread(lambda: cog._entrant_exists_sync(eid, str(member.id)), name="entrant_exists")
        if exists:
            return {"content": f"You already joined `{eid}`."}

        # cap enforcement
        if max_entrants:
            cached_state = cog._get_event_state(eid)
            cached_count = cached_state.get("entrant_count")
            if cached_count is not None and int(cached_count) >= max_entrants:
                return {"content": f"Event `{eid}` is full ({max_entrants} max)."}
            if cached_count is None:
                entrants_now = await _retry_to_thread(
                    lambda: cog._read_entrants_for_event_sync(eid), name="read_entrants_for_cap"
                )
                try:
                    entrant_ids = [e.get("user_id") for e in entrants_now if e.get("user_id")]
                    cog._update_event_state(eid, entrant_count=len(entrant_ids), entrant_ids=entrant_ids)
                except Exception:
                    pass
                if len(entrants_now) >= max_entrants:
                    return {"content": f"Event `{eid}` is full ({max_entrants} max)."}

        # user lock to avoid money race
        async with cog._lock_user(member.id):
            wallet = await cog.read_wallet(member.id)
            if wallet.get(buyin_currency, 0) < buyin_amount:
                return {
                    "content": (
                        f"Not enough {buyin_currency} to join. "
                        f"Need {buyin_amount}, you have {wallet.get(buyin_currency, 0)}."
                    )
                }

            delta = {"gold": 0, "silver": 0, "copper": 0}
            delta[buyin_currency] = -buyin_amount

            meta = {
                "event_id": eid,
                "event_type": event_type,
                "title": title,
                "item_id": "",
                "item_name": "",
                "buyin_currency": buyin_currency,
                "buyin_amount": buyin_amount,
                "channel_id": str(channel_id),
                "joined_at": _now_utc_iso(),
            }

            new_wallet = await cog.apply_bank_delta(
                member.id,
                delta,
                action="ROLL_BUYIN" if event_type == "LOOT" else "GAMBLE_BUYIN",
                event_id=eid,
                source="roll_system",
                meta=meta,
            )

            headers_ent = await cog.headers(ROLL_ENTRANTS_SHEET)
            if not headers_ent:
                return {"content": "Roll_Entrants headers missing."}

            entrant_row = [""] * len(headers_ent)

            def setv(col: str, val: Any) -> None:
                i = cog._find_header_index(headers_ent, col)
                if i != -1:
                    entrant_row[i] = str(val)

            setv("Event_ID", eid)
            setv("User_ID", str(member.id))
            setv("Joined_At", _now_utc_iso())
            setv("Paid_Currency", buyin_currency)
            setv("Paid_Amount", buyin_amount)
            await _retry_to_thread(lambda: cog._append_entrant_sync(entrant_row), name="append_entrant")

            # update in-memory event state (count + ids)
            try:
                state = cog._get_event_state(eid)
                entrant_ids = list(state.get("entrant_ids") or [])
                uid_str = str(member.id)
                if uid_str not in entrant_ids:
                    entrant_ids.append(uid_str)
                entrant_count = int(state.get("entrant_count") or 0) + 1
                cog._update_event_state(eid, entrant_ids=entrant_ids, entrant_count=entrant_count)
            except Exception:
                pass

            # invalidate cached snapshot for this event so receipt refreshes will fetch fresh data
            try:
                cog.invalidate_event_cache(eid)
            except Exception:
                pass

        # LIVE UPDATE public embed after join
        if event_type == "GAMBLE" and member.guild:
            # ANTI-429: Use debounced update instead of immediate read
            asyncio.create_task(cog._schedule_public_update(guild=member.guild, event_id=eid, delay=1.5))
        if event_type == "LOOT" and member.guild:
            asyncio.create_task(cog._update_public_loot_message(guild=member.guild, event_id=eid))
            cog._start_loot_ticker(member.guild, eid)

        if event_type == "GAMBLE":
            label = f"**{title}**" if title else "a **Gamble Pot**"
        else:
            label = f"**{title}**" if title else "a **Loot Roll**"

        # Build ephemeral join receipt embed + view
        # compute entry close from Created_At + timer
        created_at = _parse_iso_dt(gv("Created_At"))
        timer_sec = await cog.get_roll_timer_seconds()
        entry_close_at = (created_at + timedelta(seconds=timer_sec)) if created_at else None

        # ANTI-429: Use in-memory state instead of re-reading from Sheets
        state = cog._get_event_state(eid)
        entrant_count = int(state.get("entrant_count") or 1)
        print(f"[Rolls] Using in-memory state for {eid}: entrant_count={entrant_count}")

        paid_txt = await cog.format_currency(buyin_amount, buyin_currency)

        countdown = _fmt_countdown_mmss(entry_close_at) if entry_close_at else "00:00"

        embed = discord.Embed(title=("✅ Joined Gamble Pot" if event_type == "GAMBLE" else "✅ Joined Loot Roll"), color=0x00AA00)
        embed.add_field(name="Event ID", value=f"`{eid}`", inline=False)
        embed.add_field(name="Paid", value=f"**{paid_txt}**", inline=True)
        wallet_txt = await cog.format_wallet(new_wallet)
        embed.add_field(name="Wallet (after payment)", value=wallet_txt, inline=False)
        embed.add_field(name="Entry Closes In", value=f"**{countdown}**", inline=True)

        if event_type == "GAMBLE":
            pot_total = buyin_amount * entrant_count
            fee_pct = await cog.get_gamble_fee_pct()
            fee_amount = int(pot_total * fee_pct)
            payout = max(0, pot_total - fee_amount)
            pct_disp = f"{int(round(fee_pct * 100))}%" if fee_pct > 0 else "0%"
            pot_txt = await cog.format_currency(pot_total, buyin_currency)
            fee_txt = await cog.format_currency(fee_amount, buyin_currency)
            payout_txt = await cog.format_currency(payout, buyin_currency)
            embed.add_field(name="Entrants", value=str(entrant_count), inline=True)
            embed.add_field(name="Pot Total", value=f"**{pot_txt}**", inline=True)
            embed.add_field(name="Fee", value=f"{pct_disp} (≈ {fee_txt})", inline=True)
            embed.add_field(name="You may win", value=f"**{payout_txt}**", inline=False)
        else:
            if title:
                embed.add_field(name="Title", value=title, inline=False)
            embed.add_field(name="Prize", value="In-game item (provided by host)", inline=False)
            embed.add_field(name="Rules", value="1st wins loot • 2nd wins pot (minus fee)", inline=False)
            pot_total = buyin_amount * entrant_count
            fee_pct = await cog.get_gamble_fee_pct()
            fee_amount = int(pot_total * fee_pct)
            payout = max(0, pot_total - fee_amount) if entrant_count >= 2 else 0
            payout_txt = await cog.format_currency(payout, buyin_currency)
            embed.add_field(name="2nd Place Payout (est.)", value=f"**{payout_txt}**", inline=True)

        # create interactive ephemeral view for the joiner
        receipt_view = JoinReceiptView(cog=cog, event_id=eid, member_id=member.id, entry_close_at=entry_close_at, timer_sec=timer_sec)

        # compute expiry seconds: sleep until entry_close_at + 15s + small buffer
        if entry_close_at:
            expire_seconds = int(max(0, (entry_close_at - _now_utc()).total_seconds()) + 15 + 2)
        else:
            expire_seconds = 3600

        wallet_line = await cog.format_wallet(new_wallet)
        paid_line = await cog.format_currency(buyin_amount, buyin_currency)
        return {
            "content": (
                f"Joined {label} (`{eid}`) — paid **{paid_line}**.\n"
                f"Wallet: {wallet_line}"
            ),
            "receipt_embed": embed,
            "receipt_view": receipt_view,
            "receipt_expire_seconds": expire_seconds,
        }


# ----------------------------
# Public Join View (JOIN BUTTON FIX)
# ----------------------------
class PublicJoinView(discord.ui.View):
    def __init__(self, cog: RollsCog, event_id: str, disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.event_id = str(event_id).strip()

        custom_id = f"{PUBLIC_JOIN_BUTTON_CUSTOM_ID_PREFIX}{self.event_id}"
        btn = discord.ui.Button(
            label="Join",
            style=discord.ButtonStyle.success,
            emoji="🎲",
            custom_id=custom_id,
            disabled=disabled,
        )
        btn.callback = self._on_join  # type: ignore
        self.add_item(btn)

    async def _on_join(self, interaction: discord.Interaction) -> None:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            await interaction.response.send_message("Member context not available.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # ANTI-429: Check global read backoff
        if self.cog._check_read_backoff():
            cached = self.cog._get_cached(f"evt:{self.event_id}")
            if cached:
                countdown = _fmt_countdown_mmss(cached.get("entry_close_at") or _now_utc())
                await interaction.followup.send(
                    f"System under load — using cached data.\nEntry Closes In: **{countdown}** (cached)",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send("System under load. Try again in a moment.", ephemeral=True)
            return
        result = await join_event_flow(self.cog, member=member, event_id=self.event_id, channel_id=interaction.channel_id)

        # If a receipt embed/view was returned, send it as an ephemeral followup and schedule expiry
        receipt_embed = result.get("receipt_embed")
        receipt_view = result.get("receipt_view")
        expire_seconds = int(result.get("receipt_expire_seconds") or 0)

        if receipt_embed and receipt_view:
            receipt_msg = await interaction.followup.send(embed=receipt_embed, view=receipt_view, ephemeral=True, wait=True)

            async def _expire_after():
                try:
                    if expire_seconds and expire_seconds > 0:
                        await asyncio.sleep(expire_seconds)
                    else:
                        await asyncio.sleep(3600)
                    expired = discord.Embed(title="(expired)", description="This receipt has expired.")
                    try:
                        await receipt_msg.edit(embed=expired, view=None)
                    except Exception:
                        pass
                except Exception:
                    return

            try:
                asyncio.create_task(_expire_after())
            except Exception:
                pass
        else:
            await interaction.followup.send(**result, ephemeral=True)


class JoinReceiptView(discord.ui.View):
    def __init__(self, cog: RollsCog, event_id: str, member_id: int, entry_close_at: Optional[datetime], timer_sec: int):
        # timeout slightly after close
        timeout = (timer_sec + 20) if timer_sec and timer_sec > 0 else 300.0
        super().__init__(timeout=timeout)
        self.cog = cog
        self.event_id = str(event_id).strip()
        self.member_id = int(member_id)
        self.entry_close_at = entry_close_at

        self.refresh_btn = discord.ui.Button(label="Refresh Timer", style=discord.ButtonStyle.primary)
        self.refresh_btn.callback = self._on_refresh  # type: ignore
        self.add_item(self.refresh_btn)


    async def _on_refresh(self, interaction: discord.Interaction) -> None:
        # QUICK RESPOND: check ownership
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("This receipt belongs to someone else.", ephemeral=True)
            return

        # Per-user, per-event cooldown using monotonic time
        now_ts = time.monotonic()
        cd_key = (interaction.user.id, self.event_id)
        until_ts = self.cog._refresh_cd_until.get(cd_key, 0.0)
        if now_ts < until_ts:
            remaining = int(max(1, until_ts - now_ts))
            await interaction.response.send_message(
                f"⏳ Hold up — you can refresh again in {remaining}s.",
                ephemeral=True,
            )
            return

        # Set cooldown and disable button immediately
        self.cog._refresh_cd_until[cd_key] = now_ts + REFRESH_COOLDOWN_SECONDS
        self.refresh_btn.disabled = True

        # Use cached state only (no Sheets reads)
        cached = self.cog._get_cached(f"evt:{self.event_id}") or {}
        entry_close_at = cached.get("entry_close_at") or self.entry_close_at
        status = cached.get("status") or "open"
        if entry_close_at and _now_utc() >= entry_close_at:
            status = "closed"

        countdown = _fmt_countdown_mmss(entry_close_at) if entry_close_at else "00:00"
        display = "**Entry Closed**" if status != "open" else f"**{countdown}**"

        # Update only the countdown field on the existing embed
        embed = None
        if interaction.message and interaction.message.embeds:
            embed = interaction.message.embeds[0]
        if embed is None:
            embed = discord.Embed(title="✅ Joined Roll", color=0x00AA00)

        field_updated = False
        for i, f in enumerate(embed.fields):
            if f.name == "Entry Closes In":
                embed.set_field_at(i, name=f.name, value=display, inline=f.inline)
                field_updated = True
                break
        if not field_updated:
            embed.add_field(name="Entry Closes In", value=display, inline=True)

        if status != "open":
            for c in self.children:
                if isinstance(c, discord.ui.Button):
                    c.disabled = True

        await interaction.response.edit_message(embed=embed, view=self)

        # Re-enable the same button after cooldown
        async def _reenable() -> None:
            await asyncio.sleep(REFRESH_COOLDOWN_SECONDS)
            if time.monotonic() < self.cog._refresh_cd_until.get(cd_key, 0.0):
                return
            self.refresh_btn.disabled = False
            try:
                await interaction.edit_original_response(view=self)
            except Exception:
                pass

        try:
            asyncio.create_task(_reenable())
        except Exception:
            pass



# ----------------------------
# UI Views
# ----------------------------
class RollMainView(discord.ui.View):
    def __init__(self, cog: RollsCog, owner_id: int, channel_id: int):
        super().__init__(timeout=180.0)
        self.cog = cog
        self.owner_id = owner_id
        self.channel_id = channel_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return False
        return True

    async def build_main_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🎲 Roll Hall", color=0xD4AF37)
        embed.description = "```Choose a roll mode.\nAll actions happen inside this panel.```"
        embed.add_field(
            name="Modes",
            value=(
                "🧰 **Loot Roll Buy-In** — Create/join a loot roll with a buy-in.\n"
                "🎰 **Gamble Pot (PvP/Group)** — Everyone buys in, winner takes the pot.\n"
                "🌀 **Free Roll** — Just roll 1–100 for fun."
            ),
            inline=False,
        )
        return embed

    @discord.ui.button(label="Loot Roll Buy-In", style=discord.ButtonStyle.primary, emoji="🧰")
    async def loot(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        member = interaction.user  # type: ignore
        view = LootMenuView(cog=self.cog, owner_id=self.owner_id, channel_id=self.channel_id, member=member)
        await view.refresh_embed(interaction)

    @discord.ui.button(label="Gamble Pot", style=discord.ButtonStyle.secondary, emoji="🎰")
    async def gamble(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        member = interaction.user  # type: ignore
        view = GambleMenuView(cog=self.cog, owner_id=self.owner_id, channel_id=interaction.channel_id, member=member)
        await view.refresh_embed(interaction)

    @discord.ui.button(label="Free Roll", style=discord.ButtonStyle.secondary, emoji="🌀")
    async def freeroll(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        member = interaction.user  # type: ignore
        view = FreeRollView(cog=self.cog, owner_id=self.owner_id, member=member)
        await view.refresh_embed(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🚪")
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True
        embed = discord.Embed(title="🎲 Roll Hall", color=0x808080, description="```Session ended.```")
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()


class LootMenuView(discord.ui.View):
    def __init__(self, cog: RollsCog, owner_id: int, channel_id: int, member: discord.Member):
        super().__init__(timeout=240.0)
        self.cog = cog
        self.owner_id = owner_id
        self.channel_id = channel_id
        self.member = member

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return False
        return True

    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🧰 Loot Roll Buy-In", color=0xD4AF37)
        can_create = self.cog.can_create_event(self.member)
        embed.description = "```Create or cancel loot roll events.```"
        embed.add_field(
            name="Permissions",
            value=("✅ You can create events." if can_create else "⛔ You cannot create events (creator/admin only)."),
            inline=False,
        )
        embed.add_field(
            name="Actions",
            value=(
                "• **Create Event** (set title + buy-in + max entrants)\n"
                "• **Cancel Event** (creator/admin refunds all)\n"
            ),
            inline=False,
        )
        return embed

    async def refresh_embed(self, interaction: discord.Interaction) -> None:
        embed = await self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Create Event", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def create_event(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.cog.can_create_event(self.member):
            await interaction.response.send_message("You are not allowed to create loot roll events.", ephemeral=True)
            return
        panel_message_id = interaction.message.id if interaction.message else None
        modal = CreateLootEventModal(parent=self, panel_message_id=panel_message_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Cancel Event", style=discord.ButtonStyle.danger, emoji="🧨", row=0)
    async def cancel_event(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        modal = CancelEventModal(cog=self.cog, member=self.member)
        await interaction.response.send_modal(modal)


class CreateLootEventModal(discord.ui.Modal, title="Create Loot Roll Event"):
    title_text = discord.ui.TextInput(label="Title (optional)", placeholder="Group Loot / Boss Drop / etc.", required=False, max_length=60)
    buyin_currency = discord.ui.TextInput(
        label="Buy-in Currency (gold/silver/copper)",
        placeholder="silver",
        required=True,
        max_length=10,
    )
    buyin_amount = discord.ui.TextInput(
        label="Buy-in Amount (whole number)",
        placeholder="1",
        required=True,
        max_length=10,
    )
    max_entrants = discord.ui.TextInput(label="Max Entrants (optional, 0=unlimited)", placeholder="0", required=False, max_length=4)

    def __init__(self, parent: LootMenuView, panel_message_id: Optional[int]):
        super().__init__()
        self.parent = parent
        self.panel_message_id = panel_message_id
        self.buyin_currency.default = "silver"
        self.buyin_amount.default = "1"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        parent = self.parent
        cog = parent.cog

        cur = _norm_currency(self.buyin_currency.value)
        if not cur:
            await interaction.followup.send("Invalid currency. Use gold/silver/copper.", ephemeral=True)
            return

        amt = _to_int(self.buyin_amount.value)
        if amt <= 0:
            await interaction.followup.send("Buy-in amount must be a whole number > 0.", ephemeral=True)
            return

        cap = _to_int(self.max_entrants.value)
        if cap < 0:
            cap = 0

        title_txt = (self.title_text.value or "").strip()

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        event_id = f"ROLL-{stamp}-{random.randint(100, 999)}"

        headers = await cog.headers(ROLL_EVENTS_SHEET)
        if not headers:
            await interaction.followup.send("Roll_Events sheet headers missing.", ephemeral=True)
            return

        row = [""] * len(headers)

        def setv(col: str, val: Any) -> None:
            i = cog._find_header_index(headers, col)
            if i != -1:
                row[i] = str(val)

        setv("Event_ID", event_id)
        setv("Item_ID", "")
        setv("Item_Name", "")
        setv("BuyIn_Currency", cur)
        setv("BuyIn_Amount", amt)
        setv("Status", "OPEN")
        setv("Created_By", str(parent.member.id))
        setv("Created_At", _now_utc_iso())
        setv("Channel_ID", str(parent.channel_id))
        setv("Type", "LOOT")
        setv("Title", title_txt)
        setv("Max_Entrants", str(cap) if cap else "")

        await _retry_to_thread(lambda: cog._append_event_sync(row), name="append_loot_event")

        # invalidate event snapshot cache for this new event
        try:
            cog.invalidate_event_cache(event_id)
        except Exception:
            pass

        channel = interaction.client.get_channel(parent.channel_id)  # type: ignore
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send("Could not find channel to post the public roll message.", ephemeral=True)
            return

        buyin_txt = await cog.format_currency(amt, cur)

        # schedule auto-close for loot
        try:
            timer_sec = await cog.get_roll_timer_seconds()
            entry_close_at = _now_utc() + timedelta(seconds=timer_sec)
            if interaction.guild_id:
                task = asyncio.create_task(
                    cog._schedule_auto_close_loot(guild_id=interaction.guild_id, event_id=event_id, entry_close_at=entry_close_at)
                )
                cog._loot_close_tasks[event_id] = task
        except Exception:
            pass

        public_embed = discord.Embed(title="🧰 Loot Roll Event", color=0xD4AF37)
        if title_txt:
            public_embed.add_field(name="Title", value=title_txt, inline=False)
        public_embed.add_field(name="Prize", value="In-game item (provided by host)", inline=False)
        public_embed.add_field(name="Buy-In", value=buyin_txt, inline=True)
        public_embed.add_field(name="Status", value="**OPEN**", inline=True)
        public_embed.add_field(name="Event ID", value=f"`{event_id}`", inline=False)

        join_view = PublicJoinView(cog=cog, event_id=event_id)
        msg = await channel.send(embed=public_embed, view=join_view)

        try:
            row_i, hdrs, _row = await _retry_to_thread(lambda: cog._find_event_row_sync(event_id), name="find_event_row")
            col = cog._public_message_id_column(hdrs)
            if col:
                await _retry_to_thread(lambda: cog._event_write_sync(row_i, hdrs, {col: str(msg.id)}), name="write_event_message_id")
            else:
                print(f"[Rolls] No public message id column for {event_id}")
        except Exception as e:
            print(f"[Rolls] Failed to write public message id for {event_id}: {e}")

        try:
            if interaction.guild:
                asyncio.create_task(cog._update_public_loot_message(guild=interaction.guild, event_id=event_id))
                cog._start_loot_ticker(interaction.guild, event_id)
        except Exception:
            pass

        try:
            if self.panel_message_id:
                panel_channel = interaction.client.get_channel(parent.channel_id)  # type: ignore
                if isinstance(panel_channel, (discord.TextChannel, discord.Thread)):
                    panel_msg = await panel_channel.fetch_message(int(self.panel_message_id))
                    embed = await parent.build_embed()
                    await panel_msg.edit(embed=embed, view=parent)
        except Exception:
            pass

        await interaction.followup.send("✅ Loot roll created.", ephemeral=True)


class GambleMenuView(discord.ui.View):
    def __init__(self, cog: RollsCog, owner_id: int, channel_id: int, member: discord.Member):
        super().__init__(timeout=240.0)
        self.cog = cog
        self.owner_id = owner_id
        self.channel_id = channel_id
        self.member = member

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return False
        return True

    async def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🎰 Gamble Pot (PvP / Group)", color=0xD4AF37)
        embed.description = (
            "```"
            "Everyone buys in.\n"
            "When entry closes, no more joins.\n"
            "Events auto-close when the timer ends.\n"
            "Joining happens from the event embed while entry is open.\n"
            "```"
        )
        embed.add_field(
            name="Actions",
            value=(
                "• **Create Gamble Event** (set title, buy-in, optional max entrants)\n"
                "• **Cancel Event** (creator/admin refunds all)\n"
            ),
            inline=False,
        )
        return embed

    async def refresh_embed(self, interaction: discord.Interaction) -> None:
        embed = await self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Create Gamble Event", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def create_gamble(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.cog.can_create_event(self.member):
            await interaction.response.send_message("You are not allowed to create gamble events.", ephemeral=True)
            return
        panel_message_id = interaction.message.id if interaction.message else None
        modal = CreateGambleEventModal(cog=self.cog, member=self.member, channel_id=self.channel_id, panel_message_id=panel_message_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Cancel Event", style=discord.ButtonStyle.danger, emoji="🧨", row=0)
    async def cancel_event(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        modal = CancelEventModal(cog=self.cog, member=self.member)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="⬅️", row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = RollMainView(cog=self.cog, owner_id=self.owner_id, channel_id=interaction.channel_id)
        embed = await view.build_main_embed()
        await interaction.response.edit_message(embed=embed, view=view)


class CreateGambleEventModal(discord.ui.Modal, title="Create Gamble Pot Event"):
    title_text = discord.ui.TextInput(label="Title (optional)", placeholder="Duel / High Stakes / etc.", required=False, max_length=60)
    buyin_currency = discord.ui.TextInput(label="Buy-in Currency (gold/silver/copper)", placeholder="silver", required=True, max_length=10)
    buyin_amount = discord.ui.TextInput(label="Buy-in Amount (whole number)", placeholder="10", required=True, max_length=10)
    max_entrants = discord.ui.TextInput(label="Max Entrants (optional, 0=unlimited)", placeholder="0", required=False, max_length=4)

    def __init__(self, cog: RollsCog, member: discord.Member, channel_id: int, panel_message_id: Optional[int]):
        super().__init__()
        self.cog = cog
        self.member = member
        self.channel_id = channel_id
        self.panel_message_id = panel_message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        async def _respond(msg: str) -> None:
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except Exception:
                pass

        try:
            cur = _norm_currency(self.buyin_currency.value)
            if not cur:
                await _respond("Invalid currency. Use gold/silver/copper.")
                return

            amt = _to_int(self.buyin_amount.value)
            if amt <= 0:
                await _respond("Buy-in must be > 0.")
                return

            cap = _to_int(self.max_entrants.value)
            if cap < 0:
                cap = 0

            title_txt = (self.title_text.value or "").strip()

            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            event_id = f"GMBL-{stamp}-{random.randint(100, 999)}"

            headers = await self.cog.headers(ROLL_EVENTS_SHEET)
            if not headers:
                await _respond("Roll_Events sheet headers missing.")
                return

            # pull timer + fee from Permissions sheet
            timer_sec = await self.cog.get_roll_timer_seconds()
            fee_pct = await self.cog.get_gamble_fee_pct()
            entry_close_at = _now_utc() + timedelta(seconds=timer_sec)

            row = [""] * len(headers)

            def setv(col: str, val: Any) -> None:
                i = self.cog._find_header_index(headers, col)
                if i != -1:
                    row[i] = str(val)

            setv("Event_ID", event_id)
            setv("Item_ID", "")
            setv("Item_Name", "")
            setv("BuyIn_Currency", cur)
            setv("BuyIn_Amount", amt)
            setv("Status", "OPEN")
            setv("Created_By", str(self.member.id))
            setv("Created_At", _now_utc_iso())
            setv("Channel_ID", str(self.channel_id))
            setv("Type", "GAMBLE")
            setv("Title", title_txt)
            setv("Max_Entrants", str(cap) if cap else "")

            # entry_close_at is computed dynamically from Created_At + timer (not stored in sheet)
            # write optional metadata columns if they exist
            if self.cog._find_header_index(headers, "Roll_Timer_Sec") != -1:
                setv("Roll_Timer_Sec", str(timer_sec))
            if self.cog._find_header_index(headers, "Gamble_Fee_Pct") != -1:
                setv("Gamble_Fee_Pct", f"{int(round(fee_pct * 100))}%")

            await _retry_to_thread(lambda: self.cog._append_event_sync(row), name="append_gamble_event")

            # invalidate event snapshot cache for this new event
            try:
                self.cog.invalidate_event_cache(event_id)
            except Exception:
                pass

            channel = interaction.client.get_channel(self.channel_id)  # type: ignore
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                await _respond("Could not find channel to post the public gamble event.")
                return

            buyin_txt = await self.cog.format_currency(amt, cur)

            public = discord.Embed(title="🎰 Gamble Pot Event", color=0xD4AF37)
            if title_txt:
                public.add_field(name="Title", value=title_txt, inline=False)
            public.add_field(name="Buy-In", value=buyin_txt, inline=True)
            public.add_field(name="Status", value="**OPEN**", inline=True)
            if cap:
                public.add_field(name="Max Entrants", value=str(cap), inline=True)
            public.add_field(name="Event ID", value=f"`{event_id}`", inline=False)
            countdown = _fmt_countdown_mmss(entry_close_at)
            public.add_field(name="Entry Closes In", value=f"**{countdown}**", inline=True)
            public.add_field(name="Event Closes In", value=f"**{countdown}**", inline=True)
            public.add_field(name="Entrants", value="—", inline=False)
            pot_zero_txt = await self.cog.format_currency(0, cur)
            public.add_field(name="Pot Total", value=f"**{pot_zero_txt}**", inline=True)

            if fee_pct > 0.0:
                pct_disp = f"{int(round(fee_pct * 100))}%"
                public.add_field(name="Fee", value=pct_disp, inline=True)

            public.set_footer(text="Winner takes the pot. Event auto-closes when timer expires.")

            join_view = PublicJoinView(self.cog, event_id)
            msg = await channel.send(embed=public, view=join_view)

            # write message id
            try:
                row_i, hdrs, _ = await _retry_to_thread(lambda: self.cog._find_event_row_sync(event_id), name="find_event_row")
                col = self.cog._public_message_id_column(hdrs)
                if col:
                    await _retry_to_thread(lambda: self.cog._event_write_sync(row_i, hdrs, {col: str(msg.id)}), name="write_msgid")
                else:
                    print(f"[Rolls] No public message id column for {event_id}")
            except Exception as e:
                print(f"[Rolls] Failed to write public message id for {event_id}: {e}")

            # schedule auto-close (rolls + payout when timer expires)
            try:
                task = asyncio.create_task(self.cog._schedule_auto_close(guild_id=interaction.guild_id, event_id=event_id, entry_close_at=entry_close_at))  # type: ignore
                self.cog._entry_close_tasks[event_id] = task
            except Exception:
                pass

            # do one immediate refresh so it's consistent
            try:
                if interaction.guild:
                    asyncio.create_task(self.cog._update_public_gamble_message(guild=interaction.guild, event_id=event_id))
            except Exception:
                pass

            try:
                if self.panel_message_id:
                    panel_channel = interaction.client.get_channel(self.channel_id)  # type: ignore
                    if isinstance(panel_channel, (discord.TextChannel, discord.Thread)):
                        panel_msg = await panel_channel.fetch_message(int(self.panel_message_id))
                        view = GambleMenuView(cog=self.cog, owner_id=self.member.id, channel_id=self.channel_id, member=self.member)
                        embed = await view.build_embed()
                        await panel_msg.edit(embed=embed, view=view)
            except Exception:
                pass

            await _respond("✅ Gamble pot created.")
        except Exception as e:
            print(f"[Rolls] Create gamble event failed: {e}")
            traceback.print_exc()
            await _respond("❌ Something went wrong while creating the gamble pot. Please try again or contact an admin.")


class JoinEventModal(discord.ui.Modal):
    event_id = discord.ui.TextInput(label="Event ID", placeholder="ROLL-... or GMBL-...", required=True, max_length=64)

    def __init__(self, cog: RollsCog, member: discord.Member, channel_id: int, title_text: str = "Join Event"):
        super().__init__(title=title_text)
        self.cog = cog
        self.member = member
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await join_event_flow(self.cog, member=self.member, event_id=self.event_id.value, channel_id=self.channel_id)

        receipt_embed = result.get("receipt_embed")
        receipt_view = result.get("receipt_view")
        expire_seconds = int(result.get("receipt_expire_seconds") or 0)

        if receipt_embed and receipt_view:
            receipt_msg = await interaction.followup.send(embed=receipt_embed, view=receipt_view, ephemeral=True, wait=True)

            async def _expire_after():
                try:
                    if expire_seconds and expire_seconds > 0:
                        await asyncio.sleep(expire_seconds)
                    else:
                        await asyncio.sleep(3600)
                    expired = discord.Embed(title="(expired)", description="This receipt has expired.")
                    try:
                        await receipt_msg.edit(embed=expired, view=None)
                    except Exception:
                        pass
                except Exception:
                    return

            try:
                asyncio.create_task(_expire_after())
            except Exception:
                pass
        else:
            await interaction.followup.send(**result, ephemeral=True)


class CloseEventModal(discord.ui.Modal, title="Close Event"):
    event_id = discord.ui.TextInput(label="Event ID", placeholder="ROLL-... or GMBL-...", required=True, max_length=64)

    def __init__(self, cog: RollsCog, member: discord.Member):
        super().__init__()
        self.cog = cog
        self.member = member

    async def on_submit(self, interaction: discord.Interaction) -> None:
        eid = self.event_id.value.strip()
        await interaction.response.defer(ephemeral=True)

        async with self.cog._lock_event(eid):
            try:
                row_i, headers, row = await _retry_to_thread(lambda: self.cog._find_event_row_sync(eid), name="find_event_row")
            except Exception:
                await interaction.followup.send(f"Event `{eid}` not found.", ephemeral=True)
                return

            def gv(col: str) -> str:
                ci = self.cog._find_header_index(headers, col)
                return (row[ci] if ci != -1 and ci < len(row) else "").strip()

            status = _safe_lower(gv("Status")) or "open"
            created_by = gv("Created_By")
            channel_id = gv("Channel_ID")
            message_id = self.cog._get_public_message_id(headers, row)
            event_type = _norm_type(gv("Type"))
            title = gv("Title")

            buyin_currency = _safe_lower(gv("BuyIn_Currency")) or "silver"
            buyin_amount = _to_int(gv("BuyIn_Amount"))

            if status != "open":
                await interaction.followup.send(f"Event `{eid}` is not open (status: {status}).", ephemeral=True)
                return

            if not self.cog.can_manage_event(self.member, created_by):
                await interaction.followup.send("Only the event creator or an admin can close this event.", ephemeral=True)
                return

            entrants = await _retry_to_thread(lambda: self.cog._read_entrants_for_event_sync(eid), name="read_entrants")
            if not entrants:
                await interaction.followup.send(f"No entrants found for `{eid}`. You can cancel instead.", ephemeral=True)
                return

            rolls: Dict[str, int] = {e["user_id"]: random.randint(ROLL_MIN, ROLL_MAX) for e in entrants if e.get("user_id")}
            tiebreak_rounds = 0
            while True:
                max_roll = max(rolls.values())
                tied = [uid for uid, r in rolls.items() if r == max_roll]
                if len(tied) <= 1:
                    break
                tiebreak_rounds += 1
                for uid in tied:
                    rolls[uid] = random.randint(ROLL_MIN, ROLL_MAX)
                if tiebreak_rounds >= 10:
                    break

            winner_uid = max(rolls, key=lambda u: rolls[u])
            winner_roll = rolls[winner_uid]

            await _retry_to_thread(lambda: self.cog._write_rolls_for_event_sync(eid, rolls), name="write_rolls")

            # Resolve second place for LOOT (tie-break rules same as first place)
            second_uid = ""
            second_roll = 0
            second_tiebreak_rounds = 0
            if len(rolls) >= 2:
                candidates = {uid: r for uid, r in rolls.items() if uid != winner_uid}
                if candidates:
                    while True:
                        max_roll = max(candidates.values())
                        tied = [uid for uid, r in candidates.items() if r == max_roll]
                        if len(tied) <= 1:
                            break
                        second_tiebreak_rounds += 1
                        for uid in tied:
                            new_roll = random.randint(ROLL_MIN, ROLL_MAX)
                            candidates[uid] = new_roll
                            rolls[uid] = new_roll
                        if second_tiebreak_rounds >= 10:
                            break
                    second_uid = max(candidates, key=lambda u: candidates[u])
                    second_roll = candidates[second_uid]

            pot_total = max(0, buyin_amount) * len(entrants)
            fee_pct = await self.cog.get_gamble_fee_pct()
            fee_amount = int(pot_total * fee_pct)
            pot_paid = max(0, pot_total - fee_amount)

            if event_type == "GAMBLE":
                # pay pot to winner (minus fee %)
                if pot_paid > 0 and buyin_currency in ("gold", "silver", "copper"):
                    async with self.cog._lock_user(int(winner_uid)):
                        delta = {"gold": 0, "silver": 0, "copper": 0}
                        delta[buyin_currency] = pot_paid
                        await self.cog.apply_bank_delta(
                            int(winner_uid),
                            delta,
                            action="GAMBLE_POT_PAYOUT",
                            event_id=eid,
                            source="roll_system",
                            meta={
                                "event_type": "GAMBLE",
                                "pot_total": pot_total,
                                "fee_pct": fee_pct,
                                "fee_amount": fee_amount,
                                "pot_paid": pot_paid,
                                "currency": buyin_currency,
                                "entrants": len(entrants),
                            },
                        )
            else:
                # LOOT: pay pot to second place only (if at least 2 entrants)
                if second_uid and pot_paid > 0 and buyin_currency in ("gold", "silver", "copper"):
                    async with self.cog._lock_user(int(second_uid)):
                        delta = {"gold": 0, "silver": 0, "copper": 0}
                        delta[buyin_currency] = pot_paid
                        await self.cog.apply_bank_delta(
                            int(second_uid),
                            delta,
                            action="LOOT_SECOND_PAYOUT",
                            event_id=eid,
                            source="roll_system",
                            meta={
                                "event_type": "LOOT",
                                "pot_total": pot_total,
                                "fee_pct": fee_pct,
                                "fee_amount": fee_amount,
                                "second_place_payout": pot_paid,
                                "currency": buyin_currency,
                                "entrants": len(entrants),
                                "winner_uid": winner_uid,
                                "second_uid": second_uid,
                            },
                        )

            close_at = _now_utc_iso()
            await _retry_to_thread(
                lambda: self.cog._event_write_sync(
                    row_i,
                    headers,
                    {"Status": "CLOSED", "Close_At": close_at, "Winner_UserID": winner_uid, "Winner_Roll": winner_roll},
                ),
                name="close_event_write",
            )

            # invalidate snapshot cache — event closed
            try:
                self.cog.invalidate_event_cache(eid)
            except Exception:
                pass

            edited_public = False
            try:
                if event_type == "GAMBLE" and interaction.guild:
                    await self.cog._edit_closed_public_message(
                        guild=interaction.guild, channel_id=channel_id, message_id=message_id,
                        title=title, eid=eid,
                        winner_uid=winner_uid, winner_roll=winner_roll, rolls=rolls,
                        pot_total=pot_total, fee_pct=fee_pct, fee_amount=fee_amount,
                        pot_paid=pot_paid, buyin_currency=buyin_currency,
                        tiebreak_rounds=tiebreak_rounds, entrant_count=len(entrants),
                    )
                    edited_public = True
                else:
                    ch = interaction.client.get_channel(int(channel_id)) if channel_id else None  # type: ignore
                    if isinstance(ch, (discord.TextChannel, discord.Thread)) and message_id:
                        msg = await ch.fetch_message(int(message_id))
                        embed = discord.Embed(title="🧰 Loot Roll Event", color=0xD4AF37)
                        if title:
                            embed.add_field(name="Title", value=title, inline=False)
                        embed.add_field(name="Prize", value="In-game item (provided by host)", inline=False)
                        embed.add_field(name="Status", value="**CLOSED**", inline=True)
                        embed.add_field(name="Event ID", value=f"`{eid}`", inline=True)
                        embed.add_field(name="Winner", value=f"<@{winner_uid}> — **{winner_roll}**", inline=False)

                        if second_uid:
                            payout_txt = await self.cog.format_currency(pot_paid, buyin_currency)
                            embed.add_field(name="Second Place", value=f"<@{second_uid}> — **{second_roll}** (wins {payout_txt})", inline=False)
                        else:
                            embed.add_field(name="Second Place", value="— (no payout)", inline=False)

                        entrant_ids = [e.get("user_id") for e in entrants if e.get("user_id")]
                        lines = [f"<@{uid}>" for uid in entrant_ids[:25]]
                        if entrant_ids and len(entrant_ids) > 25:
                            lines.append(f"+ {len(entrant_ids) - 25} more")
                        entrants_txt = "\n".join(lines) if lines else "—"
                        embed.add_field(name="Entrants", value=entrants_txt, inline=False)
                        embed.add_field(name="Entrant Count", value=str(len(entrant_ids)), inline=True)

                        pot_txt = await self.cog.format_currency(pot_total, buyin_currency)
                        fee_txt = await self.cog.format_currency(fee_amount, buyin_currency)
                        embed.add_field(name="Pot Total", value=f"**{pot_txt}**", inline=True)
                        pct_disp = f"{int(round(fee_pct * 100))}%" if fee_pct > 0 else "0%"
                        embed.add_field(name="Fee", value=f"{pct_disp} (≈ {fee_txt})", inline=True)

                        await msg.edit(embed=embed, view=None)
                        edited_public = True
            except Exception as e:
                print(f"[Rolls] Could not edit public message for {eid}: {e}")

            if event_type == "LOOT":
                self.cog._stop_loot_ticker(eid)

            if event_type == "LOOT":
                self.cog._stop_loot_ticker(eid)

            if event_type == "LOOT":
                self.cog._stop_loot_ticker(eid)

            if event_type == "GAMBLE":
                label = f"**{title}**" if title else "**Gamble Pot**"
                extra = ""
                if pot_total:
                    extra += f"\nPot total: **{pot_total} {buyin_currency}**"
                if fee_pct > 0.0:
                    extra += f"\nFee: **{int(round(fee_pct * 100))}%** (≈ {fee_amount} {buyin_currency})"
                if pot_paid:
                    extra += f"\nPot paid: **{pot_paid} {buyin_currency}**"
                await interaction.followup.send(
                    f"Closed `{eid}` ({label}).\nWinner: <@{winner_uid}> with **{winner_roll}**.{extra}"
                    + ("\n(Updated public post.)" if edited_public else ""),
                    ephemeral=True,
                )
            else:
                label = f"**{title}**" if title else "**Loot Roll**"
                payout_line = ""
                if second_uid and pot_paid > 0:
                    payout_txt = await self.cog.format_currency(pot_paid, buyin_currency)
                    payout_line = f"\nSecond place: <@{second_uid}> with **{second_roll}** (wins {payout_txt})."
                await interaction.followup.send(
                    f"Closed `{eid}` for {label}.\nWinner: <@{winner_uid}> with **{winner_roll}**.{payout_line}"
                    + ("\n(Updated public post.)" if edited_public else ""),
                    ephemeral=True,
                )


class CancelEventModal(discord.ui.Modal, title="Cancel Event"):
    event_id = discord.ui.TextInput(label="Event ID", placeholder="ROLL-... or GMBL-...", required=True, max_length=64)
    reason = discord.ui.TextInput(label="Reason (optional)", placeholder="Event canceled", required=False, max_length=200)

    def __init__(self, cog: RollsCog, member: discord.Member):
        super().__init__()
        self.cog = cog
        self.member = member

    async def on_submit(self, interaction: discord.Interaction) -> None:
        eid = self.event_id.value.strip()
        reason = (self.reason.value or "").strip() or "Event canceled"
        await interaction.response.defer(ephemeral=True)

        async with self.cog._lock_event(eid):
            try:
                row_i, headers, row = await _retry_to_thread(lambda: self.cog._find_event_row_sync(eid), name="find_event_row")
            except Exception:
                await interaction.followup.send(f"Event `{eid}` not found.", ephemeral=True)
                return

            def gv(col: str) -> str:
                ci = self.cog._find_header_index(headers, col)
                return (row[ci] if ci != -1 and ci < len(row) else "").strip()

            status = _safe_lower(gv("Status")) or "open"
            created_by = gv("Created_By")
            channel_id = gv("Channel_ID")
            message_id = self.cog._get_public_message_id(headers, row)
            event_type = _norm_type(gv("Type"))
            title = gv("Title")

            if status != "open":
                await interaction.followup.send(f"Event `{eid}` is not open (status: {status}).", ephemeral=True)
                return

            if not self.cog.can_manage_event(self.member, created_by):
                await interaction.followup.send("Only the event creator or an admin can cancel this event.", ephemeral=True)
                return

            entrants = await _retry_to_thread(lambda: self.cog._read_entrants_for_event_sync(eid), name="read_entrants")
            refunds = 0

            for e in entrants:
                uid = e.get("user_id")
                if not uid:
                    continue
                paid_currency = e.get("paid_currency") or ""
                paid_amount = int(e.get("paid_amount") or 0)
                if paid_currency not in ("gold", "silver", "copper") or paid_amount <= 0:
                    continue

                async with self.cog._lock_user(int(uid)):
                    delta = {"gold": 0, "silver": 0, "copper": 0}
                    delta[paid_currency] = paid_amount
                    meta = {
                        "event_id": eid,
                        "event_type": event_type,
                        "reason": reason,
                        "item_name": "In-game item (provided by host)",
                        "title": title,
                        "refund_currency": paid_currency,
                        "refund_amount": paid_amount,
                    }
                    await self.cog.apply_bank_delta(
                        int(uid),
                        delta,
                        action="ROLL_REFUND" if event_type == "LOOT" else "GAMBLE_REFUND",
                        event_id=eid,
                        source="roll_system",
                        meta=meta,
                    )
                    refunds += 1

            await _retry_to_thread(
                lambda: self.cog._event_write_sync(
                    row_i,
                    headers,
                    {"Status": "CANCELED", "Close_At": _now_utc_iso(), "Winner_UserID": "", "Winner_Roll": ""},
                ),
                name="cancel_event_write",
            )

            edited_public = False
            try:
                ch = interaction.client.get_channel(int(channel_id)) if channel_id else None  # type: ignore
                if isinstance(ch, (discord.TextChannel, discord.Thread)) and message_id:
                    msg = await ch.fetch_message(int(message_id))

                    if event_type == "GAMBLE":
                        embed = discord.Embed(title="🎰 Gamble Pot Event", color=0xD4AF37)
                        if title:
                            embed.add_field(name="Title", value=title, inline=False)
                        embed.add_field(name="Status", value="**CANCELED**", inline=True)
                        embed.add_field(name="Event ID", value=f"`{eid}`", inline=True)
                    else:
                        embed = discord.Embed(title="🧰 Loot Roll Event", color=0xD4AF37)
                        if title:
                            embed.add_field(name="Title", value=title, inline=False)
                        embed.add_field(name="Prize", value="In-game item (provided by host)", inline=False)
                        embed.add_field(name="Status", value="**CANCELED**", inline=True)
                        embed.add_field(name="Event ID", value=f"`{eid}`", inline=True)

                    embed.add_field(name="Reason", value=reason, inline=False)
                    embed.add_field(name="Refunds", value=f"{refunds} entrant(s) refunded.", inline=False)
                    if event_type == "GAMBLE":
                        await msg.edit(embed=embed, view=PublicJoinView(self.cog, eid, disabled=True))
                    else:
                        await msg.edit(embed=embed, view=None)
                    edited_public = True
            except Exception as e:
                print(f"[Rolls] Could not edit public message for {eid}: {e}")

            if event_type == "GAMBLE":
                label = f"**{title}**" if title else "**Gamble Pot**"
            else:
                label = f"**{title}**" if title else "**Loot Roll**"
            await interaction.followup.send(
                f"Canceled `{eid}` for {label}.\nRefunded **{refunds}** entrant(s)."
                + ("\n(Updated public post.)" if edited_public else ""),
                ephemeral=True,
            )


class FreeRollView(discord.ui.View):
    def __init__(self, cog: RollsCog, owner_id: int, member: discord.Member):
        super().__init__(timeout=180.0)
        self.cog = cog
        self.owner_id = owner_id
        self.member = member

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel belongs to someone else.", ephemeral=True)
            return False
        return True

    async def refresh_embed(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="🌀 Free Roll", color=0xD4AF37)
        embed.description = "```Roll 1–100. No currency changes.```"
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Roll 1–100", style=discord.ButtonStyle.success, emoji="🎲")
    async def do_roll(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        roll = random.randint(ROLL_MIN, ROLL_MAX)
        embed = discord.Embed(title="🌀 Free Roll Result", color=0xD4AF37)
        embed.description = f"```You rolled: {roll}```"
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="⬅️")
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = RollMainView(cog=self.cog, owner_id=self.owner_id, channel_id=interaction.channel_id)
        embed = await view.build_main_embed()
        await interaction.response.edit_message(embed=embed, view=view)


# ----------------------------
# setup
# ----------------------------
async def setup(bot: commands.Bot):
    await bot.add_cog(RollsCog(bot))
