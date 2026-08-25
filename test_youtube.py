"""Tag-resolution ladder for /youtube — the one place non-trivial parsing lives.
No discord/db needed; library.resolve_yt_tags is pure."""

import library


def test_override_beats_everything():
    assert library.resolve_yt_tags(
        "Some Channel", "Some Track", "Random - Title (Official)",
        "Nina Simone", "Sinnerman") == ("Nina Simone", "Sinnerman")


def test_ytmusic_metadata_wins_over_title():
    assert library.resolve_yt_tags(
        "Radiohead", "Creep", "Radiohead - Creep (Official Music Video)",
        None, None) == ("Radiohead", "Creep")


def test_parse_from_video_title_when_no_metadata():
    assert library.resolve_yt_tags(
        "NA", "NA", "Soul Asylum - Runaway Train (Official Video)",
        None, None) == ("Soul Asylum", "Runaway Train")


def test_junk_stripped_from_parsed_title():
    a, t = library.resolve_yt_tags("NA", "NA", "The Cure - Just Like Heaven [HD Remastered]", None, None)
    assert a == "The Cure"
    assert t == "Just Like Heaven"


def test_no_separator_falls_back_to_title():
    a, t = library.resolve_yt_tags("NA", "NA", "just some upload", None, None)
    assert a == "Unknown Artist"
    assert t == "just some upload"


def test_partial_override_fills_the_other_from_ladder():
    # only artist overridden -> title still comes from the metadata field
    assert library.resolve_yt_tags(
        "NA", "Sinnerman", "whatever", "Nina Simone", None) == ("Nina Simone", "Sinnerman")
