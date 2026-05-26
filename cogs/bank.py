# -*- coding: utf-8 -*-
from __future__ import annotations

print("=== LOADED bank.py v4.1 (PAY_DOWN - NO AUTO-NORMALIZE) ===")

import json
import asyncio
import re
import time
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import discord
from discord import app_commands
from discord.ext import commands

from utils.google_auth import open_spreadsheet, open_worksheet

# ----------------------------
# Sheets / tabs
# ----------------------------
CURRENCY_SHEET = "Currency"
CURRENCY_RULES_SHEET = "currency_rules"
PATROLS_TOTALS_SHEET = "Patrols_User_Totals"
PATROLS_PAY_SNAPSHOTS_SHEET = "Patrols_Pay_Snapshots"
BANK_SHEET = "Bank"
BANK_LOGS_SHEET = "The bank (logs)"
NPC_SHEET = "NPC"
PERMISSIONS_SHEET = "Permissions for slash commands"
RANKS_SHEET = "Ranks"           # v4.0: For rank tier lookups

BANK_HEADERS_REQUIRED = ["User ID", "Gold", "Silver", "Copper", "Last_Collected"]
LOG_HEADERS_REQUIRED = [
    "User ID", "Timestamp (UTC)", "Event ID", "Action",
    "Gold", "Silver", "Copper", "Balance After", "Source", "Meta JSON",
]

PLACEHOLDER_TELLER_IMAGE = "https://i.imgur.com/7VqEOmH.png"

# ----------------------------
# Config
# ----------------------------
RETRY_COUNT = 3
RETRY_BASE_DELAY = 2.0
# Static/config tabs do not need minute-by-minute reads. A longer default TTL
# reduces Sheets read pressure during quest/bank bursts; user-wallet entries
# still pass shorter explicit TTLs where freshness matters.
CACHE_TTL_SECONDS = 300

LEDGER_TAIL_ROWS = 200
DEFAULT_LEDGER_LINES = 8  # permissions sheet can override

# Ledger paging UX
LEDGER_CARD_MAX = 4
LEDGER_PAGE_STEP = 3
LEDGER_PREFETCH_MIN = 30

READ_THROTTLE_MAX = 20
READ_THROTTLE_WINDOW = 60.0
WRITE_THROTTLE_MAX = 40
WRITE_THROTTLE_WINDOW = 60.0

# v4.0: Default conversion rates (fallback if sheet is missing)
# These are used if the Currency tab's conversion table section is empty or unreadable.
DEFAULT_CONVERSION_RATES: Dict[Tuple[str, str], int] = {
    ("gold", "silver"): 100,
    ("silver", "copper"): 100,
}

T = TypeVar("T")


# ----------------------------
# Helpers
# ----------------------------
def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _to_float(v: Any) -> float:
    """
    Parse a value to float, handling percentages like '1.50%'.
    Returns the decimal form: '1.50%' -> 0.015
    """
    try:
        if v is None:
            return 0.0
        s = str(v).strip()
        if s == "":
            return 0.0
        # Handle percentage notation
        if s.endswith("%"):
            s = s[:-1].strip()
            return float(s) / 100.0
        return float(s)
    except Exception:
        return 0.0


def _safe_lower(s: Any) -> str:
    return str(s).strip().lower() if s is not None else ""


def _split_csv_roles(value: str) -> List[str]:
    if not value:
        return []
    parts = [p.strip() for p in str(value).split(",")]
    return [p for p in parts if p]


def _member_has_role_name(member: discord.Member, role_name: str) -> bool:
    rn = (role_name or "").strip().lower()
    if not rn:
        return True
    return any((r.name or "").strip().lower() == rn for r in member.roles)


def _member_has_any_role(member: discord.Member, roles_csv: str) -> bool:
    roles = [r.strip().lower() for r in _split_csv_roles(roles_csv)]
    if not roles:
        return True
    return any((rr.name or "").strip().lower() in roles for rr in member.roles)


def _as_custom_emoji(name: str, emoji_id: str, fallback: str = "") -> str:
    name = (name or "").strip().strip(":").strip("<").strip(">").strip()
    emoji_id = str(emoji_id or "").strip()
    if not name or not emoji_id or not emoji_id.isdigit():
        return fallback
    return f"<:{name}:{emoji_id}>"


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _drive_to_direct_image(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if "drive.google.com/uc" in u and "id=" in u:
        return u
    m = re.search(r"/file/d/([^/]+)/", u)
    if not m:
        m2 = re.search(r"[?&]id=([^&]+)", u)
        if m2:
            file_id = m2.group(1).strip()
            return f"https://drive.google.com/uc?export=view&id={file_id}"
        return u
    file_id = m.group(1).strip()
    return f"https://drive.google.com/uc?export=view&id={file_id}"


def _col_to_a1(col_idx_0: int) -> str:
    """0-based column index -> A1 letters (A, B, ..., Z, AA, AB, ...)"""
    n = col_idx_0 + 1
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _normalize_uid_cell(v: Any) -> str:
    """
    Normalize a User ID cell into digits only.
    Handles plain digit strings, float-ish strings, scientific notation, etc.
    """
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
            as_int = int(float(s2))
            return str(as_int)
    except Exception:
        pass

    digits = re.sub(r"\D+", "", s2)
    return digits


def _is_minutes_based(rule_id: str) -> bool:
    """Return True only for rules whose delta represents minutes (duration-based stats)."""
    return _safe_lower(rule_id) == "hourly_pay"


def _format_minutes_to_hm(total_minutes: int) -> str:
    """Format minutes as 'Xh XXm' for display (e.g. 241 -> '4h 01m')."""
    h = total_minutes // 60
    m = total_minutes % 60
    return f"{h}h {m:02d}m"

def _humanize_rule_label(rule_id: Any) -> str:
    """Display-friendly rule label (e.g., 'TURRET_KILLS' -> 'Turret Kills')."""
    raw = str(rule_id or "Unknown").strip()
    if not raw:
        return "Unknown"

    words = raw.replace("_", " ").split()
    acronyms = {"FPS"}
    formatted_words = []

    for word in words:
        upper_word = word.upper()
        if upper_word in acronyms:
            formatted_words.append(upper_word)
        else:
            formatted_words.append(word.lower().capitalize())

    return " ".join(formatted_words)


def _normalize_rank_name(name: str) -> str:
    """Normalize a rank/role name for comparison.

    Steps:
      1. Strip + lowercase
      2. Remove Discord custom emoji tokens  <:name:id>  <a:name:id>
      3. Remove Unicode emojis / symbols (supplementary planes, dingbats, etc.)
      4. Remove punctuation: [](){}|•-_: and similar
      5. Collapse whitespace
    """
    s = (name or "").strip().lower()
    # Remove Discord custom emoji markup <:name:123> / <a:name:123>
    s = re.sub(r'<a?:\w+:\d+>', '', s)
    # Remove Unicode emojis and symbol blocks
    s = re.sub(
        r'[\U00010000-\U0010ffff'   # supplementary planes (most emojis)
        r'\U0000200d'               # zero-width joiner
        r'\U0000fe0f\U0000fe0e'     # variation selectors
        r'\u2600-\u27bf'            # misc symbols & dingbats
        r'\u2300-\u23ff'            # misc technical
        r'\u2b50-\u2b55'            # stars etc.
        r'\u25a0-\u25ff'            # geometric shapes
        r'\u2700-\u27bf'            # dingbats
        r'\u2900-\u297f'            # supplemental arrows
        r'\u2b00-\u2bff'            # misc symbols and arrows
        r'\u3000-\u303f'            # CJK symbols
        r'\U0001f000-\U0001ffff'    # remaining supplemental symbols
        r']+', '', s
    )
    # Remove non-alphanumeric characters except spaces
    s = re.sub(r'[^a-z0-9 ]+', '', s)
    # Collapse multiple spaces into one
    s = re.sub(r' {2,}', ' ', s).strip()
    return s


def _resolve_best_rank_from_roles(
    member: discord.Member,
    tier_map: Dict[str, int],
) -> Optional[str]:
    """
    Find the member's best rank by matching their Discord role names
    against the Ranks sheet tier map.

    Pass 1 – Exact normalized match (role name == rank name after cleanup).
    Pass 2 – Whole-word containment fallback (role name *contains* a rank
              name as a complete word boundary, e.g. "Knight (Trial)" matches
              "Knight", but "Midnight" does NOT match "Knight").

    Returns the tier_map key (lowercase) with the lowest tier number,
    or None if nothing matches.
    """
    # --- Build normalized lookup: norm_key -> (original_tier_map_key, tier) ---
    normalized_tier_map: Dict[str, Tuple[str, int]] = {}
    for rank_key, tier_val in tier_map.items():
        norm_key = _normalize_rank_name(rank_key)
        if norm_key:
            if norm_key not in normalized_tier_map or tier_val < normalized_tier_map[norm_key][1]:
                normalized_tier_map[norm_key] = (rank_key, tier_val)

    # Pre-compile whole-word patterns for each normalized rank (for pass 2)
    rank_word_patterns: Dict[str, Tuple[re.Pattern, str, int]] = {}
    for norm_key, (rank_key, tier_val) in normalized_tier_map.items():
        pattern = re.compile(r'(?:^|(?<=\s))' + re.escape(norm_key) + r'(?=\s|$)')
        rank_word_patterns[norm_key] = (pattern, rank_key, tier_val)

    # --- Collect normalized role names once ---
    role_pairs: List[Tuple[str, str]] = []  # (original, normalized)
    for role in member.roles:
        original = role.name or ""
        norm = _normalize_rank_name(original)
        if norm:
            role_pairs.append((original, norm))

    # --- Pass 1: exact normalized match ---
    best_rank: Optional[str] = None
    best_tier: Optional[int] = None

    for original, norm in role_pairs:
        match = normalized_tier_map.get(norm)
        if match:
            rank_key, tier = match
            if best_tier is None or tier < best_tier:
                best_tier = tier
                best_rank = rank_key

    if best_rank is not None:
        return best_rank

    # --- Pass 2: whole-word containment fallback ---
    for original, norm in role_pairs:
        for pattern, rank_key, tier in rank_word_patterns.values():
            if pattern.search(norm):
                if best_tier is None or tier < best_tier:
                    best_tier = tier
                    best_rank = rank_key

    if best_rank is not None:
        return best_rank

    # --- No match at all – debug log ---
    norm_roles = [n for _, n in role_pairs]
    norm_ranks = sorted(normalized_tier_map.keys())
    # Hint: find the closest near-miss via simple substring containment
    hints: List[str] = []
    for _, norm_role in role_pairs:
        for norm_rank in norm_ranks:
            if norm_rank in norm_role or norm_role in norm_rank:
                hints.append(f"'{norm_role}' ~ '{norm_rank}'")
    hint_str = "; ".join(hints[:5]) if hints else "none"
    print(
        f"[BANK DEBUG] No rank match for {member.display_name} | "
        f"roles_norm={norm_roles} | ranks_norm={norm_ranks} | closest={hint_str}"
    )

    return None


def _fmt_delta_inline(n: int) -> str:
    if n == 0:
        return "0"
    sign = "+" if n > 0 else ""
    return f"{sign}{n:,}"


def _parse_ts_to_epoch(ts_raw: Any) -> float:
    """
    Convert sheet Timestamp (UTC) cell into epoch seconds for reliable sorting.
    """
    if ts_raw is None:
        return 0.0
    s = str(ts_raw).strip()
    if not s:
        return 0.0

    s = s.replace("Z", "+00:00")

    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        pass

    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return 0.0


# ----------------------------
# v4.0: Duration Parsing
# ----------------------------
def parse_duration_to_minutes(duration_str: Any) -> int:
    """
    Parse duration strings like "41hr 15m", "2hr", "15m", "1h 30min", etc. into total MINUTES.

    Supported formats:
      - "41hr 15m" -> 2475 minutes
      - "2hr" -> 120 minutes
      - "15m" -> 15 minutes
      - "1h 30min" -> 90 minutes
      - "2hrs 5mins" -> 125 minutes
      - "90" -> 90 minutes (assume raw number is minutes)

    Returns 0 if unparseable. NO DECIMALS - always returns int.
    """
    if duration_str is None:
        return 0

    s = str(duration_str).strip().lower()
    if not s:
        return 0

    # If it's just a number, treat as minutes
    try:
        return int(float(s))
    except ValueError:
        pass

    total_minutes = 0

    # Match hours: "41hr", "2hrs", "1h", "3 hours", etc.
    # Pattern: digits followed by h/hr/hrs/hour/hours (with optional space)
    hour_pattern = r'(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b'
    hour_matches = re.findall(hour_pattern, s)
    for h in hour_matches:
        total_minutes += int(float(h) * 60)

    # Match minutes: "15m", "30min", "45mins", "5 minutes", etc.
    # Pattern: digits followed by m/min/mins/minute/minutes (with optional space)
    min_pattern = r'(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|m)\b'
    min_matches = re.findall(min_pattern, s)
    for m in min_matches:
        total_minutes += int(float(m))

    return total_minutes


# ----------------------------
# v4.0: Currency Conversion Helpers
# ----------------------------
def pay_down(
    currency_type: str,
    amount_float: float,
    rates: Dict[Tuple[str, str], int],
) -> Tuple[str, int]:
    """
    v4.1: Convert a payout to a whole number by "paying down" to lower currency if fractional.

    Rules:
    - If amount_float is already a whole number: return same currency and int(amount_float)
    - Else: multiply by conversion rate to next lower currency and repeat
    - If still fractional at Copper: floor (round down)

    Hierarchy: Gold -> Silver -> Copper

    Args:
        currency_type: Starting currency ("gold", "silver", or "copper")
        amount_float: The calculated payout amount (may have decimals)
        rates: Conversion rates dict, e.g. {("gold", "silver"): 100, ("silver", "copper"): 100}

    Returns:
        (final_currency_type, int_amount) - the currency to pay in and the whole number amount
    """
    ct = _safe_lower(currency_type)

    # Get conversion rates with fallbacks
    gold_to_silver = rates.get(("gold", "silver"), 100)
    silver_to_copper = rates.get(("silver", "copper"), 100)

    # Define the currency hierarchy for pay-down
    # Each entry: (currency_name, conversion_rate_to_next_lower)
    hierarchy = [
        ("gold", gold_to_silver),    # gold -> silver
        ("silver", silver_to_copper), # silver -> copper
        ("copper", None),             # copper is lowest, no further conversion
    ]

    # Find starting position in hierarchy
    start_idx = 0
    for i, (name, _) in enumerate(hierarchy):
        if name == ct:
            start_idx = i
            break

    current_amount = amount_float
    current_currency = ct

    # Walk down the hierarchy until we have a whole number or hit copper
    for i in range(start_idx, len(hierarchy)):
        curr_name, rate_to_next = hierarchy[i]
        current_currency = curr_name

        # Check if current amount is a whole number (within floating point tolerance)
        if current_amount == int(current_amount):
            return (current_currency, int(current_amount))

        # If we're at copper (lowest), floor it
        if rate_to_next is None:
            return (current_currency, int(current_amount))  # floor

        # Convert to next lower currency
        current_amount = current_amount * rate_to_next

    # Should not reach here, but fallback
    return (current_currency, int(current_amount))


def split_fractional_payout(
    currency_type: str,
    amount_float: float,
    rates: Dict[Tuple[str, str], int],
) -> Dict[str, int]:
    """
    Split a fractional payout into whole-number amounts across currency tiers.

    The whole part stays in the original currency. The fractional remainder is
    converted DOWN into lower currencies so no value is lost.

    Example: 13.5 gold with gold->silver=10 -> {gold: 13, silver: 5, copper: 0}
    Example: 7.35 silver with silver->copper=10 -> {silver: 7, copper: 3}  (floor at copper)
    """
    ct = _safe_lower(currency_type)
    rate_gs = rates.get(("gold", "silver"), 100)
    rate_sc = rates.get(("silver", "copper"), 100)

    result = {"gold": 0, "silver": 0, "copper": 0}

    if ct == "gold":
        whole_gold = int(amount_float)
        remainder = amount_float - whole_gold
        result["gold"] = max(whole_gold, 0)
        # Convert fractional gold -> silver
        silver_float = remainder * rate_gs
        whole_silver = int(silver_float)
        remainder_s = silver_float - whole_silver
        result["silver"] = max(whole_silver, 0)
        # Convert fractional silver -> copper
        result["copper"] = max(int(remainder_s * rate_sc), 0)
    elif ct == "silver":
        whole_silver = int(amount_float)
        remainder = amount_float - whole_silver
        result["silver"] = max(whole_silver, 0)
        # Convert fractional silver -> copper
        result["copper"] = max(int(remainder * rate_sc), 0)
    else:
        # Copper: just floor
        result["copper"] = max(int(amount_float), 0)

    return result


def normalize_up(
    payouts: Dict[str, int],
    rates: Dict[Tuple[str, str], int],
) -> Dict[str, int]:
    """
    Roll lower currencies UP into higher currencies.

    copper -> silver using silver->copper rate
    silver -> gold   using gold->silver rate

    Example with rates {gold->silver: 10, silver->copper: 10}:
      {gold: 0, silver: 3, copper: 125} -> {gold: 1, silver: 5, copper: 5}
    """
    rate_gs = rates.get(("gold", "silver"), 100)
    rate_sc = rates.get(("silver", "copper"), 100)

    gold = payouts.get("gold", 0)
    silver = payouts.get("silver", 0)
    copper = payouts.get("copper", 0)

    # Roll copper -> silver
    if rate_sc > 0 and copper >= rate_sc:
        add_silver = copper // rate_sc
        copper = copper % rate_sc
        silver += add_silver

    # Roll silver -> gold
    if rate_gs > 0 and silver >= rate_gs:
        add_gold = silver // rate_gs
        silver = silver % rate_gs
        gold += add_gold

    return {
        "gold": max(gold, 0),
        "silver": max(silver, 0),
        "copper": max(copper, 0),
    }


# ----------------------------
# Cache + Enums
# ----------------------------
class CacheEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, ttl: float):
        self.value = value
        self.expires_at = time.monotonic() + ttl

    def is_valid(self) -> bool:
        return time.monotonic() < self.expires_at


