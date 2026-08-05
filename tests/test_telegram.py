from collections import deque
from unittest.mock import AsyncMock

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
    }
