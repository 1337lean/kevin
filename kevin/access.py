"""Discord accounts excluded from invoking Kevin (not server moderation bans)."""

BLOCKED_DISCORD_USER_IDS: frozenset[int] = frozenset({1189439193861083149})


def is_blocked_discord_user(user_id: int) -> bool:
    return user_id in BLOCKED_DISCORD_USER_IDS