class BankPage(Enum):
    BALANCE = "balance"
    SOURCES = "sources"
    LEDGER = "ledger"
    CLOSED = "closed"


class ImageMode(Enum):
    THUMBNAIL = "thumbnail"
    FULL = "full"


NARRATIVES = {
    BankPage.BALANCE: (
        "The Teller adjusts her spectacles and pulls a heavy ledger.\n"
        "She runs an ink-stained finger down the page, then looks up.\n\n"
        "\"Ah yes, here we are. Your holdings, as they stand.\""
    ),
    BankPage.SOURCES: (
        "The Teller flips to a tabbed section marked 'WAGES DUE'.\n"
        "Dust motes dance in the candlelight as she tallies figures.\n\n"
        "\"Your efforts have not gone unnoticed. Here is what you're owed.\""
    ),
    "pay_collected": (
        "The Teller stamps the page with a satisfying thud.\n"
        "Coins clink as she counts them into a leather pouch.\n\n"
        "\"All settled. Your wages have been deposited.\""
    ),
    "pay_nothing": (
        "The Teller checks the register, then the ledger.\n"
        "She taps the empty column with her quill.\n\n"
        "\"Nothing is owed at this time.\""
    ),
    "pay_ineligible": (
        "The Teller frowns and shakes her head slowly.\n"
        "She taps a line in the regulations book.\n\n"
        "\"I'm afraid your rank does not yet qualify for wages.\""
    ),
    BankPage.LEDGER: (
        "With practiced ease, the Teller unfurls a scroll.\n"
        "The parchment crinkles softly in the counting house.\n\n"
        "\"Every coin accounted for. The ledger never lies.\""
    ),
    BankPage.CLOSED: (
        "The Teller carefully closes the heavy tome with a thud.\n"
        "She offers a polite nod as she returns the records.\n\n"
        "\"Until next time, traveler. May your purse stay full.\""
    ),
    "timeout": (
        "The Teller glances at the hourglass on her desk.\n"
        "With a soft sigh, she closes the ledger.\n\n"
        "\"It seems you've been called away. Return when ready.\""
    ),
    "auto_expired": (
        "The Teller glances at the hourglass on her desk.\n"
        "The sand has run out.\n\n"
        "\"Session expired. Use /bank to return.\""
    ),
}


def _is_rate_limit_error(e: Exception) -> bool:
    err_str = str(e).lower()
    return "429" in err_str or "quota" in err_str or "rate" in err_str


async def _retry_async_graceful(
    func: Callable[[], T],
    fallback: T,
    retries: int = RETRY_COUNT,
    base_delay: float = RETRY_BASE_DELAY,
    operation_name: str = "operation",
) -> Tuple[T, bool]:
    for attempt in range(retries):
        try:
            result = await asyncio.to_thread(func)
            return result, True
        except Exception as e:
            if attempt < retries - 1:
                delay = base_delay * ((4 if _is_rate_limit_error(e) else 2) ** attempt)
                print(
                    f"[BankCog] {operation_name} failed (attempt {attempt + 1}/{retries}): "
                    f"{type(e).__name__} - {e}. Retrying in {delay:.2f}s..."
                )
                await asyncio.sleep(delay)
            else:
                print(
                    f"[BankCog] {operation_name} failed after {retries} attempts, using fallback: "
                    f"{type(e).__name__} - {e}"
                )
                return fallback, False
    return fallback, False


async def _retry_async(
    func: Callable[[], T],
    retries: int = RETRY_COUNT,
    base_delay: float = RETRY_BASE_DELAY,
    operation_name: str = "operation",
) -> T:
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            return await asyncio.to_thread(func)
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                delay = base_delay * ((4 if _is_rate_limit_error(e) else 2) ** attempt)
                print(
                    f"[BankCog] {operation_name} failed (attempt {attempt + 1}/{retries}): "
                    f"{type(e).__name__} - {e}. Retrying in {delay:.2f}s..."
                )
                await asyncio.sleep(delay)
            else:
                print(
                    f"[BankCog] {operation_name} failed after {retries} attempts: "
                    f"{type(e).__name__} - {e}"
                )
    raise last_exc  # type: ignore


