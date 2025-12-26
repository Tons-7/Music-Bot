import discord
from discord.ext import commands
import json
import logging
from typing import List
from datetime import datetime

from models.song import Song

from services.queue_service import QueueService
from services.playback_service import PlaybackService
from services.music_service import MusicService

from utils.helpers import get_existing_urls, interaction_check, create_embed

from config import COLOR, MAX_PLAYLIST_SIZE, SONGS_PER_PAGE

from views.pagination import PaginationView

logger = logging.getLogger(__name__)


class PlaylistCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queue_service = QueueService(bot)
        super().__init__()

    async def _get_music_cog(self, interaction: discord.Interaction):
        music_cog = self.bot.get_cog("MusicCommands")
        if not music_cog:
            embed = create_embed("Error", "Music commands not loaded", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return None
        return music_cog

    async def _get_playlist_songs(
            self,
            interaction: discord.Interaction,
            name: str,
            *,
            use_followup: bool,
            ephemeral_not_found: bool = True,
    ) -> List[dict] | None:

        send = interaction.followup.send if use_followup else interaction.response.send_message

        result = await self.bot.fetch_db_query(
            """
            SELECT songs
            FROM playlists
            WHERE user_id = ?
              AND guild_id = ?
              AND name = ?
            """,
            (interaction.user.id, interaction.guild.id, name),
        )

        if not result or len(result) == 0 or len(result[0]) == 0:
            embed = create_embed(
                "Error", f"Playlist **{name}** not found", COLOR, self.bot.user
            )
            await send(embed=embed, ephemeral=ephemeral_not_found)
            return None

        try:
            songs_json = result[0][0]
            return json.loads(songs_json) if songs_json else []
        except (json.JSONDecodeError, IndexError, KeyError) as e:
            logger.error(f"Error parsing playlist data: {e}")
            embed = create_embed("Error", "Playlist data is corrupted", COLOR, self.bot.user)
            await send(embed=embed)
            return None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await interaction_check(self, interaction)

    playlist_group = discord.app_commands.Group(
        name="playlist", description="Manage your playlists"
    )

    history_group = discord.app_commands.Group(
        name="history", description="Manage song history"
    )

    async def queue_autocomplete(
            self, interaction: discord.Interaction, current: str
    ) -> List[discord.app_commands.Choice]:
        guild_data = self.bot.get_guild_data(interaction.guild.id)
        choices = []

        if guild_data.get("current"):
            current_song = guild_data["current"]
            choice_name = f"Now Playing: {current_song.title[:60]}"
            if len(choice_name) > 80:
                choice_name = choice_name[:77] + "..."
            choices.append(
                discord.app_commands.Choice(name=choice_name, value="current")
            )

        for i, song in enumerate(guild_data.get("queue", [])[:20]):
            choice_name = f"Queue #{i + 1}: {song.title[:60]}"
            if len(choice_name) > 80:
                choice_name = choice_name[:77] + "..."
            choices.append(
                discord.app_commands.Choice(name=choice_name, value=f"queue_{i}")
            )

        if current:
            choices = [
                choice for choice in choices if current.lower() in choice.name.lower()
            ]

        return choices[:25]

    @playlist_group.command(name="create", description="Create a new empty playlist")
    @discord.app_commands.describe(name="Name for the playlist")
    async def playlist_create(self, interaction: discord.Interaction, name: str):
        if len(name) > 50:
            embed = create_embed(
                "Error", "Playlist name must be 50 characters or less.", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        try:
            existing = await self.bot.fetch_db_query(
                """
                SELECT id
                FROM playlists
                WHERE user_id = ?
                  AND guild_id = ?
                  AND name = ?
                """,
                (interaction.user.id, interaction.guild.id, name),
            )

            if existing:
                embed = create_embed(
                    "Error", f"Playlist **{name}** already exists", COLOR, self.bot.user
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            await self.bot.execute_db_query(
                """
                INSERT INTO playlists (user_id, guild_id, name, songs)
                VALUES (?, ?, ?, ?)
                """,
                (interaction.user.id, interaction.guild.id, name, json.dumps([])),
            )

            embed = create_embed(
                "Playlist Created", f"Created empty playlist **{name}**", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed)

        except Exception as e:
            logger.error(f"Playlist create error: {e}")
            embed = create_embed("Error", "Failed to create playlist.", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)

    @playlist_group.command(name="add", description="Add a song or playlist to a playlist")
    @discord.app_commands.describe(
        name="Playlist name",
        song="Song URL, playlist URL, or search term",
    )
    async def playlist_add(
            self,
            interaction: discord.Interaction,
            name: str,
            song: str,
    ):
        await interaction.response.defer()

        try:
            existing_songs = await self._get_playlist_songs(
                interaction, name, use_followup=True, ephemeral_not_found=True
            )
            if existing_songs is None:
                return

            is_youtube_playlist = "playlist" in song.lower() and "youtube.com" in song.lower()
            is_spotify_playlist = "playlist" in song.lower() and "spotify.com" in song.lower()
            is_spotify_album = "album" in song.lower() and "spotify.com" in song.lower()

            if is_youtube_playlist:
                music_service = MusicService(self.bot)
                youtube_songs = await music_service.handle_youtube_playlist_optimized(song)

                if not youtube_songs:
                    embed = create_embed(
                        "Error", "Could not process playlist!", COLOR, self.bot.user
                    )
                    await interaction.followup.send(embed=embed)
                    return

                existing_urls = {s.get("webpage_url") for s in existing_songs}
                songs_to_add = []
                added_count = 0
                skipped_count = 0

                for song_info in youtube_songs:
                    song_url = song_info.get("webpage_url")
                    if not song_url:
                        continue

                    if song_url not in existing_urls:
                        if len(existing_songs) + len(songs_to_add) >= MAX_PLAYLIST_SIZE:
                            break
                        songs_to_add.append(song_info)
                        existing_urls.add(song_url)
                        added_count += 1
                    else:
                        skipped_count += 1

                if not songs_to_add:
                    embed = create_embed(
                        "No Songs Added",
                        f"All songs from the playlist are already in **{name}**" + (
                            f"\n({skipped_count} songs skipped)" if skipped_count > 0 else ""
                        ),
                        COLOR,
                        self.bot.user
                    )
                    await interaction.followup.send(embed=embed)
                    return

                for song_info in songs_to_add:
                    new_song = Song(song_info)
                    new_song.requested_by = interaction.user.mention
                    existing_songs.append(new_song.to_dict())

                songs_json = json.dumps(existing_songs)
                await self.bot.execute_db_query(
                    """
                    UPDATE playlists
                    SET songs = ?
                    WHERE user_id = ?
                      AND guild_id = ?
                      AND name = ?
                    """,
                    (songs_json, interaction.user.id, interaction.guild.id, name),
                )

                embed = create_embed(
                    "Playlist Added",
                    f"Added **{added_count}** song{'s' if added_count != 1 else ''} from the playlist to **{name}**" + (
                        f"\n({skipped_count} duplicate{'s' if skipped_count != 1 else ''} skipped)" if skipped_count > 0 else ""
                    ),
                    COLOR,
                    self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            elif is_spotify_playlist or is_spotify_album:
                spotify_songs = await self.bot.get_song_info_cached(song)

                if not spotify_songs or not isinstance(spotify_songs, list):
                    embed = create_embed(
                        "Error", "Could not process Spotify playlist/album!", COLOR, self.bot.user
                    )
                    await interaction.followup.send(embed=embed)
                    return

                existing_urls = {s.get("webpage_url") for s in existing_songs}
                songs_to_add = []
                added_count = 0
                skipped_count = 0

                for song_info in spotify_songs:
                    song_url = song_info.get("webpage_url")
                    if not song_url or not song_info.get("title"):
                        continue

                    if song_url not in existing_urls:
                        if len(existing_songs) + len(songs_to_add) >= MAX_PLAYLIST_SIZE:
                            break
                        songs_to_add.append(song_info)
                        existing_urls.add(song_url)
                        added_count += 1
                    else:
                        skipped_count += 1

                if not songs_to_add:
                    embed = create_embed(
                        "No Songs Added",
                        f"All songs from the Spotify {'playlist' if is_spotify_playlist else 'album'} are already in **{name}**" + (
                            f"\n({skipped_count} songs skipped)" if skipped_count > 0 else ""
                        ),
                        COLOR,
                        self.bot.user
                    )
                    await interaction.followup.send(embed=embed)
                    return

                for song_info in songs_to_add:
                    new_song = Song(song_info)
                    new_song.requested_by = interaction.user.mention
                    existing_songs.append(new_song.to_dict())

                songs_json = json.dumps(existing_songs)
                await self.bot.execute_db_query(
                    """
                    UPDATE playlists
                    SET songs = ?
                    WHERE user_id = ?
                      AND guild_id = ?
                      AND name = ?
                    """,
                    (songs_json, interaction.user.id, interaction.guild.id, name),
                )

                embed = create_embed(
                    "Spotify Playlist/Album Added",
                    f"Added **{added_count}** song{'s' if added_count != 1 else ''} from the Spotify {'playlist' if is_spotify_playlist else 'album'} to **{name}**" + (
                        f"\n({skipped_count} duplicate{'s' if skipped_count != 1 else ''} skipped)" if skipped_count > 0 else ""
                    ),
                    COLOR,
                    self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            song_info = await self.bot.get_song_info_cached(song)
            if not song_info or not song_info.get("webpage_url"):
                embed = create_embed(
                    "Error", "Could not find that song", COLOR, self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            song_url = song_info["webpage_url"]
            if any(s.get("webpage_url") == song_url for s in existing_songs):
                embed = create_embed(
                    "Error", "Song is already in the playlist", COLOR, self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            if len(existing_songs) >= MAX_PLAYLIST_SIZE:
                embed = create_embed(
                    "Error", f"Playlist is full! Maximum {MAX_PLAYLIST_SIZE} songs allowed.", COLOR, self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            new_song = Song(song_info)
            new_song.requested_by = interaction.user.mention
            existing_songs.append(new_song.to_dict())

            songs_json = json.dumps(existing_songs)
            await self.bot.execute_db_query(
                """
                UPDATE playlists
                SET songs = ?
                WHERE user_id = ?
                  AND guild_id = ?
                  AND name = ?
                """,
                (songs_json, interaction.user.id, interaction.guild.id, name),
            )

            embed = create_embed(
                "Song Added",
                f"Added **{new_song.title}** to playlist **{name}**",
                COLOR,
                self.bot.user
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Playlist add error: {e}")
            embed = create_embed("Error", "Failed to add to playlist.", COLOR, self.bot.user)
            await interaction.followup.send(embed=embed)

    @playlist_group.command(name="add-from-queue", description="Add a song from the current queue to a playlist")
    @discord.app_commands.describe(
        name="Playlist name",
        from_queue="Song from current session to add",
    )
    @discord.app_commands.autocomplete(from_queue=queue_autocomplete)
    async def playlist_add_from_queue(
            self,
            interaction: discord.Interaction,
            name: str,
            from_queue: str,
    ):
        await interaction.response.defer()

        try:
            existing_songs = await self._get_playlist_songs(
                interaction, name, use_followup=True, ephemeral_not_found=True
            )
            if existing_songs is None:
                return

            guild_data = self.bot.get_guild_data(interaction.guild.id)
            target_song = None

            if from_queue == "current" and guild_data.get("current"):
                target_song = guild_data["current"]
            elif from_queue.startswith("queue_"):
                try:
                    queue_index = int(from_queue.split("_")[1])
                    if 0 <= queue_index < len(guild_data.get("queue", [])):
                        target_song = guild_data["queue"][queue_index]
                except (ValueError, IndexError):
                    pass

            if not target_song:
                embed = create_embed(
                    "Error", "Selected song not found in current session", COLOR, self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            if any(
                    s.get("webpage_url") == target_song.webpage_url
                    for s in existing_songs
            ):
                embed = create_embed(
                    "Error", "Song is already in the playlist", COLOR, self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            if len(existing_songs) >= MAX_PLAYLIST_SIZE:
                embed = create_embed(
                    "Error", f"Playlist is full! Maximum {MAX_PLAYLIST_SIZE} songs allowed.", COLOR, self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            song_copy = Song.from_dict(target_song.to_dict())
            song_copy.requested_by = interaction.user.mention
            existing_songs.append(song_copy.to_dict())

            songs_json = json.dumps(existing_songs)
            await self.bot.execute_db_query(
                """
                UPDATE playlists
                SET songs = ?
                WHERE user_id = ?
                  AND guild_id = ?
                  AND name = ?
                """,
                (songs_json, interaction.user.id, interaction.guild.id, name),
            )

            embed = create_embed(
                "Song Added",
                f"Added **{target_song.title}** to playlist **{name}**",
                COLOR,
                self.bot.user
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Playlist add from queue error: {e}")
            embed = create_embed("Error", "Failed to add to playlist.", COLOR, self.bot.user)
            await interaction.followup.send(embed=embed)

    @playlist_group.command(name="add-all-queue", description="Add entire current queue to a playlist")
    @discord.app_commands.describe(
        name="Playlist name",
    )
    async def playlist_add_session(
            self,
            interaction: discord.Interaction,
            name: str,
    ):
        await interaction.response.defer()

        try:
            existing_songs = await self._get_playlist_songs(
                interaction, name, use_followup=True, ephemeral_not_found=True
            )
            if existing_songs is None:
                return

            guild_data = self.bot.get_guild_data(interaction.guild.id)
            queue_items = []
            current_dict = None
            seen_urls = set()

            if guild_data.get("current"):
                current_song = guild_data["current"]
                current_dict = current_song.to_dict()
                seen_urls.add(current_song.webpage_url)

            for queue_song in guild_data.get("queue", []):
                if queue_song.webpage_url not in seen_urls:
                    queue_items.append(queue_song.to_dict())
                    seen_urls.add(queue_song.webpage_url)

            if not current_dict and not queue_items:
                embed = create_embed(
                    "Error", "No songs in current session", COLOR, self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            existing_urls = {s.get("webpage_url") for s in existing_songs}
            songs_to_add = []

            if current_dict:
                current_url = current_dict.get("webpage_url")
                if current_url not in existing_urls:
                    songs_to_add.append(current_dict)
                    existing_urls.add(current_url)

            for song_info in queue_items:
                song_url = song_info.get("webpage_url")
                if song_url and song_url not in existing_urls:
                    songs_to_add.append(song_info)
                    existing_urls.add(song_url)

            if not songs_to_add:
                embed = create_embed(
                    "Error",
                    "All songs from current session are already in the playlist",
                    COLOR,
                    self.bot.user
                )
                await interaction.followup.send(embed=embed)
                return

            if len(existing_songs) + len(songs_to_add) > MAX_PLAYLIST_SIZE:
                max_can_add = MAX_PLAYLIST_SIZE - len(existing_songs)
                songs_to_add = songs_to_add[:max_can_add]
                embed = create_embed(
                    "Partial Add",
                    f"Added {len(songs_to_add)} songs to playlist **{name}**\n(Playlist size limit reached)",
                    COLOR,
                    self.bot.user
                )
            else:
                embed = create_embed(
                    "Session Added",
                    f"Added {len(songs_to_add)} songs from current session to playlist **{name}**",
                    COLOR,
                    self.bot.user
                )

            for song_info in songs_to_add:
                song_info["requested_by"] = interaction.user.mention

            existing_songs.extend(songs_to_add)

            songs_json = json.dumps(existing_songs)
            await self.bot.execute_db_query(
                """
                UPDATE playlists
                SET songs = ?
                WHERE user_id = ?
                  AND guild_id = ?
                  AND name = ?
                """,
                (songs_json, interaction.user.id, interaction.guild.id, name),
            )

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Playlist add session error: {e}")
            embed = create_embed("Error", "Failed to add session to playlist.", COLOR, self.bot.user)
            await interaction.followup.send(embed=embed)

    @playlist_group.command(name="remove", description="Remove a song from a playlist")
    @discord.app_commands.describe(
        name="Playlist name", position="Position of song to remove (1-based)"
    )
    async def playlist_remove(
            self, interaction: discord.Interaction, name: str, position: int
    ):
        try:
            playlist_items = await self._get_playlist_songs(
                interaction, name, use_followup=False, ephemeral_not_found=True
            )
            if playlist_items is None:
                return

            if not playlist_items:
                embed = create_embed("Error", "Playlist is empty", COLOR, self.bot.user)
                await interaction.response.send_message(embed=embed)
                return

            if position < 1 or position > len(playlist_items):
                embed = create_embed(
                    "Error",
                    f"Invalid position! Playlist has {len(playlist_items)} songs.",
                    COLOR,
                    self.bot.user
                )
                await interaction.response.send_message(embed=embed)
                return

            removed_song = playlist_items.pop(position - 1)

            songs_json = json.dumps(playlist_items)
            await self.bot.execute_db_query(
                """
                UPDATE playlists
                SET songs = ?
                WHERE user_id = ?
                  AND guild_id = ?
                  AND name = ?
                """,
                (songs_json, interaction.user.id, interaction.guild.id, name),
            )

            embed = create_embed(
                "Song Removed",
                f"Removed **{removed_song.get('title', 'Unknown')}** from playlist **{name}**",
                COLOR,
                self.bot.user
            )
            await interaction.response.send_message(embed=embed)

        except Exception as e:
            logger.error(f"Playlist remove error: {e}")
            embed = create_embed(
                "Error", "Failed to remove song from playlist.", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed)

    @playlist_group.command(name="move", description="Move a song to a different position in a playlist")
    @discord.app_commands.describe(
        name="Playlist name",
        from_pos="Current position of the song (1-based)",
        to_pos="New position for the song (1-based)",
    )
    async def playlist_move(
            self,
            interaction: discord.Interaction,
            name: str,
            from_pos: int,
            to_pos: int,
    ):
        try:
            playlist_items = await self._get_playlist_songs(
                interaction, name, use_followup=False, ephemeral_not_found=True
            )
            if playlist_items is None:
                return

            if not playlist_items:
                embed = create_embed("Error", "Playlist is empty", COLOR, self.bot.user)
                await interaction.response.send_message(embed=embed)
                return

            length = len(playlist_items)
            if (
                    from_pos < 1
                    or from_pos > length
                    or to_pos < 1
                    or to_pos > length
            ):
                embed = create_embed(
                    "Error",
                    f"Invalid position! Playlist has {length} songs.",
                    COLOR,
                    self.bot.user
                )
                await interaction.response.send_message(embed=embed)
                return

            from_index = from_pos - 1
            to_index = to_pos - 1

            song = playlist_items.pop(from_index)
            playlist_items.insert(to_index, song)

            songs_json = json.dumps(playlist_items)
            await self.bot.execute_db_query(
                """
                UPDATE playlists
                SET songs = ?
                WHERE user_id = ?
                  AND guild_id = ?
                  AND name = ?
                """,
                (songs_json, interaction.user.id, interaction.guild.id, name),
            )

            title = song.get("title") or "Unknown"
            embed = create_embed(
                "Song Moved",
                f"Moved **{title}**\nFrom position {from_pos} to position {to_pos} in playlist **{name}**",
                COLOR,
                self.bot.user
            )
            await interaction.response.send_message(embed=embed)

        except Exception as e:
            logger.error(f"Playlist move error: {e}")
            embed = create_embed(
                "Error", "Failed to move song in playlist.", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed)

    @playlist_group.command(name="load", description="Load a playlist into the queue")
    @discord.app_commands.describe(name="Playlist name")
    async def playlist_load(self, interaction: discord.Interaction, name: str):
        music_cog = await self._get_music_cog(interaction)
        if not music_cog:
            return

        if not await music_cog.check_voice_channel(interaction, allow_auto_join=True):
            return

        try:
            playlist_items = await self._get_playlist_songs(
                interaction, name, use_followup=False, ephemeral_not_found=True
            )
            if playlist_items is None:
                return

            if not playlist_items:
                embed = create_embed(
                    "Error", f"Playlist **{name}** is empty", COLOR, self.bot.user
                )
                await interaction.response.send_message(embed=embed)
                return

            guild_data = self.bot.get_guild_data(interaction.guild.id)

            if not guild_data.get("music_channel_id"):
                guild_data["music_channel_id"] = interaction.channel.id
                await self.bot.execute_db_query(
                    "INSERT OR REPLACE INTO guild_settings (guild_id, music_channel_id) VALUES (?, ?)",
                    (interaction.guild.id, interaction.channel.id)
                )

            if not await music_cog.ensure_voice_connection(interaction):
                embed = create_embed(
                    "Error", "Failed to connect to voice channel", COLOR, self.bot.user
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            loaded_count = 0
            seen_urls = set()

            for song_info in playlist_items:
                try:
                    if not song_info.get("webpage_url") or not song_info.get("title"):
                        continue

                    if song_info["webpage_url"] in seen_urls:
                        continue

                    song = Song.from_dict(song_info)
                    song.requested_by = interaction.user.mention

                    self.queue_service.add_song_to_queue(interaction.guild.id, song)
                    seen_urls.add(song_info["webpage_url"])
                    loaded_count += 1

                except Exception as e:
                    logger.warning(f"Skipped invalid song data: {e}")
                    continue

            if loaded_count == 0:
                embed = create_embed(
                    "Error", "No valid songs found in playlist", COLOR, self.bot.user
                )
                await interaction.response.send_message(embed=embed)
                return

            embed = create_embed(
                "Playlist Loaded",
                f"Loaded **{name}** with {loaded_count} songs",
                COLOR,
                self.bot.user
            )
            await interaction.response.send_message(embed=embed)

            playback_service = PlaybackService(self.bot)

            if (
                    not guild_data["voice_client"].is_playing()
                    and not guild_data["current"]
            ):
                await playback_service.play_next(interaction.guild.id)

            guild_data["last_activity"] = datetime.now()
            await self.bot.save_guild_queue(interaction.guild.id)

        except Exception as e:
            logger.error(f"Playlist load error: {e}")
            embed = create_embed("Error", "Failed to load playlist.", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)

    @playlist_group.command(name="show", description="Show songs in a playlist")
    @discord.app_commands.describe(name="Playlist name", page="Page number to view (optional)")
    async def playlist_show(self, interaction: discord.Interaction, name: str, page: int = 1):
        try:
            playlist_items = await self._get_playlist_songs(
                interaction, name, use_followup=False, ephemeral_not_found=True
            )
            if playlist_items is None:
                return

            if not playlist_items:
                embed = create_embed(
                    f"Playlist: {name}", "Playlist is empty", COLOR, self.bot.user
                )
                await interaction.response.send_message(embed=embed)
                return

            total_pages = max(
                1, (len(playlist_items) + SONGS_PER_PAGE - 1) // SONGS_PER_PAGE
            )

            page = max(1, min(page, total_pages))

            pages = []
            for page_num in range(total_pages):
                start_idx = page_num * SONGS_PER_PAGE
                end_idx = start_idx + SONGS_PER_PAGE

                description = ""
                for i, song_info in enumerate(playlist_items[start_idx:end_idx], start_idx + 1):
                    title = song_info.get("title", "Unknown Title")
                    uploader = song_info.get("uploader", "Unknown")
                    description += f"`{i}.` **{title}** by {uploader}\n"

                embed = create_embed(
                    f"Playlist: {name} - Page {page_num + 1}/{total_pages}",
                    description[:4000],
                    COLOR,
                    self.bot.user
                )
                embed.add_field(name="Total Songs", value=str(len(playlist_items)), inline=True)

                pages.append(embed)

            view = PaginationView(pages, interaction.user)
            view.current_page = page - 1

            view.previous_button.disabled = view.current_page == 0
            view.next_button.disabled = view.current_page == len(pages) - 1

            await interaction.response.send_message(embed=pages[page - 1], view=view)

        except Exception as e:
            logger.error(f"Playlist show error: {e}")
            embed = create_embed("Error", "Failed to show playlist.", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)

    @playlist_group.command(name="list", description="List all your playlists")
    async def playlist_list(self, interaction: discord.Interaction):
        try:
            results = await self.bot.fetch_db_query(
                """
                SELECT name, songs, created_at
                FROM playlists
                WHERE user_id = ?
                  AND guild_id = ?
                ORDER BY created_at DESC
                """,
                (interaction.user.id, interaction.guild.id),
            )

            if not results:
                embed = create_embed(
                    "Your Playlists", "You don't have any saved playlists.", COLOR, self.bot.user
                )
            else:
                description = ""
                for playlist_name, songs_json, created_at in results:
                    try:
                        playlist_items = json.loads(songs_json) if songs_json else []
                        song_count = len(playlist_items)
                        description += f"• **{playlist_name}** ({song_count} songs) - {created_at[:10]}\n"
                    except json.JSONDecodeError:
                        description += f"• **{playlist_name}** (corrupted data) - {created_at[:10]}\n"

                embed = create_embed("Your Playlists", description, COLOR, self.bot.user)

            await interaction.response.send_message(embed=embed)

        except Exception as e:
            logger.error(f"Playlist list error: {e}")
            embed = create_embed("Error", "Failed to retrieve playlists.", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)

    @playlist_group.command(name="delete", description="Delete a playlist")
    @discord.app_commands.describe(name="Playlist name to delete")
    async def playlist_delete(self, interaction: discord.Interaction, name: str):
        try:
            existing = await self.bot.fetch_db_query(
                """
                SELECT id
                FROM playlists
                WHERE user_id = ?
                  AND guild_id = ?
                  AND name = ?
                """,
                (interaction.user.id, interaction.guild.id, name),
            )

            if not existing:
                embed = create_embed(
                    "Error", f"Playlist **{name}** not found", COLOR, self.bot.user
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            await self.bot.execute_db_query(
                """
                DELETE
                FROM playlists
                WHERE user_id = ?
                  AND guild_id = ?
                  AND name = ?
                """,
                (interaction.user.id, interaction.guild.id, name),
            )

            embed = create_embed(
                "Playlist Deleted", f"Deleted playlist **{name}**.", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed)

        except Exception as e:
            logger.error(f"Playlist delete error: {e}")
            embed = create_embed("Error", "Failed to delete playlist.", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)

    @history_group.command(name="show", description="Show recently played songs")
    @discord.app_commands.describe(page="Page number to view (optional)")
    async def history_show(self, interaction: discord.Interaction, page: int = 1):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["history"]:
            embed = create_embed("History", "No songs in history yet", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)
            return

        total_pages = max(
            1, (len(guild_data["history"]) + SONGS_PER_PAGE - 1) // SONGS_PER_PAGE
        )

        page = max(1, min(page, total_pages))

        pages = []
        for page_num in range(total_pages):
            start_idx = page_num * SONGS_PER_PAGE
            end_idx = start_idx + SONGS_PER_PAGE

            description = ""
            for i, song in enumerate(guild_data["history"][start_idx:end_idx], start_idx + 1):
                description += f"`{i}.` **{song.title}** by {song.uploader}\n"

            embed = create_embed(
                f"Recent History - Page {page_num + 1}/{total_pages}",
                description[:4000],
                COLOR,
                self.bot.user
            )
            embed.add_field(name="Total", value=str(len(guild_data["history"])), inline=True)
            embed.set_footer(
                text="Use /history play <number> to replay a song or /history add_all to add all songs"
            )

            pages.append(embed)

        view = PaginationView(pages, interaction.user)
        view.current_page = page - 1

        view.previous_button.disabled = view.current_page == 0
        view.next_button.disabled = view.current_page == len(pages) - 1

        await interaction.response.send_message(
            embed=pages[page - 1],
            view=view
        )

    @history_group.command(name="play", description="Play a song from history by number")
    @discord.app_commands.describe(song_number="Song number from history to play")
    async def history_play(self, interaction: discord.Interaction, song_number: int):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["history"]:
            embed = create_embed("History", "No songs in history yet", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)
            return

        music_cog = self.bot.get_cog("MusicCommands")
        if not music_cog:
            embed = create_embed("Error", "Music commands not loaded", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not await music_cog.check_voice_channel(interaction, allow_auto_join=True):
            return

        if song_number < 1 or song_number > len(guild_data["history"]):
            embed = create_embed(
                "Error",
                f"Invalid history position! History has {len(guild_data['history'])} songs.",
                COLOR,
                self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        selected_song = guild_data["history"][song_number - 1]
        queue_urls = {song.webpage_url for song in guild_data.get("queue", [])}

        if (
                guild_data.get("current")
                and selected_song.webpage_url == guild_data["current"].webpage_url
        ):
            embed = create_embed(
                "Error", "This song is currently playing", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        elif selected_song.webpage_url in queue_urls:
            for i, song in enumerate(guild_data.get("queue", []), 1):
                if song.webpage_url == selected_song.webpage_url:
                    embed = create_embed(
                        "Error",
                        f"This song is already in queue at position {i}",
                        COLOR,
                        self.bot.user
                    )
                    await interaction.response.send_message(
                        embed=embed, ephemeral=True
                    )
                    return

        if not await music_cog.ensure_voice_connection(interaction):
            embed = create_embed(
                "Error", "Failed to connect to voice channel", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        song_copy = Song.from_dict(selected_song.to_dict())
        song_copy.requested_by = interaction.user.mention
        self.queue_service.add_song_to_queue(interaction.guild.id, song_copy)

        playback_service = PlaybackService(self.bot)
        voice_client = guild_data.get("voice_client")

        if (
                voice_client
                and not voice_client.is_playing()
                and not guild_data.get("current")
        ):
            await playback_service.play_next(interaction.guild.id)
            embed = create_embed(
                "Now Playing from History", str(song_copy), COLOR, self.bot.user
            )
        else:
            position = len(guild_data.get("queue", []))
            embed = create_embed(
                "Added from History",
                f"{song_copy}\n\nPosition in queue: {position}",
                COLOR,
                self.bot.user
            )

        if song_copy.thumbnail:
            embed.set_thumbnail(url=song_copy.thumbnail)

        await interaction.response.send_message(embed=embed)
        guild_data["last_activity"] = datetime.now()
        await self.bot.save_guild_queue(interaction.guild.id)

    @history_group.command(
        name="add_all", description="Add all songs from history to the queue"
    )
    async def history_add_all(self, interaction: discord.Interaction):
        music_cog = await self._get_music_cog(interaction)
        if not music_cog:
            return

        if not await music_cog.check_voice_channel(interaction, allow_auto_join=True):
            return

        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["history"]:
            embed = create_embed("History", "No songs in history yet", COLOR, self.bot.user)
            await interaction.response.send_message(embed=embed)
            return

        if not await music_cog.ensure_voice_connection(interaction):
            embed = create_embed(
                "Error", "Failed to connect to voice channel", COLOR, self.bot.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.defer()

        existing_urls = get_existing_urls(guild_data)
        added_count = 0
        skipped_count = 0

        for history_song in guild_data["history"]:
            if history_song.webpage_url not in existing_urls:
                song_copy = Song.from_dict(history_song.to_dict())
                song_copy.requested_by = interaction.user.mention
                self.queue_service.add_song_to_queue(interaction.guild.id, song_copy)
                existing_urls.add(history_song.webpage_url)
                added_count += 1
            else:
                skipped_count += 1

        if added_count == 0:
            embed = create_embed(
                "No Songs Added",
                "All history songs are already in queue or currently playing",
                COLOR,
                self.bot.user
            )
        else:
            embed = create_embed(
                "History Added to Queue",
                f"Added {added_count} songs from history to queue"
                + (
                    f"\nSkipped {skipped_count} duplicates."
                    if skipped_count > 0
                    else ""
                ),
                COLOR,
                self.bot.user
            )

        await interaction.followup.send(embed=embed)

        playback_service = PlaybackService(self.bot)

        if not guild_data["voice_client"].is_playing() and not guild_data["current"]:
            await playback_service.play_next(interaction.guild.id)

        guild_data["last_activity"] = datetime.now()
        await self.bot.save_guild_queue(interaction.guild.id)

    @history_group.command(
        name="clear",
        description="Clear history songs"
    )
    async def history_clear(self, interaction: discord.Interaction):
        guild_data = self.bot.get_guild_data(interaction.guild.id)

        if not guild_data["history"]:
            embed = create_embed(
                "Error",
                "History already empty!",
                COLOR,
                self.bot.user
            )
            await interaction.response.send_message(embed=embed)
            return
        else:
            guild_data["history"].clear()
            guild_data["history_position"] = 0
            embed = create_embed(
                "History cleared",
                "Removed all songs from history",
                COLOR,
                self.bot.user
            )
            await interaction.response.send_message(embed=embed)

        await self.bot.save_guild_queue(interaction.guild.id)
