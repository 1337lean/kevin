import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from kevin.cogs.ai import (
    AI,
    Source,
    extract_response,
    format_reply,
    mentioned_question,
    with_reply_context,
)
from kevin.openai_chat import (
    INSTRUCTIONS,
    MemberMemory,
    OpenAIAPIError,
    OpenAIChatClient,
    ServerMessage,
    completed_web_search,
    requires_web_search,
    safe_memory_note,
    used_native_web_feed,
    with_discord_context,
)


def test_mentioned_question_strips_direct_bot_mentions() -> None:
    assert mentioned_question("hey <@123> look this up", 123) == "hey look this up"
    assert mentioned_question("<@!123> what's new?", 123) == "what's new?"
    assert mentioned_question("hello there", 123) is None


def test_reply_context_includes_previous_answer_and_follow_up() -> None:
    prompt = with_reply_context("what about tomorrow?", "It will rain today.")

    assert "<previous_reply>\nIt will rain today.\n</previous_reply>" in prompt
    assert "<follow_up>\nwhat about tomorrow?\n</follow_up>" in prompt


def test_reply_context_is_not_added_without_a_previous_reply() -> None:
    assert with_reply_context("new question", None) == "new question"


def test_discord_context_keeps_speakers_and_notes_separate() -> None:
    prompt = with_discord_context(
        "what game should we play?",
        speaker_id=20,
        speaker_name="Bea",
        recent_messages=(
            ServerMessage(1, 10, "Alex", "I like chess."),
            ServerMessage(2, 20, "Bea", "I like co-op games."),
        ),
        member_memories=(MemberMemory(10, "Alex", ("Likes chess",)),),
    )

    context_json = prompt.split("<discord_context_json>\n", 1)[1].split(
        "\n</discord_context_json>", 1
    )[0]
    context = json.loads(context_json)

    assert context["latest_speaker"] == {"user_id": "20", "display_name": "Bea"}
    assert context["recent_public_channel_messages"][0]["message_id"] == "1"
    assert context["recent_public_channel_messages"][0]["user_id"] == "10"
    assert context["recent_public_channel_messages"][0]["author_type"] == "member"
    assert context["recent_public_channel_messages"][0]["reply_to_message"] is None
    assert context["recent_public_channel_messages"][1]["user_id"] == "20"
    assert context["server_member_notes"][0]["notes"] == ["Likes chess"]
    assert context["latest_message"] == "what game should we play?"


def test_discord_context_includes_exact_reply_target() -> None:
    prompt = with_discord_context(
        "what did Piss reply to?",
        speaker_id=20,
        speaker_name="Learn",
        recent_messages=(
            ServerMessage(100, 10, "Alex", "The original message"),
            ServerMessage(
                101,
                30,
                "Piss",
                "Thats dope",
                reply_to_message_id=100,
                reply_to_user_id=10,
                reply_to_display_name="Alex",
                reply_to_content="The original message",
            ),
        ),
    )

    context_json = prompt.split("<discord_context_json>\n", 1)[1].split(
        "\n</discord_context_json>", 1
    )[0]
    context = json.loads(context_json)
    reply_target = context["recent_public_channel_messages"][1]["reply_to_message"]

    assert reply_target == {
        "message_id": "100",
        "user_id": "10",
        "display_name": "Alex",
        "author_type": "member",
        "content": "The original message",
    }
    assert "instead of inferring or guessing" in prompt


def test_memory_note_filter_rejects_sensitive_details() -> None:
    assert safe_memory_note("Likes cooperative games") == "Likes cooperative games"
    assert safe_memory_note("Email is person@example.com") is None
    assert safe_memory_note("Has a medical diagnosis") is None
    assert safe_memory_note("API key: abc123") is None


def test_observation_filter_skips_likely_private_content() -> None:
    assert AI._observable_content("ordinary server chat") == "ordinary server chat"
    assert AI._observable_content("email me at person@example.com") is None
    assert AI._observable_content("token: super-secret") is None
    assert AI._observable_content("mfa.abcdefghijklmnopqrstuvwxyz") is None


