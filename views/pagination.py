import discord
from typing import List


class PaginationView(discord.ui.View):
    def __init__(self, pages: List[discord.Embed], user: discord.User, timeout: int = 240):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.user = user
        self.current_page = 0

        if len(pages) <= 1:
            self.previous_button.disabled = True
            self.next_button.disabled = True
        else:
            self.previous_button.disabled = True
            self.next_button.disabled = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.user:
            await interaction.response.send_message(
                "You can't use these buttons!",
                ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.primary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1

            self.previous_button.disabled = self.current_page == 0
            self.next_button.disabled = False

            await interaction.response.edit_message(
                embed=self.pages[self.current_page],
                view=self
            )
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1

            self.next_button.disabled = self.current_page == len(self.pages) - 1
            self.previous_button.disabled = False

            await interaction.response.edit_message(
                embed=self.pages[self.current_page],
                view=self
            )
        else:
            await interaction.response.defer()
