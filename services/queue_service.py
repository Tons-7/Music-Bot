import logging
import random
from typing import List, Optional
from models.song import Song
from config import MAX_HISTORY_SIZE

logger = logging.getLogger(__name__)


class QueueService:
    def __init__(self, bot):
        self.bot = bot

    def sync_loop_backup(self, guild_id: int, force_rebuild: bool = False):
        guild_data = self.bot.get_guild_data(guild_id)

        if force_rebuild:
            seen_urls = set()
            deduplicated = []
            for song in guild_data["loop_backup"]:
                if song.webpage_url not in seen_urls:
                    deduplicated.append(song)
                    seen_urls.add(song.webpage_url)
            guild_data["loop_backup"] = deduplicated
            logger.info(
                f"Deduplicated loop backup to {len(guild_data['loop_backup'])} songs"
            )

    def get_visible_queue(self, guild_id: int) -> List[Song]:
        guild_data = self.bot.get_guild_data(guild_id)
        visible_songs = []

        visible_songs.extend(guild_data["queue"])

        if (
                guild_data["loop_mode"] == "queue"
                and guild_data["loop_backup"]
        ):
            queue_urls = {song.webpage_url for song in guild_data["queue"]}

            for song in guild_data["loop_backup"]:
                if song.webpage_url not in queue_urls:
                    visible_songs.append(song)

        return visible_songs[:]

    def add_to_history(self, guild_id: int, song: Song):
        guild_data = self.bot.get_guild_data(guild_id)

        if any(
                s.webpage_url == song.webpage_url for s in guild_data["history"]
        ):
            return

        history_song = Song.from_dict(song.to_dict())
        guild_data["history"].append(history_song)

        guild_data["history_position"] = len(guild_data["history"])

        if len(guild_data["history"]) > MAX_HISTORY_SIZE:
            guild_data["history"] = guild_data["history"][-MAX_HISTORY_SIZE:]

            guild_data["history_position"] = min(
                guild_data.get(
                    "history_position",
                    len(guild_data["history"])
                ),
                len(guild_data["history"]),
            )

        existing_urls = {s.webpage_url for s in guild_data["loop_backup"]}
        if song.webpage_url not in existing_urls:
            guild_data["loop_backup"].append(Song.from_dict(song.to_dict()))
            logger.info(f"Added finished song to loop backup: {song.title}")

    async def get_next_song(self, guild_id: int) -> Optional[Song]:
        guild_data = self.bot.get_guild_data(guild_id)

        if guild_data["loop_mode"] == "song" and guild_data["current"]:
            return Song.from_dict(guild_data["current"].to_dict())

        if guild_data["queue"]:
            return guild_data["queue"].pop(0)

        if guild_data["loop_mode"] == "queue" and guild_data["loop_backup"]:
            logger.info(
                f"Queue empty, restoring from loop backup ({len(guild_data['loop_backup'])} songs)"
            )

            guild_data["queue"] = [
                Song.from_dict(song.to_dict())
                for song in guild_data["loop_backup"]
            ]

            if guild_data["shuffle"]:
                random.shuffle(guild_data["queue"])

            if guild_data["queue"]:
                return guild_data["queue"].pop(0)

        return None

    def clear_queue(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        guild_data["queue"].clear()
        guild_data["loop_backup"].clear()

    def remove_song_from_queue(self, guild_id: int, position: int) -> Optional[Song]:
        guild_data = self.bot.get_guild_data(guild_id)

        if position < 0 or position >= len(guild_data["queue"]):
            return None

        return guild_data["queue"].pop(position)

    def move_song_in_queue(self, guild_id: int, from_pos: int, to_pos: int) -> bool:
        guild_data = self.bot.get_guild_data(guild_id)

        if (from_pos < 0 or from_pos >= len(guild_data["queue"]) or
                to_pos < 0 or to_pos >= len(guild_data["queue"])):
            return False

        song = guild_data["queue"].pop(from_pos)
        guild_data["queue"].insert(to_pos, song)
        return True

    def shuffle_queue(self, guild_id: int):
        guild_data = self.bot.get_guild_data(guild_id)
        if guild_data["queue"]:
            random.shuffle(guild_data["queue"])
        if guild_data["loop_backup"]:
            random.shuffle(guild_data["loop_backup"])

    def toggle_shuffle(self, guild_id: int) -> bool:
        guild_data = self.bot.get_guild_data(guild_id)
        guild_data["shuffle"] = not guild_data["shuffle"]

        if guild_data["shuffle"]:
            self.shuffle_queue(guild_id)

        return guild_data["shuffle"]

    def set_loop_mode(self, guild_id: int, mode: str):
        guild_data = self.bot.get_guild_data(guild_id)
        guild_data["loop_mode"] = mode

        if mode == "queue":
            self.sync_loop_backup(guild_id)

    def add_song_to_queue(self, guild_id: int, song: Song):
        guild_data = self.bot.get_guild_data(guild_id)
        guild_data["queue"].append(song)
        guild_data["loop_backup"].append(Song.from_dict(song.to_dict()))