async def test_observation_records_discord_reply_message_id() -> None:
    database = SimpleNamespace(
        connection=object(),
        ai_memory_enabled=AsyncMock(return_value=True),
        record_ai_chat_message=AsyncMock(),
    )
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        settings=SimpleNamespace(openai_api_key="test"),
        db=database,
    )
    cog = AI(bot)
    guild = SimpleNamespace(id=10, me=SimpleNamespace(id=999))
    channel = SimpleNamespace(
        id=20,
        permissions_for=lambda _member: SimpleNamespace(view_channel=True),
    )
    message = SimpleNamespace(
        id=101,
        guild=guild,
        channel=channel,
        author=SimpleNamespace(id=30, display_name="Piss"),
        content="Thats dope",
        created_at=SimpleNamespace(isoformat=lambda: "2026-01-01T00:01:00+00:00"),
        reference=SimpleNamespace(message_id=100),
    )

    await cog._record_observation(message)

    assert database.record_ai_chat_message.await_args.kwargs["reply_to_message_id"] == 100


def test_memory_name_matching_uses_complete_display_names() -> None:
    assert AI._name_is_referenced("what does Alex like?", "Alex") is True
    assert AI._name_is_referenced("what does Alexandra like?", "Alex") is False


def test_channel_observation_uses_k_permissions_not_everyone_role() -> None:
    everyone = object()
    bot_member = SimpleNamespace(id=999)
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        settings=SimpleNamespace(openai_api_key="test"),
    )
    cog = AI(bot)
    guild = SimpleNamespace(id=10, default_role=everyone, me=bot_member)
    channel = SimpleNamespace(
        permissions_for=lambda target: SimpleNamespace(
            view_channel=target is bot_member
        )
    )
    message = SimpleNamespace(guild=guild, channel=channel)

    assert channel.permissions_for(everyone).view_channel is False
    assert cog._can_observe_channel(message) is True


async def test_discord_prompt_retrieves_separate_mem0_profiles_for_named_people() -> None:
    database = SimpleNamespace(
        connection=object(),
        ai_memory_enabled=AsyncMock(return_value=True),
        get_ai_chat_messages=AsyncMock(
            return_value=[
                {
                    "message_id": 40,
                    "user_id": 30,
                    "display_name": "Bea",
                    "content": "We should play co-op",
                }
            ]
        ),
        get_ai_memory_members=AsyncMock(
            return_value=[
                {"user_id": 20, "display_name": "Alex"},
                {"user_id": 30, "display_name": "Bea"},
            ]
        ),
    )
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999, display_name="K"),
        settings=SimpleNamespace(openai_api_key="test"),
        db=database,
    )
    cog = AI(bot)
    cog.memories = SimpleNamespace(
        ready=True,
        search_member=AsyncMock(
            side_effect=lambda _guild, user, _query, **_kwargs: {
                20: ["Likes chess"],
                30: ["Likes cooperative games"],
            }[user]
        ),
    )
    guild = SimpleNamespace(
        id=10,
        default_role=object(),
        me=SimpleNamespace(id=999),
        get_member=lambda _user_id: None,
    )
    channel = SimpleNamespace(
        id=40,
        permissions_for=lambda _role: SimpleNamespace(view_channel=True),
    )
    message = SimpleNamespace(
        id=50,
        guild=guild,
        channel=channel,
        author=SimpleNamespace(id=20, display_name="Alex"),
    )

    prompt = await cog._discord_prompt(message, "what does Bea like?", None)
    context_json = prompt.split("<discord_context_json>\n", 1)[1].split(
        "\n</discord_context_json>", 1
    )[0]
    context = json.loads(context_json)
    profiles = context["server_member_notes"]

    assert profiles == [
        {"user_id": "20", "display_name": "Alex", "notes": ["Likes chess"]},
        {
            "user_id": "30",
            "display_name": "Bea",
            "notes": ["Likes cooperative games"],
        },
    ]
    assert context["recent_public_channel_messages"] == [
        {
            "message_id": "40",
            "user_id": "30",
            "display_name": "Bea",
            "author_type": "member",
            "content": "We should play co-op",
            "reply_to_message": None,
        }
    ]
    database.get_ai_chat_messages.assert_awaited_once_with(
        10, 40, limit=24, exclude_message_id=50
    )
    assert [call.args[1] for call in cog.memories.search_member.await_args_list] == [20, 30]


