# Flux AI Telegram Bot

## Overview
Feature-rich Telegram AI chatbot with web admin panel. Built on Python/Flask with gunicorn.

## Architecture
- **main.py** — core bot logic, Flask API, Telegram webhook handler
- **templates/admin.html** — full single-page admin panel (Liquid Glass design)
- **templates/reset.html** — password reset page (sent via email link)
- **users.json** — persistent storage: users, message_count, banned
- **chat_log.json** — full chat history per user (int chat_id → list of messages)
- **admin_accounts.json** — persistent admin accounts (login → password), auto-created on first run
- **admin_emails.json** — admin account emails for password recovery

## Key Features
- AI chat via OpenRouter (stepfun/step-3.5-flash:free)
- Vision/image description (google/gemini-2.0-flash-exp:free)
- Voice/video note transcription (Whisper via OpenRouter)
- Text-to-speech with Russian male voice (edge-tts, ru-RU-DmitryNeural)
- File analysis
- Status commands (/start, /help, /mode, /voice)

## Admin Panel (/admin)
- **Liquid Glass design**: backdrop-filter blur, rgba backgrounds, inset highlights
- **SSE real-time updates**: `/admin/api/stream?token=...`
- **Auth**: 2 hardcoded accounts (`sergey_defa`/`Ser123asd`, `Blackjack`/`Sergey`); SHA256 tokens per account
- **2FA**: per-account two-factor auth via email or SMS (SMS.ru); 🔐 button in header/menu; 6-digit code, 5 min TTL
- **Change password**: 🔑 button in header / mobile menu; rotates token immediately
- **Email recovery**: attach email per account; forgot link on login → reset email → /admin/reset?token=...
- **Email prompt**: shown after login if no email linked (can dismiss with "Позже")
- **Chat viewer**: all user messages, admin can reply, delete, clear
- **User profiles**: avatar, stats, ban/unban + open chat buttons
- **Stats bar**: users, messages, banned count
- **Mobile-first**: sliding panels, bottom nav (Чаты/Пользователи/Бот), hamburger menu
- **Broadcast**: send message to all users
- **Bot control**: start/stop bot from panel

## Secrets
- `BOT_TOKEN` — Telegram bot token
- `OPENROUTER_API_KEY` — OpenRouter API key
- `SMTP_USER` — Gmail address for sending recovery emails (optional)
- `SMTP_PASSWORD` — Gmail App Password for SMTP (optional)

## Auth Config
- Accounts: `{"sergey_defa":"Ser123asd","Blackjack":"Sergey"}` (hardcoded in ADMIN_ACCOUNTS)
- Token: `SHA256("flux_admin_{username}_{password}_token")` per account
- `ADMIN_TOKENS` dict maps token → username (updated on password change)
- `ADMIN_EMAILS` dict maps username → email, persisted to admin_emails.json
- `RESET_TOKENS` dict maps token → {username, expires} (in-memory, 30 min TTL)
- `ADMIN_2FA` dict maps username → {enabled, method, phone}, persisted to admin_2fa.json
- `FA_SESSIONS` dict maps fa_token → {username, code, expires} (in-memory, 5 min TTL)

## SMTP Config
- Provider: Gmail via smtp.gmail.com:587 + STARTTLS
- Env vars: `SMTP_USER`, `SMTP_PASSWORD` (Gmail App Password)
- If not configured, forgot-password shows "SMTP не настроен" error

## Config
- Gunicorn: 1 worker, 16 threads, --timeout 0
- Keep-alive: self-ping every 270s (verify=False)

## API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | /admin | Admin panel HTML |
| GET | /admin/reset | Password reset page (token param) |
| POST | /admin/login | Authenticate → returns token + username |
| POST | /admin/forgot | Send reset email by email address |
| POST | /admin/reset | Submit new password via reset token |
| POST | /admin/api/change-password | Change password (requires token header) |
| POST | /admin/api/set-email | Attach email to account |
| GET | /admin/api/has-email | Check if current user has email set |
| GET | /admin/api/status | Bot on/off |
| GET | /admin/api/stats | User/message counts |
| GET | /admin/api/chats | All users list |
| GET | /admin/api/chat/:id | Chat messages |
| DELETE | /admin/api/chat/:id/message/:idx | Delete message |
| DELETE | /admin/api/chat/:id/clear | Clear history |
| POST | /admin/api/chat/:id/ban | Ban user |
| POST | /admin/api/chat/:id/unban | Unban user |
| POST | /admin/api/send | Send message to user |
| POST | /admin/api/broadcast | Broadcast to all |
| POST | /admin/api/bot/stop | Stop bot |
| POST | /admin/api/bot/start | Start bot |
| GET | /admin/api/stream | SSE event stream (token query param) |
