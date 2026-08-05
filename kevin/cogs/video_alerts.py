from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands, tasks
from yt_dlp.utils import DownloadError

from kevin.bot import KevinBot
from kevin.cogs.stream_alerts import render_alert_message
from kevin.utils.checks import owner_or_guild_permissions
from kevin.utils.formatting import embed, success

log = logging.getLogger(__name__)

YOUTUBE_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
TIKTOK_USERNAME = re.compile(r"^[A-Za-z0-9._-]{2,30}$")
YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"


class YouTubeAPIError(RuntimeError):
    pass


class QuietYTDLPLogger:
    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def normalize_youtube_creator(value: str) -> tuple[str, str]:
    value = value.strip()
    if "://" in value:
        parsed = urlparse(value)
        if parsed.netloc.casefold() not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            raise ValueError("That is not a YouTube channel URL.")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0].casefold() == "channel":
            value = parts[1]
        elif parts and parts[0].startswith("@"):
            value = parts[0]
        else:
            raise ValueError("Use a YouTube @handle or `/channel/UC…` URL.")
    if YOUTUBE_CHANNEL_ID.fullmatch(value):
        return "id", value
    handle = value.removeprefix("@").strip()
    if not handle or len(handle) > 100 or any(character.isspace() for character in handle):
        raise ValueError("Provide a valid YouTube @handle or channel ID.")
    return "handle", handle


def normalize_tiktok_username(value: str) -> str:
    value = value.strip()
    if "://" in value:
        parsed = urlparse(value)
        if parsed.netloc.casefold() not in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
            raise ValueError("That is not a TikTok profile URL.")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts or not parts[0].startswith("@"):
            raise ValueError("Use a TikTok profile URL such as `tiktok.com/@creator`.")
        value = parts[0]
    username = value.removeprefix("@").casefold()
    if not TIKTOK_USERNAME.fullmatch(username):
        raise ValueError("Provide a valid TikTok username or profile URL.")
    return username


def target_missing_permissions(channel: discord.TextChannel, member: discord.Member) -> list[str]:
    permissions = channel.permissions_for(member)
    return [
        permission
        for permission in ("send_messages", "embed_links")
        if not getattr(permissions, permission)
    ]


def allowed_role_mentions(role: discord.Role | None) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=[role] if role else False,
        replied_user=False,
    )