async def test_discord_prompt_recovers_reply_target_from_discord_for_old_rows() -> None:
    database = SimpleNamespace(
        connection=object(),
        ai_memory_enabled=AsyncMock(return_value=True),
        get_ai_chat_messages=AsyncMock(
            return_value=[
                {
                    "message_id": 101,
                    "user_id": 30,
                    "display_name": "Piss",
                    "content": "Thats dope",
                    "reply_to_message_id": None,
                    "reply_to_user_id": None,
                    "reply_to_display_name": None,
                    "reply_to_content": None,
                }
            ]
        ),
    )
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999, display_name="K"),
        settings=SimpleNamespace(openai_api_key="test"),
        db=database,
    )
    cog = AI(bot)
    cog.memories = SimpleNamespace(ready=False)
    guild = SimpleNamespace(id=10, me=SimpleNamespace(id=999))
    channel = SimpleNamespace(
        id=40,
        permissions_for=lambda _member: SimpleNamespace(view_channel=True),
    )
    target = SimpleNamespace(
        id=100,
        channel=channel,
        author=SimpleNamespace(id=20, display_name="Alex"),
        content="The original message",
    )
    source = SimpleNamespace(
        id=101,
        channel=channel,
        reference=SimpleNamespace(
            message_id=100,
            resolved=None,
            cached_message=None,
        ),
    )
    channel.fetch_message = AsyncMock(side_effect=[source, target])
    message = SimpleNamespace(
        id=102,
        guild=guild,
        channel=channel,
        author=SimpleNamespace(id=50, display_name="Learn"),
    )

    prompt = await cog._discord_prompt(message, "what did Piss reply to?", None)
    context_json = prompt.split("<discord_context_json>\n", 1)[1].split(
        "\n</discord_context_json>", 1
    )[0]
    context = json.loads(context_json)

    assert context["recent_public_channel_messages"][0]["reply_to_message"] == {
        "message_id": "100",
        "user_id": "20",
        "display_name": "Alex",
        "author_type": "member",
        "content": "The original message",
    }
    assert [call.args[0] for call in channel.fetch_message.await_args_list] == [101, 100]


def test_requires_web_search_detects_explicit_and_current_requests() -> None:
    assert requires_web_search("Search the web for this") is True
    assert requires_web_search("Can you look this up?") is True
    assert requires_web_search("What's the latest OpenAI news?") is True
    assert requires_web_search("Summarize https://example.com/article") is True
    assert requires_web_search("Explain how decorators work") is False


def test_extract_response_collects_text_and_unique_safe_sources() -> None:
    payload = {
        "output": [
            {"type": "web_search_call", "status": "completed"},
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "A short answer.",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "Example [news]",
                                "url": "https://example.com/news",
                            },
                            {
                                "type": "url_citation",
                                "title": "Duplicate",
                                "url": "https://example.com/news",
                            },
                            {
                                "type": "url_citation",
                                "title": "Unsafe",
                                "url": "javascript:alert(1)",
                            },
                        ],
                    }
                ],
            },
        ]
    }

    text, sources = extract_response(payload)

    assert text == "A short answer."
    assert sources == [Source("Example news", "https://example.com/news")]


def test_extract_response_falls_back_to_web_search_action_sources() -> None:
    payload = {
        "output": [
            {
                "type": "web_search_call",
                "status": "completed",
                "action": {
                    "type": "search",
                    "sources": [
                        {"type": "url", "url": "https://www.example.com/news"},
                        {"type": "url", "url": "javascript:alert(1)"},
                    ],
                },
            },
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "A searched answer."}],
            },
        ]
    }

    text, sources = extract_response(payload)

    assert text == "A searched answer."
    assert sources == [Source("example.com", "https://www.example.com/news")]
    assert completed_web_search(payload) is True


def test_extract_response_removes_inline_citation_markup_and_tracking() -> None:
    citation = "([Example](https://example.com/news?utm_source=openai&id=7))"
    text_with_citation = f"A clean answer. {citation}"
    start = text_with_citation.index(citation)
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": text_with_citation,
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "Example",
                                "url": "https://example.com/news?utm_source=openai&id=7",
                                "start_index": start,
                                "end_index": start + len(citation),
                            }
                        ],
                    }
                ],
            }
        ]
    }

    text, sources = extract_response(payload)

    assert text == "A clean answer."
    assert sources == [Source("Example", "https://example.com/news?id=7")]


