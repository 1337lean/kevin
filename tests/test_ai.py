from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    OpenAIAPIError,
    OpenAIChatClient,
    completed_web_search,
    requires_web_search,
    used_native_web_feed,
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
    cog.cooldowns = SimpleNamespace(
        get_bucket=lambda _message: SimpleNamespace(update_rate_limit=lambda: None)
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
