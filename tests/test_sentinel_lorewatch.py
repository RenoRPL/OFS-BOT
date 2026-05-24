import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import cogs.sentinel_lorewatch as lorewatch
from cogs.sentinel_lorewatch import (
    LORE_HEADER_ALIASES,
    PRIMARY_APPROVER_ID,
    SentinelLorewatch,
    _default_april_backfill_start,
    _find_duplicate_lore,
    _header_map,
    _is_probably_substantial_lore,
    _lore_fingerprint,
    _oracle_handoff_message,
    _oracle_mention,
    _parse_backfill_since,
    _group_chronicle_messages,
    _message_group_text,
    _message_group_source_ids,
    _message_ids_already_ticketed,
    _default_may_backfill_start,
    _default_may_backfill_end,
    _parse_backfill_until,
)


def test_lore_fingerprint_ignores_case_punctuation_and_spacing():
    a = "The Fallen Star rose — again!\n\nGlory to the Order."
    b = "the fallen star rose again glory to the order"

    assert _lore_fingerprint(a) == _lore_fingerprint(b)


def test_header_map_recognizes_lore_intake_headers():
    headers = [
        "Lore ID",
        "Status",
        "Source Message ID",
        "Original Text",
        "Attachment URLs",
        "Admin Thread ID",
    ]

    mapped = _header_map(headers, LORE_HEADER_ALIASES)

    assert mapped["lore_id"] == 0
    assert mapped["status"] == 1
    assert mapped["source_message_id"] == 2
    assert mapped["original_text"] == 3
    assert mapped["attachment_urls"] == 4
    assert mapped["admin_thread_id"] == 5


def test_find_duplicate_lore_matches_source_message_id_first():
    headers = ["Lore ID", "Status", "Source Message ID", "Original Text"]
    rows = [headers, ["LORE-20260524-001", "Captured", "12345", "different text"]]
    hmap = _header_map(headers, LORE_HEADER_ALIASES)

    duplicate = _find_duplicate_lore(rows, hmap, source_message_id="12345", source_text="new lore text")

    assert duplicate is not None
    assert duplicate["lore_id"] == "LORE-20260524-001"
    assert duplicate["reason"] == "source_message_id"


def test_find_duplicate_lore_matches_normalized_text_when_source_missing():
    headers = ["Lore ID", "Status", "Source Message ID", "Original Text"]
    rows = [headers, ["LORE-20260524-002", "Published", "", "The Yormandi were slain beneath the world."]]
    hmap = _header_map(headers, LORE_HEADER_ALIASES)

    duplicate = _find_duplicate_lore(
        rows,
        hmap,
        source_message_id="99999",
        source_text="the yormandi were slain beneath the world",
    )

    assert duplicate is not None
    assert duplicate["lore_id"] == "LORE-20260524-002"
    assert duplicate["reason"] == "normalized_text"


def test_short_chatter_without_attachments_is_not_substantial_lore():
    assert _is_probably_substantial_lore("nice", []) is False
    assert _is_probably_substantial_lore("", ["https://cdn.discordapp.com/image.png"]) is True
    assert _is_probably_substantial_lore(
        "The chronicle records the banners crossing into the dark, carrying the Fallen Star forward.",
        [],
    ) is True


def test_parse_backfill_since_accepts_april_date_as_utc_start_of_day():
    parsed = _parse_backfill_since("2026-04-01")

    assert parsed.year == 2026
    assert parsed.month == 4
    assert parsed.day == 1
    assert parsed.hour == 0
    assert parsed.tzinfo == timezone.utc


def test_default_april_backfill_start_uses_current_year():
    default_start = _default_april_backfill_start()

    assert default_start.month == 4
    assert default_start.day == 1
    assert default_start.hour == 0
    assert default_start.minute == 0
    assert default_start.tzinfo == timezone.utc


def test_oracle_handoff_message_mentions_oracle_and_includes_plain_text_lore():
    message = SimpleNamespace(
        id=999,
        guild=SimpleNamespace(id=111),
        channel=SimpleNamespace(id=222),
        author=SimpleNamespace(id=333, __str__=lambda self: "Lorekeeper"),
    )
    lore_text = "The banners crossed the void and recorded a new Chronicle for the Fallen Star."
    attachment_urls = ["https://cdn.discordapp.com/lore.png"]

    handoff = _oracle_handoff_message(
        "LORE-20260524-009",
        message,
        "Chronicles / Timeline",
        "Place in the April campaign sequence.",
        lore_text,
        attachment_urls,
    )

    assert handoff.startswith(_oracle_mention())
    assert "ORACLE LORE REVIEW REQUEST" in handoff
    assert "LORE-20260524-009" in handoff
    assert "https://discord.com/channels/111/222/999" in handoff
    assert "Chronicles / Timeline" in handoff
    assert "Place in the April campaign sequence." in handoff
    assert lore_text in handoff
    assert attachment_urls[0] in handoff
    assert "Canon Conflicts / Duplicate Risk" in handoff