def test_format_reply_adds_clickable_sources_and_stays_within_discord_limit() -> None:
    reply = format_reply("x" * 2_500, [Source("Example", "https://example.com")])

    assert len(reply) == 2_000
    assert reply.endswith("Sources: [Example](https://example.com)")


async def test_ai_reply_suppresses_discord_link_previews() -> None:
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        settings=SimpleNamespace(openai_api_key="test"),
    )
    cog = AI(bot)
    cog._ask_openai = AsyncMock(
        return_value=("A short answer.", [Source("Example", "https://example.com")])
    )
    message = SimpleNamespace(
        guild=object(),
        author=SimpleNamespace(id=1),
        content="<@999> look this up",
        reference=None,
        channel=SimpleNamespace(typing=lambda: AsyncMock()),
        reply=AsyncMock(),
    )

    await cog.on_message(message)

    message.reply.assert_awaited_once()
    args, kwargs = message.reply.await_args
    assert args == ("A short answer.\n\nSources: [Example](https://example.com)",)
    assert kwargs["mention_author"] is False
    assert kwargs["allowed_mentions"].everyone is False
    assert kwargs["allowed_mentions"].users is False
    assert kwargs["allowed_mentions"].roles is False
    assert kwargs["suppress_embeds"] is True


async def test_self_disclosure_schedules_mem0_without_ping() -> None:
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        settings=SimpleNamespace(openai_api_key="test"),
    )
    cog = AI(bot)
    cog._record_observation = AsyncMock(return_value=True)
    cog._schedule_memory_refresh = Mock()
    message = SimpleNamespace(
        guild=SimpleNamespace(id=10),
        author=SimpleNamespace(id=20, bot=False),
        channel=SimpleNamespace(id=30),
        content="I like chicken",
        reference=None,
    )

    await cog.on_message(message)

    cog._record_observation.assert_awaited_once_with(message)
    cog._schedule_memory_refresh.assert_called_once_with(10, 30)


async def test_mem0_refresh_ingests_i_like_chicken_for_the_correct_member() -> None:
    added_messages: list[str] = []

    async def capture_messages(_guild, _user, _name, messages) -> None:
        added_messages.extend(messages)

    database = SimpleNamespace(
        connection=object(),
        ai_memory_enabled=AsyncMock(return_value=True),
        get_ai_chat_messages=AsyncMock(
            return_value=[
                {
                    "message_id": 40,
                    "user_id": 20,
                    "display_name": "Alex",
                    "content": "I like chicken",
                }
            ]
        ),
        get_ai_memory_watermark=AsyncMock(return_value=0),
        set_ai_memory_watermark=AsyncMock(),
    )
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        settings=SimpleNamespace(openai_api_key="test"),
        db=database,
    )
    cog = AI(bot)
    cog.memories = SimpleNamespace(
        ready=True,
        add_member_messages=AsyncMock(side_effect=capture_messages),
    )

    await cog._refresh_memories(10, 30)

    assert cog.memories.add_member_messages.await_args.args[:3] == (10, 20, "Alex")
    assert added_messages == ["I like chicken"]
    database.set_ai_memory_watermark.assert_awaited_once_with(10, 30, 40)


async def test_memory_show_flushes_pending_observations_before_listing() -> None:
    events: list[str] = []

    async def refresh(*_args) -> None:
        events.append("refresh")

    async def list_member(*_args) -> list[str]:
        events.append("list")
        return ["User likes chicken"]

    database = SimpleNamespace(
        connection=object(),
        ai_memory_enabled=AsyncMock(return_value=True),
    )
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        settings=SimpleNamespace(openai_api_key="test"),
        db=database,
    )
    cog = AI(bot)
    cog._run_memory_refresh = AsyncMock(side_effect=refresh)
    cog.memories = SimpleNamespace(
        ready=True,
        list_member=AsyncMock(side_effect=list_member),
    )
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=10),
        channel=SimpleNamespace(id=30),
        author=SimpleNamespace(id=20),
        interaction=object(),
        send=AsyncMock(),
    )

    await AI.memory.callback(cog, ctx)

    cog._run_memory_refresh.assert_awaited_once_with(10, 30)
    cog.memories.list_member.assert_awaited_once_with(10, 20)
    assert events == ["refresh", "list"]
    ctx.send.assert_awaited_once_with(
        "Here’s what I remember about you in this server:\n• User likes chicken",
        ephemeral=True,
    )


