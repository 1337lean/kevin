from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from kevin.bot import KevinBot
from kevin.utils.checks import owner_or_guild_permissions
from kevin.utils.formatting import embed, success

log = logging.getLogger(__name__)

TWITCH_LOGIN = re.compile(r"^[A-Za-z0-9_]{3,25}$")


class TwitchAPIError(RuntimeError):
    pass


def normalize_twitch_login(value: str) -> str:
    value = value.strip()
    if "://" in value:
        parsed = urlparse(value)
        if parsed.netloc.casefold() not in {"twitch.tv", "www.twitch.tv"}:
            raise ValueError("That is not a Twitch channel URL.")
        value = parsed.path.strip("/").split("/", 1)[0]
    value = value.removeprefix("@").casefold()
    if not TWITCH_LOGIN.fullmatch(value):
        raise ValueError("Provide a valid Twitch username or channel URL.")
    return value


def render_alert_message(template: str, values: dict[str, str]) -> str:
    result = template
    for key, value in values.items():
        result = result.replace("{" + key + "}", value)
    return result[:2000]


class TwitchClient:
    def __init__(self, session: aiohttp.ClientSession, client_id: str, client_secret: str) -> None:
        self.session = session
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_expires_at = 0.0

    async def _get_token(self, *, force: bool = False) -> str:
        if not force and self._token and time.monotonic() < self._token_expires_at:
            return self._token
        async with self.session.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
        ) as response:
            data = await response.json(content_type=None)
            if response.status != 200 or "access_token" not in data:
                raise TwitchAPIError(
                    data.get("message", f"Twitch authentication failed ({response.status}).")
                )
        self._token = str(data["access_token"])
        self._token_expires_at = time.monotonic() + max(int(data.get("expires_in", 3600)) - 60, 60)
        return self._token

    async def _get(self, endpoint: str, parameters: Iterable[tuple[str, str]]) -> dict[str, Any]:
        request_parameters = list(parameters)
        for attempt in range(2):
            token = await self._get_token(force=attempt == 1)
            async with self.session.get(
                f"https://api.twitch.tv/helix/{endpoint}",
                params=request_parameters,
                headers={"Authorization": f"Bearer {token}", "Client-Id": self.client_id},
            ) as response:
                data = await response.json(content_type=None)
                if response.status == 401 and attempt == 0:
                    continue
                if response.status != 200:
                    raise TwitchAPIError(
                        data.get("message", f"Twitch API request failed ({response.status}).")
                    )
                return data
        raise TwitchAPIError("Twitch rejected the application access token.")

    async def users(self, logins: Iterable[str]) -> list[dict[str, Any]]:
        return (await self._get("users", (("login", login) for login in logins)))["data"]

    async def streams(self, logins: Iterable[str]) -> list[dict[str, Any]]:
        return (await self._get("streams", (("user_login", login) for login in logins)))["data"]


