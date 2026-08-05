from __future__ import annotations

import io

import discord
from discord.ext import commands

from kevin.bot import KevinBot
from kevin.utils.checks import owner_or_guild_permissions
from kevin.utils.formatting import embed, success


async def create_ticket(interaction: discord.Interaction, bot: KevinBot) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Tickets can only be created in a server.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    existing = await bot.db.fetchone(
        "SELECT channel_id FROM tickets WHERE guild_id = ? AND owner_id = ? AND closed_at IS NULL",
        (interaction.guild.id, interaction.user.id),
    )
    if existing:
        channel = interaction.guild.get_channel(existing["channel_id"])
        if channel:
            await interaction.followup.send(
                f"You already have an open ticket: {channel.mention}", ephemeral=True
            )
            return
        await bot.db.execute(
            "UPDATE tickets SET closed_at = CURRENT_TIMESTAMP WHERE channel_id = ?",
            (existing["channel_id"],),
        )

    category = discord.utils.get(interaction.guild.categories, name="K Tickets")
    if category is None:
        category = discord.utils.get(interaction.guild.categories, name="Kevin Tickets")
        if category is not None:
            await category.edit(name="K Tickets", reason="K rebrand")
    if category is None:
        category = await interaction.guild.create_category("K Tickets", reason="K ticket system")
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        interaction.guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True
        ),
    }
    safe_name = "".join(
        c for c in interaction.user.display_name.lower() if c.isalnum() or c == "-"
    )[:40]
    channel = await interaction.guild.create_text_channel(
        f"ticket-{safe_name or interaction.user.id}",
        category=category,
        overwrites=overwrites,
        topic=f"K ticket owner: {interaction.user.id}",
        reason=f"Ticket opened by {interaction.user}",
    )
    await bot.db.execute(
        "INSERT INTO tickets(channel_id, guild_id, owner_id) VALUES (?, ?, ?)",
        (channel.id, interaction.guild.id, interaction.user.id),
    )
    card = embed(
        "Support ticket",
        f"Welcome {interaction.user.mention}. Describe what you need and a staff member will respond. "
        "Use the button below when this is resolved.",
    )
    await channel.send(content=interaction.user.mention, embed=card, view=TicketCloseView(bot))
    await interaction.followup.send(f"Created {channel.mention}.", ephemeral=True)


async def close_ticket(interaction: discord.Interaction, bot: KevinBot) -> None:
    if not interaction.guild or not interaction.channel:
        return
    row = await bot.db.fetchone(
        "SELECT * FROM tickets WHERE channel_id = ? AND closed_at IS NULL",
        (interaction.channel.id,),
    )
    if not row:
        await interaction.response.send_message("This is not an open ticket.", ephemeral=True)
        return
    member = interaction.user
    is_staff = (
        member.id in (bot.owner_ids or ())
        or isinstance(member, discord.Member)
        and member.guild_permissions.manage_channels
    )
    if member.id != row["owner_id"] and not is_staff:
        await interaction.response.send_message(
            "Only the ticket owner or staff can close this.", ephemeral=True
        )
        return
    await interaction.response.send_message("Closing this ticket in a few seconds…")
    await bot.db.execute(
        "UPDATE tickets SET closed_at = CURRENT_TIMESTAMP WHERE channel_id = ?",
        (interaction.channel.id,),
    )
    await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")


class TicketCreateView(discord.ui.View):
    def __init__(self, bot: KevinBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Open ticket",
        emoji="🎫",
        style=discord.ButtonStyle.primary,
        custom_id="kevin:ticket:create",
    )
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await create_ticket(interaction, self.bot)


