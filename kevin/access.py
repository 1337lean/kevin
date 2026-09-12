"""Discord accounts excluded from invoking Kevin (not server moderation bans)."""

BLOCKED_DISCORD_USER_IDS: frozenset[int] = frozenset()


def is_blocked_discord_user(user_id: int) -> bool:
    return user_id in BLOCKED_DISCORD_USER_IDS
