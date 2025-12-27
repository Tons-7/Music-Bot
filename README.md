# Discord Music Bot

A **modular Discord music bot** written in Python using `discord.py`.  
This bot supports music playback, queue and playlist management, and more.

## Features

- Play music from supported sources, also with a normal search, instead of a link
- Queue, history, and playlist management
- Skip, pause, resume, and stop playback
- Modular command system

# Configuration

This bot uses **environment variables** for secrets:

1. Create a `.env` file in the root directory.
2. Add your Discord token:
  - BOT_TOKEN=your_token
3. For spotify, you will have to go to the website and get the spotify client id and secret, then put then in the .env like this:
  - SPOTIFY_CLIENT_ID=client_id
  - SPOTIFY_CLIENT_SECRET=client_secret
4. Make sure `.env` remains in `.gitignore` so it is **never committed**.

# How to run

Simply execute main.py

# License

This bot is only available for personal use