class TicketCloseView(discord.ui.View):
    def __init__(self, bot: KevinBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Close ticket",
        emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id="kevin:ticket:close",
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await close_ticket(interaction, self.bot)


class Tickets(commands.Cog):
    """Persistent-button support tickets with transcripts."""

    def __init__(self, bot: KevinBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(TicketCreateView(self.bot))
        self.bot.add_view(TicketCloseView(self.bot))

    @commands.hybrid_group(
        name="ticket", fallback="create", description="Create or manage support tickets"
    )
    @commands.guild_only()
    async def ticket(self, ctx: commands.Context) -> None:
        if ctx.interaction:
            await create_ticket(ctx.interaction, self.bot)
        else:
            # Prefix users get the panel button to preserve the same safe creation flow.
            await ctx.send(
                embed=embed(
                    "Open a support ticket", "Press the button below to create a private channel."
                ),
                view=TicketCreateView(self.bot),
            )

    @ticket.command(name="panel", description="Post a persistent ticket creation panel")
    @owner_or_guild_permissions(manage_guild=True)
    async def panel(self, ctx: commands.Context) -> None:
        await ctx.send(
            embed=embed("Need help?", "Press **Open ticket** to create a private support channel."),
            view=TicketCreateView(self.bot),
        )

    @ticket.command(name="close", description="Close the current ticket")
    async def close(self, ctx: commands.Context) -> None:
        if ctx.interaction:
            await close_ticket(ctx.interaction, self.bot)
            return
        row = await self.bot.db.fetchone(
            "SELECT * FROM tickets WHERE channel_id = ? AND closed_at IS NULL", (ctx.channel.id,)
        )
        if not row:
            raise commands.BadArgument("This is not an open ticket.")
        is_staff = (
            ctx.author.id in (self.bot.owner_ids or ())
            or ctx.author.guild_permissions.manage_channels
        )
        if ctx.author.id != row["owner_id"] and not is_staff:
            raise commands.CheckFailure("Only the ticket owner or staff can close this.")
        await self.bot.db.execute(
            "UPDATE tickets SET closed_at = CURRENT_TIMESTAMP WHERE channel_id = ?",
            (ctx.channel.id,),
        )
        await ctx.send("Closing this ticket…")
        await ctx.channel.delete(reason=f"Ticket closed by {ctx.author}")

    @ticket.command(name="add", description="Add a member to the current ticket")
    @owner_or_guild_permissions(manage_channels=True)
    async def add(self, ctx: commands.Context, member: discord.Member) -> None:
        row = await self.bot.db.fetchone(
            "SELECT 1 FROM tickets WHERE channel_id = ? AND closed_at IS NULL", (ctx.channel.id,)
        )
        if not row:
            raise commands.BadArgument("This is not an open ticket.")
        await ctx.channel.set_permissions(
            member, view_channel=True, send_messages=True, read_message_history=True
        )
        await ctx.send(embed=success(f"Added {member.mention} to the ticket."))

    @ticket.command(name="remove", description="Remove a member from the current ticket")
    @owner_or_guild_permissions(manage_channels=True)
    async def remove(self, ctx: commands.Context, member: discord.Member) -> None:
        await ctx.channel.set_permissions(member, overwrite=None)
        await ctx.send(embed=success(f"Removed {member.mention} from the ticket."))

    @ticket.command(name="transcript", description="Export recent ticket messages")
    async def transcript(self, ctx: commands.Context) -> None:
        row = await self.bot.db.fetchone(
            "SELECT owner_id FROM tickets WHERE channel_id = ? AND closed_at IS NULL",
            (ctx.channel.id,),
        )
        if not row:
            raise commands.BadArgument("This is not an open ticket.")
        is_staff = (
            ctx.author.id in (self.bot.owner_ids or ())
            or ctx.author.guild_permissions.manage_channels
        )
        if ctx.author.id != row["owner_id"] and not is_staff:
            raise commands.CheckFailure("Only the ticket owner or staff can export this.")
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
        lines: list[str] = []
        async for message in ctx.channel.history(limit=1000, oldest_first=True):
            timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            lines.append(
                f"[{timestamp}] {message.author} ({message.author.id}): {message.clean_content}"
            )
            lines.extend(f"  Attachment: {attachment.url}" for attachment in message.attachments)
        data = io.BytesIO("\n".join(lines).encode("utf-8"))
        await ctx.send(
            file=discord.File(data, filename=f"transcript-{ctx.channel.id}.txt"),
            ephemeral=bool(ctx.interaction),
        )


async def setup(bot: KevinBot) -> None:
    await bot.add_cog(Tickets(bot))