class YouTubeClient:
    def __init__(self, session: aiohttp.ClientSession, api_key: str) -> None:
        self.session = session
        self.api_key = api_key

    async def _get(self, endpoint: str, parameters: dict[str, str]) -> dict[str, Any]:
        parameters = {**parameters, "key": self.api_key}
        async with self.session.get(f"{YOUTUBE_API}/{endpoint}", params=parameters) as response:
            data = await response.json(content_type=None)
            if response.status != 200:
                message = data.get("error", {}).get("message")
                raise YouTubeAPIError(message or f"YouTube API request failed ({response.status}).")
            return data

    async def resolve_channel(self, kind: str, value: str) -> dict[str, str] | None:
        parameters = {"part": "snippet", "id" if kind == "id" else "forHandle": value}
        items = (await self._get("channels", parameters)).get("items", [])
        if not items:
            return None
        return {"id": str(items[0]["id"]), "title": str(items[0]["snippet"]["title"])}

    async def videos(self, video_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = list(video_ids)
        if not ids:
            return []
        return (
            await self._get(
                "videos",
                {"part": "snippet,liveStreamingDetails", "id": ",".join(ids)},
            )
        ).get("items", [])


async def youtube_feed_video_ids(
    session: aiohttp.ClientSession, channel_id: str
) -> list[str]:
    async with session.get(YOUTUBE_FEED, params={"channel_id": channel_id}) as response:
        if response.status != 200:
            raise YouTubeAPIError(f"YouTube feed request failed ({response.status}).")
        body = await response.read()
    return parse_youtube_feed(body)


def parse_youtube_feed(body: bytes) -> list[str]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise YouTubeAPIError("YouTube returned an invalid channel feed.") from exc
    namespace = {"yt": "http://www.youtube.com/xml/schemas/2015"}
    return [
        element.text
        for element in root.findall("atom:entry/yt:videoId", {**namespace, "atom": "http://www.w3.org/2005/Atom"})
        if element.text
    ]


@dataclass(slots=True)
class TikTokSnapshot:
    live: dict[str, Any] | None = None
    post: dict[str, Any] | None = None


def inspect_tiktok(
    username: str,
    *,
    check_live: bool,
    check_posts: bool,
    cookie_file: Path | None,
) -> TikTokSnapshot:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
        "ignoreerrors": True,
        "logger": QuietYTDLPLogger(),
    }
    if cookie_file:
        options["cookiefile"] = str(cookie_file)
    snapshot = TikTokSnapshot()
    with yt_dlp.YoutubeDL(options) as downloader:
        if check_live:
            try:
                info = downloader.extract_info(
                    f"https://www.tiktok.com/@{username}/live", download=False
                )
            except DownloadError:
                info = None
            if info and info.get("is_live"):
                snapshot.live = {
                    "id": str(info["id"]),
                    "title": str(info.get("title") or f"{username} is live"),
                    "creator": str(info.get("creator") or info.get("uploader") or username),
                    "url": f"https://www.tiktok.com/@{username}/live",
                    "thumbnail": info.get("thumbnail"),
                }
        if check_posts:
            post_options = {**downloader.params, "extract_flat": "in_playlist", "playlistend": 1}
            with yt_dlp.YoutubeDL(post_options) as post_downloader:
                try:
                    profile = post_downloader.extract_info(
                        f"https://www.tiktok.com/@{username}", download=False
                    )
                except DownloadError:
                    profile = None
            entries = profile.get("entries") if profile else None
            entry = next(iter(entries), None) if entries else None
            if entry and entry.get("id"):
                post_id = str(entry["id"])
                snapshot.post = {
                    "id": post_id,
                    "title": str(
                        entry.get("description")
                        or entry.get("title")
                        or f"New post from @{username}"
                    ),
                    "creator": str(entry.get("channel") or entry.get("uploader") or username),
                    "url": str(
                        entry.get("webpage_url")
                        or entry.get("url")
                        or f"https://www.tiktok.com/@{username}/video/{post_id}"
                    ),
                    "thumbnail": entry.get("thumbnail"),
                }
    return snapshot