def test_lore_backfill_authority_is_primary_approver_only(monkeypatch):
    class FakeMember:
        def __init__(self, user_id, *, administrator=False, manage_guild=False):
            self.id = user_id
            self.guild_permissions = SimpleNamespace(
                administrator=administrator,
                manage_guild=manage_guild,
            )

    monkeypatch.setattr(lorewatch.discord, "Member", FakeMember)
    cog = SentinelLorewatch(bot=SimpleNamespace())

    allowed = SimpleNamespace(user=FakeMember(PRIMARY_APPROVER_ID))
    admin = SimpleNamespace(user=FakeMember(111, administrator=True))
    manager = SimpleNamespace(user=FakeMember(222, manage_guild=True))
    regular = SimpleNamespace(user=FakeMember(333))

    assert asyncio.run(cog._user_can_run_backfill(allowed)) is True
    assert asyncio.run(cog._user_can_run_backfill(admin)) is False
    assert asyncio.run(cog._user_can_run_backfill(manager)) is False
    assert asyncio.run(cog._user_can_run_backfill(regular)) is False


def _fake_lore_message(message_id, author_id, content, created_at):
    return SimpleNamespace(
        id=message_id,
        content=content,
        created_at=created_at,
        author=SimpleNamespace(id=author_id, bot=False),
        attachments=[],
    )


def test_group_chronicle_messages_combines_same_author_story_parts():
    start = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    messages = [
        _fake_lore_message(101, 7, "The Burning of the Shattered Blade begins with the fleet in shadow.", start),
        _fake_lore_message(102, 7, "I. The Wounding of the Endeavor carried the same Chronicle forward.", start + timedelta(minutes=3)),
        _fake_lore_message(103, 7, "II. The flames rose again as the same tale continued beyond Discord limits.", start + timedelta(minutes=6)),
    ]

    groups = _group_chronicle_messages(messages)

    assert len(groups) == 1
    assert [m.id for m in groups[0]] == [101, 102, 103]
    assert _message_group_source_ids(groups[0]) == "101,102,103"
    merged = _message_group_text(groups[0])
    assert "[Discord message 101]" in merged
    assert "[Discord message 103]" in merged
    assert "same tale continued" in merged


def test_group_chronicle_messages_splits_different_authors_or_large_gaps():
    start = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    messages = [
        _fake_lore_message(201, 7, "A substantial first Chronicle entry that should stand on its own.", start),
        _fake_lore_message(202, 8, "Another substantial Chronicle entry from another author entirely.", start + timedelta(minutes=2)),
        _fake_lore_message(203, 7, "A later substantial Chronicle from the original author after another author break.", start + timedelta(hours=8)),
    ]

    groups = _group_chronicle_messages(messages)

    assert [[m.id for m in group] for group in groups] == [[201], [202], [203]]


def test_group_chronicle_messages_keeps_uninterrupted_same_author_run_together_even_over_long_gap():
    start = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    messages = [
        _fake_lore_message(301, 7, "The Burning of the Shattered Blade opens as one long Chronicle entry.", start),
        _fake_lore_message(302, 7, "I. The Wounding of the Endeavor continues the same uninterrupted story.", start + timedelta(hours=8)),
        _fake_lore_message(303, 7, "II. The Rally of the First Fleet continues with no author/avatar break.", start + timedelta(hours=16)),
    ]

    groups = _group_chronicle_messages(messages)

    assert [[m.id for m in group] for group in groups] == [[301, 302, 303]]


def test_default_backfill_window_is_may_2026_only():
    assert _default_may_backfill_start() == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert _default_may_backfill_end() == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert _parse_backfill_since(None) == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert _parse_backfill_until(None) == datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_message_ids_already_ticketed_reads_single_and_grouped_source_columns():
    headers = ["Lore ID", "Status", "Source Message ID", "Source Message IDs", "Original Text"]
    rows = [
        headers,
        ["LORE-20260501-001", "Captured", "101", "101,102,103", "A grouped Chronicle"],
        ["LOREWATCH-CHECKPOINT", "System Checkpoint", "103", "", ""],
    ]
    hmap = _header_map(headers, LORE_HEADER_ALIASES)

    ticketed = _message_ids_already_ticketed(rows, hmap)

    assert ticketed == {"101", "102", "103"}