class _FakeOpenAIResponse:
    status = 200

    def __init__(self, data=None):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, **_kwargs):
        if self.data is not None:
            return self.data
        return {
            "output": [
                {"type": "web_search_call", "status": "completed"},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "searched"}],
                },
            ]
        }


class _FakeOpenAISession:
    def __init__(self, responses=None) -> None:
        self.payload = None
        self.payloads = []
        self.responses = list(responses or [])

    def post(self, _url, **kwargs):
        self.payload = kwargs["json"]
        self.payloads.append(self.payload)
        if self.responses:
            return _FakeOpenAIResponse(self.responses.pop(0))
        return _FakeOpenAIResponse()


def _sourced_response(text: str = "sourced answer") -> dict:
    return {
        "output": [
            {"type": "web_search_call", "status": "completed"},
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "Example source",
                                "url": "https://example.com/source",
                            }
                        ],
                    }
                ],
            },
        ]
    }


def _native_feed_response(text: str = "native answer") -> dict:
    return {
        "output": [
            {
                "type": "web_search_call",
                "status": "completed",
                "action": {
                    "type": "search",
                    "sources": [{"type": "api", "name": "oai-finance"}],
                },
            },
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            },
        ]
    }


async def test_openai_client_can_require_web_search() -> None:
    client = OpenAIChatClient("test-key", "test-model")
    session = _FakeOpenAISession()
    client.session = session

    text, _sources = await client.ask("look this up", require_web_search=True)

    assert text == "searched"
    assert session.payload["tools"] == [{"type": "web_search"}]
    assert session.payload["tool_choice"] == "required"
    assert session.payload["include"] == ["web_search_call.action.sources"]


async def test_openai_client_defaults_to_discord_style_automatic_web_search() -> None:
    client = OpenAIChatClient("test-key", "test-model")
    session = _FakeOpenAISession()
    client.session = session

    await client.ask("look this up")

    assert session.payload["tools"] == [{"type": "web_search"}]
    assert "tool_choice" not in session.payload
    assert "must use web search" in INSTRUCTIONS
    assert "Do not claim that you searched" in INSTRUCTIONS


async def test_openai_client_source_link_mode_uses_url_backed_search() -> None:
    client = OpenAIChatClient("test-key", "test-model")
    session = _FakeOpenAISession([_sourced_response()])
    client.session = session

    text, sources = await client.ask(
        "what is the current price?",
        require_web_search=True,
        require_source_links=True,
    )

    assert text == "sourced answer"
    assert sources == [Source("Example source", "https://example.com/source")]
    assert session.payload["tools"] == [
        {"type": "web_search", "search_content_types": ["text"]}
    ]


async def test_openai_client_retries_a_search_that_has_no_public_url() -> None:
    client = OpenAIChatClient("test-key", "test-model")
    session = _FakeOpenAISession(
        [
            await _FakeOpenAIResponse().json(),
            _sourced_response("retried with a public source"),
        ]
    )
    client.session = session

    text, sources = await client.ask(
        "what is the current price?",
        require_web_search=True,
        require_source_links=True,
    )

    assert text == "retried with a public source"
    assert sources == [Source("Example source", "https://example.com/source")]
    assert len(session.payloads) == 2
    assert "User request:\nwhat is the current price?" in session.payloads[1]["input"]
    assert "searched" not in session.payloads[1]["input"]


def test_source_retry_extracts_latest_message_from_discord_context() -> None:
    prompt = with_discord_context(
        "what is the latest release?",
        speaker_id=20,
        speaker_name="Bea",
        recent_messages=(ServerMessage(1, 10, "Alex", "x" * 3_000),),
    )

    retry = OpenAIChatClient._source_retry_input(prompt)

    assert "User request:\nwhat is the latest release?" in retry
    assert "x" * 100 not in retry


