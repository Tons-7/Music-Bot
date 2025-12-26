import discord
from discord.ext import commands
import asyncio
import logging
from typing import Optional
from datetime import datetime

from models.song import Song

from services.music_service import MusicService
from services.playback_service import PlaybackService
from services.queue_service import QueueService

# from services.transcript_service import TranscriptService

from utils.helpers import (
    format_duration,
    build_progress_bar,
    get_existing_urls,
    parse_time_to_seconds,
    interaction_check,
    create_embed,
)

from utils.ban_system import is_banned, ban_user_id, unban_user_id

from views.song_select import SongSelectView
from views.pagination import PaginationView

from config import COLOR, SONGS_PER_PAGE

logger = logging.getLogger(__name__)


class MusicCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.music_service = MusicService(bot)
        self.playback_service = PlaybackService(bot)
        self.queue_service = QueueService(bot)
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await interaction_check(self, interaction)

    async def check_voice_channel(
            self, interaction: discord.Interaction, allow_auto_join: bool = False
    ) -> bool:
        if not interaction.user.voice:
            embed = create_embed(
                "Error",
                "You must be in a voice channel to use this command!",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False

        voice_client = interaction.guild.voice_client

        if not voice_client:
            if allow_auto_join:
                return True
            embed = create_embed(
                "Error",
                "The bot is not connected to any voice channel!",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False

        if interaction.user.voice.channel != voice_client.channel:
            embed = create_embed(
                "Error",
                f"You must be in the same voice channel as the bot! Bot is in: {voice_client.channel.name}",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False

        return True

    async def ensure_voice_connection(self, interaction: discord.Interaction) -> bool:
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if (
                not guild_data["voice_client"]
                or not guild_data["voice_client"].is_connected()
        ):
            if not interaction.user.voice:
                return False

            try:
                guild_data["voice_client"] = (
                    await interaction.user.voice.channel.connect()
                )
            except Exception as e:
                logger.error(f"Failed to connect to voice: {e}")
                return False

        return True

    async def get_music_channel(self, guild_id: int) -> Optional[discord.TextChannel]:
        guild_data = self.bot.get_guild_data(guild_id)
        guild = self.bot.get_guild(guild_id)

        if not guild:
            return None

        if guild_data.get("music_channel_id"):
            channel = guild.get_channel(guild_data["music_channel_id"])
            if channel and channel.permissions_for(guild.me).send_messages:
                return channel
            else:
                guild_data["music_channel_id"] = None

        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                return channel

        return None

    async def create_now_playing_message(
            self, guild_id: int, embed: discord.Embed
    ) -> Optional[discord.Message]:
        try:
            channel = await self.get_music_channel(guild_id)
            if not channel:
                return None

            guild_data = self.bot.get_guild_data(guild_id)
            guild_data["message_ready_for_timestamps"] = False

            if guild_data.get("now_playing_message"):
                try:
                    await guild_data["now_playing_message"].delete()
                    await asyncio.sleep(0.5)
                except (discord.NotFound, discord.HTTPException):
                    pass
                guild_data["now_playing_message"] = None

            msg = await channel.send(embed=embed)
            guild_data["now_playing_message"] = msg

            await self.add_reaction_controls(msg)
            await asyncio.sleep(0.2)
            guild_data["message_ready_for_timestamps"] = True

            return msg

        except Exception as e:
            logger.error(f"Failed to create now playing message: {e}")
            guild_data["now_playing_message"] = None
            guild_data["message_ready_for_timestamps"] = False
            return None

    @staticmethod
    async def add_reaction_controls(message: discord.Message):
        reactions = ["⏯️", "⏭️", "⏮️", "🔀", "🔁", "⏹️", "🔊", "🔉"]
        for reaction in reactions:
            try:
                await message.add_reaction(reaction)
                await asyncio.sleep(0.1)
            except discord.HTTPException as e:
                logger.warning(f"Failed to add reaction {reaction}: {e}")
                continue
            except Exception as e:
                logger.error(f"Unexpected error adding reaction {reaction}: {e}")
                break

    @staticmethod
    async def remove_reaction(reaction, user, emoji):
        try:
            await reaction.remove(user)
            logger.debug(f"Successfully removed reaction {emoji} from {user.name}")
        except discord.Forbidden:
            logger.debug(f"No permission to remove reaction {emoji} from {user.name}")
        except discord.HTTPException as e:
            logger.debug(f"Failed to remove reaction {emoji} from {user.name}: {e}")
        except Exception as e:
            logger.error(
                f"Unexpected error removing reaction {emoji} from {user.name}: {e}"
            )

    async def update_now_playing(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        current = guild_data["current"]

        if not current:
            if guild_data.get("now_playing_message"):
                try:
                    await guild_data["now_playing_message"].delete()
                except (discord.NotFound, discord.HTTPException):
                    pass
                guild_data["now_playing_message"] = None
                guild_data["message_ready_for_timestamps"] = False
            return

        current_position = self.playback_service.get_current_position(guild_id)
        progress = build_progress_bar(current_position, current.duration)

        status = "Playing"
        if self.playback_service.is_paused(guild_id):
            status = "Paused"
        elif (
                not guild_data["voice_client"]
                or not guild_data["voice_client"].is_playing()
        ):
            status = "Stopped"

        embed = create_embed(
            f"🎵 Now {status}",
            f"**{current.title}**\n"
            f"*by {current.uploader}*\n\n"
            f"`{format_duration(current_position)} {progress} {format_duration(current.duration)}`\n\n"
            f"🔊 Volume: {guild_data['volume']}%\n"
            f"🔁 Loop: {guild_data['loop_mode'].title()}\n"
            f"🔀 Shuffle: {'On' if guild_data['shuffle'] else 'Off'}\n"
            f"📝 Requested by: {current.requested_by}\n"
            f"📋 Queue length: {len(guild_data['queue'])} ",
            COLOR,
            self.bot.user,
        )

        if current.thumbnail:
            embed.set_thumbnail(url=current.thumbnail)

        await self.create_now_playing_message(guild_id, embed)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        if user.bot or not reaction.message.guild:
            return

        if is_banned(user.id):
            await self.remove_reaction(reaction, user, str(reaction.emoji))

            try:
                embed = create_embed(
                    "Access Denied",
                    "You are banned from using this bot.",
                    COLOR,
                    self.bot.user,
                )
                msg = await reaction.message.channel.send(
                    content=user.mention, embed=embed, delete_after=5
                )
            except discord.Forbidden:
                pass
            return

        emoji = str(reaction.emoji)
        guild_data = self.bot.get_guild_data(reaction.message.guild.id)

        if (
                not guild_data.get("now_playing_message")
                or guild_data["now_playing_message"].id != reaction.message.id
        ):
            return

        if not user.voice or not guild_data.get("voice_client"):
            await self.remove_reaction(reaction, user, emoji)
            return

        if guild_data["voice_client"].channel != user.voice.channel:
            await self.remove_reaction(reaction, user, emoji)
            return

        try:
            match emoji:
                case "⏯️":
                    if guild_data["voice_client"].is_playing():
                        self.playback_service.handle_pause(reaction.message.guild.id)
                    elif guild_data["voice_client"].is_paused():
                        self.playback_service.handle_resume(reaction.message.guild.id)

                case "⏭️":
                    if (
                            guild_data["voice_client"].is_playing()
                            or guild_data["voice_client"].is_paused()
                    ):
                        guild_data["voice_client"].stop()

                case "⏮️":
                    await self.play_previous(reaction.message.guild.id)

                case "🔀":
                    self.queue_service.toggle_shuffle(reaction.message.guild.id)

                case "🔁":
                    modes = ["off", "song", "queue"]
                    current_index = modes.index(guild_data["loop_mode"])
                    self.queue_service.set_loop_mode(
                        reaction.message.guild.id,
                        modes[(current_index + 1) % len(modes)],
                    )

                case "⏹️":
                    self.queue_service.clear_queue(reaction.message.guild.id)
                    guild_data["current"] = None
                    guild_data["start_time"] = None
                    if (
                            guild_data["voice_client"].is_playing()
                            or guild_data["voice_client"].is_paused()
                    ):
                        guild_data["voice_client"].stop()

                case "🔊":
                    new_volume = min(100, guild_data["volume"] + 10)
                    guild_data["volume"] = new_volume
                    if guild_data["voice_client"] and guild_data["voice_client"].source:
                        try:
                            guild_data["voice_client"].source.volume = new_volume / 100
                        except AttributeError:
                            pass

                case "🔉":
                    new_volume = max(0, guild_data["volume"] - 10)
                    guild_data["volume"] = new_volume
                    if guild_data["voice_client"] and guild_data["voice_client"].source:
                        try:
                            guild_data["voice_client"].source.volume = new_volume / 100
                        except AttributeError:
                            pass

            await self.bot.save_guild_queue(reaction.message.guild.id)

        except Exception as e:
            logger.error(f"Reaction control error: {e}")

        await self.remove_reaction(reaction, user, emoji)

    async def play_previous(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)

        if not guild_data["history"]:
            return False

        if "history_position" not in guild_data:
            guild_data["history_position"] = len(guild_data["history"])

        guild_data["history_position"] -= 1

        if guild_data["history_position"] < 0:
            guild_data["history_position"] = 0
            return False

        previous_song = guild_data["history"][guild_data["history_position"]]

        if guild_data["current"]:
            guild_data["queue"].insert(0, guild_data["current"])

        guild_data["current"] = Song.from_dict(previous_song.to_dict())
        guild_data["seek_offset"] = 0
        guild_data["position"] = 0
        guild_data["start_time"] = None

        if guild_data["voice_client"] and (
                guild_data["voice_client"].is_playing()
                or guild_data["voice_client"].is_paused()
        ):
            guild_data["voice_client"].stop()

        await self.play_previous_song_directly(guild_id)
        return True

    async def play_previous_song_directly(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)

        async with guild_data["play_lock"]:
            current_song = guild_data["current"]

            if not current_song:
                return

            if (
                    not guild_data["voice_client"]
                    or not guild_data["voice_client"].is_connected()
            ):
                logger.info(
                    f"Voice client disconnected for guild {guild_id}, stopping playback"
                )
                guild_data["current"] = None
                guild_data["start_time"] = None
                return

            max_retries = 2
            for attempt in range(max_retries):
                try:
                    logger.info(
                        f"Extracting fresh stream URL for previous song: {current_song.title} (attempt {attempt + 1})"
                    )

                    fresh_data = await self.bot.get_song_info_cached(
                        current_song.webpage_url
                    )

                    if not fresh_data or not fresh_data.get("url"):
                        raise Exception(
                            f"No stream URL available for {current_song.title}"
                        )

                    current_song.url = fresh_data["url"]
                    if fresh_data.get("title"):
                        current_song.title = fresh_data["title"]
                    if fresh_data.get("duration"):
                        current_song.duration = fresh_data["duration"]
                    if fresh_data.get("thumbnail"):
                        current_song.thumbnail = fresh_data["thumbnail"]
                    if fresh_data.get("uploader"):
                        current_song.uploader = fresh_data["uploader"]

                    break

                except Exception as e:
                    logger.error(
                        f"Error extracting stream URL (attempt {attempt + 1}): {e}"
                    )

                    if attempt == max_retries - 1:
                        logger.error(
                            f"Failed to extract stream for {current_song.title} after {max_retries} attempts"
                        )
                        await self.playback_service.play_next(guild_id)
                        return
                    else:
                        await asyncio.sleep(0.5)

            try:
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(current_song.url, **self.bot.ffmpeg_options),
                    volume=guild_data["volume"] / 100,
                )

                def after_playing(error):
                    if error:
                        logger.error(f"Player error: {error}")

                    coro = self.playback_service.play_next(guild_id)
                    fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
                    fut.add_done_callback(lambda f: f.exception())

                guild_data["seek_offset"] = 0
                guild_data["position"] = 0
                guild_data["start_time"] = datetime.now()
                guild_data["last_activity"] = datetime.now()

                guild_data["voice_client"].play(source, after=after_playing)

                await asyncio.sleep(0.2)
                await self.update_now_playing(guild_id)
                await self.bot.save_guild_queue(guild_id)

                logger.info(
                    f"Now playing previous song: {current_song.title} in guild {guild_id}"
                )

            except Exception as e:
                logger.error(f"Error playing previous song: {e}")
                guild_data["current"] = None
                guild_data["start_time"] = None
                await self.bot.save_guild_queue(guild_id)

    # Slash Commands Start Here

    @discord.app_commands.command(name="join", description="Join your voice channel")
    async def join_slash(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            embed = create_embed(
                "Error", "You must be in a voice channel!", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        voice_channel = interaction.user.voice.channel
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if guild_data["voice_client"] and guild_data["voice_client"].is_connected():
            if guild_data["voice_client"].channel == voice_channel:
                embed = create_embed(
                    "Already Connected",
                    f"I'm already in {voice_channel.name}!",
                    COLOR,
                    self.bot.user,
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            else:
                try:
                    await guild_data["voice_client"].move_to(voice_channel)
                    embed = create_embed(
                        "Moved", f"Moved to {voice_channel.name}!", COLOR, self.bot.user
                    )
                    await interaction.response.send_message(embed=embed)
                    return
                except Exception as e:
                    logger.error(f"Failed to move to voice channel: {e}")
                    embed = create_embed(
                        "Error",
                        "Failed to move to your voice channel!",
                        COLOR,
                        self.bot.user,
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return

        try:
            guild_data["voice_client"] = await voice_channel.connect()
            guild_data["last_activity"] = datetime.now()

            embed = create_embed(
                "Connected", f"Joined {voice_channel.name}!", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed)

        except Exception as e:
            logger.error(f"Failed to connect to voice channel: {e}")
            embed = create_embed(
                "Error",
                "Failed to connect to your voice channel! Check my permissions.",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.app_commands.command(
        name="play", description="Play a song or add it to queue"
    )
    @discord.app_commands.describe(query="Song name, URL, or search term")
    async def play_slash(self, interaction: discord.Interaction, query: str):
        if not await self.check_voice_channel(interaction, allow_auto_join=True):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data.get("music_channel_id"):
            guild_data["music_channel_id"] = interaction.channel.id
            await self.bot.save_guild_music_channel(
                interaction.guild.id, interaction.channel.id
            )

        if not await self.ensure_voice_connection(interaction):
            embed = create_embed(
                "Error", "Failed to connect to voice channel!", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if guild_data["voice_client"].channel != interaction.user.voice.channel:
            try:
                await guild_data["voice_client"].move_to(interaction.user.voice.channel)
            except Exception as e:
                logger.error(f"Failed to move to voice channel: {e}")

        searching_embed = create_embed(
            "Searching...", f"Looking for: `{query}`", COLOR, self.bot.user
        )
        await interaction.response.send_message(embed=searching_embed)

        try:
            is_playlist = "playlist" in query.lower() and "youtube.com" in query.lower()

            if is_playlist:
                playlist_songs = (
                    await self.music_service.handle_youtube_playlist_optimized(query)
                )

                if not playlist_songs:
                    embed = create_embed(
                        "Error", "Could not process playlist!", COLOR, self.bot.user
                    )
                    await interaction.edit_original_response(embed=embed)
                    return

                existing_urls = get_existing_urls(guild_data)
                added_count = 0
                skipped_count = 0

                for data in playlist_songs:
                    if data.get("webpage_url") not in existing_urls:
                        song = Song(data)
                        song.requested_by = interaction.user.mention
                        self.queue_service.add_song_to_queue(interaction.guild.id, song)
                        existing_urls.add(data.get("webpage_url"))
                        added_count += 1
                    else:
                        skipped_count += 1

                if added_count > 0:
                    embed = create_embed(
                        "Playlist Added",
                        f"Added {added_count} songs to queue!\n"
                        + (
                            f"Skipped {skipped_count} duplicates."
                            if skipped_count > 0
                            else ""
                        ),
                        COLOR,
                        self.bot.user,
                    )
                    self.queue_service.sync_loop_backup(interaction.guild.id)
                else:
                    embed = create_embed(
                        "No Songs Added",
                        "All songs were duplicates!",
                        COLOR,
                        self.bot.user,
                    )

                await interaction.edit_original_response(embed=embed)

            else:
                song_data = await self.bot.get_song_info_cached(query)

                if not song_data:
                    embed = create_embed(
                        "Error", "Could not find the song!", COLOR, self.bot.user
                    )
                    await interaction.edit_original_response(embed=embed)
                    return

                if isinstance(song_data, list):
                    existing_urls = get_existing_urls(guild_data)
                    added_count = 0
                    skipped_count = 0

                    for data in song_data:
                        if data.get("webpage_url") and data.get("title"):
                            if data["webpage_url"] not in existing_urls:
                                song = Song(data)
                                song.requested_by = interaction.user.mention
                                self.queue_service.add_song_to_queue(
                                    interaction.guild.id, song
                                )
                                existing_urls.add(data["webpage_url"])
                                added_count += 1
                            else:
                                skipped_count += 1

                    if added_count > 0:
                        embed = create_embed(
                            "Playlist Added",
                            f"Added {added_count} songs to queue!"
                            + (
                                f"\nSkipped {skipped_count} duplicates."
                                if skipped_count > 0
                                else ""
                            ),
                            COLOR,
                            self.bot.user,
                        )
                        self.queue_service.sync_loop_backup(interaction.guild.id)
                    else:
                        embed = create_embed(
                            "No Songs Added",
                            "All songs were duplicates!",
                            COLOR,
                            self.bot.user,
                        )

                    await interaction.edit_original_response(embed=embed)
                else:
                    if not song_data.get("webpage_url") or not song_data.get("title"):
                        embed = create_embed(
                            "Error", "Invalid song data!", COLOR, self.bot.user
                        )
                        await interaction.edit_original_response(embed=embed)
                        return

                    song_url = song_data["webpage_url"]

                    if (
                            guild_data["current"]
                            and guild_data["current"].webpage_url == song_url
                    ):
                        embed = create_embed(
                            "Duplicate Song",
                            "This song is currently playing!",
                            COLOR,
                            self.bot.user,
                        )
                        await interaction.edit_original_response(embed=embed)
                        return

                    for i, existing_song in enumerate(guild_data["queue"], 1):
                        if existing_song.webpage_url == song_url:
                            embed = create_embed(
                                "Duplicate Song",
                                f"This song is already in queue at position {i}!",
                                COLOR,
                                self.bot.user,
                            )
                            await interaction.edit_original_response(embed=embed)
                            return

                    song = Song(song_data)
                    song.requested_by = interaction.user.mention
                    self.queue_service.add_song_to_queue(interaction.guild.id, song)

                    position = len(guild_data["queue"])
                    if position == 1 and not guild_data["current"]:
                        embed = create_embed("Added to Queue", "", COLOR, self.bot.user)
                    else:
                        embed = create_embed(
                            "Added to Queue",
                            f"{song}\n\nPosition in queue: {position}",
                            COLOR,
                            self.bot.user,
                        )

                    if hasattr(song, "thumbnail") and song.thumbnail:
                        embed.set_thumbnail(url=song.thumbnail)

                    await interaction.edit_original_response(embed=embed)

            if guild_data["queue"]:
                asyncio.create_task(
                    self.playback_service.play_next(interaction.guild.id)
                )

            guild_data["last_activity"] = datetime.now()

        except Exception as e:
            logger.error(f"Error in play command: {e}")
            embed = create_embed(
                "Error", f"An error occurred: {str(e)}", COLOR, self.bot.user
            )
            await interaction.edit_original_response(embed=embed)

    @discord.app_commands.command(name="pause", description="Pause the current song")
    async def pause_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return

        if self.playback_service.handle_pause(interaction.guild.id):
            embed = create_embed(
                "⏸️ Paused", "Music has been paused.", COLOR, self.bot.user
            )
        else:
            embed = create_embed(
                "❌ Error", "Nothing is playing!", COLOR, self.bot.user
            )

        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(name="resume", description="Resume the paused song")
    async def resume_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return

        if self.playback_service.handle_resume(interaction.guild.id):
            embed = create_embed(
                "▶️ Resumed", "Music has been resumed.", COLOR, self.bot.user
            )
        else:
            embed = create_embed(
                "❌ Error", "Music is not paused!", COLOR, self.bot.user
            )

        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(name="skip", description="Skip the current song")
    async def skip_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if guild_data["voice_client"] and (
                guild_data["voice_client"].is_playing()
                or guild_data["voice_client"].is_paused()
        ):
            skipped_song = (
                guild_data["current"].title if guild_data["current"] else "Unknown"
            )

            if guild_data["current"]:
                self.queue_service.add_to_history(
                    interaction.guild.id, guild_data["current"]
                )

            guild_data["voice_client"].stop()
            embed = create_embed(
                "Skipped", f"Skipped: **{skipped_song}**", COLOR, self.bot.user
            )
        else:
            embed = create_embed("Error", "Nothing is playing!", COLOR, self.bot.user)

        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(name="previous", description="Play the previous song")
    async def previous_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["history"]:
            embed = create_embed(
                "❌ No Previous Songs",
                "No previous songs in history",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if "history_position" not in guild_data:
            guild_data["history_position"] = len(guild_data["history"])

        target_position = guild_data["history_position"] - 1

        if target_position < 0:
            embed = create_embed(
                "❌ No Previous Songs",
                "Already at the beginning of history",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        previous_song_title = guild_data["history"][target_position].title

        success = await self.play_previous(interaction.guild.id)

        if success:
            embed = create_embed(
                "⏮️ Previous Song",
                f"Playing previous: **{previous_song_title}**",
                COLOR,
                self.bot.user,
            )
            await self.bot.save_guild_queue(interaction.guild.id)
        else:
            embed = create_embed(
                "❌ Error", "Could not play previous song!", COLOR, self.bot.user
            )

        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(
        name="skipto", description="Skip to a specific song in the queue"
    )
    @discord.app_commands.describe(position="Position in queue to skip to")
    async def skipto_slash(self, interaction: discord.Interaction, position: int):
        if not await self.check_voice_channel(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)
        visible_queue = self.queue_service.get_visible_queue(interaction.guild.id)

        if not visible_queue:
            embed = create_embed("Error", "Queue is empty!", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if position < 1 or position > len(visible_queue):
            embed = create_embed(
                "Error",
                f"Invalid position! Queue has {len(visible_queue)} songs.",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if position == 1:
            if guild_data["voice_client"] and (
                    guild_data["voice_client"].is_playing()
                    or guild_data["voice_client"].is_paused()
            ):
                if guild_data["current"]:
                    self.queue_service.add_to_history(
                        interaction.guild.id, guild_data["current"]
                    )

                guild_data["voice_client"].stop()
                embed = create_embed(
                    "Skipped to Song",
                    f"Skipped to: **{visible_queue[0].title}**",
                    COLOR,
                    self.bot.user,
                )
            else:
                embed = create_embed(
                    "Error", "Nothing is playing!", COLOR, self.bot.user
                )

            await interaction.response.send_message(embed=embed)
            return

        async with guild_data["play_lock"]:
            target_song = visible_queue[position - 1]
            primary_queue_size = len(guild_data["queue"])

            if guild_data["current"]:
                self.queue_service.add_to_history(
                    interaction.guild.id, guild_data["current"]
                )

            if position <= primary_queue_size:
                songs_to_skip = position - 1

                for _ in range(songs_to_skip):
                    if guild_data["queue"]:
                        skipped_song = guild_data["queue"].pop(0)
                        self.queue_service.add_to_history(
                            interaction.guild.id, skipped_song
                        )

            else:
                for song in guild_data["queue"]:
                    self.queue_service.add_to_history(interaction.guild.id, song)
                guild_data["queue"].clear()

                target_song_copy = Song.from_dict(target_song.to_dict())
                target_song_copy.requested_by = interaction.user.mention
                guild_data["queue"].insert(0, target_song_copy)

            if guild_data["voice_client"] and (
                    guild_data["voice_client"].is_playing()
                    or guild_data["voice_client"].is_paused()
            ):
                guild_data["voice_client"].stop()

            embed = create_embed(
                "Skipped to Song",
                f"Skipped to: **{target_song.title}**",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed)

            await self.bot.save_guild_queue(interaction.guild.id)

    @discord.app_commands.command(name="queue", description="Show the current queue")
    @discord.app_commands.describe(page="Page number to view (optional)")
    async def queue_slash(self, interaction: discord.Interaction, page: int = 1):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["current"] and not guild_data["queue"]:
            embed = create_embed("📋 Queue", "Queue is empty!", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)
            return

        all_visible_songs = self.queue_service.get_visible_queue(interaction.guild.id)

        total_pages = max(
            1, (len(all_visible_songs) + SONGS_PER_PAGE - 1) // SONGS_PER_PAGE
        )

        page = max(1, min(page, total_pages))

        pages = []
        for page_num in range(total_pages):
            start_idx = page_num * SONGS_PER_PAGE
            end_idx = start_idx + SONGS_PER_PAGE

            description = ""

            if guild_data["current"]:
                description += f"**🎵 Now Playing:**\n{guild_data['current']}\n\n"

            if all_visible_songs:
                description += f"**📋 Up Next:**\n"
                for i, song in enumerate(
                        all_visible_songs[start_idx:end_idx], start_idx + 1
                ):
                    description += f"`{i}.` {song}\n"

            if not description.strip():
                description = "Queue is empty!"

            embed = create_embed(
                f"📋 Queue - Page {page_num + 1}/{total_pages}",
                description[:4000],
                COLOR,
                self.bot.user,
            )

            embed.add_field(
                name="Queue", value=str(len(all_visible_songs)), inline=True
            )
            embed.add_field(
                name="Loop Mode", value=guild_data["loop_mode"].title(), inline=True
            )
            embed.add_field(
                name="Shuffle",
                value="On" if guild_data["shuffle"] else "Off",
                inline=True,
            )

            pages.append(embed)

        view = PaginationView(pages, interaction.user)
        view.current_page = page - 1

        view.previous_button.disabled = view.current_page == 0
        view.next_button.disabled = view.current_page == len(pages) - 1

        await interaction.response.send_message(embed=pages[page - 1], view=view)

    @discord.app_commands.command(
        name="volume", description="Set or show the volume (0-100)"
    )
    @discord.app_commands.describe(level="Volume level (0-100)")
    async def volume_slash(self, interaction: discord.Interaction, level: int = None):
        if not await self.check_voice_channel(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if level is None:
            embed = create_embed(
                "🔊 Volume",
                f"Current volume: {guild_data['volume']}%",
                COLOR,
                self.bot.user,
            )
        else:
            level = max(0, min(100, level))
            guild_data["volume"] = level

            if guild_data["voice_client"] and guild_data["voice_client"].source:
                guild_data["voice_client"].source.volume = level / 100

            embed = create_embed(
                "🔊 Volume", f"Volume set to {level}%", COLOR, self.bot.user
            )
            await self.bot.save_guild_queue(interaction.guild.id)

        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(
        name="loop", description="Set loop mode (off/song/queue)"
    )
    @discord.app_commands.describe(mode="Loop mode: off, song, or queue")
    @discord.app_commands.choices(
        mode=[
            discord.app_commands.Choice(name="Off", value="off"),
            discord.app_commands.Choice(name="Current Song", value="song"),
            discord.app_commands.Choice(name="Queue", value="queue"),
        ]
    )
    async def loop_slash(self, interaction: discord.Interaction, mode: str):
        if not await self.check_voice_channel(interaction):
            return

        self.queue_service.set_loop_mode(interaction.guild.id, mode)

        mode_emojis = {"off": "🔄", "song": "🔂", "queue": "🔁"}
        embed = create_embed(
            f"{mode_emojis.get(mode, '🔄')} Loop Mode",
            f"Loop mode set to: **{mode.title()}**",
            COLOR,
            self.bot.user,
        )

        await self.bot.save_guild_queue(interaction.guild.id)
        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(name="shuffle", description="Toggle shuffle mode")
    async def shuffle_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return

        shuffle_state = self.queue_service.toggle_shuffle(interaction.guild.id)

        embed = create_embed(
            "🔀 Shuffle",
            f"Shuffle mode: **{'On' if shuffle_state else 'Off'}**",
            COLOR,
            self.bot.user,
        )

        await self.bot.save_guild_queue(interaction.guild.id)
        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(
        name="stop", description="Stop playback and clear queue"
    )
    async def stop_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        self.queue_service.clear_queue(interaction.guild.id)
        guild_data["current"] = None
        guild_data["start_time"] = None

        if guild_data["voice_client"] and (
                guild_data["voice_client"].is_playing()
                or guild_data["voice_client"].is_paused()
        ):
            guild_data["voice_client"].stop()

        await self.bot.clear_guild_queue_from_db(interaction.guild.id)

        embed = create_embed(
            "Stopped", "Playback stopped and queue cleared.", COLOR, self.bot.user
        )
        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(
        name="clear", description="Clear the queue without stopping current song"
    )
    async def clear_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["queue"] and not guild_data["loop_backup"]:
            embed = create_embed(
                "Error", "Queue is already empty!", COLOR, self.bot.user
            )
        else:
            self.queue_service.clear_queue(interaction.guild.id)
            embed = create_embed(
                "Queue Cleared", f"Removed all songs from queue.", COLOR, self.bot.user
            )

        await self.bot.save_guild_queue(interaction.guild.id)
        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(
        name="leave", description="Disconnect from voice channel"
    )
    async def leave_slash(self, interaction: discord.Interaction):
        if not await self.check_voice_channel(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if guild_data["voice_client"]:
            guild_data["intentional_disconnect"] = True

            await guild_data["voice_client"].disconnect()
            guild_data["voice_client"] = None
            self.queue_service.clear_queue(interaction.guild.id)
            guild_data["current"] = None
            guild_data["start_time"] = None

            await self.bot.clear_guild_queue_from_db(interaction.guild.id)

            embed = create_embed(
                "Disconnected", "Left the voice channel.", COLOR, self.bot.user
            )
        else:
            embed = create_embed(
                "Error", "Not connected to a voice channel!", COLOR, self.bot.user
            )

        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(
        name="nowplaying", description="Show the currently playing song"
    )
    async def nowplaying_slash(self, interaction: discord.Interaction):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["current"]:
            embed = create_embed(
                "❌ Nothing Playing",
                "No song is currently playing!",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed)
            return

        current = guild_data["current"]
        current_position = self.playback_service.get_current_position(
            interaction.guild.id
        )

        progress = build_progress_bar(current_position, current.duration)

        if self.playback_service.is_paused(interaction.guild.id):
            status = "Paused"
            status_emoji = "⏸️"
        elif guild_data["voice_client"] and guild_data["voice_client"].is_playing():
            status = "Playing"
            status_emoji = "🎵"
        else:
            status = "Stopped"
            status_emoji = "⏹️"

        embed = create_embed(
            f"{status_emoji} Now {status}",
            f"**{current.title}**\n"
            f"*by {current.uploader}*\n\n"
            f"`{format_duration(current_position)} {progress} {format_duration(current.duration)}`\n\n"
            f"🔊 Volume: {guild_data['volume']}%\n"
            f"🔁 Loop: {guild_data['loop_mode'].title()}\n"
            f"🔀 Shuffle: {'On' if guild_data['shuffle'] else 'Off'}\n"
            f"📝 Requested by: {current.requested_by}\n"
            f"📋 Queue length: {len(guild_data['queue'])}",
            COLOR,
            self.bot.user,
        )

        if current.thumbnail:
            embed.set_thumbnail(url=current.thumbnail)

        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(
        name="remove", description="Remove a song from the queue permanently"
    )
    @discord.app_commands.describe(position="Position of the song to remove (1-based)")
    async def remove_slash(self, interaction: discord.Interaction, position: int):
        if not await self.check_voice_channel(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        visible_songs = self.queue_service.get_visible_queue(interaction.guild.id)

        if not visible_songs:
            embed = create_embed("Error", "Queue is empty!", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)
            return

        if position < 1 or position > len(visible_songs):
            embed = create_embed(
                "Error",
                f"Invalid position! Visible queue has {len(visible_songs)} songs.",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed)
            return

        song_to_remove = visible_songs[position - 1]
        actual_queue_size = len(guild_data["queue"])
        removed_song = None

        if position <= actual_queue_size:
            removed_song = self.queue_service.remove_song_from_queue(
                interaction.guild.id, position - 1
            )

        guild_data["loop_backup"] = [
            song
            for song in guild_data["loop_backup"]
            if song.webpage_url != song_to_remove.webpage_url
        ]

        if not removed_song:
            removed_song = song_to_remove

        embed = create_embed(
            "Song Removed",
            f"Permanently removed: **{removed_song.title}**\n",
            COLOR,
            self.bot.user,
        )

        await self.bot.save_guild_queue(interaction.guild.id)
        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(
        name="move", description="Move a song to a different position in queue"
    )
    @discord.app_commands.describe(
        from_pos="Current position of the song", to_pos="New position for the song"
    )
    async def move_slash(
            self, interaction: discord.Interaction, from_pos: int, to_pos: int
    ):
        if not await self.check_voice_channel(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["queue"]:
            embed = create_embed("❌ Error", "Queue is empty!", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)
            return

        queue_length = len(guild_data["queue"])
        if (
                from_pos < 1
                or from_pos > queue_length
                or to_pos < 1
                or to_pos > queue_length
        ):
            embed = create_embed(
                "❌ Error",
                f"Invalid position! Queue has {queue_length} songs.",
                COLOR,
                self.bot.user,
            )
            await interaction.response.send_message(embed=embed)
            return

        from_pos -= 1
        to_pos -= 1

        song = guild_data["queue"][from_pos]
        self.queue_service.move_song_in_queue(interaction.guild.id, from_pos, to_pos)

        embed = create_embed(
            "🔄 Song Moved",
            f"Moved **{song.title}**\nFrom position {from_pos + 1} to position {to_pos + 1}",
            COLOR,
            self.bot.user,
        )

        await self.bot.save_guild_queue(interaction.guild.id)
        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(
        name="search", description="Search for songs and choose which to play"
    )
    @discord.app_commands.describe(query="Search term")
    async def search_slash(self, interaction: discord.Interaction, query: str):
        if not await self.check_voice_channel(interaction, allow_auto_join=True):
            return

        searching_embed = create_embed(
            "Searching...", f"Looking for: `{query}`", COLOR, self.bot.user
        )
        await interaction.response.send_message(embed=searching_embed)

        try:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(
                self.bot.executor,
                lambda: self.bot.ytdl.extract_info(
                    f"ytsearch5:{query}", download=False
                ),
            )

            if not data or "entries" not in data or not data["entries"]:
                embed = create_embed(
                    "❌ Error", "No results found!", COLOR, self.bot.user
                )
                await interaction.edit_original_response(embed=embed)
                return

            valid_entries = []
            for entry in data["entries"]:
                if not entry:
                    continue

                title = entry.get("title") or entry.get("alt_title")
                if not title:
                    continue
                entry["title"] = title

                webpage_url = entry.get("webpage_url")
                url = entry.get("url")
                video_id = entry.get("id")

                if not webpage_url:
                    if isinstance(url, str) and url.startswith("http"):
                        webpage_url = url
                    elif video_id:
                        webpage_url = f"https://www.youtube.com/watch?v={video_id}"

                if not webpage_url:
                    continue

                entry["webpage_url"] = webpage_url

                if "url" not in entry or not entry["url"]:
                    entry["url"] = webpage_url

                valid_entries.append(entry)

            if not valid_entries:
                embed = create_embed(
                    "❌ Error", "No valid results found!", COLOR, self.bot.user
                )
                await interaction.edit_original_response(embed=embed)
                return

            description = ""
            for i, entry in enumerate(valid_entries[:5], 1):
                duration = entry.get("duration", 0)
                if duration:
                    minutes, seconds = divmod(int(duration), 60)
                    duration_str = f"{minutes}:{seconds:02d}"
                else:
                    duration_str = "0:00"

                title = entry["title"]
                if len(title) > 50:
                    title = title[:47] + "..."

                description += f"`{i}.` **{title}**\n"
                description += (
                    f"    by {entry.get('uploader', 'Unknown')} • {duration_str}\n\n"
                )

            embed = create_embed("🔍 Search Results", description, COLOR, self.bot.user)

            view = SongSelectView(valid_entries, interaction.user, self)
            message = await interaction.edit_original_response(embed=embed, view=view)
            view.message = message

        except Exception as e:
            logger.error(f"Search command error: {e}")
            embed = create_embed(
                "❌ Error", "An error occurred during search.", COLOR, self.bot.user
            )
            await interaction.edit_original_response(embed=embed)

    async def process_selected_song(
            self, interaction: discord.Interaction, selected_song: dict
    ):
        try:
            guild_data = self.bot.get_guild_data(interaction.guild.id)

            if not guild_data.get("music_channel_id"):
                guild_data["music_channel_id"] = interaction.channel.id
                await self.bot.save_guild_music_channel(
                    interaction.guild.id, interaction.channel.id
                )

            if (
                    not guild_data["voice_client"]
                    or not guild_data["voice_client"].is_connected()
            ):
                if not interaction.user.voice:
                    embed = create_embed(
                        "Error", "You must be in a voice channel!", COLOR, self.bot.user
                    )
                    await interaction.edit_original_response(embed=embed, view=None)
                    return

                try:
                    guild_data["voice_client"] = (
                        await interaction.user.voice.channel.connect()
                    )
                except Exception as e:
                    logger.error(f"Failed to connect to voice: {e}")
                    embed = create_embed(
                        "Error",
                        "Failed to connect to voice channel!",
                        COLOR,
                        self.bot.user,
                    )
                    await interaction.edit_original_response(embed=embed, view=None)
                    return
            elif guild_data["voice_client"].channel != interaction.user.voice.channel:
                try:
                    await guild_data["voice_client"].move_to(
                        interaction.user.voice.channel
                    )
                except Exception as e:
                    logger.error(f"Failed to move to voice channel: {e}")

            song = Song(selected_song)
            song.requested_by = interaction.user.mention

            existing_urls = get_existing_urls(guild_data)
            if song.webpage_url in existing_urls:
                embed = create_embed(
                    "Duplicate Song",
                    "This song is already in queue or playing!",
                    COLOR,
                    self.bot.user,
                )
                await interaction.edit_original_response(embed=embed, view=None)
                return

            self.queue_service.add_song_to_queue(interaction.guild.id, song)

            if (
                    not guild_data["voice_client"].is_playing()
                    and not guild_data["current"]
            ):
                await self.playback_service.play_next(interaction.guild.id)
                embed = create_embed("🎵 Now Playing", str(song), COLOR, self.bot.user)
            else:
                position = len(guild_data["queue"])
                embed = create_embed(
                    "📋 Added to Queue",
                    f"{song}\n\nPosition in queue: {position}",
                    COLOR,
                    self.bot.user,
                )

            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)

            await interaction.edit_original_response(embed=embed, view=None)
            guild_data["last_activity"] = datetime.now()
            await self.bot.save_guild_queue(interaction.guild.id)

        except Exception as e:
            logger.error(f"Error processing selected song: {e}")
            embed = create_embed(
                "❌ Error", "Failed to add song to queue.", COLOR, self.bot.user
            )
            try:
                await interaction.edit_original_response(embed=embed, view=None)
            except discord.HTTPException:
                pass

    @discord.app_commands.command(
        name="setmusicchannel", description="Set the channel for music messages"
    )
    async def set_music_channel_slash(self, interaction: discord.Interaction):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        old_channel_id = guild_data.get("music_channel_id")
        guild_data["music_channel_id"] = interaction.channel.id
        await self.bot.save_guild_music_channel(
            interaction.guild.id, interaction.channel.id
        )

        if old_channel_id and old_channel_id != interaction.channel.id:
            old_channel = interaction.guild.get_channel(old_channel_id)
            if old_channel:
                embed = create_embed(
                    "📺 Music Channel Updated",
                    f"Music messages moved from {old_channel.mention} to {interaction.channel.mention}",
                    COLOR,
                    self.bot.user,
                )
            else:
                embed = create_embed(
                    "📺 Music Channel Set",
                    f"Music messages will now be sent to {interaction.channel.mention}",
                    COLOR,
                    self.bot.user,
                )
        else:
            embed = create_embed(
                "📺 Music Channel Set",
                f"Music messages will now be sent to {interaction.channel.mention}",
                COLOR,
                self.bot.user,
            )

        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(
        name="seek", description="Seek to a specific position in the current song"
    )
    @discord.app_commands.describe(
        position="Time position (e.g., '1:30', '90', '2:15')"
    )
    async def seek_slash(self, interaction: discord.Interaction, position: str):
        if not await self.check_voice_channel(interaction):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["current"]:
            embed = create_embed(
                "Error", "No song is currently playing!", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if (
                not guild_data["voice_client"]
                or not guild_data["voice_client"].is_connected()
        ):
            embed = create_embed(
                "Error", "Bot is not connected to voice!", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not (
                guild_data["voice_client"].is_playing()
                or guild_data["voice_client"].is_paused()
        ):
            if guild_data["current"]:
                try:
                    await self.playback_service.play_next(interaction.guild.id)
                    await asyncio.sleep(0.5)
                except:
                    pass

            if not (
                    guild_data["voice_client"].is_playing()
                    or guild_data["voice_client"].is_paused()
            ):
                embed = create_embed(
                    "Error", "No song is currently playing!", COLOR, self.bot.user
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        try:
            seek_seconds = parse_time_to_seconds(position)
        except ValueError as e:
            embed = create_embed("Error", str(e), COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        current_song = guild_data["current"]
        if seek_seconds < 0:
            seek_seconds = 0
        elif current_song.duration > 0 and seek_seconds >= current_song.duration - 5:
            seek_seconds = max(0, current_song.duration - 5)

        if guild_data.get("seeking", False):
            embed = create_embed(
                "Error", "Already seeking, please wait...", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        was_paused = guild_data["voice_client"].is_paused()
        await interaction.response.defer()
        async with guild_data["play_lock"]:
            try:
                guild_data["seeking"] = True
                guild_data["seeking_start_time"] = asyncio.get_event_loop().time()

                seek_embed = create_embed(
                    "Seeking...",
                    f"Seeking to {format_duration(seek_seconds)} in **{current_song.title}**",
                    COLOR,
                    self.bot.user,
                )
                await interaction.followup.send(embed=seek_embed)

                if (
                        guild_data["voice_client"].is_playing()
                        or guild_data["voice_client"].is_paused()
                ):
                    guild_data["voice_client"].stop()

                await asyncio.sleep(0.2)

                fresh_data = None
                for attempt in range(3):
                    try:
                        fresh_data = await self.bot.get_song_info_cached(
                            current_song.webpage_url
                        )
                        if fresh_data and fresh_data.get("url"):
                            break
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.warning(
                            f"Stream extraction attempt {attempt + 1} failed: {e}"
                        )
                        if attempt < 2:
                            await asyncio.sleep(1)

                if not fresh_data or not fresh_data.get("url"):
                    embed = create_embed(
                        "Error",
                        "Failed to seek - could not get fresh stream URL",
                        COLOR,
                        self.bot.user,
                    )
                    await interaction.edit_original_response(embed=embed)
                    return

                seek_strategies = [
                    {
                        "before_options": f"-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -ss {seek_seconds} -nostdin -user_agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0'",
                        "options": "-vn -bufsize 1024k",
                    },
                    {
                        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin -user_agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0'",
                        "options": f"-vn -ss {seek_seconds} -bufsize 1024k",
                    },
                    {
                        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin",
                        "options": "-vn",
                    },
                ]

                source = None
                strategy_used = 0

                for i, ffmpeg_options in enumerate(seek_strategies):
                    try:
                        source = discord.PCMVolumeTransformer(
                            discord.FFmpegPCMAudio(fresh_data["url"], **ffmpeg_options),
                            volume=guild_data["volume"] / 100,
                        )
                        strategy_used = i
                        break
                    except Exception as e:
                        logger.warning(f"Seek strategy {i + 1} failed: {e}")
                        if i < len(seek_strategies) - 1:
                            continue
                        else:
                            raise e

                if not source:
                    embed = create_embed(
                        "Error",
                        "Failed to seek - stream format not supported",
                        COLOR,
                        self.bot.user,
                    )
                    await interaction.edit_original_response(embed=embed)
                    return

                guild_data["seek_offset"] = seek_seconds if strategy_used == 0 else 0
                guild_data["start_time"] = datetime.now()

                def after_seeking(error):
                    if error:
                        logger.error(f"Seek player error: {error}")
                    else:
                        if guild_data["current"] and not guild_data.get(
                                "seeking", False
                        ):
                            self.queue_service.add_to_history(
                                interaction.guild.id, guild_data["current"]
                            )

                    if not guild_data.get("seeking", False):
                        coro = self.playback_service.play_next(interaction.guild.id)
                        fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
                        fut.add_done_callback(lambda f: f.exception())

                guild_data["voice_client"].play(source, after=after_seeking)

                if was_paused:
                    await asyncio.sleep(0.2)
                    guild_data["voice_client"].pause()
                    guild_data["pause_position"] = seek_seconds

                success_embed = create_embed(
                    "Seeked",
                    f"Moved to {format_duration(seek_seconds)} in **{current_song.title}**"
                    + (f" (strategy {strategy_used + 1})" if strategy_used > 0 else ""),
                    COLOR,
                    self.bot.user,
                )
                await interaction.edit_original_response(embed=success_embed)

                guild_data["message_ready_for_timestamps"] = True

            except Exception as e:
                logger.error(f"Seek error: {e}")
                try:
                    guild_data["seek_offset"] = 0
                    guild_data["start_time"] = datetime.now()
                    await self.playback_service.play_next(interaction.guild.id)
                    embed = create_embed(
                        "Seek Failed",
                        "Could not seek, restarted song from beginning",
                        COLOR,
                        self.bot.user,
                    )
                except:
                    embed = create_embed(
                        "Error",
                        "Failed to seek and could not recover playback",
                        COLOR,
                        self.bot.user,
                    )
                await interaction.edit_original_response(embed=embed)
            finally:
                guild_data["seeking"] = False
                if "seeking_start_time" in guild_data:
                    del guild_data["seeking_start_time"]

    @discord.app_commands.command(
        name="autoplay", description="Auto play related songs after queue"
    )
    async def autoplay_slash(self, interaction: discord.Interaction):
        embed = create_embed("Under construction. Check again after 2 weeks", "", COLOR, self.bot.user)
        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(
        name="help", description="Show all available commands and how to use them"
    )
    async def help_slash(self, interaction: discord.Interaction):
        description = """**Music Bot Commands Guide**

        **Basic Commands:**
        `/join` - Join your voice channel
        `/play <query>` - Play a song or add it to queue (supports YouTube URLs, Spotify links, or search terms)
        `/pause` - Pause the current song
        `/resume` - Resume the paused song
        `/skip` - Skip the current song
        `/previous` - Play the previous song from history
        `/autoplay` - Auto play related songs after queue
        `/stop` - Stop playback and clear queue
        `/leave` - Disconnect from voice channel

        **Queue Management:**
        `/queue [page]` - Show the current queue (paginated)
        `/clear` - Clear the queue without stopping current song
        `/remove <position>` - Remove a song from queue by position
        `/move <from_pos> <to_pos>` - Move a song to different position
        `/skipto <position>` - Skip to a specific song in queue

        **Playback Controls:**
        `/volume [level]` - Set or show volume (0-100)
        `/loop <mode>` - Set loop mode: off, song, or queue
        `/shuffle` - Toggle shuffle mode
        `/seek <position>` - Seek to specific time (e.g., '1:30', '90', etc)
        `/nowplaying` - Show currently playing song info

        **Search & Discovery:**
        `/search <query>` - Search for songs and choose which to play
        `/history show [page]` - Show recently played songs
        `/history play <number>` - Play a song from history by number
        `/history add_all` - Add all songs from history to queue
        `/history clear` - Clear all history songs

        **Playlist Commands: (your playlist(s) are guild specific)**
        `/playlist create <name>` - Create a new empty playlist
        `/playlist add <name> <song>` - Add a song to playlist by searching
        `/playlist add-from-queue <name> <from_queue>` - Add song from current queue to playlist
        `/playlist add-all-queue <name>` - Add entire current queue to playlist
        `/playlist remove <name> <position>` - Remove a song from playlist
        `/playlist move <name> <from_pos> <to_pos>` - Move song in playlist
        `/playlist load <name>` - Load a playlist into the queue
        `/playlist show <name> [page]` - Show songs in a playlist
        `/playlist list` - List all your playlists
        `/playlist delete <name>` - Delete a playlist

        **Settings:**
        `/setmusicchannel` - Set the channel for music messages

        **Tips:**
        • Use reaction controls on the 'Now Playing' message: 
        ⏯️ Pause/Resume, ⏭️ Skip, ⏮️ Previous, 🔀 Shuffle, 🔁 Loop, ⏹️ Stop, 🔊/🔉 Volume
        • Supports YouTube URLs, YouTube playlists, Spotify links, and search queries
        • Queue persists across bot restarts
        • You must be in the same voice channel as the bot to control playback
        • Duplicates are not allowed
        """

        embed = create_embed("Command Guide", description, COLOR, self.bot.user)
        await interaction.response.send_message(embed=embed)

    # @discord.app_commands.command(name="transcript", description="Get and summarize a YouTube video transcript")
    # @discord.app_commands.describe(url="YouTube video URL",
    #                                summarize="Whether to summarize the transcript (default: False)")
    # async def transcript_slash(self, interaction: discord.Interaction, url: str, summarize: bool = False):
    #
    #     if is_banned(interaction.user.id):
    #         embed = create_embed(
    #             "Access Denied",
    #             "You are banned from using this bot.",
    #             COLOR,
    #             self.bot.user
    #         )
    #         await interaction.response.send_message(embed=embed, ephemeral=True)
    #         return
    #
    #     await interaction.response.defer()
    #
    #     try:
    #         transcript_service = TranscriptService(self.bot)
    #
    #         loading_embed = create_embed(
    #             "Loading",
    #             "Fetching transcript...",
    #             COLOR,
    #             self.bot.user
    #         )
    #         await interaction.followup.send(embed=loading_embed)
    #
    #         transcript = transcript_service.get_transcript(url)
    #
    #         if transcript == "disabled":
    #             embed = create_embed(
    #                 "Error",
    #                 "Transcripts are disabled for this video.",
    #                 COLOR,
    #                 self.bot.user
    #             )
    #             await interaction.edit_original_response(embed=embed)
    #             return
    #
    #         if transcript == "not_found":
    #             embed = create_embed(
    #                 "Error",
    #                 "No transcript found for this video.",
    #                 COLOR,
    #                 self.bot.user
    #             )
    #             await interaction.edit_original_response(embed=embed)
    #             return
    #
    #         if not transcript:
    #             embed = create_embed(
    #                 "Error",
    #                 "Could not extract video ID from URL. Make sure it's a valid YouTube URL.",
    #                 COLOR,
    #                 self.bot.user
    #             )
    #             await interaction.edit_original_response(embed=embed)
    #             return
    #
    #         if summarize:
    #             summarizing_embed = create_embed(
    #                 "Processing",
    #                 "Summarizing transcript (this may take a minute)...",
    #                 COLOR,
    #                 self.bot.user
    #             )
    #             await interaction.edit_original_response(embed=summarizing_embed)
    #
    #             summary = await transcript_service.summarize_transcript(transcript)
    #
    #             if summary:
    #                 if len(summary) > 2000:
    #                     filename = "transcript_summary.txt"
    #                     with open(filename, 'w', encoding='utf-8') as f:
    #                         f.write(summary)
    #
    #                     embed = create_embed(
    #                         "Transcript Summary",
    #                         f"Summary is {len(summary)} characters. Sending as file.",
    #                         COLOR,
    #                         self.bot.user
    #                     )
    #                     await interaction.edit_original_response(
    #                         embed=embed,
    #                         attachments=[discord.File(filename)]
    #                     )
    #
    #                     import os
    #                     os.remove(filename)
    #                 else:
    #                     embed = create_embed(
    #                         "Transcript Summary",
    #                         summary,
    #                         COLOR,
    #                         self.bot.user
    #                     )
    #                     await interaction.edit_original_response(embed=embed)
    #             else:
    #                 embed = create_embed(
    #                     "Error",
    #                     "Could not summarize transcript.",
    #                     COLOR,
    #                     self.bot.user
    #                 )
    #                 await interaction.edit_original_response(embed=embed)
    #         else:
    #             if len(transcript) > 2000:
    #                 filename = "transcript.txt"
    #                 with open(filename, 'w', encoding='utf-8') as f:
    #                     f.write(transcript)
    #
    #                 embed = create_embed(
    #                     "Transcript",
    #                     f"Transcript is {len(transcript)} characters. Sending as file.",
    #                     COLOR,
    #                     self.bot.user
    #                 )
    #                 await interaction.edit_original_response(
    #                     embed=embed,
    #                     attachments=[discord.File(filename)]
    #                 )
    #
    #                 import os
    #                 os.remove(filename)
    #             else:
    #                 embed = create_embed(
    #                     "Transcript",
    #                     transcript,
    #                     COLOR,
    #                     self.bot.user
    #                 )
    #                 await interaction.edit_original_response(embed=embed)
    #
    #     except Exception as e:
    #         logger.error(f"Error in transcript command: {e}")
    #         embed = create_embed(
    #             "Error",
    #             f"An error occurred: {str(e)}",
    #             COLOR,
    #             self.bot.user
    #         )
    #         await interaction.edit_original_response(embed=embed)

    async def cog_app_command_error(
            self,
            interaction: discord.Interaction,
            error: discord.app_commands.AppCommandError,
    ):
        logger.error(f"Slash command error: {error}")

        if isinstance(error, discord.app_commands.CommandOnCooldown):
            embed = create_embed(
                "⏰ Command on Cooldown",
                f"Try again in {error.retry_after:.2f} seconds.",
                COLOR,
                self.bot.user,
            )
        else:
            embed = create_embed(
                "❌ Error",
                "An unexpected error occurred. Please try again.",
                COLOR,
                self.bot.user,
            )

        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except:
            pass

    @commands.command(name="leaveguild")
    @commands.is_owner()
    async def leave_guild(self, ctx, guild_id: int = None):
        if guild_id:
            guild = self.bot.get_guild(guild_id)
        else:
            guild = ctx.guild

        if guild:
            await guild.leave()
            await ctx.send(f"✅ Left guild: {guild.name} ({guild.id})")
        else:
            await ctx.send("❌ Could not find that guild.")

    @commands.command(name="banuser")
    @commands.is_owner()
    async def ban_user(self, ctx, user: discord.User):
        if ban_user_id(user.id):
            await ctx.send(f"Banned {user.mention} ({user.id})")
        else:
            await ctx.send(f"{user.mention} ({user.id}) is already banned")

    @commands.command(name="unbanuser")
    @commands.is_owner()
    async def unban_user(self, ctx, user: discord.User):
        if unban_user_id(user.id):
            await ctx.send(f"Unbanned {user.mention} ({user.id})")
        else:
            await ctx.send(f"{user.mention} ({user.id}) was not banned")

    @commands.command(name="listbanned")
    @commands.is_owner()
    async def list_banned(self, ctx):
        try:
            with open("banned_users.txt", "r") as f:
                banned_ids = [line.strip() for line in f.readlines() if line.strip()]

            if not banned_ids:
                await ctx.send("No banned users.")
                return

            msg = "Banned users:\n" + "\n".join(banned_ids)
            await ctx.send(msg)
        except FileNotFoundError:
            await ctx.send("No banned users.")
