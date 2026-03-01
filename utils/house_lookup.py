# utils/house_lookup.py
from __future__ import annotations

import time
from typing import Dict, Optional

import discord

# Cache: guild_id -> {"map": {banner: house}, "ts": unix_time}
_HOUSE_CACHE: Dict[int, Dict[str, object]] = {}

# How long to keep cache before refreshing (seconds)
CACHE_TTL = 300  # 5 minutes


async def build_banner_to_house_map(
    forum_channel: discord.ForumChannel,
    *,
    include_archived: bool = True,
    limit_archived: int = 200,
) -> Dict[str, str]:
    """
    Returns a mapping:
        { "<banner role name>": "<house name (thread title)>" }

    Assumes:
      - One forum post (thread) per banner.
      - That thread has an applied tag whose NAME matches the banner role name.
      - House name is the thread title.
    """
    mapping: Dict[str, str] = {}

    # Active threads
    for thread in forum_channel.threads:
        for tag in thread.applied_tags:
            mapping[tag.name] = thread.name

    if include_archived:
        # Public archived threads
        try:
            async for thread in forum_channel.archived_threads(limit=limit_archived):
                for tag in thread.applied_tags:
                    # Don't overwrite an active mapping if already found
                    mapping.setdefault(tag.name, thread.name)
        except Exception:
            # If bot lacks perms for archived threads, we just skip silently.
            pass

    return mapping


async def get_house_for_banner(
    guild: discord.Guild,
    forum_channel_id: int,
    banner_role_name: str,
    *,
    force_refresh: bool = False,
) -> Optional[str]:
    """
    Looks up the house name for a given banner role name.
    Uses cached mapping per guild for speed.
    """
    now = time.time()
    cache = _HOUSE_CACHE.get(guild.id)

    if (
        force_refresh
        or cache is None
        or (now - float(cache["ts"])) > CACHE_TTL
    ):
        channel = guild.get_channel(forum_channel_id)
        if not isinstance(channel, discord.ForumChannel):
            return None

        mapping = await build_banner_to_house_map(channel)
        _HOUSE_CACHE[guild.id] = {"map": mapping, "ts": now}
        cache = _HOUSE_CACHE[guild.id]

    mapping: Dict[str, str] = cache["map"]  # type: ignore[assignment]
    return mapping.get(banner_role_name)