# ----------------------------
# Cog
# ----------------------------
class BankCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # LAZY: sheet is opened on first use (in a thread), not at startup
        self.sheet = None
        self._collect_locks: Dict[int, asyncio.Lock] = {}
        self._cache: Dict[str, CacheEntry] = {}

        self._read_timestamps: deque = deque()
        self._write_timestamps: deque = deque()
        self._throttle_lock = asyncio.Lock()

    async def _throttle_read(self) -> bool:
        async with self._throttle_lock:
            now = time.monotonic()
            while self._read_timestamps and (now - self._read_timestamps[0]) > READ_THROTTLE_WINDOW:
                self._read_timestamps.popleft()

            if len(self._read_timestamps) >= READ_THROTTLE_MAX:
                oldest = self._read_timestamps[0]
                wait_time = READ_THROTTLE_WINDOW - (now - oldest) + 0.5
                if wait_time > 0:
                    print(f"[BankCog] Read throttle exceeded, skipping read (would wait {wait_time:.2f}s)")
                    return False

            self._read_timestamps.append(time.monotonic())
            return True

    async def _throttle_write(self) -> bool:
        async with self._throttle_lock:
            now = time.monotonic()
            while self._write_timestamps and (now - self._write_timestamps[0]) > WRITE_THROTTLE_WINDOW:
                self._write_timestamps.popleft()

            if len(self._write_timestamps) >= WRITE_THROTTLE_MAX:
                oldest = self._write_timestamps[0]
                wait_time = WRITE_THROTTLE_WINDOW - (now - oldest) + 0.5
                if wait_time > 0:
                    print(f"[BankCog] Write throttle: waiting {wait_time:.2f}s")
                    await asyncio.sleep(wait_time)
                    now = time.monotonic()
                    while self._write_timestamps and (now - self._write_timestamps[0]) > WRITE_THROTTLE_WINDOW:
                        self._write_timestamps.popleft()

            self._write_timestamps.append(time.monotonic())
            return True

    def _get_lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._collect_locks:
            self._collect_locks[user_id] = asyncio.Lock()
        return self._collect_locks[user_id]

    def _get_cached(self, key: str) -> Optional[Any]:
        entry = self._cache.get(key)
        if entry and entry.is_valid():
            return entry.value
        return None

    def _get_cached_stale(self, key: str) -> Optional[Any]:
        """Return a cached value even after TTL expiry for quota-failure display fallback."""
        entry = self._cache.get(key)
        return entry.value if entry is not None else None

    def _set_cached(self, key: str, value: Any, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._cache[key] = CacheEntry(value, ttl)

    def _invalidate_cache(self, key: str) -> None:
        self._cache.pop(key, None)

    def _get_member_best_tier(self, member: discord.Member, tier_map: Dict[str, int]) -> Optional[int]:
        """Return the member's best (lowest number) tier from their Discord roles, or None."""
        best: Optional[int] = None
        for role in member.roles:
            tier = tier_map.get((role.name or "").strip().lower())
            if tier is not None and (best is None or tier < best):
                best = tier
        return best

    def _ensure_sheet(self):
        """Lazy-open spreadsheet on first use (runs in a thread, never at startup)."""
        if self.sheet is None:
            self.sheet = open_spreadsheet()
        return self.sheet

    def _ws(self, name: str):
        """Open a worksheet through the shared Google helper so tab objects are cached."""
        if not self._ensure_sheet():
            return None
        return open_worksheet(name)

    def _find_header_index(self, headers: List[str], name: str) -> int:
        name_l = name.strip().lower()
        for i, h in enumerate(headers):
            if str(h).strip().lower() == name_l:
                return i
        return -1

    def _find_header_index_contains(self, headers: List[str], keyword: str) -> int:
        kw = (keyword or "").strip().lower()
        if not kw:
            return -1
        for i, h in enumerate(headers):
            hl = str(h or "").strip().lower()
            if kw in hl:
                return i
        return -1

    def _find_header_index_contains_all(self, headers: List[str], keywords: List[str]) -> int:
        kws = [k.strip().lower() for k in (keywords or []) if k and str(k).strip()]
        if not kws:
            return -1
        for i, h in enumerate(headers):
            hl = str(h or "").strip().lower()
            if all(k in hl for k in kws):
                return i
        return -1

    # ----------------------------
    # v4.0: Conversion Table Reader
    # ----------------------------
    def _read_conversion_table_sync(self) -> Dict[Tuple[str, str], int]:
        """
        Read conversion rates from Currency tab columns H:J (hard-locked).
          H (idx 7): Conversion From
          I (idx 8): Conversion To
          J (idx 9): Rate

        Scans down to find the header row (where H/I/J match expected labels),
        then reads data rows until all three cells are blank.

        Returns dict like {("gold", "silver"): 10, ("silver", "copper"): 10}
        Falls back to DEFAULT_CONVERSION_RATES if missing/error.
        """
        IDX_FROM = 7   # Column H
        IDX_TO   = 8   # Column I
        IDX_RATE = 9   # Column J

        ws = self._ws(CURRENCY_SHEET)
        if not ws:
            return dict(DEFAULT_CONVERSION_RATES)

        try:
            data = ws.get_all_values()
        except Exception:
            return dict(DEFAULT_CONVERSION_RATES)

        if not data or len(data) < 2:
            return dict(DEFAULT_CONVERSION_RATES)

        # Find the header row by scanning for H/I/J labels
        header_row_idx = -1
        for i, row in enumerate(data):
            if len(row) <= IDX_RATE:
                continue
            h = _safe_lower(row[IDX_FROM])
            t = _safe_lower(row[IDX_TO])
            r = _safe_lower(row[IDX_RATE])
            if h == "conversion from" and t == "conversion to" and r == "rate":
                header_row_idx = i
                break

        if header_row_idx == -1:
            return dict(DEFAULT_CONVERSION_RATES)

        rates: Dict[Tuple[str, str], int] = {}

        for row in data[header_row_idx + 1:]:
            if len(row) <= IDX_RATE:
                break

            from_currency = _safe_lower(row[IDX_FROM])
            to_currency = _safe_lower(row[IDX_TO])
            rate_raw = _safe_lower(row[IDX_RATE])

            # Stop at first fully-blank row in H/I/J
            if not from_currency and not to_currency and not rate_raw:
                break

            rate_val = _to_int(row[IDX_RATE])
            if from_currency and to_currency and rate_val > 0:
                rates[(from_currency, to_currency)] = rate_val

        # Ensure we have the essential rates, use defaults if missing
        if ("gold", "silver") not in rates:
            rates[("gold", "silver")] = DEFAULT_CONVERSION_RATES[("gold", "silver")]
        if ("silver", "copper") not in rates:
            rates[("silver", "copper")] = DEFAULT_CONVERSION_RATES[("silver", "copper")]

        return rates

    async def fetch_conversion_rates_cached(self) -> Dict[Tuple[str, str], int]:
        """Cached fetch for conversion rates."""
        cache_key = "conversion_rates"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        stale = self._get_cached_stale(cache_key)

        if not await self._throttle_read():
            return stale or dict(DEFAULT_CONVERSION_RATES)

        result, ok = await _retry_async_graceful(
            self._read_conversion_table_sync,
            fallback=stale or dict(DEFAULT_CONVERSION_RATES),
            operation_name="fetch_conversion_rates",
        )
        if ok:
            self._set_cached(cache_key, result)
        return result

    # ----------------------------
    # v4.0: Ranks Tier Map Reader
    # ----------------------------
    def _read_ranks_tier_map_sync(self) -> Dict[str, int]:
        """
        Read rank -> tier mapping from Ranks tab.
        Expected columns:
          A: Rank Name
          D: Tier (lower number = better rank)

        Returns dict like {"knight": 5, "baron": 4, "duke": 2, ...}
        Keys are lowercase for case-insensitive matching.
        """
        ws = self._ws(RANKS_SHEET)
        if not ws:
            return {}

        try:
            data = ws.get_all_values()
        except Exception:
            return {}

        if not data or len(data) < 2:
            return {}

        headers = data[0]

        # Find "Rank Name" or similar column (usually A)
        idx_name = self._find_header_index(headers, "Rank Name")
        if idx_name == -1:
            idx_name = self._find_header_index_contains(headers, "rank")
        if idx_name == -1:
            idx_name = 0  # Fallback to column A

        # Find "Tier" column (usually D)
        idx_tier = self._find_header_index(headers, "Tier")
        if idx_tier == -1:
            idx_tier = self._find_header_index_contains(headers, "tier")
        if idx_tier == -1:
            idx_tier = 3  # Fallback to column D

        tier_map: Dict[str, int] = {}

        for row in data[1:]:
            if len(row) <= max(idx_name, idx_tier):
                continue

            rank_name = str(row[idx_name]).strip()
            tier_val = _to_int(row[idx_tier])

            if rank_name and tier_val > 0:
                tier_map[_safe_lower(rank_name)] = tier_val

        return tier_map

    async def fetch_ranks_tier_map_cached(self) -> Dict[str, int]:
        """Cached fetch for ranks tier map."""
        cache_key = "ranks_tier_map"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        stale = self._get_cached_stale(cache_key)

        if not await self._throttle_read():
            return stale or {}

        result, ok = await _retry_async_graceful(
            self._read_ranks_tier_map_sync,
            fallback=stale or {},
            operation_name="fetch_ranks_tier_map",
        )
        if ok:
            self._set_cached(cache_key, result)
        return result

    # ----------------------------
    # v4.0: Rank Multiplier Calculation
    # ----------------------------
    def _calculate_rank_multiplier(
        self,
        user_rank: Optional[str],
        base_rank: str,
        tier_map: Dict[str, int],
        multiplier_per_tier: float,
    ) -> Tuple[float, bool, str]:
        """
        Calculate the rank-based multiplier for payouts.

        Args:
            user_rank: User's best rank name (from Discord roles matched against Ranks sheet)
            base_rank: Base rank required (from Currency!F)
            tier_map: Dict of rank_name -> tier (lower = better)
            multiplier_per_tier: Percentage per tier difference (e.g., 0.015 for 1.5%)

        Returns:
            (multiplier, is_eligible, reason)
            - multiplier: 1.0 + (tier_delta * multiplier_per_tier), clamped >= 1.0
            - is_eligible: True if user can earn
            - reason: Explanation string for logging

        Eligibility: user_tier <= base_tier (lower tier number = better rank)
        """
        # If no base rank specified, everyone is eligible at 1.0x
        if not base_rank or not base_rank.strip():
            return (1.0, True, "No base rank requirement")

        base_rank_lower = _safe_lower(base_rank)
        base_tier = tier_map.get(base_rank_lower)

        if base_tier is None:
            # Base rank not found in tier map - allow at 1.0x (safe default)
            return (1.0, True, f"Base rank '{base_rank}' not found in Ranks tab, defaulting to eligible")

        # If user has no rank, they are NOT eligible (safest choice)
        # This prevents unranked users from earning currency
        if not user_rank or not user_rank.strip():
            return (0.0, False, "User has no rank assigned")

        user_rank_lower = _safe_lower(user_rank)
        user_tier = tier_map.get(user_rank_lower)

        if user_tier is None:
            # User's rank not found in tier map - NOT eligible (safest choice)
            # This handles cases where user has a role that isn't in the Ranks tab
            return (0.0, False, f"User rank '{user_rank}' not found in Ranks tab")

        # Check eligibility: user_tier must be <= base_tier (lower = better)
        if user_tier > base_tier:
            return (0.0, False, f"User tier {user_tier} > base tier {base_tier} (rank '{user_rank}' below '{base_rank}')")

        # Calculate multiplier
        # tier_delta = how many tiers BETTER than base (positive = better)
        tier_delta = base_tier - user_tier

        multiplier = 1.0 + (tier_delta * multiplier_per_tier)

        # Clamp to minimum 1.0
        if multiplier < 1.0:
            multiplier = 1.0

        reason = f"Tier delta {tier_delta} (user tier {user_tier}, base tier {base_tier}) -> {multiplier:.4f}x"
        return (multiplier, True, reason)

    # ----------------------------
    # Cached static data
    # ----------------------------
    def _read_permissions_row_sync(self, command_name: str) -> Tuple[int, int, ImageMode]:
        """
        Permissions sheet columns (0-based):
          A: Slash command
          E (idx 4): Timer (minutes)
          G (idx 6): How many lines shown on ledger
          H (idx 7): Image Thumbnail or Full
        """
        ws = self._ws(PERMISSIONS_SHEET)
        if not ws:
            return 0, DEFAULT_LEDGER_LINES, ImageMode.THUMBNAIL

        data = ws.get_all_values()
        if not data or len(data) < 2:
            return 0, DEFAULT_LEDGER_LINES, ImageMode.THUMBNAIL

        target = command_name.strip().lower()
        for row in data[1:]:
            cmd = (row[0] if len(row) > 0 else "").strip().lower()
            if cmd != target:
                continue

            raw_timer = (row[4] if len(row) > 4 else "").strip()
            raw_lines = (row[6] if len(row) > 6 else "").strip()
            raw_img = (row[7] if len(row) > 7 else "").strip()

            timer = _clamp(_to_int(raw_timer), 0, 60)
            lines = _to_int(raw_lines)
            lines = _clamp(lines, 1, 50) if lines > 0 else DEFAULT_LEDGER_LINES

            img_l = raw_img.strip().lower()
            image_mode = ImageMode.FULL if img_l.startswith("full") else ImageMode.THUMBNAIL

            return timer, lines, image_mode

        return 0, DEFAULT_LEDGER_LINES, ImageMode.THUMBNAIL

    async def fetch_permissions_cached(self, command_name: str) -> Tuple[int, int, ImageMode]:
        cache_key = f"perms_{command_name.lower()}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        if not await self._throttle_read():
            return 0, DEFAULT_LEDGER_LINES, ImageMode.THUMBNAIL

        result, ok = await _retry_async_graceful(
            lambda: self._read_permissions_row_sync(command_name),
            fallback=(0, DEFAULT_LEDGER_LINES, ImageMode.THUMBNAIL),
            operation_name=f"fetch_permissions({command_name})",
        )
        if ok:
            self._set_cached(cache_key, result)
        return result

    def _read_npc_image_sync(self) -> str:
        ws = self._ws(NPC_SHEET)
        if not ws:
            return PLACEHOLDER_TELLER_IMAGE
        data = ws.get_all_values()
        if not data or len(data) < 2:
            return PLACEHOLDER_TELLER_IMAGE
        headers = data[0]
        idx_name = self._find_header_index(headers, "NPC Name")
        idx_img = self._find_header_index(headers, "Image")
        if idx_name == -1:
            idx_name = 0
        if idx_img == -1:
            idx_img = 1
        target = "the teller"
        for row in data[1:]:
            name = (row[idx_name] if idx_name < len(row) else "").strip().lower()
            if name == target:
                raw_url = (row[idx_img] if idx_img < len(row) else "").strip()
                result = _drive_to_direct_image(raw_url)
                return result if result else PLACEHOLDER_TELLER_IMAGE
        return PLACEHOLDER_TELLER_IMAGE

    async def fetch_teller_image_cached(self) -> str:
        cached = self._get_cached("teller_image")
        if cached is not None:
            return cached
        if not await self._throttle_read():
            return PLACEHOLDER_TELLER_IMAGE
        result, ok = await _retry_async_graceful(
            self._read_npc_image_sync,
            fallback=PLACEHOLDER_TELLER_IMAGE,
            operation_name="fetch_teller_image",
        )
        if ok:
            self._set_cached("teller_image", result)
        return result

    def _read_currency_meta_sync(self) -> Dict[str, Dict[str, Any]]:
        """
        Read currency metadata from Currency tab.
        Also reads the rank multiplier (E) and base rank (F) from row 2.
        """
        ws = self._ws(CURRENCY_SHEET)
        if not ws:
            return {}
        data = ws.get_all_values()
        if not data or len(data) < 2:
            return {}

        multiplier_per_tier = 0.0  # v4.0: Per-tier multiplier (from percentage)
        base_rank = ""
        headers = data[0]
        col_E, col_F = 4, 5

        # Find the columns by header name
        idx_mult = self._find_header_index_contains(headers, "multiplier")
        idx_roles = self._find_header_index_contains(headers, "roles allowed")
        if idx_mult != -1:
            col_E = idx_mult
        if idx_roles != -1:
            col_F = idx_roles

        row2 = data[1]
        raw_mult = row2[col_E] if len(row2) > col_E else ""
        raw_roles = row2[col_F] if len(row2) > col_F else ""

        # v4.0: Parse multiplier as a percentage (e.g., "1.50%" -> 0.015)
        multiplier_per_tier = _to_float(raw_mult)

        # The base rank is stored in column F (roles allowed)
        base_rank = str(raw_roles or "").strip()

        meta: Dict[str, Dict[str, Any]] = {}
        for r in data[1:]:
            ctype = (r[0] if len(r) > 0 else "").strip()
            emoji = (r[1] if len(r) > 1 else "").strip()
            emoji_id = (r[2] if len(r) > 2 else "").strip()
            if not ctype:
                continue
            emoji_name = emoji.strip().strip(":")
            render = _as_custom_emoji(emoji_name, emoji_id)
            meta[_safe_lower(ctype)] = {
                "type": ctype,
                "emoji_name": emoji_name,
                "emoji_id": emoji_id,
                "emoji_render": render,
                "multiplier_per_tier": multiplier_per_tier,  # v4.0
                "base_rank": base_rank,                      # v4.0
            }
        return meta

    async def fetch_currency_meta_cached(self) -> Dict[str, Dict[str, Any]]:
        cached = self._get_cached("currency_meta")
        if cached is not None:
            return cached
        if not await self._throttle_read():
            return {}
        result, ok = await _retry_async_graceful(
            self._read_currency_meta_sync,
            fallback={},
            operation_name="fetch_currency_meta",
        )
        if ok:
            self._set_cached("currency_meta", result)
        return result

    def _read_rules_sync(self) -> List[Dict[str, Any]]:
        ws = self._ws(CURRENCY_RULES_SHEET)
        if not ws:
            return []
        data = ws.get_all_values()
        if not data or len(data) < 2:
            return []
        headers = data[0]
        idx = {_safe_lower(h): i for i, h in enumerate(headers)}

        def get(row: List[str], key: str) -> str:
            i = idx.get(_safe_lower(key), -1)
            return (row[i] if i != -1 and i < len(row) else "").strip()

        rules: List[Dict[str, Any]] = []
        for row in data[1:]:
            rule_id = get(row, "Rule_ID")
            if _safe_lower(rule_id) in ("", "instructions"):
                continue
            enabled_raw = _safe_lower(get(row, "Enabled"))
            enabled = enabled_raw not in ("false", "0", "no", "off")
            rules.append({
                "rule_id": rule_id,
                "stat_col": get(row, "Stat_Column"),
                "snap_col": get(row, "Snapshot_Column"),
                "currency_type": get(row, "Currency_Type"),
                "amount_per_unit": _to_int(get(row, "Amount_Per_Unit")),
                "min_role": get(row, "Min_Role"),
                "enabled": enabled,
                "notes": get(row, "Notes"),
            })
        return rules

    async def fetch_rules_cached(self) -> List[Dict[str, Any]]:
        cached = self._get_cached("currency_rules")
        if cached is not None:
            return cached
        stale = self._get_cached_stale("currency_rules")
        if not await self._throttle_read():
            return stale or []
        result, ok = await _retry_async_graceful(
            self._read_rules_sync,
            fallback=stale or [],
            operation_name="fetch_rules",
        )
        if ok:
            self._set_cached("currency_rules", result)
        return result

    # ----------------------------
    # Targeted user data reads
    # ----------------------------
    def _get_bank_headers_sync(self) -> Optional[List[str]]:
        cached = self._get_cached("bank_headers")
        if cached is not None:
            return cached
        ws = self._ws(BANK_SHEET)
        if not ws:
            return None
        row1 = ws.row_values(1)
        if row1:
            self._set_cached("bank_headers", row1)
        return row1

    def _get_patrol_headers_sync(self) -> Optional[List[str]]:
        cached = self._get_cached("patrol_headers")
        if cached is not None:
            return cached
        ws = self._ws(PATROLS_TOTALS_SHEET)
        if not ws:
            return None
        row1 = ws.row_values(1)
        if row1:
            self._set_cached("patrol_headers", row1)
        return row1

    def _get_log_headers_sync(self) -> Optional[List[str]]:
        cached = self._get_cached("log_headers")
        if cached is not None:
            return cached
        ws = self._ws(BANK_LOGS_SHEET)
        if not ws:
            return None
        row1 = ws.row_values(1)
        if row1:
            self._set_cached("log_headers", row1)
        return row1

    def _get_log_last_row_sync(self) -> int:
        cached = self._get_cached("log_last_row")
        if cached is not None:
            return int(cached)
        ws = self._ws(BANK_LOGS_SHEET)
        if not ws:
            return 1
        try:
            col_a = ws.col_values(1)
            last = len(col_a) if col_a else 1
        except Exception:
            last = 1
        self._set_cached("log_last_row", last, ttl=15)
        return last

    def _get_user_row_map_bank_sync(self) -> Dict[str, int]:
        cached = self._get_cached("bank_user_row_map")
        if cached is not None:
            return cached
        ws = self._ws(BANK_SHEET)
        if not ws:
            return {}
        headers = self._get_bank_headers_sync()
        if not headers:
            return {}
        uid_idx = self._find_header_index(headers, "User ID")
        if uid_idx == -1:
            return {}
        col_values = ws.col_values(uid_idx + 1)
        user_map: Dict[str, int] = {}
        for i, val in enumerate(col_values[1:], start=2):
            uid = str(val).strip()
            if uid:
                user_map[uid] = i
        self._set_cached("bank_user_row_map", user_map)
        return user_map

    def _get_user_row_map_patrol_sync(self) -> Dict[str, int]:
        cached = self._get_cached("patrol_user_row_map")
        if cached is not None:
            return cached
        ws = self._ws(PATROLS_TOTALS_SHEET)
        if not ws:
            return {}
        headers = self._get_patrol_headers_sync()
        if not headers:
            return {}
        uid_idx = self._find_header_index(headers, "UserID")
        if uid_idx == -1:
            uid_idx = self._find_header_index(headers, "User ID")
        if uid_idx == -1:
            return {}
        col_values = ws.col_values(uid_idx + 1)
        user_map: Dict[str, int] = {}
        for i, val in enumerate(col_values[1:], start=2):
            uid = str(val).strip()
            if uid:
                user_map[uid] = i
        self._set_cached("patrol_user_row_map", user_map)
        return user_map

    # ----------------------------
    # Snapshot sheet helpers (Patrols_Pay_Snapshots)
    # ----------------------------
    def _get_snap_headers_sync(self) -> Optional[List[str]]:
        cached = self._get_cached("snap_headers")
        if cached is not None:
            return cached
        ws = self._ws(PATROLS_PAY_SNAPSHOTS_SHEET)
        if not ws:
            return None
        try:
            row1 = ws.row_values(1)
        except Exception as e:
            print(f"[BankCog] Failed to read Patrols_Pay_Snapshots headers: {e}")
            return None
        if row1:
            self._set_cached("snap_headers", row1)
        return row1

    def _get_user_row_map_snap_sync(self) -> Dict[str, int]:
        cached = self._get_cached("snap_user_row_map")
        if cached is not None:
            return cached
        ws = self._ws(PATROLS_PAY_SNAPSHOTS_SHEET)
        if not ws:
            return {}
        headers = self._get_snap_headers_sync()
        if not headers:
            return {}
        uid_idx = self._find_header_index(headers, "User ID")
        if uid_idx == -1:
            uid_idx = self._find_header_index_contains_all(headers, ["user", "id"])
        if uid_idx == -1:
            return {}
        try:
            col_values = ws.col_values(uid_idx + 1)
        except Exception as e:
            print(f"[BankCog] Failed to read Patrols_Pay_Snapshots User ID column: {e}")
            return {}
        user_map: Dict[str, int] = {}
        for i, val in enumerate(col_values[1:], start=2):
            uid = _normalize_uid_cell(val)
            if uid:
                user_map[uid] = i
        self._set_cached("snap_user_row_map", user_map)
        return user_map

    def _ensure_snap_row_sync(self, user_id: str) -> Tuple[int, List[str]]:
        """Ensure a row exists in Patrols_Pay_Snapshots for this user. Returns (row_idx, headers)."""
        ws = self._ws(PATROLS_PAY_SNAPSHOTS_SHEET)
        if not ws:
            raise RuntimeError("Patrols_Pay_Snapshots sheet not available")
        headers = self._get_snap_headers_sync()
        if not headers:
            raise RuntimeError("Patrols_Pay_Snapshots sheet missing headers")

        uid_idx = self._find_header_index(headers, "User ID")
        if uid_idx == -1:
            uid_idx = self._find_header_index_contains_all(headers, ["user", "id"])
        if uid_idx == -1:
            raise RuntimeError("Patrols_Pay_Snapshots missing 'User ID' header")

        normalized_uid = _normalize_uid_cell(user_id)
        user_map = self._get_user_row_map_snap_sync()
        row_idx = user_map.get(normalized_uid)
        if row_idx:
            return row_idx, headers

        # Append a new row with the User ID
        new_row = [""] * len(headers)
        new_row[uid_idx] = str(user_id).strip()
        ws.append_row(new_row, value_input_option="RAW")
        self._invalidate_cache("snap_user_row_map")
        user_map = self._get_user_row_map_snap_sync()
        row_idx = user_map.get(normalized_uid)
        if not row_idx:
            raise RuntimeError("Failed to create snapshot row")
        return row_idx, headers

    def _get_snapshot_value(self, snap_headers: List[str], snap_row: List[str], snap_col: str) -> str:
        """Read a single snapshot cell from a Patrols_Pay_Snapshots row. Returns '' if header missing."""
        idx = self._find_header_index(snap_headers, snap_col)
        if idx == -1:
            print(f"[BankCog] WARNING: Snapshot header '{snap_col}' not found in Patrols_Pay_Snapshots")
            return ""
        return (snap_row[idx] if idx < len(snap_row) else "").strip()

    def _read_wallet_only_sync(self, user_id: str) -> Dict[str, int]:
        headers = self._get_bank_headers_sync()
        if not headers:
            return {"gold": 0, "silver": 0, "copper": 0}
        user_map = self._get_user_row_map_bank_sync()
        row_idx = user_map.get(user_id.strip())
        if not row_idx:
            return {"gold": 0, "silver": 0, "copper": 0}
        ws = self._ws(BANK_SHEET)
        if not ws:
            return {"gold": 0, "silver": 0, "copper": 0}
        row = ws.row_values(row_idx)
        if not row:
            return {"gold": 0, "silver": 0, "copper": 0}
        idx_gold = self._find_header_index(headers, "Gold")
        idx_silver = self._find_header_index(headers, "Silver")
        idx_copper = self._find_header_index(headers, "Copper")
        return {
            "gold": _to_int(row[idx_gold] if idx_gold != -1 and idx_gold < len(row) else 0),
            "silver": _to_int(row[idx_silver] if idx_silver != -1 and idx_silver < len(row) else 0),
            "copper": _to_int(row[idx_copper] if idx_copper != -1 and idx_copper < len(row) else 0),
        }

    async def fetch_wallet_only(
        self,
        user_id: int,
        fallback: Optional[Dict[str, int]] = None
    ) -> Tuple[Dict[str, int], bool]:
        cache_key = f"wallet_{user_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached, True
        if not await self._throttle_read():
            return fallback or {"gold": 0, "silver": 0, "copper": 0}, False
        result, ok = await _retry_async_graceful(
            lambda: self._read_wallet_only_sync(str(user_id)),
            fallback=fallback or {"gold": 0, "silver": 0, "copper": 0},
            operation_name="fetch_wallet_only",
        )
        if ok:
            self._set_cached(cache_key, result, ttl=30)
        elif fallback is None:
            stale = self._get_cached_stale(cache_key)
            if stale is not None:
                return stale, False
        return result, ok

    def _get_value_by_header(self, headers: List[str], row: List[str], header_name: str) -> str:
        i = self._find_header_index(headers, header_name)
        if i == -1:
            return ""
        return (row[i] if i < len(row) else "").strip()

    # ----------------------------
    # v4.1: Updated Pay Data Reader (PAY_DOWN - NO AUTO-NORMALIZE)
    # ----------------------------
    def _read_pay_data_only_sync(
        self,
        user_id: str,
        member: discord.Member,
        currency_meta: Dict[str, Dict[str, Any]],
        rules: List[Dict[str, Any]],
        conversion_rates: Dict[Tuple[str, str], int],
        tier_map: Dict[str, int],
        user_rank: Optional[str],
    ) -> Tuple[Dict[str, int], Dict[str, int], List[Dict[str, Any]], List[Tuple[str, str, str]], bool, str]:
        """
        v4.2: Updated to:
        - Parse durations to minutes
        - Apply rank multiplier
        - Use split_fractional_payout() to distribute fractional amounts across tiers
        - normalize_up() the final payouts (copper->silver->gold)

        Returns:
            (wallet, payouts_by_currency, breakdown, snapshot_plan, is_eligible, ineligible_reason)

        payouts_by_currency: {"gold": int, "silver": int, "copper": int} - amounts to ADD to each
        """
        wallet = self._read_wallet_only_sync(user_id)
        empty_payouts = {"gold": 0, "silver": 0, "copper": 0}

        headers = self._get_patrol_headers_sync()
        if not headers:
            return wallet, empty_payouts, [], [], True, ""

        user_map = self._get_user_row_map_patrol_sync()
        row_idx = user_map.get(user_id.strip())
        if not row_idx:
            return wallet, empty_payouts, [], [], True, ""

        ws = self._ws(PATROLS_TOTALS_SHEET)
        if not ws:
            return wallet, empty_payouts, [], [], True, ""

        row = ws.row_values(row_idx)
        if not row:
            return wallet, empty_payouts, [], [], True, ""

        # Get base rank and multiplier from currency meta
        any_meta = next(iter(currency_meta.values()), None)
        base_rank = str(any_meta.get("base_rank", "")).strip() if any_meta else ""
        multiplier_per_tier = float(any_meta.get("multiplier_per_tier", 0.0)) if any_meta else 0.0

        # Calculate rank multiplier and check eligibility
        rank_multiplier, is_eligible, eligibility_reason = self._calculate_rank_multiplier(
            user_rank, base_rank, tier_map, multiplier_per_tier
        )

        print(
            f"[BANK DEBUG] RankCalc | "
            f"user_rank={user_rank} "
            f"user_tier={tier_map.get(_safe_lower(user_rank or ''))} | "
            f"base_rank={base_rank} "
            f"base_tier={tier_map.get(_safe_lower(base_rank))} | "
            f"per_tier={multiplier_per_tier} | "
            f"mult={rank_multiplier} | "
            f"eligible={is_eligible} | "
            f"reason={eligibility_reason}"
        )

        if not is_eligible:
            return wallet, empty_payouts, [], [], False, eligibility_reason

        # --- Read snapshot row from Patrols_Pay_Snapshots ---
        snap_headers = self._get_snap_headers_sync()
        snap_row: List[str] = []
        if snap_headers:
            normalized_uid = _normalize_uid_cell(user_id)
            snap_user_map = self._get_user_row_map_snap_sync()
            snap_row_idx = snap_user_map.get(normalized_uid)
            if snap_row_idx:
                ws_snap = self._ws(PATROLS_PAY_SNAPSHOTS_SHEET)
                if ws_snap:
                    try:
                        snap_row = ws_snap.row_values(snap_row_idx)
                    except Exception as e:
                        print(f"[BankCog] Failed to read snapshot row for {user_id}: {e}")
                        snap_row = []
        else:
            print("[BankCog] WARNING: Patrols_Pay_Snapshots headers unavailable; treating all snapshots as empty")

        # v4.1: Track payouts by currency type (no auto-normalization)
        payouts_by_currency: Dict[str, int] = {"gold": 0, "silver": 0, "copper": 0}
        breakdown: List[Dict[str, Any]] = []
        snapshot_plan: List[Tuple[str, str, str]] = []
        seen_snaps: set = set()

        # Resolve user's best tier once for all rules
        user_best_tier = self._get_member_best_tier(member, tier_map)

        for rule in rules:
            if not rule.get("enabled", True):
                continue

            stat_col = rule.get("stat_col", "")
            snap_col = rule.get("snap_col", "")
            currency_type = rule.get("currency_type", "")
            amt_per = int(rule.get("amount_per_unit", 0))
            min_role = rule.get("min_role", "")

            if not stat_col or not snap_col or not currency_type or amt_per <= 0:
                continue

            # Tier-based eligibility check for Min_Role
            if min_role:
                min_role_tier = tier_map.get(_safe_lower(min_role))
                if min_role_tier is None:
                    # Min_Role not found in Ranks sheet — skip this rule
                    continue
                if user_best_tier is None:
                    # User has no ranked role — not eligible for rank-gated rules
                    continue
                if user_best_tier > min_role_tier:
                    # User's tier is worse (higher number) than required
                    continue

            # Current stat value: still from Patrols_User_Totals
            cur_raw = self._get_value_by_header(headers, row, stat_col)

            # Snapshot value: now from Patrols_Pay_Snapshots (not Patrols_User_Totals)
            if snap_headers and snap_row:
                snap_raw = self._get_snapshot_value(snap_headers, snap_row, snap_col)
            else:
                snap_raw = ""  # No snapshot yet (first time) -> treat as 0

            # Only parse as duration for minutes-based rules (e.g. Hourly_Pay);
            # all other rules (kills, quests, etc.) are plain unit counts.
            rule_id_str = rule.get("rule_id", "")
            if _is_minutes_based(rule_id_str):
                cur_val = parse_duration_to_minutes(cur_raw) if cur_raw else 0
                snap_val = parse_duration_to_minutes(snap_raw) if snap_raw else 0
            else:
                cur_val = _to_int(cur_raw) if cur_raw else 0
                snap_val = _to_int(snap_raw) if snap_raw else 0

            delta_units = cur_val - snap_val
            if delta_units <= 0:
                continue

            # Calculate payout in the rule's currency type (as float for multiplier)
            payout_raw = float(delta_units * amt_per)

            # Apply rank multiplier (may produce fractional result)
            payout_with_mult = payout_raw * rank_multiplier
            print(
                f"[BANK DEBUG] PayoutCalc | rule={rule.get('rule_id')} "
                f"delta_units={delta_units} amt_per={amt_per} "
                f"payout_raw={payout_raw} mult={rank_multiplier} "
                f"payout_with_mult={payout_with_mult}"
            )

            # Split fractional payout across currency tiers (whole part stays, remainder goes down)
            split = split_fractional_payout(currency_type, payout_with_mult, conversion_rates)
            # The "final" currency for breakdown display is the rule's original currency
            final_currency = _safe_lower(currency_type)
            final_amount = split.get(final_currency, 0)

            # Add each split bucket to the running totals
            for ctype in ("gold", "silver", "copper"):
                payouts_by_currency[ctype] = payouts_by_currency.get(ctype, 0) + split.get(ctype, 0)

            total_split = split.get("gold", 0) + split.get("silver", 0) + split.get("copper", 0)
            if total_split <= 0:
                continue

            breakdown.append({
                "rule_id": rule.get("rule_id"),
                "original_currency": currency_type,
                "final_currency": final_currency,
                "stat_col": stat_col,
                "snap_col": snap_col,
                "delta_units": delta_units,
                "is_minutes": _is_minutes_based(rule_id_str),
                "payout_raw": payout_raw,
                "payout_with_mult": payout_with_mult,
                "final_amount": final_amount,
                "rank_multiplier": rank_multiplier,
            })

            if snap_col not in seen_snaps:
                seen_snaps.add(snap_col)
                # Store the ORIGINAL string value for snapshot (not parsed)
                snapshot_plan.append((stat_col, snap_col, cur_raw))

        # Normalize UP: roll copper -> silver -> gold using conversion rates
        pre_norm = dict(payouts_by_currency)
        payouts_by_currency = normalize_up(payouts_by_currency, conversion_rates)
        print(
            f"[BANK DEBUG] NormalizeUp payouts | before={pre_norm} | after={payouts_by_currency}"
        )

        return wallet, payouts_by_currency, breakdown, snapshot_plan, True, ""

    async def fetch_pay_data_only(
        self,
        user_id: int,
        member: discord.Member,
        fallback_wallet: Optional[Dict[str, int]] = None,
    ) -> Tuple[Dict[str, int], Dict[str, int], List[Dict[str, Any]], List[Tuple[str, str, str]], bool, str, Dict[Tuple[str, str], int]]:
        """
        v4.1: Returns (wallet, payouts_by_currency, breakdown, snapshot_plan, is_eligible, reason, conversion_rates)

        payouts_by_currency: {"gold": int, "silver": int, "copper": int} - amounts to add per currency
        """
        currency_meta = await self.fetch_currency_meta_cached()
        rules = await self.fetch_rules_cached()
        conversion_rates = await self.fetch_conversion_rates_cached()
        tier_map = await self.fetch_ranks_tier_map_cached()
        # Resolve user's best rank from their Discord roles (not Member Log)
        user_rank = _resolve_best_rank_from_roles(member, tier_map)

        empty_payouts = {"gold": 0, "silver": 0, "copper": 0}

        if not await self._throttle_read():
            w = fallback_wallet or {"gold": 0, "silver": 0, "copper": 0}
            return w, empty_payouts, [], [], True, "", conversion_rates

        result, ok = await _retry_async_graceful(
            lambda: self._read_pay_data_only_sync(
                str(user_id), member, currency_meta, rules,
                conversion_rates, tier_map, user_rank
            ),
            fallback=(
                fallback_wallet or {"gold": 0, "silver": 0, "copper": 0},
                empty_payouts, [], [], True, ""
            ),
            operation_name="fetch_pay_data_only",
        )

        if ok:
            wallet, payouts, breakdown, snapshot_plan, is_eligible, reason = result
            self._set_cached(f"wallet_{user_id}", wallet, ttl=30)
            return wallet, payouts, breakdown, snapshot_plan, is_eligible, reason, conversion_rates
        else:
            w, p, b, s, e, r = result
            return w, p, b, s, e, r, conversion_rates

    # Ledger reader (unchanged from v3.0)
    def _read_ledger_only_sync(self, user_id: str, limit: int = 8) -> List[Dict[str, Any]]:
        ws = self._ws(BANK_LOGS_SHEET)
        if not ws:
            return []

        headers = self._get_log_headers_sync()
        if not headers:
            return []

        uid_idx = self._find_header_index_contains_all(headers, ["user", "id"])
        ts_idx = self._find_header_index_contains(headers, "timestamp")
        action_idx = self._find_header_index_contains(headers, "action")
        gold_idx = self._find_header_index_contains(headers, "gold")
        silver_idx = self._find_header_index_contains(headers, "silver")
        copper_idx = self._find_header_index_contains(headers, "copper")
        if uid_idx == -1:
            return []

        last_row = self._get_log_last_row_sync()
        if last_row < 2:
            return []

        start_row = max(2, last_row - LEDGER_TAIL_ROWS + 1)

        num_cols = max(len(headers), 10)
        end_col_letter = _col_to_a1(min(num_cols - 1, 25))
        range_str = f"A{start_row}:{end_col_letter}{last_row}"

        try:
            data = ws.get(range_str)
        except Exception:
            return []
        if not data:
            return []

        target_uid = _normalize_uid_cell(user_id)
        collected: List[Dict[str, Any]] = []

        for row in data:
            uid_cell = row[uid_idx] if uid_idx < len(row) else ""
            if _normalize_uid_cell(uid_cell) != target_uid:
                continue

            ts_raw = (row[ts_idx] if ts_idx != -1 and ts_idx < len(row) else "")
            epoch = _parse_ts_to_epoch(ts_raw)
            date_str = str(ts_raw)[:10] if ts_raw else ""

            action = (row[action_idx] if action_idx != -1 and action_idx < len(row) else "Unknown")
            action = str(action).strip()

            g = _to_int(row[gold_idx] if gold_idx != -1 and gold_idx < len(row) else 0)
            s = _to_int(row[silver_idx] if silver_idx != -1 and silver_idx < len(row) else 0)
            c = _to_int(row[copper_idx] if copper_idx != -1 and copper_idx < len(row) else 0)

            collected.append({
                "epoch": epoch,
                "date": date_str,
                "action": action,
                "gold": g,
                "silver": s,
                "copper": c,
            })

        collected.sort(key=lambda x: x.get("epoch", 0.0), reverse=True)

        result: List[Dict[str, Any]] = []
        for e in collected[:limit]:
            e.pop("epoch", None)
            result.append(e)

        return result

    async def fetch_ledger_only(self, user_id: int, limit: int = 8) -> Tuple[List[Dict[str, Any]], bool]:
        if not await self._throttle_read():
            return [], False
        result, ok = await _retry_async_graceful(
            lambda: self._read_ledger_only_sync(str(user_id), limit),
            fallback=[],
            operation_name="fetch_ledger_only",
        )
        return result, ok

    # ----------------------------
    # v4.1: WRITE: Collect Pay (NO AUTO-NORMALIZE - per-currency deposits)
    # ----------------------------
    def _ensure_bank_row_sync(self, user_id: str) -> Tuple[int, List[str]]:
        ws = self._ws(BANK_SHEET)
        if not ws:
            raise RuntimeError("Bank sheet not available")
        headers = self._get_bank_headers_sync()
        if not headers:
            ws.append_row(BANK_HEADERS_REQUIRED, value_input_option="RAW")
            self._invalidate_cache("bank_headers")
            headers = self._get_bank_headers_sync()
        if not headers:
            raise RuntimeError("Bank sheet missing headers")

        uid_idx = self._find_header_index(headers, "User ID")
        if uid_idx == -1:
            raise RuntimeError("Bank sheet missing 'User ID' header")

        user_map = self._get_user_row_map_bank_sync()
        row_idx = user_map.get(user_id.strip())
        if row_idx:
            return row_idx, headers

        new_row = [""] * len(headers)
        new_row[uid_idx] = str(user_id).strip()
        for col_name in ["Gold", "Silver", "Copper"]:
            ci = self._find_header_index(headers, col_name)
            if ci != -1 and ci < len(new_row):
                new_row[ci] = "0"

        ws.append_row(new_row, value_input_option="RAW")
        self._invalidate_cache("bank_user_row_map")
        user_map = self._get_user_row_map_bank_sync()
        row_idx = user_map.get(user_id.strip())
        if not row_idx:
            raise RuntimeError("Failed to create bank row")
        return row_idx, headers

    def _apply_collect_pay_sync(
        self,
        user_id: str,
        payouts_by_currency: Dict[str, int],
        breakdown: List[Dict[str, Any]],
        snapshot_plan: List[Tuple[str, str, str]],
        cached_wallet: Dict[str, int],
        conversion_rates: Dict[Tuple[str, str], int],
    ) -> Dict[str, int]:
        """
        v4.2: Updated to:
        - Add payouts per-currency to existing balance
        - Normalize UP (copper->silver->gold) before writing
        - Write updated integers to Bank tab
        """
        ws_bank = self._ws(BANK_SHEET)
        ws_log = self._ws(BANK_LOGS_SHEET)

        if not ws_bank:
            raise RuntimeError("Bank sheet not available")

        row_i, headers = self._ensure_bank_row_sync(user_id)

        idx_gold = self._find_header_index(headers, "Gold")
        idx_silver = self._find_header_index(headers, "Silver")
        idx_copper = self._find_header_index(headers, "Copper")
        idx_last = self._find_header_index(headers, "Last_Collected")

        # Add payouts to existing balance, then normalize up
        old_gold = cached_wallet.get("gold", 0)
        old_silver = cached_wallet.get("silver", 0)
        old_copper = cached_wallet.get("copper", 0)

        delta_gold = payouts_by_currency.get("gold", 0)
        delta_silver = payouts_by_currency.get("silver", 0)
        delta_copper = payouts_by_currency.get("copper", 0)

        raw_balance = {
            "gold": old_gold + delta_gold,
            "silver": old_silver + delta_silver,
            "copper": old_copper + delta_copper,
        }
        normalized = normalize_up(raw_balance, conversion_rates)
        print(
            f"[BANK DEBUG] NormalizeUp balance | before={raw_balance} | after={normalized}"
        )

        new_gold = normalized["gold"]
        new_silver = normalized["silver"]
        new_copper = normalized["copper"]

        updates = []
        if idx_gold != -1:
            updates.append({"range": f"{_col_to_a1(idx_gold)}{row_i}", "values": [[str(_clamp(new_gold, 0, 10**12))]]})
        if idx_silver != -1:
            updates.append({"range": f"{_col_to_a1(idx_silver)}{row_i}", "values": [[str(_clamp(new_silver, 0, 10**12))]]})
        if idx_copper != -1:
            updates.append({"range": f"{_col_to_a1(idx_copper)}{row_i}", "values": [[str(_clamp(new_copper, 0, 10**12))]]})
        if idx_last != -1:
            updates.append({"range": f"{_col_to_a1(idx_last)}{row_i}", "values": [[_now_utc_iso()]]})
        if updates:
            ws_bank.batch_update(updates, value_input_option="RAW")

        # Update snapshots in Patrols_Pay_Snapshots (NOT Patrols_User_Totals)
        if snapshot_plan:
            try:
                snap_row_i, snap_headers = self._ensure_snap_row_sync(user_id)
                ws_snap = self._ws(PATROLS_PAY_SNAPSHOTS_SHEET)
                if ws_snap and snap_headers:
                    snap_updates = []
                    for _, snap_col, stat_val in snapshot_plan:
                        snap_idx = self._find_header_index(snap_headers, snap_col)
                        if snap_idx != -1:
                            # Write the original string value (like "41hr 15m")
                            snap_updates.append({"range": f"{_col_to_a1(snap_idx)}{snap_row_i}", "values": [[str(stat_val)]]})
                        else:
                            print(f"[BankCog] WARNING: Snapshot column '{snap_col}' not found in Patrols_Pay_Snapshots, skipping write")
                    if snap_updates:
                        ws_snap.batch_update(snap_updates, value_input_option="RAW")
                    self._invalidate_cache("snap_user_row_map")
            except Exception as e:
                print(f"[BankCog] ERROR writing snapshots to Patrols_Pay_Snapshots: {e}")

        balance_after = {"gold": new_gold, "silver": new_silver, "copper": new_copper}

        # Log the transaction
        if ws_log:
            event_id = f"CP-{user_id}-{int(datetime.now(timezone.utc).timestamp())}"
            log_row = [
                str(user_id),
                _now_utc_iso(),
                event_id,
                "COLLECT_PAY",
                str(delta_gold),
                str(delta_silver),
                str(delta_copper),
                json.dumps(balance_after, ensure_ascii=False),
                "bank_ui",
                json.dumps({
                    "breakdown": breakdown,
                    "payouts_by_currency": payouts_by_currency,
                    "conversion_rates": {f"{k[0]}->{k[1]}": v for k, v in conversion_rates.items()},
                }, ensure_ascii=False),
            ]
            ws_log.append_row(log_row, value_input_option="RAW")
            self._invalidate_cache("log_last_row")

        self._invalidate_cache(f"wallet_{user_id}")
        self._invalidate_cache("bank_user_row_map")

        return balance_after

    async def apply_collect_pay(
        self,
        user_id: int,
        payouts_by_currency: Dict[str, int],
        breakdown: List[Dict[str, Any]],
        snapshot_plan: List[Tuple[str, str, str]],
        cached_wallet: Dict[str, int],
        conversion_rates: Dict[Tuple[str, str], int],
    ) -> Dict[str, int]:
        await self._throttle_write()
        return await _retry_async(
            lambda: self._apply_collect_pay_sync(
                str(user_id), payouts_by_currency, breakdown, snapshot_plan,
                cached_wallet, conversion_rates
            ),
            operation_name="apply_collect_pay",
        )

    # ----------------------------
    # Slash command
    # ----------------------------
    @app_commands.command(name="bank", description="Open the Royal Bank interface")
    async def bank_command(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not member:
            await interaction.response.send_message(
                "Member context not available.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        timer_minutes, ledger_lines, image_mode = await self.fetch_permissions_cached("/bank")

        view = BankView(
            cog=self,
            owner_id=member.id,
            member=member,
            timeout=180.0,
            ledger_limit=ledger_lines,
            image_mode=image_mode,
        )

        try:
            embed = await view.build_embed_for_page(BankPage.BALANCE)
        except Exception as e:
            print(f"[BankCog] bank_command embed build failed: {e}")
            embed = view.build_error_embed(f"Failed to load bank: {type(e).__name__}")

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

        if timer_minutes > 0 and view.message:
            view.start_auto_delete_timer(timer_minutes)


# ----------------------------
# View
# ----------------------------
class BankView(discord.ui.View):
    def __init__(
        self,
        cog: BankCog,
        owner_id: int,
        member: discord.Member,
        timeout: float = 180.0,
        ledger_limit: int = DEFAULT_LEDGER_LINES,
        image_mode: ImageMode = ImageMode.THUMBNAIL,
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = owner_id
        self.member = member
        self.current_page = BankPage.BALANCE
        self.message: Optional[discord.Message] = None
        self._is_closed = False
        self._is_collecting = False
        self._auto_delete_task: Optional[asyncio.Task] = None
        self._ledger_limit = ledger_limit
        self._image_mode = image_mode

        self._teller_image: Optional[str] = None
        self._currency_meta: Optional[Dict[str, Dict[str, Any]]] = None

        self._cached_wallet: Optional[Dict[str, int]] = None
        self._cached_payouts_by_currency: Dict[str, int] = {"gold": 0, "silver": 0, "copper": 0}  # v4.1
        self._cached_breakdown: Optional[List[Dict]] = None
        self._cached_snapshot_plan: Optional[List[Tuple[str, str, str]]] = None
        self._cached_ledger: Optional[List[Dict]] = None
        self._cached_conversion_rates: Optional[Dict[Tuple[str, str], int]] = None
        self._cached_is_eligible: bool = True
        self._cached_ineligible_reason: str = ""
        self._wallet_read_ok: bool = True

        self._pay_data_loaded = False
        self._ledger_unavailable = False

        self._ledger_offset = 0

    # ----------------------------
    # Lifetime + perms
    # ----------------------------
    def start_auto_delete_timer(self, minutes: int) -> None:
        if self._auto_delete_task is not None:
            return
        self._auto_delete_task = asyncio.create_task(self._auto_delete_after(minutes))

    async def _auto_delete_after(self, minutes: int) -> None:
        try:
            await asyncio.sleep(minutes * 60)
            if self._is_closed:
                return
            self._is_closed = True
            self._disable_all_buttons()
            if self.message:
                try:
                    await self.message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    try:
                        embed = self._build_auto_expired_embed()
                        await self.message.edit(embed=embed, view=self)
                    except (discord.NotFound, discord.HTTPException):
                        pass
            self.stop()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[BankView] _auto_delete_after error: {e}")

    def _cancel_auto_delete_timer(self) -> None:
        if self._auto_delete_task is not None and not self._auto_delete_task.done():
            self._auto_delete_task.cancel()
            self._auto_delete_task = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This panel belongs to someone else.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self._is_closed:
            return
        self._is_closed = True
        self._cancel_auto_delete_timer()
        self._disable_all_buttons()
        if self.message:
            try:
                embed = self._build_timeout_embed()
                await self.message.edit(embed=embed, view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    def _disable_all_buttons(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    def _enable_all_buttons(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = False

    def _update_button_styles(self) -> None:
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue

            if child.custom_id == "bank_close":
                child.style = discord.ButtonStyle.danger
                continue

            if child.custom_id == "bank_sources":
                if self.current_page == BankPage.SOURCES:
                    child.label = "Collect Pay"
                    child.emoji = "💵"
                    child.style = discord.ButtonStyle.success
                else:
                    child.label = "View Pay"
                    child.emoji = "📊"
                    child.style = discord.ButtonStyle.secondary
                continue

            if child.custom_id == "bank_balance":
                child.label = "Balance"
                child.emoji = "💰"
                child.style = discord.ButtonStyle.primary if self.current_page == BankPage.BALANCE else discord.ButtonStyle.secondary
                continue

            if child.custom_id == "bank_ledger":
                if self.current_page == BankPage.LEDGER:
                    child.label = "Next Ledger Page"
                    child.emoji = "➡️"
                    child.style = discord.ButtonStyle.primary
                else:
                    child.label = "Ledger"
                    child.emoji = "📜"
                    child.style = discord.ButtonStyle.secondary
                continue

    # ----------------------------
    # Static data
    # ----------------------------
    async def _ensure_static_data(self) -> None:
        if self._teller_image is None:
            self._teller_image = await self.cog.fetch_teller_image_cached()
        if self._currency_meta is None:
            self._currency_meta = await self.cog.fetch_currency_meta_cached()

    def _apply_image_mode(self, embed: discord.Embed) -> None:
        teller = self._teller_image or PLACEHOLDER_TELLER_IMAGE
        embed.set_author(name="The Royal Bank", icon_url=teller)

        if self._image_mode == ImageMode.FULL:
            embed.set_image(url=teller)
            embed.set_thumbnail(url=None)
        else:
            embed.set_thumbnail(url=teller)
            embed.set_image(url=None)

    def _get_emoji(self, currency_type: str, fallback: str) -> str:
        if self._currency_meta:
            return self._currency_meta.get(currency_type, {}).get("emoji_render", fallback) or fallback
        return fallback

    # ----------------------------
    # Embed build
    # ----------------------------
    async def build_embed_for_page(
        self,
        page: BankPage,
        narrative_override: Optional[str] = None,
    ) -> discord.Embed:
        await self._ensure_static_data()

        embed = discord.Embed(color=0xD4AF37)
        self._apply_image_mode(embed)

        narrative = NARRATIVES.get(narrative_override, NARRATIVES.get(page, "")) if narrative_override else NARRATIVES.get(page, "")
        embed.description = f"```\n{narrative}\n```"

        try:
            if page == BankPage.BALANCE:
                await self._load_balance_data()
                self._add_balance_content(embed)
            elif page == BankPage.SOURCES:
                await self._load_pay_data()
                self._add_sources_content(embed)
            elif page == BankPage.LEDGER:
                await self._load_ledger_data()
                self._add_ledger_content(embed)
            elif page == BankPage.CLOSED:
                embed.add_field(
                    name="Session Ended",
                    value="*The counting house falls silent.*",
                    inline=False,
                )
        except Exception as e:
            print(f"[BankView] build_embed_for_page content failed: {type(e).__name__}: {e}")
            embed.add_field(
                name="⚠️ Error",
                value=f"```\nThe Teller frowns at a smudged entry.\n({type(e).__name__})\n```",
                inline=False,
            )

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        embed.set_footer(text=f"Account: {self.member.display_name} • {ts}")
        return embed

    # ----------------------------
    # Loaders
    # ----------------------------
    async def _load_balance_data(self) -> None:
        wallet, ok = await self.cog.fetch_wallet_only(self.member.id, fallback=self._cached_wallet)
        self._wallet_read_ok = ok
        if ok:
            self._cached_wallet = wallet
        elif wallet:
            self._cached_wallet = wallet

    async def _load_pay_data(self) -> None:
        if self._pay_data_loaded and self._cached_wallet is not None:
            return

        result = await self.cog.fetch_pay_data_only(
            self.member.id, self.member, fallback_wallet=self._cached_wallet
        )
        wallet, payouts_by_currency, breakdown, snapshot_plan, is_eligible, reason, rates = result

        self._cached_wallet = wallet
        self._cached_payouts_by_currency = payouts_by_currency  # v4.1: per-currency payouts
        self._cached_breakdown = breakdown
        self._cached_snapshot_plan = snapshot_plan
        self._cached_is_eligible = is_eligible
        self._cached_ineligible_reason = reason
        self._cached_conversion_rates = rates
        self._pay_data_loaded = True

    async def _load_ledger_data(self) -> None:
        await self._load_balance_data()
        desired = max(self._ledger_limit, LEDGER_PREFETCH_MIN, LEDGER_CARD_MAX + LEDGER_PAGE_STEP)
        ledger, ok = await self.cog.fetch_ledger_only(self.member.id, limit=desired)
        if ok:
            self._cached_ledger = list(ledger)
            self._ledger_unavailable = False
        else:
            self._ledger_unavailable = True

    # ----------------------------
    # Content builders
    # ----------------------------
    def _add_balance_content(self, embed: discord.Embed) -> None:
        wallet = self._cached_wallet or {"gold": 0, "silver": 0, "copper": 0}
        g_emoji = self._get_emoji("gold", "🟡")
        s_emoji = self._get_emoji("silver", "⚪")
        c_emoji = self._get_emoji("copper", "🟠")

        wallet_display = (
            f"{g_emoji} **Gold:** {wallet.get('gold', 0):,}\n"
            f"{s_emoji} **Silver:** {wallet.get('silver', 0):,}\n"
            f"{c_emoji} **Copper:** {wallet.get('copper', 0):,}"
        )
        embed.add_field(name="Your Wallet", value=wallet_display, inline=False)
        if not self._wallet_read_ok:
            embed.add_field(
                name="⚠️ Treasury Read Delayed",
                value=(
                    "Google Sheets is rate-limiting the treasury right now. "
                    "If a last-known balance was available, it is shown above; "
                    "otherwise your balance has **not** been changed and should be retried shortly."
                ),
                inline=False,
            )

    def _add_sources_content(self, embed: discord.Embed) -> None:
        wallet = self._cached_wallet or {"gold": 0, "silver": 0, "copper": 0}
        payouts = self._cached_payouts_by_currency  # v4.1: per-currency payouts
        sources = self._cached_breakdown or []

        g_emoji = self._get_emoji("gold", "🟡")
        s_emoji = self._get_emoji("silver", "⚪")
        c_emoji = self._get_emoji("copper", "🟠")

        wallet_display = (
            f"{g_emoji} Gold: {wallet['gold']:,}\n"
            f"{s_emoji} Silver: {wallet['silver']:,}\n"
            f"{c_emoji} Copper: {wallet['copper']:,}"
        )
        embed.add_field(name="Wallet", value=wallet_display, inline=True)

        # v4.1: Display owed amounts per currency (no normalization)
        owed_display = (
            f"{g_emoji} Gold: {payouts.get('gold', 0):,}\n"
            f"{s_emoji} Silver: {payouts.get('silver', 0):,}\n"
            f"{c_emoji} Copper: {payouts.get('copper', 0):,}"
        )
        embed.add_field(name="Owed", value=owed_display, inline=True)

        embed.add_field(name="\u200b", value="\u200b", inline=False)

        # Show ineligibility warning
        if not self._cached_is_eligible:
            embed.add_field(
                name="⚠️ Not Eligible",
                value=f"*{self._cached_ineligible_reason}*",
                inline=False,
            )

        if sources:
            lines = ["```", "INCOME BREAKDOWN", "───────────────", ""]
            for src in sources[:8]:
                rule_id = str(src.get("rule_id", "Unknown"))
                display_rule_id = _humanize_rule_label(rule_id)
                delta_units = src.get("delta_units", 0)
                final_currency = src.get("final_currency", "copper")
                final_amount = src.get("final_amount", 0)
                rank_mult = src.get("rank_multiplier", 1.0)

                # v4.1: Show units worked and final currency earned
                print(
                    f"[BANK DEBUG] EmbedLine | rule={rule_id} delta_units={delta_units} "
                    f"final_amount={final_amount} final_currency={final_currency} rank_mult={rank_mult}"
                )
                currency_icon = {"gold": "🟡", "silver": "⚪", "copper": "🟠"}.get(final_currency, "🟠")
                mult_str = f" ({rank_mult:.2f}x)" if rank_mult != 1.0 else ""
                if src.get("is_minutes", False):
                    hm_str = _format_minutes_to_hm(delta_units)
                    lines.append(f"{display_rule_id:<12} {hm_str} -> {final_amount:,} {currency_icon}{mult_str}")
                else:
                    lines.append(f"{display_rule_id:<12} {delta_units:,} -> {final_amount:,} {currency_icon}{mult_str}")

            if len(sources) > 8:
                lines.append("")
                lines.append(f"... and {len(sources) - 8} more entries")
            lines.append("```")
            embed.add_field(name="Sources", value="\n".join(lines), inline=False)
        else:
            embed.add_field(
                name="Sources",
                value="*No pending income at this time.*",
                inline=False,
            )

        # v4.1: Check if any payout is owed
        total_owed = payouts.get("gold", 0) + payouts.get("silver", 0) + payouts.get("copper", 0)
        if total_owed > 0:
            embed.add_field(
                name="Owed Total",
                value=f"{g_emoji} {payouts.get('gold', 0):,}   {s_emoji} {payouts.get('silver', 0):,}   {c_emoji} {payouts.get('copper', 0):,}",
                inline=False,
            )
        else:
            embed.add_field(
                name="Owed Total",
                value="*Nothing owed at this time.*",
                inline=False,
            )

    def _add_ledger_content(self, embed: discord.Embed) -> None:
        if self._ledger_unavailable:
            embed.add_field(
                name="📜 Transaction Ledger",
                value="*Ledger temporarily unavailable. Try again in a minute.*",
                inline=False,
            )
            return

        ledger = self._cached_ledger or []
        if not ledger:
            embed.add_field(
                name="📜 Transaction Ledger",
                value="*No transactions recorded yet.*",
                inline=False,
            )
            return

        if self._ledger_offset < 0:
            self._ledger_offset = 0
        if self._ledger_offset >= len(ledger):
            self._ledger_offset = 0

        page_items = ledger[self._ledger_offset:self._ledger_offset + LEDGER_CARD_MAX]
        if not page_items:
            self._ledger_offset = 0
            page_items = ledger[:LEDGER_CARD_MAX]

        g_emoji = self._get_emoji("gold", "🟡")
        s_emoji = self._get_emoji("silver", "⚪")
        c_emoji = self._get_emoji("copper", "🟠")

        added = 0
        for entry in page_items:
            date = str(entry.get("date", ""))[:10] or "(date unknown)"
            action = str(entry.get("action", "Unknown")).strip()

            g = int(entry.get("gold", 0) or 0)
            s = int(entry.get("silver", 0) or 0)
            c = int(entry.get("copper", 0) or 0)

            changes: List[str] = []
            if g != 0:
                changes.append(f"{g_emoji} {_fmt_delta_inline(g)}")
            if s != 0:
                changes.append(f"{s_emoji} {_fmt_delta_inline(s)}")
            if c != 0:
                changes.append(f"{c_emoji} {_fmt_delta_inline(c)}")
            if not changes:
                changes.append("*No currency change recorded.*")

            value = f"**{action}**\n" + "\n".join(changes)
            embed.add_field(name=f"🗓️ {date}", value=value, inline=True)
            added += 1

            if added % 2 == 0 and added < LEDGER_CARD_MAX:
                embed.add_field(name="\u200b", value="\u200b", inline=True)

        if added % 2 == 1:
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)

        wallet = self._cached_wallet or {"gold": 0, "silver": 0, "copper": 0}
        summary = (
            f"{g_emoji} **{wallet.get('gold', 0):,}**   "
            f"{s_emoji} **{wallet.get('silver', 0):,}**   "
            f"{c_emoji} **{wallet.get('copper', 0):,}**"
        )
        embed.add_field(name="🔍 Quick View", value=summary, inline=False)

        start = self._ledger_offset + 1
        end = min(self._ledger_offset + len(page_items), len(ledger))
        embed.add_field(
            name="Page",
            value=f"Showing **{start}–{end}** of **{len(ledger)}** recent entries.",
            inline=False,
        )

    # ----------------------------
    # Error / timeout embeds
    # ----------------------------
    def build_error_embed(self, error_msg: str) -> discord.Embed:
        embed = discord.Embed(color=0xFF0000)
        self._apply_image_mode(embed)
        embed.description = (
            "```\n"
            "The Teller sighs and shakes her head.\n"
            "\"There seems to be a problem with the records.\"\n"
            "```"
        )
        embed.add_field(name="⚠️ Error", value=f"```\n{error_msg}\n```", inline=False)
        return embed

    def _build_timeout_embed(self) -> discord.Embed:
        embed = discord.Embed(color=0x808080)
        self._apply_image_mode(embed)
        embed.description = f"```\n{NARRATIVES['timeout']}\n```"
        embed.add_field(
            name="Panel Expired",
            value="*Use `/bank` to start a new session.*",
            inline=False,
        )
        return embed

    def _build_auto_expired_embed(self) -> discord.Embed:
        embed = discord.Embed(color=0x808080)
        self._apply_image_mode(embed)
        embed.description = f"```\n{NARRATIVES['auto_expired']}\n```"
        embed.add_field(
            name="Session Expired",
            value="*Use `/bank` to start a new session.*",
            inline=False,
        )
        return embed

    def _build_pay_embed_no_read(self, new_wallet: Dict[str, int], narrative_key: str) -> discord.Embed:
        embed = discord.Embed(color=0xD4AF37)
        self._apply_image_mode(embed)
        embed.description = f"```\n{NARRATIVES.get(narrative_key, NARRATIVES['pay_collected'])}\n```"

        g_emoji = self._get_emoji("gold", "🟡")
        s_emoji = self._get_emoji("silver", "⚪")
        c_emoji = self._get_emoji("copper", "🟠")

        wallet_display = (
            f"{g_emoji} Gold: {new_wallet['gold']:,}\n"
            f"{s_emoji} Silver: {new_wallet['silver']:,}\n"
            f"{c_emoji} Copper: {new_wallet['copper']:,}"
        )
        embed.add_field(name="Wallet", value=wallet_display, inline=True)

        owed_display = (
            f"{g_emoji} Gold: 0\n"
            f"{s_emoji} Silver: 0\n"
            f"{c_emoji} Copper: 0"
        )
        embed.add_field(name="Owed", value=owed_display, inline=True)

        embed.add_field(name="\u200b", value="\u200b", inline=False)

        embed.add_field(
            name="Sources",
            value="*No pending income at this time.*",
            inline=False,
        )

        embed.add_field(
            name="Owed Total",
            value="*Nothing owed at this time.*",
            inline=False,
        )

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        embed.set_footer(text=f"Account: {self.member.display_name} • {ts}")
        return embed

    # ----------------------------
    # Message update
    # ----------------------------
    async def _update_message(
        self,
        interaction: discord.Interaction,
        page: BankPage,
        narrative_override: Optional[str] = None,
    ) -> None:
        # ACK immediately to prevent interaction timeout (3s limit)
        if not interaction.response.is_done():
            await interaction.response.defer()

        self.current_page = page
        self._update_button_styles()
        embed = await self.build_embed_for_page(page, narrative_override)

        try:
            await interaction.edit_original_response(embed=embed, view=self)
        except discord.HTTPException:
            try:
                if interaction.message:
                    await interaction.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

    # ----------------------------
    # Buttons
    # ----------------------------
    @discord.ui.button(
        label="Balance",
        style=discord.ButtonStyle.primary,
        custom_id="bank_balance",
        emoji="💰",
        row=0,
    )
    async def balance_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._pay_data_loaded = False
        self._ledger_offset = 0
        await self._update_message(interaction, BankPage.BALANCE)

    @discord.ui.button(
        label="View Pay",
        style=discord.ButtonStyle.secondary,
        custom_id="bank_sources",
        emoji="📊",
        row=0,
    )
    async def sources_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.current_page == BankPage.SOURCES:
            await self._handle_collect_pay(interaction)
        else:
            self._pay_data_loaded = False
            self._ledger_offset = 0
            await self._update_message(interaction, BankPage.SOURCES)

    async def _handle_collect_pay(self, interaction: discord.Interaction) -> None:
        lock = self.cog._get_lock(self.owner_id)

        if lock.locked():
            if not interaction.response.is_done():
                await interaction.response.defer()
            return

        async with lock:
            self._is_collecting = True
            self._disable_all_buttons()

            # ACK immediately to prevent interaction timeout (3s limit)
            if not interaction.response.is_done():
                await interaction.response.defer()

            # Show disabled buttons while processing
            try:
                await interaction.edit_original_response(view=self)
            except discord.HTTPException:
                pass

            try:
                if not self._pay_data_loaded or self._cached_wallet is None:
                    await self._load_pay_data()

                # v4.0: Check eligibility
                if not self._cached_is_eligible:
                    self._is_collecting = False
                    self._enable_all_buttons()
                    self._update_button_styles()

                    embed = self._build_pay_embed_no_read(
                        self._cached_wallet or {"gold": 0, "silver": 0, "copper": 0},
                        "pay_ineligible"
                    )
                    try:
                        await interaction.edit_original_response(embed=embed, view=self)
                    except discord.HTTPException:
                        pass
                    return

                payouts = self._cached_payouts_by_currency  # v4.1
                breakdown = self._cached_breakdown or []
                snapshot_plan = self._cached_snapshot_plan or []
                cached_wallet = self._cached_wallet or {"gold": 0, "silver": 0, "copper": 0}
                rates = self._cached_conversion_rates or dict(DEFAULT_CONVERSION_RATES)

                # v4.1: Check if any payout is owed
                total_owed = payouts.get("gold", 0) + payouts.get("silver", 0) + payouts.get("copper", 0)
                if total_owed == 0:
                    self._is_collecting = False
                    self._enable_all_buttons()
                    self._update_button_styles()

                    embed = self._build_pay_embed_no_read(cached_wallet, "pay_nothing")
                    try:
                        await interaction.edit_original_response(embed=embed, view=self)
                    except discord.HTTPException:
                        pass
                    return

                fresh_wallet, wallet_ok = await self.cog.fetch_wallet_only(self.member.id, fallback=None)
                if not wallet_ok:
                    self._is_collecting = False
                    self._enable_all_buttons()
                    self._update_button_styles()
                    embed = self.build_error_embed(
                        "Treasury read is currently rate-limited. Pay was not collected; try again shortly."
                    )
                    try:
                        await interaction.edit_original_response(embed=embed, view=self)
                    except discord.HTTPException:
                        pass
                    return

                new_wallet = await self.cog.apply_collect_pay(
                    self.member.id, payouts, breakdown, snapshot_plan,
                    fresh_wallet, rates
                )

                self._cached_wallet = new_wallet
                self._cached_payouts_by_currency = {"gold": 0, "silver": 0, "copper": 0}  # v4.1
                self._cached_breakdown = []
                self._cached_snapshot_plan = []
                self._pay_data_loaded = True

                self._is_collecting = False
                self._enable_all_buttons()
                self._update_button_styles()

                embed = self._build_pay_embed_no_read(new_wallet, "pay_collected")
                try:
                    await interaction.edit_original_response(embed=embed, view=self)
                except discord.HTTPException:
                    pass

            except Exception as e:
                print(f"[BankView] _handle_collect_pay failed: {type(e).__name__}: {e}")
                self._is_collecting = False
                self._enable_all_buttons()
                self._update_button_styles()

                try:
                    embed = self.build_error_embed("Failed to collect pay. Please try again.")
                    await interaction.edit_original_response(embed=embed, view=self)
                except discord.HTTPException:
                    pass

    @discord.ui.button(
        label="Ledger",
        style=discord.ButtonStyle.secondary,
        custom_id="bank_ledger",
        emoji="📜",
        row=0,
    )
    async def ledger_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._pay_data_loaded = False

        if self.current_page == BankPage.LEDGER:
            ledger = self._cached_ledger or []
            if ledger:
                self._ledger_offset += LEDGER_PAGE_STEP
                if self._ledger_offset >= len(ledger):
                    self._ledger_offset = 0
            await self._update_message(interaction, BankPage.LEDGER)
            return

        self._ledger_offset = 0
        await self._update_message(interaction, BankPage.LEDGER)

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.danger,
        custom_id="bank_close",
        emoji="🚪",
        row=0,
    )
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._is_closed = True
        self._cancel_auto_delete_timer()
        self._disable_all_buttons()
        self.current_page = BankPage.CLOSED

        # ACK immediately to prevent interaction timeout (3s limit)
        if not interaction.response.is_done():
            await interaction.response.defer()

        embed = await self.build_embed_for_page(BankPage.CLOSED)

        try:
            await interaction.edit_original_response(embed=embed, view=self)
        except discord.HTTPException:
            try:
                if interaction.message:
                    await interaction.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

        self.stop()


async def setup(bot: commands.Bot):
    await bot.add_cog(BankCog(bot))