class VideoAlerts(commands.Cog):
    """YouTube live and TikTok live/post notifications."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self.youtube: YouTubeClient | None = None

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        if self.bot.settings.youtube_api_key:
            self.youtube = YouTubeClient(self.session, self.bot.settings.youtube_api_key)
            self.youtube_worker.start()
        self.tiktok_worker.start()

    async def cog_unload(self) -> None:
        if self.youtube_worker.is_running():
            self.youtube_worker.cancel()
        if self.tiktok_worker.is_running():
            self.tiktok_worker.cancel()
        if self.session:
            await self.session.close()

    def require_youtube(self) -> YouTubeClient:
        if self.youtube is None:
            raise commands.CheckFailure(
                "YouTube alerts need `YOUTUBE_API_KEY` in K's `.env`."
            )
        return self.youtube

    @commands.hybrid_group(
        name="youtubealert", fallback="list", description="Configure YouTube live alerts"
    )
    @commands.guild_only()
    @owner_or_guild_permissions(manage_guild=True)
    async def youtubealert(self, ctx: commands.Context) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT * FROM youtube_alerts WHERE guild_id = ? ORDER BY display_name LIMIT 25",
            (ctx.guild.id,),
        )
        lines = []
        for row in rows:
            role = f" · <@&{row['mention_role_id']}>" if row["mention_role_id"] else ""
            lines.append(
                f"[**{row['display_name']}**](https://youtube.com/channel/{row['youtube_channel_id']}) "
                f"→ <#{row['discord_channel_id']}>{role}"
            )
        note = "" if self.youtube else "\n\n⚠️ `YOUTUBE_API_KEY` is not configured."
        await ctx.send(embed=embed("YouTube live alerts", ("\n".join(lines) or "None configured.") + note))

    @youtubealert.command(name="add", description="Notify a channel when a YouTube creator goes live")
    @app_commands.describe(
        creator="YouTube @handle, channel ID, or channel URL",
        destination="Discord channel for alerts (defaults to this channel)",
        role="Optional role to mention",
        message="Optional text; supports {creator}, {title}, {url}, and {role}",
    )
    @owner_or_guild_permissions(manage_guild=True)
    @commands.bot_has_guild_permissions(send_messages=True, embed_links=True)
    async def youtubealert_add(
        self,
        ctx: commands.Context,
        creator: str,
        destination: discord.TextChannel | None = None,
        role: discord.Role | None = None,
        *,
        message: str | None = None,
    ) -> None:
        youtube = self.require_youtube()
        try:
            kind, value = normalize_youtube_creator(creator)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        target = destination or (
            ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None
        )
        if target is None:
            raise commands.BadArgument("Choose a server text channel for the alert.")
        missing = target_missing_permissions(target, ctx.guild.me)
        if missing:
            raise commands.BotMissingPermissions(missing)
        if message and len(message) > 1000:
            raise commands.BadArgument("The custom alert message must be 1,000 characters or fewer.")
        try:
            channel = await youtube.resolve_channel(kind, value)
        except (aiohttp.ClientError, TimeoutError, YouTubeAPIError) as exc:
            raise commands.CheckFailure(f"YouTube could not be reached: {exc}") from exc
        if not channel:
            raise commands.BadArgument("I couldn't find that YouTube channel.")
        await self.bot.db.execute(
            """INSERT INTO youtube_alerts(
                   guild_id, youtube_channel_id, display_name, discord_channel_id,
                   mention_role_id, custom_message
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, youtube_channel_id) DO UPDATE SET
                   display_name = excluded.display_name,
                   discord_channel_id = excluded.discord_channel_id,
                   mention_role_id = excluded.mention_role_id,
                   custom_message = excluded.custom_message""",
            (
                ctx.guild.id,
                channel["id"],
                channel["title"],
                target.id,
                role.id if role else None,
                message,
            ),
        )
        mention = f" and mention {role.mention}" if role else ""
        await ctx.send(
            embed=success(
                f"I'll announce **{channel['title']}** in {target.mention}{mention} when they go live."
            )
        )

    @youtubealert.command(name="remove", description="Stop alerts for a YouTube creator")
    @owner_or_guild_permissions(manage_guild=True)
    async def youtubealert_remove(self, ctx: commands.Context, creator: str) -> None:
        youtube = self.require_youtube()
        try:
            kind, value = normalize_youtube_creator(creator)
            channel = await youtube.resolve_channel(kind, value)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        except (aiohttp.ClientError, TimeoutError, YouTubeAPIError) as exc:
            raise commands.CheckFailure(f"YouTube could not be reached: {exc}") from exc
        if not channel:
            raise commands.BadArgument("I couldn't find that YouTube channel.")
        row = await self.bot.db.fetchone(
            "SELECT display_name FROM youtube_alerts WHERE guild_id = ? AND youtube_channel_id = ?",
            (ctx.guild.id, channel["id"]),
        )
        if not row:
            raise commands.BadArgument("That creator does not have a YouTube alert here.")
        await self.bot.db.execute(
            "DELETE FROM youtube_alerts WHERE guild_id = ? AND youtube_channel_id = ?",
            (ctx.guild.id, channel["id"]),
        )
        await ctx.send(embed=success(f"Removed YouTube alerts for **{row['display_name']}**."))

    @commands.hybrid_group(
        name="tiktokalert",
        fallback="list",
        description="Configure TikTok live and post alerts",
    )
    @commands.guild_only()
    @owner_or_guild_permissions(manage_guild=True)
    async def tiktokalert(self, ctx: commands.Context) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT * FROM tiktok_alerts WHERE guild_id = ? ORDER BY username LIMIT 25",
            (ctx.guild.id,),
        )
        lines = []
        for row in rows:
            kinds = " + ".join(
                kind
                for enabled, kind in (
                    (row["live_enabled"], "live"),
                    (row["posts_enabled"], "posts"),
                )
                if enabled
            )
            role = f" · <@&{row['mention_role_id']}>" if row["mention_role_id"] else ""
            lines.append(
                f"[**@{row['username']}**](https://tiktok.com/@{row['username']}) "
                f"({kinds}) → <#{row['discord_channel_id']}>{role}"
            )
        card = embed("TikTok alerts", "\n".join(lines) or "None configured.")
        card.set_footer(text="TikTok checking is best-effort and may be affected by platform blocking.")
        await ctx.send(embed=card)

    @tiktokalert.command(name="add", description="Configure TikTok live and/or post alerts")
    @app_commands.describe(
        creator="TikTok username or profile URL",
        destination="Discord channel for alerts (defaults to this channel)",
        role="Optional role to mention",
        live="Alert when the creator goes live",
        posts="Alert when the creator publishes a post",
        message="Optional text; supports {type}, {creator}, {title}, {url}, and {role}",
    )
    @owner_or_guild_permissions(manage_guild=True)
    @commands.bot_has_guild_permissions(send_messages=True, embed_links=True)
    async def tiktokalert_add(
        self,
        ctx: commands.Context,
        creator: str,
        destination: discord.TextChannel | None = None,
        role: discord.Role | None = None,
        live: bool = True,
        posts: bool = True,
        *,
        message: str | None = None,
    ) -> None:
        try:
            username = normalize_tiktok_username(creator)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        if not live and not posts:
            raise commands.BadArgument("Enable live alerts, post alerts, or both.")
        target = destination or (
            ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None
        )
        if target is None:
            raise commands.BadArgument("Choose a server text channel for the alert.")
        missing = target_missing_permissions(target, ctx.guild.me)
        if missing:
            raise commands.BotMissingPermissions(missing)
        if message and len(message) > 1000:
            raise commands.BadArgument("The custom alert message must be 1,000 characters or fewer.")
        await self.bot.db.execute(
            """INSERT INTO tiktok_alerts(
                   guild_id, username, discord_channel_id, mention_role_id,
                   live_enabled, posts_enabled, custom_message
               ) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, username) DO UPDATE SET
                   discord_channel_id = excluded.discord_channel_id,
                   mention_role_id = excluded.mention_role_id,
                   live_enabled = excluded.live_enabled,
                   posts_enabled = excluded.posts_enabled,
                   custom_message = excluded.custom_message""",
            (
                ctx.guild.id,
                username,
                target.id,
                role.id if role else None,
                int(live),
                int(posts),
                message,
            ),
        )
        enabled = " and ".join(kind for value, kind in ((live, "live"), (posts, "post")) if value)
        await ctx.send(
            embed=success(
                f"Enabled best-effort TikTok **{enabled} alerts** for **@{username}** in {target.mention}."
            )
        )

    @tiktokalert.command(name="remove", description="Stop alerts for a TikTok creator")
    @owner_or_guild_permissions(manage_guild=True)
    async def tiktokalert_remove(self, ctx: commands.Context, creator: str) -> None:
        try:
            username = normalize_tiktok_username(creator)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        row = await self.bot.db.fetchone(
            "SELECT 1 FROM tiktok_alerts WHERE guild_id = ? AND username = ?",
            (ctx.guild.id, username),
        )
        if not row:
            raise commands.BadArgument("That creator does not have a TikTok alert here.")
        await self.bot.db.execute(
            "DELETE FROM tiktok_alerts WHERE guild_id = ? AND username = ?",
            (ctx.guild.id, username),
        )
        await ctx.send(embed=success(f"Removed TikTok alerts for **@{username}**."))

    async def send_youtube_alert(self, row: Any, video: dict[str, Any]) -> bool:
        guild = self.bot.get_guild(int(row["guild_id"]))
        channel = guild.get_channel(int(row["discord_channel_id"])) if guild else None
        if not isinstance(channel, discord.TextChannel):
            return False
        snippet = video["snippet"]
        video_id = str(video["id"])
        url = f"https://www.youtube.com/watch?v={video_id}"
        role = guild.get_role(int(row["mention_role_id"])) if row["mention_role_id"] else None
        values = {
            "creator": str(snippet["channelTitle"]),
            "title": str(snippet["title"]),
            "url": url,
            "role": role.mention if role else "",
        }
        content = (
            render_alert_message(str(row["custom_message"]), values)
            if row["custom_message"]
            else (role.mention if role else None)
        )
        card = embed(
            f"🔴 {snippet['channelTitle']} is live on YouTube!",
            f"## {str(snippet['title'])[:1000]}\n[Watch on YouTube]({url})",
        )
        thumbnails = snippet.get("thumbnails", {})
        thumbnail = next(
            (thumbnails[key]["url"] for key in ("maxres", "standard", "high", "medium") if key in thumbnails),
            None,
        )
        if thumbnail:
            card.set_image(url=thumbnail)
        card.timestamp = discord.utils.utcnow()
        try:
            await channel.send(
                content=content,
                embed=card,
                allowed_mentions=allowed_role_mentions(role),
            )
        except discord.HTTPException:
            log.warning("Could not send YouTube alert in guild %s", row["guild_id"])
            return False
        return True

    async def send_tiktok_alert(
        self, row: Any, item: dict[str, Any], *, alert_type: str
    ) -> bool:
        guild = self.bot.get_guild(int(row["guild_id"]))
        channel = guild.get_channel(int(row["discord_channel_id"])) if guild else None
        if not isinstance(channel, discord.TextChannel):
            return False
        role = guild.get_role(int(row["mention_role_id"])) if row["mention_role_id"] else None
        values = {
            "type": alert_type,
            "creator": str(item["creator"]),
            "title": str(item["title"]),
            "url": str(item["url"]),
            "role": role.mention if role else "",
        }
        content = (
            render_alert_message(str(row["custom_message"]), values)
            if row["custom_message"]
            else (role.mention if role else None)
        )
        heading = (
            f"🔴 {item['creator']} is live on TikTok!"
            if alert_type == "live"
            else f"🎬 New TikTok from {item['creator']}"
        )
        action = "Watch live" if alert_type == "live" else "View post"
        card = embed(
            heading,
            f"## {str(item['title'])[:1000]}\n[{action} on TikTok]({item['url']})",
        )
        if item.get("thumbnail"):
            card.set_image(url=str(item["thumbnail"]))
        card.set_footer(text="TikTok · Best-effort public-page check")
        card.timestamp = discord.utils.utcnow()
        try:
            await channel.send(
                content=content,
                embed=card,
                allowed_mentions=allowed_role_mentions(role),
            )
        except discord.HTTPException:
            log.warning("Could not send TikTok alert in guild %s", row["guild_id"])
            return False
        return True

    @tasks.loop(minutes=3)
    async def youtube_worker(self) -> None:
        if self.youtube is None or self.session is None:
            return
        rows = await self.bot.db.fetchall("SELECT * FROM youtube_alerts")
        if not rows:
            return
        channel_ids = list(dict.fromkeys(str(row["youtube_channel_id"]) for row in rows))
        semaphore = asyncio.Semaphore(5)

        async def load_feed(channel_id: str) -> tuple[str, list[str]]:
            async with semaphore:
                try:
                    ids = await youtube_feed_video_ids(self.session, channel_id)
                except (aiohttp.ClientError, TimeoutError, YouTubeAPIError) as exc:
                    log.warning("YouTube feed check failed for %s: %s", channel_id, exc)
                    ids = []
                return channel_id, ids

        feed_results = await asyncio.gather(*(load_feed(channel_id) for channel_id in channel_ids))
        feed_ids = dict(feed_results)
        all_video_ids = list(
            dict.fromkeys(video_id for ids in feed_ids.values() for video_id in ids[:5])
        )
        live_videos: dict[str, dict[str, Any]] = {}
        try:
            for index in range(0, len(all_video_ids), 50):
                videos = await self.youtube.videos(all_video_ids[index : index + 50])
                for video in videos:
                    snippet = video.get("snippet", {})
                    if snippet.get("liveBroadcastContent") == "live":
                        live_videos[str(snippet["channelId"])] = video
        except (aiohttp.ClientError, TimeoutError, YouTubeAPIError) as exc:
            log.warning("YouTube live check failed: %s", exc)
            return
        for row in rows:
            video = live_videos.get(str(row["youtube_channel_id"]))
            if not video or str(row["last_video_id"] or "") == str(video["id"]):
                continue
            if await self.send_youtube_alert(row, video):
                await self.bot.db.execute(
                    "UPDATE youtube_alerts SET last_video_id = ? "
                    "WHERE guild_id = ? AND youtube_channel_id = ?",
                    (str(video["id"]), row["guild_id"], row["youtube_channel_id"]),
                )

    @youtube_worker.before_loop
    async def before_youtube_worker(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def tiktok_worker(self) -> None:
        rows = await self.bot.db.fetchall("SELECT * FROM tiktok_alerts")
        if not rows:
            return
        by_username: dict[str, list[Any]] = {}
        for row in rows:
            by_username.setdefault(str(row["username"]), []).append(row)
        semaphore = asyncio.Semaphore(2)

        async def inspect(username: str, creator_rows: list[Any]) -> tuple[str, TikTokSnapshot]:
            async with semaphore:
                snapshot = await asyncio.to_thread(
                    inspect_tiktok,
                    username,
                    check_live=any(row["live_enabled"] for row in creator_rows),
                    check_posts=any(row["posts_enabled"] for row in creator_rows),
                    cookie_file=self.bot.settings.ytdlp_cookie_file,
                )
                return username, snapshot

        results = await asyncio.gather(
            *(inspect(username, creator_rows) for username, creator_rows in by_username.items()),
            return_exceptions=True,
        )
        snapshots: dict[str, TikTokSnapshot] = {}
        for result in results:
            if isinstance(result, BaseException):
                log.warning("TikTok alert check failed: %s", result)
            else:
                snapshots[result[0]] = result[1]
        for row in rows:
            snapshot = snapshots.get(str(row["username"]))
            if snapshot is None:
                continue
            if row["live_enabled"] and snapshot.live:
                live_id = str(snapshot.live["id"])
                if live_id != str(row["last_live_id"] or "") and await self.send_tiktok_alert(
                    row, snapshot.live, alert_type="live"
                ):
                    await self.bot.db.execute(
                        "UPDATE tiktok_alerts SET last_live_id = ? "
                        "WHERE guild_id = ? AND username = ?",
                        (live_id, row["guild_id"], row["username"]),
                    )
            if row["posts_enabled"] and snapshot.post:
                post_id = str(snapshot.post["id"])
                if not row["last_post_id"]:
                    await self.bot.db.execute(
                        "UPDATE tiktok_alerts SET last_post_id = ? "
                        "WHERE guild_id = ? AND username = ?",
                        (post_id, row["guild_id"], row["username"]),
                    )
                elif post_id != str(row["last_post_id"]) and await self.send_tiktok_alert(
                    row, snapshot.post, alert_type="post"
                ):
                    await self.bot.db.execute(
                        "UPDATE tiktok_alerts SET last_post_id = ? "
                        "WHERE guild_id = ? AND username = ?",
                        (post_id, row["guild_id"], row["username"]),
                    )

    @tiktok_worker.before_loop
    async def before_tiktok_worker(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(VideoAlerts(bot))
