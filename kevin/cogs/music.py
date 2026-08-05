from __future__ import annotations

import asyncio
import ctypes.util
import logging
import os
import random
import shutil
import sys
from collections import deque
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

import discord
import yt_dlp
from discord.ext import commands

from kevin.bot import KevinBot
from kevin.utils.formatting import embed, human_duration, success

log = logging.getLogger(__name__)


def load_opus_library() -> bool:
    """Load Opus from common package-manager locations.

    discord.py's automatic lookup does not find Homebrew's Apple Silicon prefix,
    even though FFmpeg and libopus are both installed there.
    """
    if discord.opus.is_loaded():
        return True

    candidates: list[str] = []
    if configured := os.environ.get("OPUS_LIBRARY"):
        candidates.append(configured)
    if discovered := ctypes.util.find_library("opus"):
        candidates.append(discovered)

    prefixes = [Path(sys.prefix)]
    if ffmpeg := shutil.which("ffmpeg"):
        prefixes.append(Path(ffmpeg).parent.parent)
    prefixes.extend((Path("/opt/homebrew"), Path("/usr/local"), Path("/usr")))
    for prefix in prefixes:
        candidates.extend(
            str(prefix / relative)
            for relative in (
                "lib/libopus.dylib",
                "lib/libopus.0.dylib",
                "lib/libopus.so.0",
                "lib/libopus.so",
                "bin/opus.dll",
                "bin/libopus-0.dll",
            )
        )

    for candidate in dict.fromkeys(candidates):
        path = Path(candidate)
        if path.is_absolute() and not path.exists():
            continue
        try:
            discord.opus.load_opus(candidate)
        except OSError:
            log.debug("Could not load Opus from %s", candidate, exc_info=True)
            continue
        log.info("Loaded Opus voice encoder from %s", candidate)
        return True
    return False


@dataclass(slots=True)
class Track:
    title: str
    webpage_url: str
    stream_url: str
    duration: int
    requester_id: int
    thumbnail: str | None = None


