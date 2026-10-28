# AI Content Bot for Telegram & TikTok

[Русская версия](README.ru.md)

A private Telegram bot that helps a creator run a Telegram channel and short-video accounts:
it suggests content ideas, writes posts in the author's voice, publishes them to the channel after
a two-step confirmation, writes TikTok scripts and automatically edits the recorded video.

## Features

- **Channel posts:** 5 ideas → a draft in the author's voice → edits in plain words ("shorter", "add the price")
  → optional photo → publish to the channel only after **Publish** + **Yes** confirmation.
- **TikTok / Shorts / Reels:** 5 ideas → a shooting script (hook, shots, lines, call to action, filming tips)
  → send the recorded video → get back an edited vertical video plus a caption with hashtags.
- **Automatic video editing (ffmpeg + faster-whisper):**
  1. cuts pauses and hesitations;
  2. cleans the audio: removes hum, reduces noise, normalizes loudness to social-media level;
  3. converts to 1080×1920 and slightly boosts contrast and saturation;
  4. recognizes speech and burns in large 2–3-word subtitles, Reels style.
- **Pluggable AI:** Claude API, GigaChat, any OpenAI-compatible API (OpenRouter free models), or no AI at all —
  then the bot publishes your ready texts and still edits videos.
- **Author profile:** `profile.md` describes the author, audience and tone of voice; the AI reads it before every request.
- **Private:** only the owner can use the bot; other users are ignored. All drafts and ideas are stored in SQLite.

## Tech stack

Python 3.12 · aiogram 3 · SQLite · ffmpeg · faster-whisper · Anthropic / GigaChat / OpenAI SDKs · python-dotenv

## Quick start (Windows)

```bash
winget install ffmpeg
pip install -r requirements.txt
copy .env.example .env      # fill in BOT_TOKEN, OWNER_ID, CHANNEL_ID and one AI key (optional)
python bot.py               # or double-click run.bat
```

Add the bot to your channel as an admin with the "Post messages" permission, then send it `/start`.

## Project structure

| File | Responsibility |
|---|---|
| `bot.py` | menus, buttons, drafts, publishing to the channel, receiving videos |
| `llm.py` | AI providers: ideas, posts, scripts, captions; structured JSON output |
| `video.py` | ffmpeg pipeline: pause cutting, audio cleanup, 9:16, subtitles |
| `storage.py` | SQLite storage for ideas and drafts |
| `config.py` | settings from `.env` |
| `profile.md` | author profile used as the AI system prompt |

## Customization

The bot is easy to adapt for a business or a blog: change `profile.md` for a different author or brand,
add platforms, scheduling or several channels. Need a bot like this? Contact me on Upwork.
