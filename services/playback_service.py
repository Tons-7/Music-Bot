import discord
import asyncio
import logging
import aiohttp
from datetime import datetime
from models.song import Song
from utils.helpers import format_duration, build_progress_bar, create_embed
from config import COLOR

logger = logging.getLogger(__name__)


class PlaybackService:
    def __init__(self, bot):
        self.bot = bot

    def handle_pause(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        if guild_data["voice_client"] and guild_data["voice_client"].is_playing():
            current_pos = self.get_current_position(guild_id)
            guild_data["pause_position"] = current_pos
            guild_data["voice_client"].pause()
            return True
        return False

    def handle_resume(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        if guild_data["voice_client"] and guild_data["voice_client"].is_paused():
            if "pause_position" in guild_data:
                guild_data["seek_offset"] = guild_data["pause_position"]
                guild_data["start_time"] = datetime.now()
                del guild_data["pause_position"]
            guild_data["voice_client"].resume()
            return True
        return False

    def get_current_position(self, guild_id: int) -> int:
        guild_data = self.bot.get_guild_data(guild_id)

        if guild_data.get("seeking", False):
            return guild_data.get("seek_offset", 0)

        if not guild_data["start_time"]:
            return guild_data["seek_offset"]

        voice_client = guild_data["voice_client"]
        if not voice_client:
            return guild_data["seek_offset"]

        if voice_client.is_paused():
            if "pause_position" in guild_data:
                return guild_data["pause_position"]
            elapsed = int((datetime.now() - guild_data["start_time"]).total_seconds())
            return elapsed + guild_data["seek_offset"]

        if voice_client.is_playing():
            elapsed = int((datetime.now() - guild_data["start_time"]).total_seconds())
            return elapsed + guild_data["seek_offset"]

        return guild_data["seek_offset"]

    def is_paused(self, guild_id: int) -> bool:
        guild_data = self.bot.get_guild_data(guild_id)
        return guild_data["voice_client"] and guild_data["voice_client"].is_paused()

    async def update_timestamps_task(self):
        current_time = asyncio.get_event_loop().time()

        for guild_id, guild_data in list(self.bot.guilds_data.items()):
            try:
                if self.bot.is_closed():
                    return

                if guild_data.get("seeking_start_time"):
                    if current_time - guild_data["seeking_start_time"] > 15:
                        guild_data["seeking"] = False
                        del guild_data["seeking_start_time"]

                if not self._should_update_timestamp(guild_id, guild_data, current_time):
                    continue

                asyncio.create_task(self._update_single_timestamp(guild_id, current_time))

            except Exception as e:
                logger.error(f"Timer loop error for guild {guild_id}: {e}")
                continue

    def _should_update_timestamp(self, guild_id: int, guild_data: dict, current_time: float) -> bool:
        return (
                guild_data.get("current")
                and guild_data.get("now_playing_message")
                and guild_data.get("voice_client")
                and guild_data.get("message_ready_for_timestamps", False)
                and (
                        guild_data["voice_client"].is_playing()
                        or guild_data["voice_client"].is_paused()
                )
                and not self._is_update_locked(guild_id, current_time)
        )

    def _is_update_locked(self, guild_id: int, current_time: float) -> bool:
        if guild_id not in self.bot.message_update_locks:
            return False

        lock_time = self.bot.message_update_locks[guild_id]
        if current_time - lock_time > 2.0:
            del self.bot.message_update_locks[guild_id]
            return False

        return True

    async def _update_single_timestamp(self, guild_id: int, current_time: float):
        try:
            self.bot.message_update_locks[guild_id] = current_time

            guild_data = self.bot.get_guild_data(guild_id)

            if not await self._validate_message_cached(guild_id, current_time):
                return

            current_position = self.get_current_position(guild_id)
            is_paused = self.is_paused(guild_id)

            embed = self._build_timestamp_embed(guild_data, current_position, is_paused)

            await self._safe_message_edit(guild_data["now_playing_message"], embed)

        except Exception as e:
            logger.warning(f"Timestamp update failed for guild {guild_id}: {e}")
        finally:
            if guild_id in self.bot.message_update_locks:
                del self.bot.message_update_locks[guild_id]

    async def _validate_message_cached(self, guild_id: int, current_time: float) -> bool:
        guild_data = self.bot.get_guild_data(guild_id)
        message = guild_data.get("now_playing_message")

        if not message:
            return False

        cache_key = f"{guild_id}_{message.id}"
        cached = self.bot.message_validation_cache.get(cache_key)

        if cached and current_time - cached["time"] < 10.0:
            return cached["valid"]

        try:
            await message.fetch()
            self.bot.message_validation_cache[cache_key] = {
                "valid": True,
                "time": current_time,
            }
            return True
        except discord.NotFound:
            guild_data["now_playing_message"] = None
            guild_data["message_ready_for_timestamps"] = False
            self.bot.message_validation_cache[cache_key] = {
                "valid": False,
                "time": current_time,
            }
            return False
        except discord.HTTPException:
            return False

    def _build_timestamp_embed(self, guild_data: dict, current_position: int, is_paused: bool) -> discord.Embed:
        current = guild_data["current"]

        progress = build_progress_bar(current_position, current.duration)

        if is_paused:
            status = "Paused"
            status_emoji = "⏸️"
        else:
            status = "Playing"
            status_emoji = "🎵"

        embed = discord.Embed(
            title=f"{status_emoji} Now {status}",
            description=(
                f"**{current.title}**\n"
                f"*by {current.uploader}*\n\n"
                f"`{format_duration(current_position)} {progress} {format_duration(current.duration)}`\n\n"
                f"🔊 Volume: {guild_data['volume']}%\n"
                f"🔁 Loop: {guild_data['loop_mode'].title()}\n"
                f"🔀 Shuffle: {'On' if guild_data['shuffle'] else 'Off'}\n"
                f"👤 Requested by: {current.requested_by}\n"
                f"📋 Queue length: {len(guild_data['queue'])}"
            ),
            color=COLOR,
        )
        embed.set_footer(
            text="Music Bot",
            icon_url=self.bot.user.avatar.url if self.bot.user.avatar else None,
        )

        if current.thumbnail:
            embed.set_thumbnail(url=current.thumbnail)

        return embed

    @staticmethod
    async def _safe_message_edit(message: discord.Message, embed: discord.Embed):
        try:
            await message.edit(embed=embed)
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            if "rate limited" not in str(e).lower():
                logger.warning(f"Message edit failed: {e}")
        except Exception as e:
            logger.warning(f"Unexpected message edit error: {e}")

    async def check_voice_connection(self, guild_id: int, voice_channel) -> bool:
        """Check and repair voice connection if needed"""
        guild_data = self.bot.get_guild_data(guild_id)
        voice_client = guild_data["voice_client"]

        if not voice_client or not voice_client.is_connected():
            logger.warning(f"Voice client disconnected for guild {guild_id}, attempting reconnect...")
            try:
                current_song = guild_data.get("current")
                current_position = self.get_current_position(guild_id) if current_song else 0
                was_paused = voice_client.is_paused() if voice_client else False

                if voice_client:
                    try:
                        await voice_client.disconnect(force=True)
                    except:
                        pass

                guild_data["voice_client"] = await voice_channel.connect(timeout=10.0, reconnect=True)
                logger.info(f"Successfully reconnected to voice in guild {guild_id}")

                if current_song:
                    guild_data["seek_offset"] = current_position
                    await self._resume_after_reconnect(guild_id, current_song, was_paused)

                return True

            except asyncio.TimeoutError:
                logger.error(f"Voice reconnection timeout for guild {guild_id}")
                return False
            except Exception as e:
                logger.error(f"Failed to reconnect voice for guild {guild_id}: {e}")
                return False

        return True

    async def _resume_after_reconnect(self, guild_id: int, song: Song, was_paused: bool):
        """Resume playback after reconnection"""
        try:
            guild_data = self.bot.get_guild_data(guild_id)

            fresh_data = await self.bot.get_song_info(song.webpage_url)
            if not fresh_data or not fresh_data.get("url"):
                logger.error(f"Could not get stream URL after reconnect for {song.title}")
                return

            song.url = fresh_data["url"]

            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(song.url, **self.bot.ffmpeg_options),
                volume=guild_data["volume"] / 100,
            )

            def after_playing(error):
                if error:
                    logger.error(f"Player error after reconnect: {error}")

                if not guild_data.get("seeking", False):
                    from services.queue_service import QueueService
                    queue_service = QueueService(self.bot)
                    if guild_data["current"] and not guild_data.get("seeking", False):
                        queue_service.add_to_history(guild_id, guild_data["current"])

                    coro = self.play_next(guild_id)
                    fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
                    fut.add_done_callback(lambda f: f.exception())

            guild_data["start_time"] = datetime.now()
            guild_data["voice_client"].play(source, after=after_playing)

            if was_paused:
                guild_data["voice_client"].pause()

            logger.info(f"Resumed playback after reconnect: {song.title}")

        except Exception as e:
            logger.error(f"Error resuming playback after reconnect: {e}")

    async def play_next(self, guild_id: int):
        from services.queue_service import QueueService
        queue_service = QueueService(self.bot)

        guild_data = self.bot.get_guild_data(guild_id)

        async with guild_data["play_lock"]:
            if guild_data.get("seeking", False):
                return

            if guild_data["current"] and guild_data["voice_client"]:
                if (
                        guild_data["voice_client"].is_playing()
                        or guild_data["voice_client"].is_paused()
                ):
                    return

            if (
                    not guild_data["voice_client"]
                    or not guild_data["voice_client"].is_connected()
            ):
                logger.info(
                    f"Voice client disconnected for guild {guild_id}, stopping playback"
                )
                guild_data["current"] = None
                guild_data["position"] = 0
                guild_data["start_time"] = None
                guild_data["last_activity"] = datetime.now()
                return

            max_skip_attempts = 10
            skip_count = 0

            while skip_count < max_skip_attempts:
                next_song = await queue_service.get_next_song(guild_id)

                if not next_song:
                    await self._handle_empty_queue(guild_id)
                    return

                stream_success = await self._extract_and_play_song(guild_id, next_song, skip_count)

                if stream_success:
                    break

                skip_count += 1

            if skip_count >= max_skip_attempts:
                await self._handle_max_retries_exceeded(guild_id)

    async def _extract_and_play_song(self, guild_id: int, song: Song, skip_count: int) -> bool:
        guild_data = self.bot.get_guild_data(guild_id)
        max_retries = 3

        for attempt in range(max_retries):
            try:
                logger.info(f"Extracting fresh stream URL for: {song.title} (attempt {attempt + 1})")

                fresh_data = await self.bot.get_song_info(song.webpage_url)

                if not fresh_data or not fresh_data.get("url"):
                    raise Exception(f"No stream URL available for {song.title}")

                try:
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                        async with session.head(fresh_data["url"]) as response:
                            if response.status not in [200, 206]:
                                raise Exception(f"Stream URL returned status {response.status}")
                except Exception as e:
                    logger.warning(f"URL validation failed: {e}")

                song.url = fresh_data["url"]
                if fresh_data.get("title"):
                    song.title = fresh_data["title"]
                if fresh_data.get("duration"):
                    song.duration = fresh_data["duration"]
                if fresh_data.get("thumbnail"):
                    song.thumbnail = fresh_data["thumbnail"]
                if fresh_data.get("uploader"):
                    song.uploader = fresh_data["uploader"]

                return await self._start_playback(guild_id, song)

            except Exception as e:
                logger.error(f"Error extracting stream URL (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                continue

        await self._handle_song_skip(guild_id, song)
        return False

    async def _start_playback(self, guild_id: int, song: Song) -> bool:
        from services.queue_service import QueueService
        queue_service = QueueService(self.bot)

        guild_data = self.bot.get_guild_data(guild_id)

        try:
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(song.url, **self.bot.ffmpeg_options),
                volume=guild_data["volume"] / 100,
            )

            def after_playing(error):
                try:
                    if error:
                        logger.error(f"Player error: {error}")

                        if "Connection" in str(error) or "1006" in str(error):
                            logger.warning(f"Connection error detected in guild {guild_id}")
                    else:
                        if guild_data["current"] and not guild_data.get("seeking", False):
                            queue_service.add_to_history(guild_id, guild_data["current"])

                    if not guild_data.get("seeking", False):
                        coro = self.play_next(guild_id)
                        fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)

                        def handle_future_result(future):
                            try:
                                future.result()
                            except Exception as e:
                                logger.error(f"Error in play_next callback: {e}")

                        fut.add_done_callback(handle_future_result)

                except Exception as e:
                    logger.error(f"Error in after_playing callback: {e}")

            guild_data["current"] = song
            guild_data["seek_offset"] = 0
            guild_data["position"] = 0
            guild_data["start_time"] = datetime.now()
            guild_data["last_activity"] = datetime.now()

            guild_data["voice_client"].play(source, after=after_playing)

            await asyncio.sleep(0.2)

            music_cog = self.bot.get_cog("MusicCommands")
            if music_cog:
                await music_cog.update_now_playing(guild_id)

            await self.bot.save_guild_queue(guild_id)

            logger.info(f"Now playing: {song.title} in guild {guild_id}")

            return True

        except Exception as e:
            logger.error(f"Error creating audio source for {song.title}: {e}")
            await self._handle_song_skip(guild_id, song)
            return False

    async def _handle_empty_queue(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        guild_data["current"] = None
        guild_data["position"] = 0
        guild_data["start_time"] = None
        guild_data["last_activity"] = datetime.now()

        if guild_data.get("now_playing_message"):
            try:
                await guild_data["now_playing_message"].edit(
                    embed=create_embed("Queue Empty", "Add songs with `/play`", COLOR, self.bot.user)
                )
                await guild_data["now_playing_message"].clear_reactions()
            except:
                pass
            guild_data["now_playing_message"] = None

        await self.bot.save_guild_queue(guild_id)

    async def _handle_song_skip(self, guild_id: int, song: Song):
        from services.queue_service import QueueService
        queue_service = QueueService(self.bot)

        guild_data = self.bot.get_guild_data(guild_id)

        if guild_data["loop_mode"] != "song":
            queue_service.add_to_history(guild_id, song)

        try:
            music_cog = self.bot.get_cog("MusicCommands")
            if music_cog:
                channel = await music_cog.get_music_channel(guild_id)
                if channel:
                    skip_embed = create_embed(
                        "Song Skipped",
                        f"**{song.title}** was skipped (stream unavailable)",
                        COLOR,
                        self.bot.user
                    )
                    await channel.send(embed=skip_embed, delete_after=10)
        except:
            pass

        if guild_data["loop_mode"] == "song":
            guild_data["loop_mode"] = "off"
            logger.info(f"Disabled song loop mode due to stream failure for {song.title}")

    async def _handle_max_retries_exceeded(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        logger.error(f"Exhausted retry attempts for guild {guild_id}, stopping playback")
        guild_data["current"] = None
        guild_data["start_time"] = None
        await self.bot.save_guild_queue(guild_id)

        try:
            music_cog = self.bot.get_cog("MusicCommands")
            if music_cog:
                channel = await music_cog.get_music_channel(guild_id)
                if channel:
                    error_embed = create_embed(
                        "Playback Stopped",
                        "Too many consecutive song failures. Please check your queue and try again.",
                        COLOR,
                        self.bot.user
                    )
                    await channel.send(embed=error_embed)
        except:
            pass