@dataclass(slots=True)
class Player:
    queue: deque[Track] = field(default_factory=deque)
    current: Track | None = None
    volume: float = 0.5
    loop_track: bool = False
    text_channel_id: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Music(commands.Cog):
    """YouTube/search audio queues for Discord voice channels."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot
        self.players: dict[int, Player] = {}
        if not load_opus_library():
            log.warning(
                "Opus could not be loaded; music playback will be unavailable. "
                "Set OPUS_LIBRARY to the installed library path."
            )
        self.ytdlp_options = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "default_search": "ytsearch",
            "noplaylist": True,
            "source_address": "0.0.0.0",
        }
        if bot.settings.ytdlp_cookie_file:
            self.ytdlp_options["cookiefile"] = str(bot.settings.ytdlp_cookie_file)

    def player(self, guild_id: int) -> Player:
        return self.players.setdefault(guild_id, Player())

    async def cog_check(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            raise commands.NoPrivateMessage()
        controls = {
            "play",
            "pause",
            "resume",
            "skip",
            "stop",
            "volume",
            "loop",
            "shuffle",
            "remove",
            "disconnect",
        }
        voice = ctx.guild.voice_client
        author = ctx.author
        is_owner = await self.bot.is_owner(author)
        if (
            voice
            and ctx.command
            and ctx.command.name in controls
            and isinstance(author, discord.Member)
            and not is_owner
            and not author.guild_permissions.manage_guild
            and (not author.voice or author.voice.channel != voice.channel)
        ):
            raise commands.CheckFailure("Join K's voice channel to control playback.")
        return True

    async def cog_unload(self) -> None:
        for voice in self.bot.voice_clients:
            await voice.disconnect(force=True)

    async def ensure_voice(self, ctx: commands.Context) -> discord.VoiceClient:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            raise commands.NoPrivateMessage()
        author_channel = ctx.author.voice.channel if ctx.author.voice else None
        if not author_channel:
            raise commands.CheckFailure("Join a voice channel first.")
        voice = ctx.guild.voice_client
        if voice and not isinstance(voice, discord.VoiceClient):
            raise commands.CheckFailure("An incompatible voice connection is already active.")
        if voice:
            if voice.channel != author_channel:
                if voice.is_playing() or voice.is_paused():
                    raise commands.CheckFailure(
                        f"K is already playing in {voice.channel.mention}."
                    )
                await voice.move_to(author_channel)
            return voice
        return await author_channel.connect(self_deaf=True)

    async def extract(self, query: str, requester_id: int) -> Track:
        target = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"

        def run_extract():
            with yt_dlp.YoutubeDL(self.ytdlp_options) as downloader:
                return downloader.extract_info(target, download=False)

        try:
            info = await asyncio.to_thread(run_extract)
        except yt_dlp.utils.DownloadError as exc:
            raise commands.BadArgument("I couldn't load audio for that search or URL.") from exc
        if "entries" in info:
            entries = [entry for entry in info["entries"] if entry]
            if not entries:
                raise commands.BadArgument("No playable results were found.")
            info = entries[0]
        stream_url = info.get("url")
        if not stream_url:
            raise commands.BadArgument("That result did not contain a playable audio stream.")
        return Track(
            title=info.get("title") or "Unknown title",
            webpage_url=info.get("webpage_url") or info.get("original_url") or query,
            stream_url=stream_url,
            duration=int(info.get("duration") or 0),
            requester_id=requester_id,
            thumbnail=info.get("thumbnail"),
        )

    async def start_next(self, guild_id: int) -> None:
        guild = self.bot.get_guild(guild_id)
        if not guild or not isinstance(guild.voice_client, discord.VoiceClient):
            return
        player = self.player(guild_id)
        async with player.lock:
            voice = guild.voice_client
            if voice.is_playing() or voice.is_paused():
                return
            if player.loop_track and player.current:
                track = player.current
            elif player.queue:
                track = player.queue.popleft()
                player.current = track
            else:
                player.current = None
                return
            source = discord.FFmpegPCMAudio(
                track.stream_url,
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                options="-vn",
            )
            audio = discord.PCMVolumeTransformer(source, volume=player.volume)
            callback = partial(self.after_track, guild_id)
            voice.play(audio, after=callback)
        channel = self.bot.get_channel(player.text_channel_id) if player.text_channel_id else None
        if isinstance(channel, discord.abc.Messageable):
            card = embed("Now playing", f"[{track.title}]({track.webpage_url})")
            card.add_field(
                name="Duration",
                value=human_duration(track.duration) if track.duration else "Live/unknown",
            )
            card.add_field(name="Requested by", value=f"<@{track.requester_id}>")
            if track.thumbnail:
                card.set_thumbnail(url=track.thumbnail)
            try:
                await channel.send(embed=card)
            except discord.HTTPException:
                pass

    def after_track(self, guild_id: int, error: Exception | None) -> None:
        if error:
            log.error("Music playback failed in guild %s: %s", guild_id, error)
        future = asyncio.run_coroutine_threadsafe(self.start_next(guild_id), self.bot.loop)
        future.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    @commands.hybrid_command(description="Join your voice channel")
    @commands.guild_only()
    @commands.bot_has_guild_permissions(connect=True, speak=True)
    async def join(self, ctx: commands.Context) -> None:
        voice = await self.ensure_voice(ctx)
        await ctx.send(embed=success(f"Joined {voice.channel.mention}."))

    @commands.hybrid_command(description="Play a URL or search for a song")
    @commands.guild_only()
    @commands.bot_has_guild_permissions(connect=True, speak=True)
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        if not load_opus_library():
            raise commands.CheckFailure(
                "Voice audio is unavailable because K could not load Opus. "
                "Install libopus, set `OPUS_LIBRARY` if needed, and restart K."
            )
        voice = await self.ensure_voice(ctx)
        if ctx.interaction:
            await ctx.defer()
        track = await self.extract(query, ctx.author.id)
        player = self.player(ctx.guild.id)
        player.text_channel_id = ctx.channel.id
        player.queue.append(track)
        position = len(player.queue) + (1 if voice.is_playing() or voice.is_paused() else 0)
        await ctx.send(
            embed=embed(
                "Added to queue", f"[{track.title}]({track.webpage_url}) · position **{position}**"
            )
        )
        await self.start_next(ctx.guild.id)

    @commands.hybrid_command(description="Pause the current track")
    @commands.guild_only()
    async def pause(self, ctx: commands.Context) -> None:
        voice = ctx.guild.voice_client
        if not voice or not voice.is_playing():
            raise commands.CheckFailure("Nothing is currently playing.")
        voice.pause()
        await ctx.send(embed=success("Playback paused."))

    @commands.hybrid_command(description="Resume paused playback")
    @commands.guild_only()
    async def resume(self, ctx: commands.Context) -> None:
        voice = ctx.guild.voice_client
        if not voice or not voice.is_paused():
            raise commands.CheckFailure("Playback is not paused.")
        voice.resume()
        await ctx.send(embed=success("Playback resumed."))

    @commands.hybrid_command(description="Skip the current track")
    @commands.guild_only()
    async def skip(self, ctx: commands.Context) -> None:
        voice = ctx.guild.voice_client
        if not voice or not (voice.is_playing() or voice.is_paused()):
            raise commands.CheckFailure("Nothing is currently playing.")
        self.player(ctx.guild.id).loop_track = False
        voice.stop()
        await ctx.send(embed=success("Skipped."))

    @commands.hybrid_command(description="Stop playback and clear the queue")
    @commands.guild_only()
    async def stop(self, ctx: commands.Context) -> None:
        voice = ctx.guild.voice_client
        player = self.player(ctx.guild.id)
        player.queue.clear()
        player.loop_track = False
        player.current = None
        if voice:
            voice.stop()
        await ctx.send(embed=success("Stopped playback and cleared the queue."))

    @commands.hybrid_command(name="queue", aliases=["q"], description="Show the music queue")
    @commands.guild_only()
    async def queue_command(self, ctx: commands.Context) -> None:
        player = self.player(ctx.guild.id)
        lines: list[str] = []
        if player.current:
            lines.append(f"**Now:** [{player.current.title}]({player.current.webpage_url})")
        lines.extend(
            f"**{index}.** [{track.title}]({track.webpage_url}) · {human_duration(track.duration) if track.duration else 'live'}"
            for index, track in enumerate(list(player.queue)[:15], 1)
        )
        if len(player.queue) > 15:
            lines.append(f"…and {len(player.queue) - 15} more")
        await ctx.send(embed=embed("Music queue", "\n".join(lines) or "The queue is empty."))

    @commands.hybrid_command(description="Show the current track")
    @commands.guild_only()
    async def nowplaying(self, ctx: commands.Context) -> None:
        track = self.player(ctx.guild.id).current
        if not track:
            raise commands.CheckFailure("Nothing is currently playing.")
        await ctx.send(
            embed=embed(
                "Now playing", f"[{track.title}]({track.webpage_url}) · <@{track.requester_id}>"
            )
        )

    @commands.hybrid_command(description="Set music volume from 1 to 100")
    @commands.guild_only()
    async def volume(self, ctx: commands.Context, percent: commands.Range[int, 1, 100]) -> None:
        player = self.player(ctx.guild.id)
        player.volume = percent / 100
        voice = ctx.guild.voice_client
        if voice and isinstance(voice.source, discord.PCMVolumeTransformer):
            voice.source.volume = player.volume
        await ctx.send(embed=success(f"Volume set to **{percent}%**."))

    @commands.hybrid_command(description="Toggle looping the current track")
    @commands.guild_only()
    async def loop(self, ctx: commands.Context) -> None:
        player = self.player(ctx.guild.id)
        player.loop_track = not player.loop_track
        await ctx.send(
            embed=success(f"Track loop is now **{'on' if player.loop_track else 'off'}**.")
        )

    @commands.hybrid_command(description="Shuffle the music queue")
    @commands.guild_only()
    async def shuffle(self, ctx: commands.Context) -> None:
        player = self.player(ctx.guild.id)
        shuffled = list(player.queue)
        random.shuffle(shuffled)
        player.queue = deque(shuffled)
        await ctx.send(embed=success(f"Shuffled **{len(shuffled)}** tracks."))

    @commands.hybrid_command(description="Remove a track by queue position")
    @commands.guild_only()
    async def remove(self, ctx: commands.Context, position: int) -> None:
        player = self.player(ctx.guild.id)
        if not 1 <= position <= len(player.queue):
            raise commands.BadArgument("That queue position does not exist.")
        tracks = list(player.queue)
        removed = tracks.pop(position - 1)
        player.queue = deque(tracks)
        await ctx.send(embed=success(f"Removed **{removed.title}**."))

    @commands.hybrid_command(aliases=["leave"], description="Disconnect K from voice")
    @commands.guild_only()
    async def disconnect(self, ctx: commands.Context) -> None:
        voice = ctx.guild.voice_client
        if not voice:
            raise commands.CheckFailure("K is not in a voice channel.")
        self.players.pop(ctx.guild.id, None)
        await voice.disconnect(force=True)
        await ctx.send(embed=success("Disconnected from voice."))


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(Music(bot))
