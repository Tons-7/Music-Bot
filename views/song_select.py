import discord
import logging
from config import COLOR
from utils.helpers import create_embed

logger = logging.getLogger(__name__)


class SongSelectView(discord.ui.View):
    def __init__(self, songs_data, user, music_commands_cog):
        super().__init__(timeout=30)
        self.songs_data = songs_data
        self.user = user
        self.music_commands_cog = music_commands_cog
        self.selected = False
        self.message = None

        self.add_item(SongSelect(songs_data, self))

    async def on_timeout(self):
        if not self.selected:
            for item in self.children:
                item.disabled = True

        try:
            embed = create_embed(
                "⏰ Search Timed Out",
                "Search selection timed out.",
                COLOR,
            )
            await self.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.user:
            await interaction.response.send_message(
                "This search menu is not for you!", ephemeral=True
            )
            return False
        return True


class SongSelect(discord.ui.Select):
    def __init__(self, songs_data, view_parent):
        self.songs_data = songs_data
        self.view_parent = view_parent

        options = []
        for i, entry in enumerate(songs_data[:5]):
            title = entry.get("title", "Unknown Title")
            uploader = entry.get("uploader", "Unknown")
            duration = entry.get("duration", 0)

            if duration:
                minutes, seconds = divmod(int(duration), 60)
                duration_str = f"{minutes}:{seconds:02d}"
            else:
                duration_str = "0:00"

            if len(title) > 60:
                title = title[:57] + "..."

            description = f"by {uploader} • {duration_str}"
            if len(description) > 100:
                description = description[:97] + "..."

            options.append(
                discord.SelectOption(
                    label=title, description=description, value=str(i), emoji="🎵"
                )
            )

        super().__init__(
            placeholder="Choose a song to add to queue...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.view_parent.selected:
            await interaction.response.send_message(
                "A song has already been selected!", ephemeral=True
            )
            return

        self.view_parent.selected = True
        self.disabled = True

        self.view_parent.stop()

        try:
            selected_index = int(self.values[0])
            selected_song = self.songs_data[selected_index]

            loading_embed = create_embed(
                "Adding Song...",
                f"Adding **{selected_song['title']}** to queue...",
                COLOR,
            )
            await interaction.response.edit_message(embed=loading_embed, view=self.view)

            await self.view_parent.music_commands_cog.process_selected_song(
                interaction, selected_song
            )

        except Exception as e:
            logger.error(f"Error in song selection: {e}")
            error_embed = create_embed(
                "❌ Error",
                "Failed to add song to queue.",
                COLOR,
            )
            try:
                if not interaction.response.is_done():
                    await interaction.response.edit_message(
                        embed=error_embed, view=self.view
                    )
                else:
                    await interaction.edit_original_response(
                        embed=error_embed, view=self.view
                    )
            except discord.HTTPException:
                pass
