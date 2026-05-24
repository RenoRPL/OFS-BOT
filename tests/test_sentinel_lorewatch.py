from cogs.sentinel_lorewatch import (
    LORE_HEADER_ALIASES,
    _find_duplicate_lore,
    _header_map,
    _is_probably_substantial_lore,
    _lore_fingerprint,
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
