import asyncio
import logging
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)


class MusicService:
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def _normalize_youtube_entry(entry: Dict) -> Optional[Dict]:
        if not entry:
            return None

        normalized = dict(entry)

        title = normalized.get("title") or normalized.get("alt_title")
        if not title:
            return None
        normalized["title"] = title

        webpage_url = normalized.get("webpage_url")
        url = normalized.get("url")
        video_id = normalized.get("id")

        if not webpage_url:
            if isinstance(url, str) and url.startswith("http"):
                webpage_url = url
            elif video_id:
                webpage_url = f"https://www.youtube.com/watch?v={video_id}"

        if not webpage_url:
            return None

        normalized["webpage_url"] = webpage_url

        if "url" not in normalized or not normalized["url"]:
            normalized["url"] = webpage_url

        return normalized

    async def get_song_info_cached(self, url_or_query: str) -> Optional[Dict]:
        cache_key = url_or_query.lower().strip()

        if cache_key in self.bot.song_cache:
            cached_data = self.bot.song_cache[cache_key]
            current_time = asyncio.get_event_loop().time()
            if current_time - cached_data["cached_at"] < self.bot.cache_ttl:
                logger.debug(f"Using cached data for: {url_or_query[:50]}")
                return cached_data["data"]

        data = await self.get_song_info(url_or_query)

        if data:
            current_time = asyncio.get_event_loop().time()
            self.bot.song_cache[cache_key] = {"data": data, "cached_at": current_time}
            await self._cleanup_cache_if_needed()

        return data

    async def _cleanup_cache_if_needed(self):
        if len(self.bot.song_cache) > self.bot.max_cache_size:
            sorted_items = sorted(
                self.bot.song_cache.items(), key=lambda x: x[1]["cached_at"]
            )

            for key, _ in sorted_items[:100]:
                del self.bot.song_cache[key]

            logger.debug(f"Cleaned cache, now has {len(self.bot.song_cache)} entries")

    async def get_song_info(self, url_or_query: str) -> Optional[Dict]:
        try:
            loop = asyncio.get_event_loop()

            if any(
                    platform in url_or_query.lower()
                    for platform in [
                        "youtube.com",
                        "youtu.be",
                        "soundcloud.com",
                        "spotify.com",
                    ]
            ):
                if "spotify.com" in url_or_query and self.bot.spotify:
                    return await self.handle_spotify_url(url_or_query)
                else:
                    for attempt in range(2):
                        try:
                            data = await loop.run_in_executor(
                                self.bot.executor,
                                lambda: self.bot.ytdl.extract_info(
                                    url_or_query, download=False
                                ),
                            )
                            if data:
                                return data
                        except Exception as e:
                            logger.warning(f"Attempt {attempt + 1} failed: {e}")
                            if attempt < 1:
                                await asyncio.sleep(1)
            else:
                data = await self.search_youtube(url_or_query)

            return data
        except Exception as e:
            logger.error(f"Error getting song info: {e}")
        return None

    async def search_youtube(self, query: str) -> Optional[Dict]:
        try:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(
                self.bot.executor,
                lambda: self.bot.ytdl.extract_info(f"ytsearch:{query}", download=False),
            )

            if data and "entries" in data and data["entries"]:
                for raw_entry in data["entries"]:
                    normalized = self._normalize_youtube_entry(raw_entry)
                    if normalized:
                        return normalized
        except Exception as e:
            logger.error(f"YouTube search error: {e}")
        return None

    async def handle_spotify_url(self, url: str) -> Optional[Dict]:
        if not self.bot.spotify:
            return None

        try:
            if "track/" in url:
                track_id = url.split("track/")[-1].split("?")[0]
                track = self.bot.spotify.track(track_id)

                track_name = track.get("name", "")
                artists = track.get("artists", [])
                if not artists or len(artists) == 0:
                    logger.warning(f"Spotify track has no artists: {track_name}")
                    search_query = track_name
                else:
                    artist_name = artists[0].get("name", "")
                    search_query = f"{track_name} {artist_name}" if artist_name else track_name

                return await self.search_youtube(search_query)
            elif "playlist/" in url:
                playlist_id = url.split("playlist/")[-1].split("?")[0]
                results = self.bot.spotify.playlist_tracks(playlist_id)
                songs = []

                items = results.get("items", [])
                for item in items[:25]:
                    track = item.get("track") if item else None
                    if not track or not track.get("name"):
                        continue

                    track_name = track.get("name", "")
                    artists = track.get("artists", [])
                    if not artists or len(artists) == 0:
                        logger.warning(f"Spotify track has no artists: {track_name}")
                        search_query = track_name
                    else:
                        artist_name = artists[0].get("name", "")
                        search_query = f"{track_name} {artist_name}" if artist_name else track_name

                    song_data = await self.search_youtube(search_query)
                    if song_data:
                        songs.append(song_data)
                    await asyncio.sleep(0.1)
                return songs if songs else None
            elif "album/" in url:
                album_id = url.split("album/")[-1].split("?")[0]
                results = self.bot.spotify.album_tracks(album_id)
                songs = []

                items = results.get("items", [])
                for track in items[:25]:
                    if not track or not track.get("name"):
                        continue

                    track_name = track.get("name", "")
                    artists = track.get("artists", [])
                    if not artists or len(artists) == 0:
                        logger.warning(f"Spotify track has no artists: {track_name}")
                        search_query = track_name
                    else:
                        artist_name = artists[0].get("name", "")
                        search_query = f"{track_name} {artist_name}" if artist_name else track_name

                    song_data = await self.search_youtube(search_query)
                    if song_data:
                        songs.append(song_data)
                    await asyncio.sleep(0.1)
                return songs if songs else None
        except Exception as e:
            logger.error(f"Spotify error: {e}")
        return None

    async def handle_youtube_playlist_optimized(self, url: str) -> List[Dict]:
        try:
            loop = asyncio.get_event_loop()

            playlist_info = await loop.run_in_executor(
                self.bot.executor,
                lambda: self.bot.ytdl.extract_info(url, download=False, process=False),
            )

            if not playlist_info or "entries" not in playlist_info:
                logger.error("No playlist entries found")
                return []

            entries = list(playlist_info["entries"])[:]
            if not entries:
                logger.error("Playlist entries list is empty")
                return []

            songs = []

            for i, entry in enumerate(entries):
                if entry and entry.get("id"):
                    song_data = {
                        "url": None,
                        "title": entry.get("title", f"Song {i + 1}"),
                        "duration": entry.get("duration", 0),
                        "thumbnail": entry.get("thumbnail", ""),
                        "uploader": entry.get("uploader", "Unknown"),
                        "webpage_url": f"https://www.youtube.com/watch?v={entry['id']}",
                        "requested_by": "Unknown",
                    }
                    songs.append(song_data)

            logger.info(f"Playlist metadata extracted: {len(songs)} songs")
            return songs

        except Exception as e:
            logger.error(f"Playlist handling error: {e}")
            return []
