# Flux AI Telegram Bot

## Overview
Feature-rich Telegram AI chatbot with web admin panel. Built on Python/Flask with gunicorn.

## Architecture
- **main.py** — core bot logic, Flask API, Telegram webhook handler
- **templates/admin.html** — full single-page admin panel (Liquid Glass design)
- **users.json** — persistent storage: users, message_count, banned

## Key Features
- AI chat via OpenRouter (stepfun/step-3.5-flash:free)
- Vision/image description (google/gemini-2.0-flash-exp:free)
- Voice/video note transcription (Whisper via OpenRouter)
- Text-to-speech with Russian male voice (edge-tts, ru-RU-DmitryNeural)
- Image generation (placeholder)
- File analysis
- Status commands (/start, /help, /mode, /voice)

## Admin Panel (/admin)
- **Liquid Glass design**: backdrop-filter blur, rgba backgrounds, inset highlights
- **SSE real-time updates**: `/admin/api/stream?token=...`
- **Chat viewer**: all user messages, admin can reply, delete, clear
- **Moderation**: Mute (bot reads but ignores) / Ban (bot fully ignores)
- **Muted/Banned tabs** on desktop, modal sheets on mobile
- **Stats bar**: users, messages, muted count, banned count
- **Mobile-first**: sliding panels, bottom nav (Чаты/Mute/Ban/Бот), hamburger menu
- **Broadcast**: send message to all users
- **Bot control**: start/stop bot from panel

## Secrets
- `BOT_TOKEN` — Telegram bot token
- `OPENROUTER_API_KEY` — OpenRouter API key
- `ADMIN_PASSWORD` — admin panel password

## Config
- Admin username: sergey_defa
- Token: SHA256 of `flux_{ADMIN_PASSWORD}_secret`
- Gunicorn: 1 worker, 16 threads, --timeout 0
- Keep-alive: self-ping every 270s

## API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | /admin/api/status | Bot on/off |
| GET | /admin/api/stats | User/message counts |
| GET | /admin/api/chats | All users list |
| GET | /admin/api/chat/:id | Chat messages |
| DELETE | /admin/api/chat/:id/message/:idx | Delete message |
| DELETE | /admin/api/chat/:id/clear | Clear history |
| POST | /admin/api/chat/:id/mute | Mute user |
| POST | /admin/api/chat/:id/unmute | Unmute user |
| POST | /admin/api/chat/:id/ban | Ban user |
| POST | /admin/api/chat/:id/unban | Unban user |
| POST | /admin/api/send | Send message to user |
| POST | /admin/api/broadcast | Broadcast to all |
| POST | /admin/api/bot/stop | Stop bot |
| POST | /admin/api/bot/start | Start bot |
| GET | /admin/api/stream | SSE event stream |
