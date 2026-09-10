"""Discord accounts excluded from invoking Kevin (not server moderation bans)."""

# Sadrew / Andrew — verified Discord account, blocked at the owner's request.
BLOCKED_DISCORD_USER_IDS = frozenset({272282285141786625})


def is_blocked_discord_user(user_id: int) -> bool:
    return user_id in BLOCKED_DISCORD_USER_IDS
