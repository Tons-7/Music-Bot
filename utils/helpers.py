import discord

from datetime import datetime

from config import COLOR
from .ban_system import is_banned


async def interaction_check(self, interaction: discord.Interaction) -> bool:
    if is_banned(interaction.user.id):
        embed = create_embed(
            "Access Denied",
            "You are banned from using this bot.",
            COLOR,
            self.bot.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    return True


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0:00"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return (
        f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    )


def build_progress_bar(current: int, total: int, length: int = 20) -> str:
    if total <= 0:
        return "▬" * length
    pos = min(length - 1, int((current / total) * length))
    return "▬" * pos + "🔘" + "▬" * (length - pos - 1)


def get_existing_urls(guild_data) -> set:
    urls = {song.webpage_url for song in guild_data["queue"]}
    urls.update(song.webpage_url for song in guild_data["loop_backup"])
    if guild_data.get("current"):
        urls.add(guild_data["current"].webpage_url)
    return urls


def parse_time_to_seconds(time_str: str) -> int:
    time_str = time_str.strip()

    if time_str.isdigit():
        return int(time_str)

    parts = time_str.split(":")

    if len(parts) == 2:
        try:
            minutes = int(parts[0])
            seconds = int(parts[1])
            if minutes < 0 or seconds < 0 or seconds >= 60:
                raise ValueError("Invalid time format! Use MM:SS where SS < 60")
            return minutes * 60 + seconds
        except ValueError:
            raise ValueError("Invalid time format! Use MM:SS or just seconds")

    elif len(parts) == 3:
        try:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2])
            if (
                    hours < 0
                    or minutes < 0
                    or seconds < 0
                    or minutes >= 60
                    or seconds >= 60
            ):
                raise ValueError(
                    "Invalid time format! Use HH:MM:SS where MM,SS < 60"
                )
            return hours * 3600 + minutes * 60 + seconds
        except ValueError:
            raise ValueError(
                "Invalid time format! Use HH:MM:SS or MM:SS or just seconds"
            )

    else:
        raise ValueError(
            "Invalid time format! Use formats like '1:30', '90', or '2:15:30'"
        )


def create_embed(title: str, description: str = "", color: int = COLOR, bot_user=None) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(
        text="Music Bot",
        icon_url=bot_user.avatar.url if bot_user and bot_user.avatar else None,
    )
    embed.timestamp = datetime.now()
    return embed

