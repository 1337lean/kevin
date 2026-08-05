import pytest

from kevin.cogs.stream_alerts import normalize_twitch_login, render_alert_message
from kevin.cogs.trivia import CATEGORIES, DIFFICULTIES, QUESTIONS, matching_questions
from kevin.cogs.video_alerts import (
    normalize_tiktok_username,
    normalize_youtube_creator,
    parse_youtube_feed,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("TwitchDev", "twitchdev"),
        ("@TwitchDev", "twitchdev"),
        ("https://www.twitch.tv/TwitchDev", "twitchdev"),
        ("https://twitch.tv/twitchdev/videos", "twitchdev"),
    ],
)
def test_normalize_twitch_login(value: str, expected: str) -> None:
    assert normalize_twitch_login(value) == expected


@pytest.mark.parametrize("value", ["", "x", "https://example.com/name", "bad name"])
def test_reject_invalid_twitch_login(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_twitch_login(value)


def test_render_alert_message_only_replaces_supported_values() -> None:
    message = render_alert_message(
        "{role} {streamer}: {title} — {unknown}",
        {"role": "@Live", "streamer": "Kevin", "title": "Hello"},
    )
    assert message == "@Live Kevin: Hello — {unknown}"


def test_trivia_bank_has_every_category_and_difficulty() -> None:
    assert len(QUESTIONS) == len(CATEGORIES) * len(DIFFICULTIES) * 2
    for category in CATEGORIES:
        for difficulty in DIFFICULTIES:
            matches = matching_questions(category, difficulty)
            assert len(matches) == 2
            for _, question in matches:
                assert question.answer not in question.distractors
                assert len({question.answer, *question.distractors}) == 4


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("@GoogleDevelopers", ("handle", "GoogleDevelopers")),
        ("GoogleDevelopers", ("handle", "GoogleDevelopers")),
        ("https://youtube.com/@GoogleDevelopers/videos", ("handle", "GoogleDevelopers")),
        (
            "https://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA",
            ("id", "UCX6OQ3DkcsbYNE6H8uQQuVA"),
        ),
    ],
)
def test_normalize_youtube_creator(value: str, expected: tuple[str, str]) -> None:
    assert normalize_youtube_creator(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Creator.Name", "creator.name"),
        ("@Creator_Name", "creator_name"),
        ("https://www.tiktok.com/@Creator.Name/video/123", "creator.name"),
    ],
)
def test_normalize_tiktok_username(value: str, expected: str) -> None:
    assert normalize_tiktok_username(value) == expected


def test_parse_youtube_feed() -> None:
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:yt="http://www.youtube.com/xml/schemas/2015">
      <entry><yt:videoId>video-one</yt:videoId></entry>
      <entry><yt:videoId>video-two</yt:videoId></entry>
    </feed>"""
    assert parse_youtube_feed(body) == ["video-one", "video-two"]