class StreamAlerts(commands.Cog):
    """Twitch go-live notifications."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self.twitch: TwitchClient | None = None

    async def cog_load(self) -> None:
        timeout = aiohttp.ClientTimeout(total=20)
        self.session = aiohttp.ClientSession(timeout=timeout)
        if self.bot.settings.twitch_client_id and self.bot.settings.twitch_client_secret:
            self.twitch = TwitchClient(
                self.session,
                self.bot.settings.twitch_client_id,
                self.bot.settings.twitch_client_secret,
            )
            self.alert_worker.start()

    async def cog_unload(self) -> None:
        if self.alert_worker.is_running():
            self.alert_worker.cancel()
        if self.session:
            await self.session.close()

    def require_twitch(self) -> TwitchClient:
        if self.twitch is None:
            raise commands.CheckFailure(
                "Twitch alerts need `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` in K's `.env`."
            )
        return self.twitch

    @commands.hybrid_group(
        name="streamalert", fallback="list", description="Configure Twitch go-live alerts"
    )
    @commands.guild_only()
    @owner_or_guild_permissions(manage_guild=True)
    async def streamalert(self, ctx: commands.Context) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT * FROM stream_alerts WHERE guild_id = ? ORDER BY twitch_login LIMIT 25",
            (ctx.guild.id,),
        )
        lines = []
        for row in rows:
            role = f" · <@&{row['mention_role_id']}>" if row["mention_role_id"] else ""
            lines.append(
                f"[**{row['display_name']}**](https://twitch.tv/{row['twitch_login']}) "
                f"→ <#{row['channel_id']}>{role}"
            )
        note = ""
        if self.twitch is None:
            note = "\n\n⚠️ Twitch API credentials are not configured."
        await ctx.send(embed=embed("Twitch stream alerts", ("\n".join(lines) or "None configured.") + note))

    @streamalert.command(name="add", description="Notify a channel when a Twitch streamer goes live")
    @app_commands.describe(
        streamer="Twitch username or channel URL",
        channel="Discord channel for alerts (defaults to this channel)",
        role="Optional role to mention",
        message="Optional text; supports {streamer}, {title}, {game}, {url}, and {role}",
    )
    @owner_or_guild_permissions(manage_guild=True)
    @commands.bot_has_guild_permissions(send_messages=True, embed_links=True)
    async def streamalert_add(
        self,
        ctx: commands.Context,
        streamer: str,
        channel: discord.TextChannel | None = None,
        role: discord.Role | None = None,
        *,
        message: str | None = None,
    ) -> None:
        twitch = self.require_twitch()
        try:
            login = normalize_twitch_login(streamer)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        target = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
        if target is None:
            raise commands.BadArgument("Choose a server text channel for the alert.")
        target_permissions = target.permissions_for(ctx.guild.me)
        missing_permissions = [
            permission
            for permission in ("send_messages", "embed_links")
            if not getattr(target_permissions, permission)
        ]
        if missing_permissions:
            raise commands.BotMissingPermissions(missing_permissions)
        if message and len(message) > 1000:
            raise commands.BadArgument("The custom alert message must be 1,000 characters or fewer.")
        try:
            users = await twitch.users([login])
        except (aiohttp.ClientError, TimeoutError, TwitchAPIError) as exc:
            raise commands.CheckFailure(f"Twitch could not be reached: {exc}") from exc
        if not users:
            raise commands.BadArgument("I couldn't find that Twitch channel.")
        user = users[0]
        await self.bot.db.execute(
            """INSERT INTO stream_alerts(
                   guild_id, twitch_login, twitch_user_id, display_name, channel_id,
                   mention_role_id, custom_message
               ) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, twitch_login) DO UPDATE SET
                   twitch_user_id = excluded.twitch_user_id,
                   display_name = excluded.display_name,
                   channel_id = excluded.channel_id,
                   mention_role_id = excluded.mention_role_id,
                   custom_message = excluded.custom_message""",
            (
                ctx.guild.id,
                login,
                str(user["id"]),
                str(user["display_name"]),
                target.id,
                role.id if role else None,
                message,
            ),
        )
        mention = f" and mention {role.mention}" if role else ""
        await ctx.send(
            embed=success(
                f"I'll announce **{user['display_name']}** in {target.mention}{mention} when they go live."
            )
        )

    @streamalert.command(name="remove", description="Stop alerts for a Twitch streamer")
    @owner_or_guild_permissions(manage_guild=True)
    async def streamalert_remove(self, ctx: commands.Context, streamer: str) -> None:
        try:
            login = normalize_twitch_login(streamer)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        row = await self.bot.db.fetchone(
            "SELECT display_name FROM stream_alerts WHERE guild_id = ? AND twitch_login = ?",
            (ctx.guild.id, login),
        )
        if not row:
            raise commands.BadArgument("That streamer does not have an alert in this server.")
        await self.bot.db.execute(
            "DELETE FROM stream_alerts WHERE guild_id = ? AND twitch_login = ?",
            (ctx.guild.id, login),
        )
        await ctx.send(embed=success(f"Removed stream alerts for **{row['display_name']}**."))

    async def send_alert(self, row: Any, stream: dict[str, Any]) -> bool:
        guild = self.bot.get_guild(int(row["guild_id"]))
        channel = guild.get_channel(int(row["channel_id"])) if guild else None
        if not isinstance(channel, discord.TextChannel):
            return False
        url = f"https://twitch.tv/{row['twitch_login']}"
        role = guild.get_role(int(row["mention_role_id"])) if row["mention_role_id"] else None
        values = {
            "streamer": str(stream["user_name"]),
            "title": str(stream["title"]),
            "game": str(stream.get("game_name") or "No category"),
            "url": url,
            "role": role.mention if role else "",
        }
        content = (
            render_alert_message(str(row["custom_message"]), values)
            if row["custom_message"]
            else (role.mention if role else None)
        )
        card = embed(
            f"🔴 {stream['user_name']} is live!",
            f"## {stream['title']}\n**{values['game']}**\n[Watch on Twitch]({url})",
        )
        thumbnail = str(stream.get("thumbnail_url") or "").replace("{width}", "1280").replace(
            "{height}", "720"
        )
        if thumbnail:
            card.set_image(url=f"{thumbnail}?t={int(time.time())}")
        card.set_footer(text="Twitch")
        card.timestamp = discord.utils.utcnow()
        mentions = discord.AllowedMentions(
            everyone=False, users=False, roles=[role] if role else False, replied_user=False
        )
        try:
            await channel.send(content=content, embed=card, allowed_mentions=mentions)
        except discord.HTTPException:
            log.warning(
                "Could not send Twitch alert for %s in guild %s",
                row["twitch_login"],
                row["guild_id"],
            )
            return False
        return True

    @tasks.loop(seconds=60)
    async def alert_worker(self) -> None:
        if self.twitch is None:
            return
        rows = await self.bot.db.fetchall("SELECT * FROM stream_alerts ORDER BY twitch_login")
        if not rows:
            return
        logins = list(dict.fromkeys(str(row["twitch_login"]) for row in rows))
        live: dict[str, dict[str, Any]] = {}
        try:
            for index in range(0, len(logins), 100):
                streams = await self.twitch.streams(logins[index : index + 100])
                live.update((str(stream["user_login"]).casefold(), stream) for stream in streams)
        except (aiohttp.ClientError, TimeoutError, TwitchAPIError) as exc:
            log.warning("Twitch alert check failed: %s", exc)
            return
        for row in rows:
            stream = live.get(str(row["twitch_login"]))
            if not stream or str(row["last_stream_id"] or "") == str(stream["id"]):
                continue
            if await self.send_alert(row, stream):
                await self.bot.db.execute(
                    "UPDATE stream_alerts SET last_stream_id = ? "
                    "WHERE guild_id = ? AND twitch_login = ?",
                    (str(stream["id"]), row["guild_id"], row["twitch_login"]),
                )

    @alert_worker.before_loop
    async def before_alert_worker(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(StreamAlerts(bot))
