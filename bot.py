from discord.ext import commands, tasks
import asyncio
import json
import os
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict
import sqlite3
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import concurrent.futures
import discord

from config import get_intents
from models.song import Song

logger = logging.getLogger(__name__)


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=get_intents(), help_command=None)

        self.message_update_locks = {}
        self.message_validation_cache = {}
        self.last_update_times = {}
        self.loop = None

        self.init_database()
        self.guilds_data = {}
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)

        self.song_cache = {}
        self.max_cache_size = 500
        self.cache_ttl = 3600

        self.db_save_tasks = {}

        self.ytdl_format_options = {
            "format": "bestaudio[ext=m4a]/bestaudio[abr<=128]/bestaudio/best",
            "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
            "restrictfilenames": True,
            "noplaylist": False,
            "extract_flat": True,
            "nocheckcertificate": True,
            "ignoreerrors": True,
            "logtostderr": False,
            "quiet": True,
            "no_warnings": True,
            "default_search": "auto",
            "source_address": "0.0.0.0",
            "age_limit": 18,
            "retries": 15,
            "fragment_retries": 15,
            "skip_unavailable_fragments": True,
            "keep_fragments": False,
            "concurrent_fragment_downloads": 1,
            "extractor_retries": 10,
            "file_access_retries": 10,
            "socket_timeout": 60,
            "http_chunk_size": 10485760,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            },
            "geo_bypass": True,
            "prefer_free_formats": True,
            "playliststart": 1,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "web", "mweb"],
                    "player_skip": ["webpage"],
                }
            },
        }

        self.ffmpeg_options = {
            "before_options": (
                "-reconnect 1 "
                "-reconnect_streamed 1 "
                "-reconnect_delay_max 5 "
                "-reconnect_on_network_error 1 "
                "-reconnect_on_http_error 5xx "
                "-nostdin "
                "-user_agent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0'"
            ),
            "options": (
                "-vn "
                "-bufsize 512k "
                "-probesize 10M "
                "-analyzeduration 10M "
                "-fflags +discardcorrupt "
                "-flags +low_delay"
            ),
        }

        self.voice_reconnect_enabled = True
        self.voice_reconnect_delay = 2

        self.ytdl = yt_dlp.YoutubeDL(self.ytdl_format_options)

        try:
            spotify_client_id = os.getenv("SPOTIFY_CLIENT_ID")
            spotify_client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")

            if spotify_client_id and spotify_client_secret:
                self.spotify = spotipy.Spotify(
                    client_credentials_manager=SpotifyClientCredentials(
                        client_id=spotify_client_id, client_secret=spotify_client_secret
                    )
                )
                logger.info("Spotify integration enabled")
            else:
                self.spotify = None
                logger.info("Spotify credentials not found. Spotify features disabled.")
        except Exception as e:
            self.spotify = None
            logger.warning(f"Spotify setup failed: {e}")

    @staticmethod
    def init_database():
        try:
            conn = sqlite3.connect("music_bot.db", check_same_thread=False)
            cursor = conn.cursor()

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS playlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    songs TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    auto_disconnect INTEGER DEFAULT 300,
                    default_volume INTEGER DEFAULT 100,
                    music_channel_id INTEGER,
                    queue_data TEXT
                )
            """
            )

            conn.commit()
            conn.close()
            logger.info("Database initialized successfully")

        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
            raise

    @staticmethod
    def get_db_connection():
        return sqlite3.connect("music_bot.db", check_same_thread=False)

    async def execute_db_query(self, query: str, params: tuple = None):
        def _execute():
            with self.get_db_connection() as conn:
                cursor = conn.cursor()
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                conn.commit()
                return cursor.fetchall()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _execute)

    async def fetch_db_query(self, query: str, params: tuple = None):
        def _fetch():
            with self.get_db_connection() as conn:
                cursor = conn.cursor()
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                return cursor.fetchall()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _fetch)

    def get_guild_data(self, guild_id: int) -> Dict:
        if guild_id not in self.guilds_data:
            self.guilds_data[guild_id] = {
                "guild_id": guild_id,
                "queue": [],
                "loop_backup": [],
                "history": [],
                "history_position": 0,
                "current": None,
                "position": 0,
                "seek_offset": 0,
                "loop_mode": "off",
                "shuffle": False,
                "volume": 100,
                "voice_client": None,
                "intentional_disconnect": False,
                "last_activity": datetime.now(),
                "now_playing_message": None,
                "music_channel_id": None,
                "start_time": None,
                "message_ready_for_timestamps": False,
                "message_last_validated": 0,
                "seeking": False,
                "pause_position": None,
                "play_lock": asyncio.Lock(),
            }
        return self.guilds_data[guild_id]

    async def save_guild_music_channel(self, guild_id: int, channel_id: int):
        try:
            await self.execute_db_query(
                """
                INSERT OR REPLACE INTO guild_settings (guild_id, music_channel_id)
                VALUES (?, ?)
            """,
                (guild_id, channel_id),
            )
        except Exception as e:
            logger.error(f"Failed to save music channel: {e}")

    async def setup_hook(self):
        pass

    async def on_ready(self):
        logger.info(f"{self.user} has connected to Discord!")
        logger.info(f"Connected to {len(self.guilds)} guilds")

        self.loop = asyncio.get_running_loop()

        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} slash command(s)")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")

        self.cleanup_inactive.start()
        self.cleanup_cache.start()
        self.cleanup_inactive_guilds.start()
        self.update_now_playing_timestamps.start()
        self.cleanup_validation_cache.start()
        self.check_voice_health.start()
        await self.load_persistent_queues()

    async def on_voice_state_update(self, member, before, after):
        if member.id != self.user.id:
            return

        guild_id = before.channel.guild.id if before.channel else after.channel.guild.id
        guild_data = self.get_guild_data(guild_id)

        if not before.channel and after.channel:
            logger.info(f"Bot reconnected to voice in guild {guild_id} (auto-reconnect)")
            guild = self.get_guild(guild_id)
            if guild and guild.voice_client:
                guild_data["voice_client"] = guild.voice_client

                await asyncio.sleep(1)
                await self._resume_playback_after_reconnect(guild_id)
            return

        if before.channel and not after.channel:
            logger.warning(f"Bot disconnected from voice in guild {guild_id}")

            had_current_song = guild_data.get("current") is not None
            had_queue = len(guild_data.get("queue", [])) > 0
            voice_channel = before.channel

            if guild_data["voice_client"]:
                guild_data["voice_client"] = None

            if guild_data.get("intentional_disconnect", False):
                logger.info(f"Intentional disconnect for guild {guild_id}, skipping reconnect")
                guild_data["intentional_disconnect"] = False
                guild_data["current"] = None
                guild_data["start_time"] = None
                guild_data["history_position"] = len(guild_data["history"])
                guild_data["queue"].clear()
                guild_data["loop_backup"].clear()
                await self.clear_guild_queue_from_db(guild_id)
                return

            if self.voice_reconnect_enabled and (had_current_song or had_queue):
                logger.info(f"Attempting voice reconnection for guild {guild_id}...")
                asyncio.create_task(self._attempt_voice_reconnect(guild_id, voice_channel))
            else:
                guild_data["current"] = None
                guild_data["start_time"] = None
                guild_data["history_position"] = len(guild_data["history"])
                guild_data["queue"].clear()
                guild_data["loop_backup"].clear()
                await self.clear_guild_queue_from_db(guild_id)

    async def _attempt_voice_reconnect(self, guild_id: int, voice_channel):
        guild_data = self.get_guild_data(guild_id)

        try:
            await asyncio.sleep(self.voice_reconnect_delay)

            guild = self.get_guild(guild_id)
            if guild and guild.voice_client and guild.voice_client.is_connected():
                logger.info(f"Discord auto-reconnected to guild {guild_id}")
                guild_data["voice_client"] = guild.voice_client

                await self._resume_playback_after_reconnect(guild_id)
                return

            if guild_data["voice_client"] and guild_data["voice_client"].is_connected():
                logger.info(f"Already reconnected to guild {guild_id}")
                await self._resume_playback_after_reconnect(guild_id)
                return

            logger.info(f"Reconnecting to voice channel in guild {guild_id}...")
            voice_client = await voice_channel.connect(timeout=10.0, reconnect=True)
            guild_data["voice_client"] = voice_client

            logger.info(f"Successfully reconnected to voice in guild {guild_id}")

            await self._resume_playback_after_reconnect(guild_id)

        except discord.ClientException as e:
            if "already connected" in str(e).lower():
                logger.info(f"Voice already connected for guild {guild_id}, using existing connection")

                guild = self.get_guild(guild_id)
                if guild and guild.voice_client:
                    guild_data["voice_client"] = guild.voice_client
                    await self._resume_playback_after_reconnect(guild_id)
            else:
                logger.error(f"Client exception during reconnect for guild {guild_id}: {e}")
                await self._cleanup_after_failed_reconnect(guild_id)
        except asyncio.TimeoutError:
            logger.error(f"Voice reconnection timeout for guild {guild_id}")
            await self._cleanup_after_failed_reconnect(guild_id)
        except Exception as e:
            logger.error(f"Failed to reconnect voice for guild {guild_id}: {e}")
            await self._cleanup_after_failed_reconnect(guild_id)

    async def _resume_playback_after_reconnect(self, guild_id: int):
        guild_data = self.get_guild_data(guild_id)

        try:
            from services.playback_service import PlaybackService
            playback_service = PlaybackService(self)

            if guild_data.get("current"):
                current_song = guild_data["current"]
                logger.info(f"Resuming playback of: {current_song.title}")

                guild_data["current"] = None

                guild_data["queue"].insert(0, current_song)

                await playback_service.play_next(guild_id)
            elif guild_data.get("queue"):
                logger.info(f"Starting queue playback after reconnect")
                await playback_service.play_next(guild_id)
        except Exception as e:
            logger.error(f"Error resuming playback after reconnect: {e}")

    async def _cleanup_after_failed_reconnect(self, guild_id: int):
        guild_data = self.get_guild_data(guild_id)

        logger.info(f"Cleaning up after failed reconnect for guild {guild_id}")

        guild_data["current"] = None
        guild_data["start_time"] = None
        guild_data["voice_client"] = None

        try:
            from config import COLOR
            from utils.helpers import create_embed
            music_cog = self.get_cog("MusicCommands")
            if music_cog and guild_data.get("music_channel_id"):
                channel = self.get_channel(guild_data["music_channel_id"])
                if channel:
                    embed = create_embed(
                        "Voice Connection Lost",
                        "The bot was disconnected from voice and couldn't reconnect. Use `/play` to start again.",
                        COLOR,
                        self.user
                    )
                    await channel.send(embed=embed, delete_after=30)
        except Exception as e:
            logger.error(f"Error sending disconnect notification: {e}")

    @tasks.loop(minutes=5)
    async def cleanup_inactive(self):
        try:
            for guild_id, data in list(self.guilds_data.items()):
                if data["voice_client"] and data["voice_client"].is_connected():
                    inactive_time = datetime.now() - data["last_activity"]

                    is_truly_inactive = (
                            not data["voice_client"].is_playing()
                            and not data["voice_client"].is_paused()
                            and not data.get("current")
                    )

                    if inactive_time > timedelta(minutes=5) and is_truly_inactive:
                        await data["voice_client"].disconnect()
                        data["voice_client"] = None
                        logger.info(f"Disconnected from inactive guild: {guild_id}")
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")

    @tasks.loop(minutes=10)
    async def cleanup_cache(self):
        try:
            current_time = asyncio.get_event_loop().time()
            expired_keys = [
                key
                for key, value in self.song_cache.items()
                if current_time - value["cached_at"] > self.cache_ttl
            ]

            for key in expired_keys:
                del self.song_cache[key]

            if expired_keys:
                logger.info(f"Cleaned {len(expired_keys)} expired cache entries")
        except Exception as e:
            logger.error(f"Cache cleanup error: {e}")

    @tasks.loop(hours=1)
    async def cleanup_inactive_guilds(self):
        try:
            current_guild_ids = {guild.id for guild in self.guilds}
            inactive_guilds = []

            for guild_id in list(self.guilds_data.keys()):
                if guild_id not in current_guild_ids:
                    inactive_guilds.append(guild_id)
                    del self.guilds_data[guild_id]

            if inactive_guilds:
                logger.info(
                    f"Cleaned up data for {len(inactive_guilds)} inactive guilds"
                )
        except Exception as e:
            logger.error(f"Guild cleanup error: {e}")

    @tasks.loop(seconds=1)
    async def update_now_playing_timestamps(self):
        from services.playback_service import PlaybackService
        playback_service = PlaybackService(self)
        await playback_service.update_timestamps_task()

    @tasks.loop(minutes=5)
    async def cleanup_validation_cache(self):
        try:
            current_time = asyncio.get_event_loop().time()
            expired_keys = [
                key
                for key, value in self.message_validation_cache.items()
                if current_time - value["time"] > 60.0
            ]

            for key in expired_keys:
                del self.message_validation_cache[key]

            if expired_keys:
                logger.debug(f"Cleaned {len(expired_keys)} validation cache entries")
        except Exception as e:
            logger.error(f"Validation cache cleanup error: {e}")

    @tasks.loop(seconds=30)
    async def check_voice_health(self):
        try:
            for guild_id, guild_data in list(self.guilds_data.items()):
                voice_client = guild_data.get("voice_client")

                if not voice_client:
                    continue

                if voice_client.is_connected():

                    has_current = guild_data.get("current") is not None
                    is_playing = voice_client.is_playing()
                    is_paused = voice_client.is_paused()

                    if has_current and not is_playing and not is_paused:
                        logger.warning(f"Detected stalled playback in guild {guild_id}")

                        from services.playback_service import PlaybackService
                        playback_service = PlaybackService(self)

                        guild_data["current"] = None
                        await playback_service.play_next(guild_id)
                else:
                    if guild_data.get("current") or guild_data.get("queue"):
                        logger.warning(f"Voice client disconnected but has active state in guild {guild_id}")
                        guild_data["voice_client"] = None

        except Exception as e:
            logger.error(f"Voice health check error: {e}")

    async def load_persistent_queues(self):
        try:
            results = await self.fetch_db_query(
                "SELECT guild_id, queue_data, music_channel_id FROM guild_settings WHERE queue_data IS NOT NULL OR music_channel_id IS NOT NULL"
            )

            for guild_id, queue_data, music_channel_id in results:
                guild_data = self.get_guild_data(guild_id)

                if music_channel_id:
                    guild_data["music_channel_id"] = music_channel_id

                if queue_data:
                    try:
                        data = json.loads(queue_data)
                        guild_data["queue"] = [
                            Song.from_dict(song_data)
                            for song_data in data.get("queue", [])
                        ]
                        guild_data["loop_backup"] = [
                            Song.from_dict(song_data)
                            for song_data in data.get("loop_backup", [])
                        ]

                        history_data = data.get("history", [])

                        guild_data["history"] = [
                            Song.from_dict(song_data) for song_data in history_data
                        ]
                        guild_data["history_position"] = data.get(
                            "history_position", len(guild_data["history"])
                        )

                        guild_data["loop_mode"] = data.get("loop_mode", "off")
                        guild_data["shuffle"] = data.get("shuffle", False)
                        guild_data["volume"] = data.get("volume", 100)
                    except json.JSONDecodeError:
                        continue

            logger.info("Persistent data loaded")
        except Exception as e:
            logger.error(f"Failed to load persistent data: {e}")

    async def save_guild_queue(self, guild_id: int):
        if guild_id in self.db_save_tasks:
            self.db_save_tasks[guild_id].cancel()

        self.db_save_tasks[guild_id] = asyncio.create_task(
            self._delayed_save_guild_queue(guild_id)
        )

    async def _delayed_save_guild_queue(self, guild_id: int):
        await asyncio.sleep(1)

        try:
            guild_data = self.get_guild_data(guild_id)

            queue_data = {
                "queue": [song.to_dict() for song in guild_data["queue"]],
                "loop_backup": [song.to_dict() for song in guild_data["loop_backup"]],
                "history": [song.to_dict() for song in guild_data["history"]],
                "history_position": guild_data.get(
                    "history_position", len(guild_data["history"])
                ),
                "loop_mode": guild_data["loop_mode"],
                "shuffle": guild_data["shuffle"],
                "volume": guild_data["volume"],
            }

            await self.execute_db_query(
                """
                INSERT OR REPLACE INTO guild_settings (guild_id, queue_data, music_channel_id)
                VALUES (?, ?, ?)
            """,
                (guild_id, json.dumps(queue_data), guild_data.get("music_channel_id")),
            )

        except Exception as e:
            logger.error(f"Failed to save guild queue: {e}")
        finally:
            if guild_id in self.db_save_tasks:
                del self.db_save_tasks[guild_id]

    async def clear_guild_queue_from_db(self, guild_id: int):
        try:
            guild_data = self.get_guild_data(guild_id)
            
            queue_data = {
                "queue": [],
                "loop_backup": [],
                "history": [song.to_dict() for song in guild_data.get("history", [])],
                "history_position": guild_data.get(
                    "history_position", len(guild_data.get("history", []))
                ),
                "loop_mode": "off",
                "shuffle": False,
                "volume": guild_data.get("volume", 100),
            }
            
            await self.execute_db_query(
                """
                INSERT OR REPLACE INTO guild_settings (guild_id, queue_data, music_channel_id)
                VALUES (?, ?, ?)
            """,
                (guild_id, json.dumps(queue_data), guild_data.get("music_channel_id")),
            )
            logger.info(f"Cleared queue data from database for guild {guild_id} (history preserved)")
        except Exception as e:
            logger.error(f"Failed to clear guild queue from database: {e}")

    async def get_song_info_cached(self, url_or_query: str) -> Optional[Dict]:
        from services.music_service import MusicService
        music_service = MusicService(self)
        return await music_service.get_song_info_cached(url_or_query)

    async def get_song_info(self, url_or_query: str) -> Optional[Dict]:
        from services.music_service import MusicService
        music_service = MusicService(self)
        return await music_service.get_song_info(url_or_query)

    async def close(self):
        logger.info("Shutting down bot...")

        logger.info("Saving all queues before shutdown...")
        for guild_id in list(self.guilds_data.keys()):
            try:
                guild_data = self.get_guild_data(guild_id)
                
                if guild_data.get("queue") or guild_data.get("current") or guild_data.get("loop_backup"):
                    queue_data = {
                        "queue": [song.to_dict() for song in guild_data["queue"]],
                        "loop_backup": [song.to_dict() for song in guild_data["loop_backup"]],
                        "history": [song.to_dict() for song in guild_data["history"]],
                        "history_position": guild_data.get(
                            "history_position", len(guild_data["history"])
                        ),
                        "loop_mode": guild_data["loop_mode"],
                        "shuffle": guild_data["shuffle"],
                        "volume": guild_data["volume"],
                    }
                    await self.execute_db_query(
                        """
                        INSERT OR REPLACE INTO guild_settings (guild_id, queue_data, music_channel_id)
                        VALUES (?, ?, ?)
                    """,
                        (guild_id, json.dumps(queue_data), guild_data.get("music_channel_id")),
                    )
            except Exception as e:
                logger.error(f"Failed to save queue for guild {guild_id} on shutdown: {e}")

        for task in self.db_save_tasks.values():
            task.cancel()

        if self.db_save_tasks:
            await asyncio.gather(*self.db_save_tasks.values(), return_exceptions=True)
            logger.info("All database save tasks completed")

        for guild_data in self.guilds_data.values():
            if guild_data["voice_client"]:
                try:
                    await guild_data["voice_client"].disconnect()
                except Exception as e:
                    logger.debug(f"Error disconnecting voice client: {e}")

        self.executor.shutdown(wait=True)

        await super().close()

    def load_guild_music_channel(self, guild_id: int):
        async def _load():
            try:
                result = await self.fetch_db_query(
                    "SELECT music_channel_id FROM guild_settings WHERE guild_id = ?",
                    (guild_id,),
                )
                if result and result[0][0]:
                    guild_data = self.get_guild_data(guild_id)
                    guild_data["music_channel_id"] = result[0][0]
            except Exception as e:
                logger.error(f"Failed to load music channel for guild {guild_id}: {e}")

        asyncio.create_task(_load())
