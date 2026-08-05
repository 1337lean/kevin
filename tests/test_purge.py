from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from kevin.cogs.moderation import PURGE_SEARCH_LIMIT, Moderation


def message(message_id: int, author_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=message_id, author=SimpleNamespace(id=author_id))


class FakeChannel:
    """Records what history() walked and what purge() was asked to delete."""

    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self.messages = messages  # newest first, as Discord returns them
        self.scanned = 0
        self.purge_kwargs: dict | None = None

    def history(self, *, limit=None, before=None):
        async def walk():
            for item in self.messages[:limit]:
                self.scanned += 1
                yield item

        return walk()

    async def purge(self, **kwargs):
        self.purge_kwargs = kwargs
        window = self.messages
        after = kwargs.get("after")
        if after is not None:
            window = [m for m in window if m.id > after.id]
        check = kwargs.get("check")
        if check is not None:
            return [m for m in window if check(m)]
        return window[: kwargs["limit"]]


def context(channel: FakeChannel) -> SimpleNamespace:
    return SimpleNamespace(
        channel=channel,
        interaction=None,
        message=SimpleNamespace(id=10_000),
        author=SimpleNamespace(id=1, __str__=lambda self: "mod"),
        send=AsyncMock(),
    )


def cog() -> Moderation:
    return Moderation(SimpleNamespace())


async def test_purge_deletes_the_requested_count_of_a_members_messages() -> None:
    # The member's messages are spread thinly through a busy channel.
    channel = FakeChannel(
        [message(900 - i, author_id=7 if i % 5 == 0 else 99) for i in range(200)]
    )
    ctx = context(channel)

    deleted = await cog().purge_member_messages(
        ctx, SimpleNamespace(id=7), 10, ctx.message, "reason"
    )

    assert len(deleted) == 10
    assert {m.author.id for m in deleted} == {7}


async def test_purge_stops_scanning_once_it_has_enough() -> None:
    channel = FakeChannel([message(900 - i, author_id=7) for i in range(500)])
    ctx = context(channel)

    await cog().purge_member_messages(ctx, SimpleNamespace(id=7), 5, ctx.message, "reason")

    assert channel.scanned == 5, "history should stop at the fifth match"


async def test_purge_bounds_the_delete_to_the_window_it_walked() -> None:
    channel = FakeChannel([message(900 - i, author_id=7 if i < 3 else 99) for i in range(100)])
    ctx = context(channel)

    await cog().purge_member_messages(ctx, SimpleNamespace(id=7), 3, ctx.message, "reason")

    assert channel.purge_kwargs["after"].id == 897  # one below the oldest target (898)
    assert channel.purge_kwargs["limit"] is None
    assert channel.purge_kwargs["before"] is ctx.message


async def test_purge_takes_what_it_can_when_the_member_has_too_few() -> None:
    channel = FakeChannel(
        [message(900, 7), message(899, 99), message(898, 7), message(897, 99)]
    )
    ctx = context(channel)

    deleted = await cog().purge_member_messages(
        ctx, SimpleNamespace(id=7), 50, ctx.message, "reason"
    )

    assert len(deleted) == 2
    assert channel.scanned == 4, "it should walk the whole history looking for more"


async def test_purge_handles_a_member_with_nothing_to_delete() -> None:
    channel = FakeChannel([message(900 - i, author_id=99) for i in range(20)])
    ctx = context(channel)

    deleted = await cog().purge_member_messages(
        ctx, SimpleNamespace(id=7), 5, ctx.message, "reason"
    )

    assert deleted == []
    assert channel.purge_kwargs is None, "nothing found means no delete call"


async def test_purge_searches_no_further_than_the_cap() -> None:
    channel = FakeChannel([message(90_000 - i, author_id=99) for i in range(PURGE_SEARCH_LIMIT + 50)])
    ctx = context(channel)

    await cog().purge_member_messages(ctx, SimpleNamespace(id=7), 5, ctx.message, "reason")

    assert channel.scanned == PURGE_SEARCH_LIMIT


async def test_purge_without_a_member_deletes_the_last_n_messages() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    channel.purge = AsyncMock(return_value=[message(i, 99) for i in range(10)])
    ctx = context(channel)

    await Moderation.purge.callback(cog(), ctx, 10)

    assert channel.purge.await_args.kwargs["limit"] == 10
    assert "check" not in channel.purge.await_args.kwargs
    assert ctx.send.await_args.kwargs["embed"].description == "Deleted **10** messages."


async def test_purge_reports_the_member_it_cleaned_up_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = MagicMock(spec=discord.TextChannel)
    ctx = context(channel)
    monkeypatch.setattr(
        Moderation,
        "purge_member_messages",
        AsyncMock(return_value=[message(i, 7) for i in range(4)]),
    )
    member = SimpleNamespace(id=7, mention="<@7>")

    await Moderation.purge.callback(cog(), ctx, 4, member)

    assert ctx.send.await_args.kwargs["embed"].description == "Deleted **4** messages from <@7>."


async def test_purge_says_so_when_it_ran_out_of_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = MagicMock(spec=discord.TextChannel)
    ctx = context(channel)
    monkeypatch.setattr(
        Moderation, "purge_member_messages", AsyncMock(return_value=[message(1, 7)])
    )

    await Moderation.purge.callback(cog(), ctx, 50, SimpleNamespace(id=7, mention="<@7>"))

    description = ctx.send.await_args.kwargs["embed"].description
    assert description.startswith("Deleted **1** messages from <@7>.")
    assert "all they had" in description


def test_purge_keeps_the_member_parameter_optional() -> None:
    assert Moderation.purge.clean_params["member"].required is False
    assert Moderation.purge.app_command.get_parameter("member").required is False
    assert Moderation.purge.app_command.get_parameter("amount").required is True
