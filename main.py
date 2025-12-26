import os
import asyncio
import logging
from dotenv import load_dotenv

from bot import MusicBot
from cogs.music_commands import MusicCommands
from cogs.playlist_commands import PlaylistCommands

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        print("❌ ERROR: BOT_TOKEN environment variable not found")
        print("Set your bot token as an environment variable in the .env file")
        return

    logger.info("Starting Discord Music Bot...")

    bot = MusicBot()

    try:
        await bot.add_cog(MusicCommands(bot))
        await bot.add_cog(PlaylistCommands(bot))
        logger.info("Commands loaded successfully")

        async with bot:
            await bot.start(bot_token)

    except KeyboardInterrupt:
        logger.info("Bot shutting down via keyboard interrupt...")
    except Exception as e:
        logger.error(f"Bot error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