async def test_openai_client_rejects_native_feeds_and_retries_independently() -> None:
    client = OpenAIChatClient("test-key", "test-model")
    first_answer = "stale native answer"
    session = _FakeOpenAISession(
        [
            _native_feed_response(first_answer),
            _sourced_response("independent webpage answer"),
        ]
    )
    client.session = session

    text, sources = await client.ask(
        "what is the current price?",
        require_web_search=True,
        require_source_links=True,
        reject_native_feeds=True,
    )

    assert text == "independent webpage answer"
    assert sources == [Source("Example source", "https://example.com/source")]
    assert first_answer not in session.payloads[1]["input"]
    assert used_native_web_feed(_native_feed_response()) is True


async def test_openai_client_fails_if_the_retry_still_uses_a_native_feed() -> None:
    client = OpenAIChatClient("test-key", "test-model")
    session = _FakeOpenAISession([_native_feed_response(), _native_feed_response()])
    client.session = session

    try:
        await client.ask(
            "what is the current price?",
            require_web_search=True,
            require_source_links=True,
            reject_native_feeds=True,
        )
    except OpenAIAPIError as exc:
        assert exc.status == 502
        assert "rejected native feed" in str(exc)
    else:
        raise AssertionError("A repeated native-feed response should fail")


async def test_openai_client_rejects_a_missing_required_web_search() -> None:
    client = OpenAIChatClient("test-key", "test-model")
    session = _FakeOpenAISession()
    client.session = session

    original_json = _FakeOpenAIResponse.json

    async def json_without_search(self, **_kwargs):
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "not searched"}],
                }
            ]
        }

    _FakeOpenAIResponse.json = json_without_search
    try:
        try:
            await client.ask("look this up", require_web_search=True)
        except OpenAIAPIError as exc:
            assert exc.status == 502
            assert "required web search" in str(exc)
        else:
            raise AssertionError("A missing required web search should fail")
    finally:
        _FakeOpenAIResponse.json = original_json


def test_extract_image_decodes_b64_payload() -> None:
    from kevin.openai_images import extract_image

    payload = {"data": [{"b64_json": "aGVsbG8="}]}

    assert extract_image(payload) == b"hello"


def test_extract_image_returns_url_when_no_b64() -> None:
    from kevin.openai_images import extract_image

    payload = {"data": [{"url": "https://example.com/image.png"}]}

    assert extract_image(payload) == "https://example.com/image.png"


def test_extract_image_rejects_empty_payload() -> None:
    from kevin.openai_images import extract_image

    for payload in ({}, {"data": []}, {"data": [{}]}, {"data": [{"b64_json": "!!"}]}):
        try:
            extract_image(payload)
        except OpenAIAPIError as exc:
            assert exc.status == 502
        else:
            raise AssertionError(f"payload {payload!r} should fail")


async def test_discord_cog_uses_a_live_price_api_for_quote_questions() -> None:
    """Discord must take the same verified-price path Telegram does."""
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        settings=SimpleNamespace(openai_api_key="test"),
    )
    cog = AI(bot)
    cog.http = object()
    cog.openai.ask = AsyncMock()
    quote = ("Bitcoin (BTC) is **$68,000.00** on Coinbase.", [Source("Coinbase BTC quote", "u")])

    with patch(
        "kevin.cogs.ai.live_price_reply", new=AsyncMock(return_value=quote)
    ) as fetch_price:
        text, sources = await cog._ask_openai("what's the price of bitcoin")

    fetch_price.assert_awaited_once()
    cog.openai.ask.assert_not_awaited()
    assert text == "Bitcoin (BTC) is **$68,000.00** on Coinbase."
    assert sources[0].title == "Coinbase BTC quote"


async def test_discord_cog_requires_sourced_search_for_non_price_questions() -> None:
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        settings=SimpleNamespace(openai_api_key="test"),
    )
    cog = AI(bot)
    cog.http = object()
    cog.openai.ask = AsyncMock(return_value=("answer", []))

    await cog._ask_openai("what's the latest news about OpenAI")

    assert cog.openai.ask.await_args.kwargs == {
        "require_web_search": True,
        "require_source_links": True,
        "reject_native_feeds": True,
    }
