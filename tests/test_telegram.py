from collections import deque
from unittest.mock import AsyncMock, patch

from kevin.config import TelegramSettings
from kevin.openai_chat import (
    MAX_CONVERSATION_TURNS,
    ConversationTurn,
    Source,
    with_conversation_context,
)
from kevin.telegram_bot import (
    MAX_TELEGRAM_LENGTH,
    TelegramKevin,
    format_telegram_reply,
    telegram_question,
)


def message(text: str, *, chat_type: str = "private", reply_to_bot: bool = False) -> dict:
    payload = {
        "message_id": 10,
        "text": text,
        "chat": {"id": 20, "type": chat_type},
        "from": {"id": 30, "is_bot": False},
    }
    if reply_to_bot:
        payload["reply_to_message"] = {
            "message_id": 9,
            "text": "Kevin's earlier answer.",
            "from": {"id": 99, "is_bot": True},
        }
    return payload


def test_private_messages_activate_without_a_mention() -> None:
    assert telegram_question(message("what's new?"), bot_id=99, username="KevinBot") == (
        "what's new?",
        None,
    )


def test_group_messages_require_a_mention_or_reply() -> None:
    quiet = message("what's new?", chat_type="supergroup")
    mentioned = message("hey @KevinBot, what's new?", chat_type="supergroup")
    replied = message("what about tomorrow?", chat_type="supergroup", reply_to_bot=True)

    assert telegram_question(quiet, bot_id=99, username="KevinBot") == (None, None)
    assert telegram_question(mentioned, bot_id=99, username="KevinBot") == (
        "hey what's new?",
        None,
    )
    assert telegram_question(replied, bot_id=99, username="KevinBot") == (
        "what about tomorrow?",
        "Kevin's earlier answer.",
    )


def test_telegram_reply_has_clickable_sources_and_fits_limit() -> None:
    reply = format_telegram_reply(
        "x" * 5_000,
        [Source("Example", "https://example.com/news")],
    )

    assert len(reply) == MAX_TELEGRAM_LENGTH
    assert reply.endswith("Sources:\n• Example — https://example.com/news")


def test_telegram_reply_shows_only_one_clean_link_per_site() -> None:
    reply = format_telegram_reply(
        "A sourced answer.",
        [
            Source("Example article", "https://example.com/one"),
            Source("Duplicate site", "https://www.example.com/two"),
            Source("Another source", "https://another.example/news"),
        ],
    )

    assert reply == (
        "A sourced answer.\n\nSources:\n"
        "• Example article — https://example.com/one\n"
        "• Another source — https://another.example/news"
    )


def test_telegram_reply_uses_site_name_instead_of_a_truncated_title() -> None:
    reply = format_telegram_reply(
        "A sourced answer.",
        [Source("A very long article title " * 5, "https://www.example.com/news")],
    )

    assert reply.endswith("Sources:\n• example.com — https://www.example.com/news")


def test_telegram_reply_does_not_claim_search_without_sources() -> None:
    assert format_telegram_reply("A regular answer.", []) == "A regular answer."


def test_conversation_context_keeps_multiple_recent_turns() -> None:
    history = deque(
        ConversationTurn(f"question {number}", f"answer {number}")
        for number in range(MAX_CONVERSATION_TURNS + 2)
    )

    context = with_conversation_context("latest question", history)

    assert len(context) == MAX_CONVERSATION_TURNS * 2 + 1
    assert context[0] == {"role": "user", "content": "question 2"}
    assert context[1] == {"role": "assistant", "content": "answer 2"}
    assert context[-1] == {"role": "user", "content": "latest question"}


async def test_telegram_answers_reuse_history_without_forcing_search_for_regular_chat() -> None:
    bot = TelegramKevin(TelegramSettings(token="telegram-token", openai_api_key="openai-key"))
    bot.openai.ask = AsyncMock(side_effect=[("first answer", []), ("second answer", [])])
    bot._typing = AsyncMock()
    bot._send_message = AsyncMock()
    incoming = message("question", chat_type="supergroup")

    await bot._answer(incoming, "first question", None)
    bot.cooldowns.clear()
    await bot._answer(incoming, "second question", None)

    second_call = bot.openai.ask.await_args_list[1]
    assert second_call.kwargs == {
        "require_web_search": False,
        "require_source_links": True,
        "reject_native_feeds": True,
    }
    assert second_call.args[0][:2] == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]
    assert second_call.args[0][-1] == {
        "role": "user",
        "content": "second question",
    }


async def test_telegram_requires_search_for_an_explicit_web_request() -> None:
    bot = TelegramKevin(TelegramSettings(token="telegram-token", openai_api_key="openai-key"))
    bot.openai.ask = AsyncMock(return_value=("searched answer", []))
    bot._typing = AsyncMock()
    bot._send_message = AsyncMock()

    await bot._answer(message("question"), "Search the web for the latest news", None)

    assert bot.openai.ask.await_args.kwargs == {
        "require_web_search": True,
        "require_source_links": True,
        "reject_native_feeds": True,
    }


async def test_telegram_uses_a_live_price_api_instead_of_openai_for_quotes() -> None:
    bot = TelegramKevin(TelegramSettings(token="telegram-token", openai_api_key="openai-key"))
    bot.session = object()
    bot.openai.ask = AsyncMock()
    bot._typing = AsyncMock()
    bot._send_message = AsyncMock()
    sourced_quote = (
        "Ethereum (ETH) is **$1,900.00** on Coinbase.",
        [Source("Coinbase ETH quote", "https://api.coinbase.com/price")],
    )

    with patch(
        "kevin.telegram_bot.live_price_reply",
        new=AsyncMock(return_value=sourced_quote),
    ) as fetch_price:
        await bot._answer(
            message("what's ethereum's current price"),
            "what's ethereum's current price",
            None,
        )

    fetch_price.assert_awaited_once()
    bot.openai.ask.assert_not_awaited()
    sent_text = bot._send_message.await_args.args[1]
    assert "Coinbase ETH quote" in sent_text
    assert "https://api.coinbase.com/price" in sent_text


async def test_telegram_falls_back_to_search_when_no_asset_is_recognised() -> None:
    """A price question about something neither provider quotes still gets answered."""
    bot = TelegramKevin(TelegramSettings(token="telegram-token", openai_api_key="openai-key"))
    bot.session = object()
    bot.openai.ask = AsyncMock(return_value=("about $500", []))
    bot._typing = AsyncMock()
    bot._send_message = AsyncMock()

    with patch("kevin.telegram_bot.live_price_reply", new=AsyncMock(return_value=None)):
        await bot._answer(
            message("what's the price of a PS5"), "what's the price of a PS5", None
        )

    bot.openai.ask.assert_awaited_once()
    assert bot.openai.ask.await_args.kwargs["require_source_links"] is True
