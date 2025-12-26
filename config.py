import discord

COLOR = 0x00CCFF
SONGS_PER_PAGE = 15
MAX_PLAYLIST_SIZE = 250
MAX_HISTORY_SIZE = 30
MAX_CACHE_SIZE = 500
CACHE_TTL = 3600
INACTIVE_TIMEOUT_MINUTES = 5


def get_intents():
    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True
    intents.guilds = True
    intents.members = True
    return intents
