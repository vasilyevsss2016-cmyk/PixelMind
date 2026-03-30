"""
Flux — AI чат-бот для Telegram + веб-панель администратора.
"""

import os
import re
import io
import json
import uuid as uuid_module
import base64
import hashlib
import logging
import queue
import random
import secrets
import smtplib
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, request, jsonify, render_template, redirect, session, Response, stream_with_context
import requests as http_requests

import pyotp
import qrcode

try:
    import edge_tts
    import asyncio
    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False

try:
    import speech_recognition as sr
    from pydub import AudioSegment
    SPEECH_AVAILABLE = True
except ImportError:
    SPEECH_AVAILABLE = False

# ============ НАСТРОЙКИ ============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
REPLIT_URL = os.environ.get("REPLIT_DEV_DOMAIN", "")
BOT_NAME = "Flux"
AI_MODEL = "stepfun/step-3.5-flash:free"
VISION_MODEL = "google/gemini-2.0-flash-exp:free"
PORT = int(os.environ.get("PORT", 5000))

# Аккаунты админ-панели: логин → пароль
# Defaults — используются только если admin_accounts.json не существует
_DEFAULT_ADMIN_ACCOUNTS = {
    "sergey_defa": "Ser123asd",
    "Blackjack": "Sergey",
}
ADMIN_ACCOUNTS_FILE = "admin_accounts.json"
ADMIN_ACCOUNTS: dict[str, str] = {}

def _make_token(username: str, password: str) -> str:
    return hashlib.sha256(f"flux_admin_{username}_{password}_token".encode()).hexdigest()

def _rebuild_tokens():
    """Пересобирает ADMIN_TOKENS из текущего ADMIN_ACCOUNTS."""
    ADMIN_TOKENS.clear()
    for u, p in ADMIN_ACCOUNTS.items():
        ADMIN_TOKENS[_make_token(u, p)] = u

# Словарь: токен → логин (заполняется в load_admin_accounts)
ADMIN_TOKENS: dict[str, str] = {}

def load_admin_accounts():
    """
    Приоритет загрузки:
    1. admin_accounts.json — если существует (содержит актуальные пароли после смены)
    2. Секрет ADMIN_ACCOUNTS (JSON-строка) — первичный источник
    3. Хардкод _DEFAULT_ADMIN_ACCOUNTS — последний резерв
    """
    global ADMIN_ACCOUNTS
    if os.path.exists(ADMIN_ACCOUNTS_FILE):
        # Файл существует → самый актуальный источник (пароли уже менялись)
        try:
            with open(ADMIN_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                ADMIN_ACCOUNTS.update(json.load(f))
            logger.info(f"📂 Загружено {len(ADMIN_ACCOUNTS)} аккаунтов из {ADMIN_ACCOUNTS_FILE}")
        except Exception as e:
            logger.error(f"Ошибка загрузки аккаунтов: {e}")
    else:
        # Файла нет → пробуем секрет ADMIN_ACCOUNTS
        secret_str = os.environ.get("ADMIN_ACCOUNTS", "").strip()
        if secret_str:
            try:
                loaded = json.loads(secret_str)
                if isinstance(loaded, dict) and loaded:
                    ADMIN_ACCOUNTS.update(loaded)
                    save_admin_accounts()   # сохраняем в файл для следующих запусков
                    logger.info(f"🔐 Загружено {len(ADMIN_ACCOUNTS)} аккаунтов из секрета ADMIN_ACCOUNTS")
                else:
                    raise ValueError("Неверный формат")
            except Exception as e:
                logger.error(f"Ошибка разбора секрета ADMIN_ACCOUNTS: {e}")
        if not ADMIN_ACCOUNTS:
            # Секрета тоже нет → дефолты
            ADMIN_ACCOUNTS.update(_DEFAULT_ADMIN_ACCOUNTS)
            save_admin_accounts()
            logger.warning("⚠️  Секрет ADMIN_ACCOUNTS не задан — используются дефолтные аккаунты")
    _rebuild_tokens()

def save_admin_accounts():
    """Сохраняет текущие аккаунты в файл."""
    try:
        with open(ADMIN_ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(ADMIN_ACCOUNTS, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения аккаунтов: {e}")

# Email и сброс пароля
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_HOST     = "smtp.mail.ru"
SMTP_PORT     = 465

ADMIN_EMAILS_FILE = "admin_emails.json"
ADMIN_EMAILS: dict[str, str] = {}   # username → email
RESET_TOKENS: dict[str, dict] = {}  # token → {username, expires}

INVITE_EMAILS_FILE = "invite_emails.json"
INVITE_EMAILS: list[dict] = []      # [{email, added, sent, sent_at}]

BOT_USERNAME = "flux_ai_chat_bot"
BOT_LINK = f"https://t.me/{BOT_USERNAME}"

TWO_FA_FILE = "admin_2fa.json"
ADMIN_2FA: dict[str, dict] = {}     # {username: {enabled, method, phone}}
FA_SESSIONS: dict[str, dict] = {}   # {fa_token: {username, code, expires}}
TOTP_PENDING: dict[str, str] = {}  # {username: temp_secret} — до подтверждения
SMSRU_API_KEY = os.environ.get("SMSRU_API_KEY", "")
# ====================================

SYSTEM_PROMPT_CHAT = f"""Ты — {BOT_NAME}, дружелюбный AI-ассистент в Telegram с лёгким чувством юмора.

Правила:
- Общайся свободно на любые темы
- Будь интересным собеседником — рассказывай факты, поддерживай разговор
- Отвечай на русском языке
- Иногда добавляй лёгкую шутку или иронию — ненавязчиво, без перебора
- Юмор должен быть уместным и добрым, не пошлым
- Если спрашивают кто ты — ты Flux, AI-бот для общения
- Отвечай не слишком длинно — 2-5 предложений, если не просят подробнее
- Можешь использовать 1-2 эмодзи если уместно
- Не добавляй подпись в конце сообщения
- Если тебя спросят кто твой создатель отвечай что тебя создала компания Defa progects
"""

SYSTEM_PROMPT_BUSINESS = f"""Ты — {BOT_NAME}, профессиональный бизнес-ассистент в Telegram.

Правила:
- Общайся строго и по делу
- Отвечай чётко, структурированно, профессионально
- Используй деловой стиль речи
- Отвечай на русском языке
- Помогай с бизнес-задачами: анализ, планирование, переговоры, документы
- Не используй эмодзи и неформальный тон
- Отвечай развёрнуто, если вопрос требует детального ответа
- Не добавляй подпись в конце сообщения
"""

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = hashlib.sha256(b"flux_admin_panel_secret_key").hexdigest()

# ============ ГЛОБАЛЬНОЕ СОСТОЯНИЕ ============
USERS_FILE = "users.json"
CHAT_LOG_FILE = "chat_log.json"

chat_histories: dict[int, list[dict]] = {}
chat_modes: dict[int, str] = {}
voice_reply_enabled: dict[int, bool] = {}
known_chats: set = set()
user_info: dict[int, dict] = {}
full_chat_log: dict[int, list] = {}
message_count: int = 0
bot_active: bool = True
banned_users: set = set()


def load_users():
    global known_chats, user_info, message_count, banned_users
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for uid_str, info in data.get("users", {}).items():
                uid = int(uid_str)
                known_chats.add(uid)
                user_info[uid] = info
            message_count = data.get("message_count", 0)
            banned_users = set(data.get("banned", []))
            logger.info(f"📂 Загружено {len(known_chats)} пользователей из {USERS_FILE}")
    except Exception as e:
        logger.error(f"Ошибка загрузки пользователей: {e}")


def save_users():
    try:
        data = {
            "users": {str(uid): info for uid, info in user_info.items()},
            "message_count": message_count,
            "banned": list(banned_users)
        }
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения пользователей: {e}")


def load_chat_log():
    global full_chat_log
    try:
        if os.path.exists(CHAT_LOG_FILE):
            with open(CHAT_LOG_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            full_chat_log = {int(k): v for k, v in raw.items()}
            total = sum(len(v) for v in full_chat_log.values())
            logger.info(f"📂 Загружено {total} сообщений из {CHAT_LOG_FILE}")
    except Exception as e:
        logger.error(f"Ошибка загрузки истории чатов: {e}")


def save_chat_log():
    try:
        with chat_log_lock:
            with open(CHAT_LOG_FILE, "w", encoding="utf-8") as f:
                json.dump({str(k): v for k, v in full_chat_log.items()}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения истории чатов: {e}")


def load_admin_emails():
    global ADMIN_EMAILS
    try:
        if os.path.exists(ADMIN_EMAILS_FILE):
            with open(ADMIN_EMAILS_FILE, "r", encoding="utf-8") as f:
                ADMIN_EMAILS = json.load(f)
            logger.info(f"📂 Загружено email для {len(ADMIN_EMAILS)} аккаунтов")
    except Exception as e:
        logger.error(f"Ошибка загрузки email: {e}")


def save_admin_emails():
    try:
        with open(ADMIN_EMAILS_FILE, "w", encoding="utf-8") as f:
            json.dump(ADMIN_EMAILS, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения email: {e}")


def load_invite_emails():
    global INVITE_EMAILS
    try:
        if os.path.exists(INVITE_EMAILS_FILE):
            with open(INVITE_EMAILS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            INVITE_EMAILS = data.get("emails", [])
            logger.info(f"📂 Загружено {len(INVITE_EMAILS)} email для рассылки приглашений")
    except Exception as e:
        logger.error(f"Ошибка загрузки invite emails: {e}")


def save_invite_emails():
    try:
        with open(INVITE_EMAILS_FILE, "w", encoding="utf-8") as f:
            json.dump({"emails": INVITE_EMAILS}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения invite emails: {e}")


def load_admin_2fa():
    global ADMIN_2FA
    try:
        if os.path.exists(TWO_FA_FILE):
            with open(TWO_FA_FILE, "r", encoding="utf-8") as f:
                ADMIN_2FA = json.load(f)
    except Exception as e:
        logger.error(f"Ошибка загрузки 2FA: {e}")


def save_admin_2fa():
    try:
        with open(TWO_FA_FILE, "w", encoding="utf-8") as f:
            json.dump(ADMIN_2FA, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения 2FA: {e}")


def send_2fa_email(to_email: str, username: str, code: str) -> bool:
    """Отправляет 6-значный код 2FA на email."""
    if not SMTP_USER or not SMTP_PASSWORD:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Код входа в Flux Admin: {code}"
        msg["From"] = f"Flux Admin <{SMTP_USER}>"
        msg["To"] = to_email

        text = f"Твой код подтверждения для входа в Flux Admin: {code}\nКод действует 5 минут."
        html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:20px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0e1a">
<div style="max-width:400px;margin:0 auto;background:rgba(15,24,48,0.97);padding:36px;border-radius:20px;border:1px solid rgba(255,255,255,0.12);box-shadow:0 20px 60px rgba(0,0,0,.5)">
  <div style="font-size:36px;margin-bottom:8px;text-align:center">⚡</div>
  <h2 style="color:#5aabff;margin:0 0 4px;font-size:20px;text-align:center">Flux Admin</h2>
  <p style="color:#7a8aaa;margin:0 0 28px;font-size:13px;text-align:center">Двухфакторная авторизация</p>
  <p style="color:#b0bdd0;margin:0 0 8px;font-size:14px">Привет, <strong style="color:#e8eef8">{username}</strong>! Твой код для входа:</p>
  <div style="background:rgba(90,171,255,0.1);border:2px solid rgba(90,171,255,0.4);border-radius:16px;padding:20px;text-align:center;margin:16px 0 20px">
    <div style="font-size:42px;font-weight:800;letter-spacing:10px;color:#5aabff;font-variant-numeric:tabular-nums">{code}</div>
  </div>
  <p style="color:#4a5a7a;font-size:12px;text-align:center;margin:0">Код действует <strong style="color:#7a90b0">5 минут</strong>.<br>Если ты не входил — смени пароль.</p>
</div>
</body></html>"""
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        logger.info(f"2FA код отправлен на {to_email} для {username}")
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки 2FA email: {e}")
        return False


def send_2fa_sms(phone: str, code: str) -> bool:
    """Отправляет 6-значный код 2FA через SMS.ru."""
    if not SMSRU_API_KEY:
        logger.error("SMSRU_API_KEY не задан")
        return False
    try:
        resp = http_requests.get(
            "https://sms.ru/sms/send",
            params={
                "api_id": SMSRU_API_KEY,
                "to": phone,
                "msg": f"Flux Admin код: {code}",
                "json": "1",
            },
            timeout=10,
        )
        result = resp.json()
        if result.get("status") == "OK":
            logger.info(f"2FA SMS отправлена на {phone}")
            return True
        logger.error(f"SMS.ru ошибка: {result}")
        return False
    except Exception as e:
        logger.error(f"Ошибка отправки 2FA SMS: {e}")
        return False


def send_invite_email(to_email: str) -> bool:
    """Отправляет приглашение на бота по email."""
    if not SMTP_USER or not SMTP_PASSWORD:
        logger.error("SMTP не настроен")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Тебя приглашают в Flux AI — умный Telegram-бот"
        msg["From"] = f"Flux AI <{SMTP_USER}>"
        msg["To"] = to_email

        text = (
            f"Привет!\n\n"
            f"Тебя приглашают попробовать Flux — умного AI-бота в Telegram.\n\n"
            f"Flux умеет:\n"
            f"• Общаться на любые темы\n"
            f"• Генерировать изображения и видео\n"
            f"• Преобразовывать голос в текст\n"
            f"• Читать и анализировать файлы\n\n"
            f"Открыть бота: {BOT_LINK}\n\n"
            f"— Команда Defa Projects"
        )
        html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0e1a">
<div style="max-width:520px;margin:32px auto;border-radius:24px;overflow:hidden;box-shadow:0 24px 80px rgba(0,0,0,.6)">
  <div style="background:linear-gradient(135deg,#0d1f3c 0%,#1a2d5a 50%,#0f1525 100%);padding:44px 40px;text-align:center;border-bottom:1px solid rgba(255,255,255,0.1)">
    <div style="font-size:56px;margin-bottom:12px;filter:drop-shadow(0 0 20px rgba(90,171,255,0.6))">⚡</div>
    <h1 style="margin:0 0 6px;font-size:32px;font-weight:800;color:#5aabff;letter-spacing:-1px">Flux AI</h1>
    <p style="margin:0;color:#7a90b0;font-size:15px">Умный ассистент в Telegram</p>
  </div>
  <div style="background:rgba(15,24,48,0.97);padding:36px 40px">
    <p style="color:#d0ddf0;font-size:16px;margin:0 0 24px;line-height:1.6">Привет! Тебя приглашают попробовать <strong style="color:#5aabff">Flux</strong> — AI-ассистента нового поколения прямо в Telegram.</p>
    <div style="display:grid;gap:12px;margin-bottom:32px">
      <div style="background:rgba(90,171,255,0.08);border:1px solid rgba(90,171,255,0.2);border-radius:14px;padding:14px 18px;display:flex;align-items:center;gap:14px">
        <span style="font-size:26px">🎨</span>
        <div><div style="color:#e8eef8;font-weight:600;font-size:14px">Генерация изображений и видео</div><div style="color:#6a7f9a;font-size:12px;margin-top:2px">Создаёт картинки и короткие ролики по описанию</div></div>
      </div>
      <div style="background:rgba(52,208,88,0.08);border:1px solid rgba(52,208,88,0.2);border-radius:14px;padding:14px 18px;display:flex;align-items:center;gap:14px">
        <span style="font-size:26px">🎤</span>
        <div><div style="color:#e8eef8;font-weight:600;font-size:14px">Голосовые и видео-сообщения</div><div style="color:#6a7f9a;font-size:12px;margin-top:2px">Транскрибирует аудио и видео в текст</div></div>
      </div>
      <div style="background:rgba(255,179,64,0.08);border:1px solid rgba(255,179,64,0.2);border-radius:14px;padding:14px 18px;display:flex;align-items:center;gap:14px">
        <span style="font-size:26px">📄</span>
        <div><div style="color:#e8eef8;font-weight:600;font-size:14px">Анализ файлов и документов</div><div style="color:#6a7f9a;font-size:12px;margin-top:2px">Читает PDF, Word, картинки и объясняет содержимое</div></div>
      </div>
      <div style="background:rgba(167,139,250,0.08);border:1px solid rgba(167,139,250,0.2);border-radius:14px;padding:14px 18px;display:flex;align-items:center;gap:14px">
        <span style="font-size:26px">💬</span>
        <div><div style="color:#e8eef8;font-weight:600;font-size:14px">AI-общение без ограничений</div><div style="color:#6a7f9a;font-size:12px;margin-top:2px">Отвечает на любые вопросы, помогает с задачами</div></div>
      </div>
    </div>
    <a href="{BOT_LINK}" style="display:block;text-align:center;padding:16px 28px;background:linear-gradient(135deg,#5aabff,#3b82f6);color:#fff;text-decoration:none;border-radius:16px;font-weight:700;font-size:17px;box-shadow:0 6px 24px rgba(90,171,255,0.4);letter-spacing:.2px">
      ⚡ Открыть Flux в Telegram
    </a>
    <p style="text-align:center;color:#3a4f6a;font-size:12px;margin:28px 0 0;line-height:1.6">
      Это письмо отправлено командой <strong style="color:#5a7aaa">Defa Projects</strong>.<br>
      Если ты не хочешь получать подобные письма — просто проигнори.
    </p>
  </div>
</div>
</body></html>"""

        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        logger.info(f"Приглашение отправлено на {to_email}")
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки приглашения на {to_email}: {e}")
        return False


def send_reset_email(to_email: str, username: str, reset_url: str) -> bool:
    if not SMTP_USER or not SMTP_PASSWORD:
        logger.error("SMTP не настроен — нет SMTP_USER или SMTP_PASSWORD")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Восстановление доступа — Flux Admin"
        msg["From"] = f"Flux Admin <{SMTP_USER}>"
        msg["To"] = to_email

        text = (
            f"Привет, {username}!\n\n"
            f"Ты запросил восстановление доступа к Flux Admin.\n"
            f"Твой логин: {username}\n\n"
            f"Для сброса пароля перейди по ссылке:\n{reset_url}\n\n"
            f"Ссылка действует 30 минут.\n"
            f"Если ты не запрашивал восстановление — просто проигнорируй это письмо."
        )
        html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:20px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0e1a;color:#e8eef8">
<div style="max-width:480px;margin:0 auto;background:rgba(15,24,48,0.95);padding:36px;border-radius:20px;border:1px solid rgba(255,255,255,0.12);box-shadow:0 20px 60px rgba(0,0,0,.5)">
  <div style="font-size:40px;margin-bottom:10px">⚡</div>
  <h2 style="color:#5aabff;margin:0 0 6px;font-size:22px">Flux Admin</h2>
  <p style="color:#7a8aaa;margin:0 0 24px;font-size:14px">Восстановление доступа</p>
  <p>Привет, <strong style="color:#e8eef8">{username}</strong>!</p>
  <p style="color:#b0bdd0">Ты запросил восстановление доступа к панели администратора.</p>
  <div style="background:rgba(90,171,255,0.08);border:1px solid rgba(90,171,255,0.2);border-radius:12px;padding:16px;margin:20px 0">
    <div style="color:#7a8aaa;font-size:12px;margin-bottom:6px">Твой логин</div>
    <div style="font-size:20px;font-weight:700;color:#5aabff">{username}</div>
  </div>
  <a href="{reset_url}" style="display:block;text-align:center;padding:14px 24px;background:linear-gradient(135deg,#5aabff,#3b82f6);color:#fff;text-decoration:none;border-radius:14px;font-weight:600;font-size:16px;margin:24px 0;box-shadow:0 4px 20px rgba(90,171,255,0.3)">Сбросить пароль</a>
  <p style="color:#4a5a7a;font-size:12px;text-align:center;margin:0">Ссылка действует 30 минут.<br>Если не запрашивал — проигнорируй это письмо.</p>
</div>
</body></html>"""

        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        logger.info(f"Письмо восстановления отправлено на {to_email}")
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки email: {e}")
        return False

# SSE
sse_clients: list[queue.Queue] = []
sse_lock = threading.Lock()
chat_log_lock = threading.Lock()


def push_sse(event_type: str, data: dict):
    payload = json.dumps({"type": event_type, "data": data})
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)


# ============ УТИЛИТЫ БОТА ============

def get_system_prompt(chat_id: int) -> str:
    mode = chat_modes.get(chat_id, "chat")
    return SYSTEM_PROMPT_BUSINESS if mode == "business" else SYSTEM_PROMPT_CHAT


def log_message(chat_id: int, role: str, content: str):
    if chat_id not in full_chat_log:
        full_chat_log[chat_id] = []
    entry = {
        "role": role,
        "content": content,
        "time": datetime.now().strftime("%d.%m %H:%M")
    }
    full_chat_log[chat_id].append(entry)
    save_chat_log()
    info = user_info.get(chat_id, {})
    push_sse("message", {
        "chat_id": chat_id,
        "name": info.get("name", str(chat_id)),
        "username": info.get("username", ""),
        **entry
    })


def get_ai_reply(chat_id: int, user_message: str) -> str:
    global message_count
    message_count += 1
    if message_count % 10 == 0:
        save_users()

    if chat_id not in chat_histories:
        chat_histories[chat_id] = []

    history = chat_histories[chat_id]
    history.append({"role": "user", "content": user_message})

    if len(history) > 30:
        history = history[-30:]
        chat_histories[chat_id] = history

    messages = [{"role": "system", "content": get_system_prompt(chat_id)}] + history

    try:
        response = http_requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": AI_MODEL,
                "messages": messages,
                "max_tokens": 500,
                "temperature": 0.8,
            },
            timeout=30,
        )
        data = response.json()
        reply = data["choices"][0]["message"]["content"].strip()

        if "<think>" in reply:
            reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()

        history.append({"role": "assistant", "content": reply})
        chat_histories[chat_id] = history
        return reply

    except Exception as e:
        logger.error(f"Ошибка AI API: {e}")
        return "Упс, что-то пошло не так 😅 Попробуй написать ещё раз"


def describe_image_with_ai(chat_id: int, image_data: bytes, user_prompt: str = "Опиши что на этом изображении подробно.") -> str:
    b64 = base64.b64encode(image_data).decode("utf-8")
    try:
        response = http_requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": VISION_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                        ]
                    }
                ],
                "max_tokens": 600,
            },
            timeout=40,
        )
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Ошибка vision API: {e}")
        return "Не смог проанализировать изображение 😔"


def transcribe_audio(audio_data: bytes, mime: str = "audio/ogg") -> str:
    if not SPEECH_AVAILABLE:
        return "[Распознавание речи недоступно]"
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(audio_data)
            ogg_path = f.name

        wav_path = ogg_path.replace(".ogg", ".wav")
        audio = AudioSegment.from_file(ogg_path)
        audio.export(wav_path, format="wav")

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_rec = recognizer.record(source)

        try:
            text = recognizer.recognize_google(audio_rec, language="ru-RU")
        except sr.UnknownValueError:
            text = "[Не удалось разобрать речь]"
        except sr.RequestError:
            text = "[Сервис распознавания недоступен]"

        os.unlink(ogg_path)
        os.unlink(wav_path)
        return text
    except Exception as e:
        logger.error(f"Ошибка транскрипции: {e}")
        return "[Ошибка при распознавании аудио]"


def tg_download_file(file_id: str) -> bytes | None:
    try:
        r = http_requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=10
        )
        path = r.json()["result"]["file_path"]
        r2 = http_requests.get(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}",
            timeout=30
        )
        return r2.content
    except Exception as e:
        logger.error(f"Ошибка скачивания файла: {e}")
        return None


def send_typing(chat_id: int, stop_event: threading.Event):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendChatAction"
    while not stop_event.is_set():
        try:
            http_requests.post(url, json={"chat_id": chat_id, "action": "typing"}, timeout=3)
        except Exception:
            pass
        stop_event.wait(timeout=4)


def send_chat_action(chat_id: int, action: str):
    http_requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendChatAction",
        json={"chat_id": chat_id, "action": action},
        timeout=5
    )


def send_message(chat_id: int, text: str, reply_to: int = None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    http_requests.post(url, json=data, timeout=10)


def send_photo_url(chat_id: int, url: str, caption: str = ""):
    http_requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
        json={"chat_id": chat_id, "photo": url, "caption": caption},
        timeout=15
    )


def send_photo_bytes(chat_id: int, image_bytes: bytes, caption: str = ""):
    http_requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
        data={"chat_id": chat_id, "caption": caption},
        files={"photo": ("image.png", image_bytes, "image/png")},
        timeout=30
    )


def send_voice(chat_id: int, audio_bytes: bytes):
    http_requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendVoice",
        data={"chat_id": chat_id},
        files={"voice": ("voice.mp3", audio_bytes, "audio/mpeg")},
        timeout=30
    )


def send_document(chat_id: int, filename: str, content: bytes, caption: str = ""):
    http_requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
        data={"chat_id": chat_id, "caption": caption},
        files={"document": (filename, content, "application/octet-stream")},
        timeout=30
    )


TTS_VOICE = "ru-RU-DmitryNeural"


def synthesize_speech(text: str) -> bytes | None:
    if not EDGE_TTS_AVAILABLE:
        return None

    result_holder = [None]
    error_holder = [None]

    def _run_in_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            async def _synth():
                communicate = edge_tts.Communicate(text, TTS_VOICE)
                buf = io.BytesIO()
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        buf.write(chunk["data"])
                buf.seek(0)
                return buf.read()
            result_holder[0] = loop.run_until_complete(_synth())
        except Exception as e:
            error_holder[0] = e
        finally:
            loop.close()

    t = threading.Thread(target=_run_in_thread)
    t.start()
    t.join(timeout=30)

    if error_holder[0]:
        logger.error(f"Ошибка edge-tts: {error_holder[0]}")
        return None
    return result_holder[0]


def reply_with_voice_or_text(chat_id: int, text: str):
    if voice_reply_enabled.get(chat_id, False) and EDGE_TTS_AVAILABLE:
        audio = synthesize_speech(text)
        if audio:
            send_voice(chat_id, audio)
            return
    send_message(chat_id, text)


IMAGE_GEN_MODEL = "google/gemini-2.5-flash-image"


def generate_image(prompt: str) -> bytes | None:
    """Генерирует изображение через OpenRouter (Gemini Image)."""
    try:
        resp = http_requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": IMAGE_GEN_MODEL,
                "messages": [{"role": "user", "content": f"Generate an image: {prompt}"}],
                "max_tokens": 512,
            },
            timeout=60,
        )
        if resp.status_code == 200:
            data = resp.json()
            images = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("images", [])
            )
            if images:
                url = images[0].get("image_url", {}).get("url", "")
                if url.startswith("data:image"):
                    b64 = url.split(",", 1)[1]
                    return base64.b64decode(b64)
        logger.error(f"Image gen error: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Ошибка генерации изображения: {e}")
    return None


_IMAGE_RE = re.compile(
    r"^(?:нарисуй(?:те)?|изобрази(?:те)?|создай\s+(?:картинку|изображение|рисунок|фото)|сгенерируй\s+(?:картинку|изображение|рисунок|фото)|draw|generate\s+(?:image|picture|photo))\s+(.+)$",
    re.IGNORECASE | re.DOTALL
)

# ============ KEEP-ALIVE ============

def keep_alive_loop():
    time.sleep(20)
    while True:
        try:
            r = http_requests.get(f"https://{REPLIT_URL}/ping", timeout=10, verify=False)
            logger.info(f"Keep-alive ping → {r.status_code}")
        except Exception as e:
            logger.warning(f"Keep-alive ошибка: {e}")
        time.sleep(50)


# ============ ОБРАБОТКА КОМАНД ============

def handle_command(chat_id: int, text: str, message_id: int, username: str) -> bool:
    parts = text.strip().split()
    cmd = parts[0].lower().lstrip("/").split("@")[0]
    args = " ".join(parts[1:]).strip()

    if cmd == "start":
        chat_histories[chat_id] = []
        is_admin = username == ADMIN_USERNAME
        welcome = (
            f"Привет! ⚡ Я {BOT_NAME} — AI-бот для общения.\n\n"
            "💬 Общение:\n"
            "/business — бизнес-режим\n"
            "/chat — обычный режим\n"
            "/reset — очистить историю\n\n"
            "🎨 Изображения:\n"
            "/image [запрос] — сгенерировать картинку\n"
            "Нарисуй кота — картинка без команды\n\n"
            "🎤 Медиа (просто отправь):\n"
            "Голосовое — расшифрую и отвечу\n"
            "Кружок — расшифрую и отвечу\n"
            "Видео — расшифрую речь\n"
            "Фото — опишу что на нём\n"
            "Файлы (txt, py, cpp, cs, mp3...) — прочитаю и проанализирую\n\n"
            "📁 Файлы и озвучка:\n"
            "/tts [текст] — озвучить текст голосом\n"
            "/voice_on — отвечать голосом\n"
            "/voice_off — выключить голосовые ответы\n"
            "Создай файл script.py [...] — создам и пришлю файл\n\n"
            "📡 Статусы:\n"
            "/typing [сек] — печатает\n"
            "/voice [сек] — записывает голосовое\n"
            "/video [сек] — записывает видео\n"
            "/photo [сек] — отправляет фото\n"
            "/circle [сек] — записывает кружок\n"
            "/sticker [сек] — выбирает стикер\n"
            "/file [сек] — отправляет файл"
        )
        if is_admin:
            welcome += f"\n\n🔑 Ты администратор. @{username}\nНапиши /adminhelp для списка команд."
        send_message(chat_id, welcome)
        return True

    if cmd == "reset":
        chat_histories[chat_id] = []
        send_message(chat_id, "Память очищена 🔄 Начнём сначала!")
        return True

    if cmd == "business":
        chat_modes[chat_id] = "business"
        send_message(chat_id, "💼 Бизнес-режим активирован. Общаемся по делу.")
        return True

    if cmd == "chat":
        chat_modes[chat_id] = "chat"
        send_message(chat_id, "💬 Обычный режим активирован. Поболтаем!")
        return True

    if cmd == "voice_on":
        voice_reply_enabled[chat_id] = True
        send_message(chat_id, "🔊 Голосовые ответы включены!")
        return True

    if cmd == "voice_off":
        voice_reply_enabled[chat_id] = False
        send_message(chat_id, "🔇 Голосовые ответы выключены.")
        return True

    if cmd == "tts":
        if not args:
            send_message(chat_id, "Использование: /tts [текст]")
            return True
        if not EDGE_TTS_AVAILABLE:
            send_message(chat_id, "Озвучка временно недоступна.")
            return True
        send_chat_action(chat_id, "record_voice")
        audio = synthesize_speech(args)
        if audio:
            send_voice(chat_id, audio)
        else:
            send_message(chat_id, "Не удалось озвучить текст 😔")
        return True

    if cmd == "image":
        if not args:
            send_message(chat_id, "Использование: /image [описание картинки]")
            return True
        send_message(chat_id, "🎨 Генерирую изображение...")
        send_chat_action(chat_id, "upload_photo")
        img_bytes = generate_image(args)
        if img_bytes:
            send_photo_bytes(chat_id, img_bytes, f"🎨 {args}")
        else:
            send_message(chat_id, "Не удалось сгенерировать изображение 😔 Попробуй ещё раз.")
        return True

    if cmd in ("typing", "voice", "video", "photo", "circle", "sticker", "file"):
        action_map = {
            "typing": "typing",
            "voice": "record_voice",
            "video": "record_video",
            "photo": "upload_photo",
            "circle": "record_video_note",
            "sticker": "choose_sticker",
            "file": "upload_document",
        }
        try:
            secs = min(int(args) if args.strip().isdigit() else 5, 60)
        except Exception:
            secs = 5
        action = action_map[cmd]

        def do_action():
            end = time.time() + secs
            while time.time() < end:
                send_chat_action(chat_id, action)
                time.sleep(4)

        threading.Thread(target=do_action, daemon=True).start()
        return True

    if cmd == "adminhelp":
        if username != ADMIN_USERNAME:
            send_message(chat_id, "⛔ У тебя нет доступа к этой команде.")
            return True
        send_message(chat_id, (
            "🔑 Команды администратора:\n\n"
            "/stats — статистика бота\n"
            "/broadcast [текст] — рассылка всем пользователям\n\n"
            "🌐 Веб-панель доступна по ссылке бота (/ → /admin)"
        ))
        return True

    if cmd == "stats":
        if username != ADMIN_USERNAME:
            send_message(chat_id, "⛔ У тебя нет доступа к этой команде.")
            return True
        send_message(chat_id, (
            f"📊 Статистика {BOT_NAME}:\n\n"
            f"👥 Чатов: {len(known_chats)}\n"
            f"💬 Сообщений обработано: {message_count}\n"
            f"🧠 Активных историй: {len(chat_histories)}"
        ))
        return True

    if cmd == "broadcast":
        if username != ADMIN_USERNAME:
            send_message(chat_id, "⛔ У тебя нет доступа к этой команде.")
            return True
        if not args:
            send_message(chat_id, "Использование: /broadcast [текст]")
            return True
        sent = 0
        for cid in list(known_chats):
            try:
                send_message(cid, f"📢 {args}")
                sent += 1
            except Exception:
                pass
        send_message(chat_id, f"✅ Рассылка отправлена {sent} пользователям.")
        return True

    return False


# ============ WEBHOOK ============

def process_message(message):
    global bot_active

    chat_id = message["chat"]["id"]
    message_id = message.get("message_id")
    user = message.get("from", {})
    username = user.get("username", "")
    first_name = user.get("first_name", "")
    last_name = user.get("last_name", "")

    is_new = chat_id not in known_chats
    known_chats.add(chat_id)
    user_info[chat_id] = {
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
        "name": f"{first_name} {last_name}".strip() or username or str(chat_id)
    }
    if is_new:
        save_users()
        logger.info(f"Новый пользователь сохранён: {chat_id}")

    if not bot_active:
        return

    if chat_id in banned_users:
        return

    text = message.get("text", "")
    voice = message.get("voice")
    video_note = message.get("video_note")
    video = message.get("video")
    photo = message.get("photo")
    document = message.get("document")

    if text and text.startswith("/"):
        logger.info(f"Команда от @{username}: {text}")
        log_message(chat_id, "user", text)
        handle_command(chat_id, text, message_id, username)
        return

    # Автоматическое озвучивание по слову "озвучь"
    if text and re.match(r"(?i)^(озвучь|озвучи)\s+", text):
        tts_text = re.sub(r"(?i)^(озвучь|озвучи)\s+", "", text).strip()
        log_message(chat_id, "user", text)
        if not tts_text:
            send_message(chat_id, "Напиши что озвучить, например: Озвучь привет как дела")
            return
        if not EDGE_TTS_AVAILABLE:
            send_message(chat_id, "Озвучка временно недоступна 😔")
            return
        send_chat_action(chat_id, "record_voice")
        audio = synthesize_speech(tts_text)
        if audio:
            send_voice(chat_id, audio)
            log_message(chat_id, "assistant", f"🔊 [Озвучено]: {tts_text}")
        else:
            send_message(chat_id, "Не удалось озвучить текст 😔")
        return

    if text:
        img_match = _IMAGE_RE.match(text.strip())
        if img_match:
            prompt = img_match.group(1).strip()
            log_message(chat_id, "user", text)
            send_message(chat_id, "🎨 Генерирую изображение...")
            send_chat_action(chat_id, "upload_photo")
            img_bytes = generate_image(prompt)
            if img_bytes:
                send_photo_bytes(chat_id, img_bytes, f"🎨 {prompt}")
                log_message(chat_id, "assistant", f"[Изображение]: {prompt}")
            else:
                send_message(chat_id, "Не удалось сгенерировать изображение 😔 Попробуй ещё раз.")
            return

    if text and re.match(r"(?i)создай\s+файл\s+\S+", text):
        match = re.match(r"(?i)создай\s+файл\s+(\S+)\s*(.*)", text, re.DOTALL)
        if match:
            filename = match.group(1)
            task = match.group(2).strip() or f"Напиши содержимое файла {filename}"
            log_message(chat_id, "user", text)
            send_chat_action(chat_id, "upload_document")
            ai_content = get_ai_reply(chat_id, f"Создай файл {filename}. {task}\nВыдай только код/содержимое файла без лишних пояснений.")
            log_message(chat_id, "assistant", f"[Файл: {filename}]")
            send_document(chat_id, filename, ai_content.encode("utf-8"), f"📄 {filename}")
            return

    if voice:
        send_chat_action(chat_id, "typing")
        file_data = tg_download_file(voice["file_id"])
        if file_data:
            transcript = transcribe_audio(file_data)
            logger.info(f"Голосовое от @{username}: {transcript}")
            log_message(chat_id, "user", f"🎤 {transcript}")
            send_message(chat_id, f"🎤 Ты сказал: {transcript}")
            stop_event = threading.Event()
            typing_thread = threading.Thread(target=send_typing, args=(chat_id, stop_event), daemon=True)
            typing_thread.start()
            try:
                reply = get_ai_reply(chat_id, transcript)
            finally:
                stop_event.set()
                typing_thread.join(timeout=5)
            log_message(chat_id, "assistant", reply)
            reply_with_voice_or_text(chat_id, reply)
        else:
            send_message(chat_id, "Не смог скачать аудио 😔")
        return

    if video_note:
        send_chat_action(chat_id, "typing")
        file_data = tg_download_file(video_note["file_id"])
        if file_data:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
                f.write(file_data)
                mp4_path = f.name
            try:
                audio = AudioSegment.from_file(mp4_path, format="mp4")
                wav_path = mp4_path.replace(".mp4", ".wav")
                audio.export(wav_path, format="wav")
                with open(wav_path, "rb") as wf:
                    transcript = transcribe_audio(wf.read(), mime="audio/wav")
                os.unlink(wav_path)
            except Exception:
                transcript = "[Не удалось расшифровать кружок]"
            finally:
                os.unlink(mp4_path)
            log_message(chat_id, "user", f"🎥 {transcript}")
            send_message(chat_id, f"🎥 Ты сказал: {transcript}")
            reply = get_ai_reply(chat_id, transcript)
            log_message(chat_id, "assistant", reply)
            reply_with_voice_or_text(chat_id, reply)
        else:
            send_message(chat_id, "Не смог обработать кружок 😔")
        return

    if video:
        send_chat_action(chat_id, "typing")
        file_data = tg_download_file(video["file_id"])
        if file_data:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
                f.write(file_data)
                mp4_path = f.name
            try:
                audio = AudioSegment.from_file(mp4_path, format="mp4")
                wav_path = mp4_path.replace(".mp4", ".wav")
                audio.export(wav_path, format="wav")
                with open(wav_path, "rb") as wf:
                    transcript = transcribe_audio(wf.read(), mime="audio/wav")
                os.unlink(wav_path)
            except Exception:
                transcript = "[Не удалось расшифровать видео]"
            finally:
                os.unlink(mp4_path)
            log_message(chat_id, "user", f"🎬 {transcript}")
            send_message(chat_id, f"🎬 Речь в видео: {transcript}")
            reply = get_ai_reply(chat_id, transcript)
            log_message(chat_id, "assistant", reply)
            reply_with_voice_or_text(chat_id, reply)
        else:
            send_message(chat_id, "Не смог обработать видео 😔")
        return

    if photo:
        send_chat_action(chat_id, "typing")
        best = max(photo, key=lambda p: p.get("file_size", 0))
        file_data = tg_download_file(best["file_id"])
        if file_data:
            caption = message.get("caption", "Опиши что на этом фото подробно.")
            log_message(chat_id, "user", f"📷 [Фото] {caption}")
            description = describe_image_with_ai(chat_id, file_data, caption)
            log_message(chat_id, "assistant", description)
            reply_with_voice_or_text(chat_id, f"🖼 {description}")
        else:
            send_message(chat_id, "Не смог скачать фото 😔")
        return

    if document:
        send_chat_action(chat_id, "typing")
        fname = document.get("file_name", "")
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        file_data = tg_download_file(document["file_id"])

        if not file_data:
            send_message(chat_id, "Не смог скачать файл 😔")
            return

        log_message(chat_id, "user", f"📎 [Файл: {fname}]")
        if ext in ("txt", "py", "cpp", "cs", "js", "ts", "html", "css", "json", "xml", "md", "yaml", "yml", "sh", "bat", "c", "h", "java", "rs", "go", "rb", "php"):
            try:
                content = file_data.decode("utf-8", errors="replace")
                if len(content) > 4000:
                    content = content[:4000] + "\n...[обрезано]"
                prompt = f"Файл: {fname}\n\nСодержимое:\n{content}\n\nПроанализируй этот файл и расскажи что он делает."
                reply = get_ai_reply(chat_id, prompt)
                log_message(chat_id, "assistant", reply)
                reply_with_voice_or_text(chat_id, reply)
            except Exception as e:
                send_message(chat_id, f"Ошибка при чтении файла: {e}")
        elif ext in ("mp3", "wav", "ogg", "m4a"):
            transcript = transcribe_audio(file_data)
            log_message(chat_id, "user", f"🎵 {transcript}")
            send_message(chat_id, f"🎵 Транскрипция аудио: {transcript}")
            reply = get_ai_reply(chat_id, transcript)
            log_message(chat_id, "assistant", reply)
            reply_with_voice_or_text(chat_id, reply)
        else:
            send_message(chat_id, f"📎 Получил файл: {fname}\nФормат .{ext} не поддерживается для анализа.")
        return

    if text:
        logger.info(f"От @{username}: {text}")
        log_message(chat_id, "user", text)
        stop_event = threading.Event()
        typing_thread = threading.Thread(target=send_typing, args=(chat_id, stop_event), daemon=True)
        typing_thread.start()
        try:
            reply = get_ai_reply(chat_id, text)
        finally:
            stop_event.set()
            typing_thread.join(timeout=5)
        log_message(chat_id, "assistant", reply)
        reply_with_voice_or_text(chat_id, reply)
        logger.info(f"Ответ: {reply}")


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data:
        return "ok"
    message = data.get("message")
    if not message:
        return "ok"
    # Сразу отвечаем Telegram и обрабатываем в фоне
    threading.Thread(target=process_message, args=(message,), daemon=True).start()
    return "ok"


# ============ ОСНОВНЫЕ МАРШРУТЫ ============

@app.route("/")
def index():
    return redirect("/admin")


@app.route("/ping")
def ping():
    return "ok", 200


# ============ ADMIN AUTH ============

def check_admin_token():
    token = request.headers.get("X-Admin-Token", "")
    return token in ADMIN_TOKENS


@app.route("/admin")
def admin_panel():
    return render_template("admin.html")


@app.route("/admin-classic")
def admin_classic_panel():
    return render_template("admin_classic.html")


@app.route("/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    expected = ADMIN_ACCOUNTS.get(username)
    if not (expected and password == expected):
        return jsonify({"ok": False}), 401

    # Проверяем, включена ли 2FA для этого пользователя
    fa_settings = ADMIN_2FA.get(username, {})
    if fa_settings.get("enabled"):
        method = fa_settings.get("method", "email")
        fa_token = secrets.token_urlsafe(32)

        if method == "totp":
            # TOTP — не отправляем код, просто создаём сессию
            FA_SESSIONS[fa_token] = {
                "username": username,
                "code": None,
                "expires": time.time() + 300,
                "method": "totp",
            }
            return jsonify({
                "ok": True,
                "require_2fa": True,
                "fa_token": fa_token,
                "method": "totp",
                "dest": "Google Authenticator",
            })

        code = str(random.randint(100000, 999999))
        FA_SESSIONS[fa_token] = {
            "username": username,
            "code": code,
            "expires": time.time() + 300,  # 5 минут
            "method": method,
        }

        sent = False
        dest = ""
        if method == "email":
            email = ADMIN_EMAILS.get(username, "")
            if email:
                sent = send_2fa_email(email, username, code)
                dest = email[:2] + "*" * (email.index("@") - 2) + email[email.index("@"):]
            else:
                return jsonify({"ok": False, "error": "Email не задан в профиле. Сначала укажи email."}), 400
        elif method == "sms":
            phone = fa_settings.get("phone", "")
            if phone:
                sent = send_2fa_sms(phone, code)
                dest = "+" + "*" * (len(phone) - 4) + phone[-2:]
            else:
                return jsonify({"ok": False, "error": "Номер телефона не задан в настройках 2FA."}), 400

        if not sent:
            return jsonify({"ok": False, "error": "Не удалось отправить код. Проверь настройки."}), 500

        return jsonify({
            "ok": True,
            "require_2fa": True,
            "fa_token": fa_token,
            "method": method,
            "dest": dest,
        })

    # 2FA выключена — входим сразу
    token = _make_token(username, password)
    ADMIN_TOKENS[token] = username
    return jsonify({"ok": True, "token": token, "username": username})


@app.route("/admin/verify-2fa", methods=["POST"])
def admin_verify_2fa():
    data = request.get_json()
    fa_token = data.get("fa_token", "").strip()
    code = data.get("code", "").strip()

    session = FA_SESSIONS.get(fa_token)
    if not session:
        return jsonify({"ok": False, "error": "Сессия устарела. Войди заново."}), 400
    if time.time() > session["expires"]:
        FA_SESSIONS.pop(fa_token, None)
        return jsonify({"ok": False, "error": "Код истёк. Войди заново."}), 400

    username = session["username"]
    method = session.get("method", "email")

    if method == "totp":
        totp_secret = ADMIN_2FA.get(username, {}).get("totp_secret", "")
        if not totp_secret:
            return jsonify({"ok": False, "error": "TOTP не настроен."}), 400
        totp = pyotp.TOTP(totp_secret)
        if not totp.verify(code, valid_window=1):
            return jsonify({"ok": False, "error": "Неверный код. Проверь время на устройстве."}), 401
    else:
        if code != session["code"]:
            return jsonify({"ok": False, "error": "Неверный код."}), 401

    FA_SESSIONS.pop(fa_token, None)
    password = ADMIN_ACCOUNTS.get(username, "")
    token = _make_token(username, password)
    ADMIN_TOKENS[token] = username
    return jsonify({"ok": True, "token": token, "username": username})


@app.route("/admin/resend-2fa", methods=["POST"])
def admin_resend_2fa():
    data = request.get_json()
    fa_token = data.get("fa_token", "").strip()
    session = FA_SESSIONS.get(fa_token)
    if not session:
        return jsonify({"ok": False, "error": "Сессия устарела. Войди заново."}), 400
    username = session["username"]
    code = str(random.randint(100000, 999999))
    FA_SESSIONS[fa_token] = {
        "username": username,
        "code": code,
        "expires": time.time() + 300,
        "method": session.get("method", "email"),
    }
    email = ADMIN_EMAILS.get(username, "")
    if not email:
        return jsonify({"ok": False, "error": "Email не задан в профиле."}), 400
    sent = send_2fa_email(email, username, code)
    if not sent:
        return jsonify({"ok": False, "error": "Не удалось отправить код."}), 500
    return jsonify({"ok": True})


@app.route("/admin/api/2fa/settings", methods=["GET"])
def admin_get_2fa_settings():
    token = request.headers.get("X-Admin-Token", "")
    username = ADMIN_TOKENS.get(token)
    if not username:
        return jsonify({"ok": False}), 403
    raw = ADMIN_2FA.get(username, {"enabled": False, "method": "email", "phone": ""})
    settings = {
        "enabled": raw.get("enabled", False),
        "method": raw.get("method", "email"),
        "phone": raw.get("phone", ""),
        "totp_secret": "SET" if raw.get("totp_secret") else "",
    }
    return jsonify({"ok": True, "settings": settings})


@app.route("/admin/api/2fa/settings", methods=["POST"])
def admin_save_2fa_settings():
    token = request.headers.get("X-Admin-Token", "")
    username = ADMIN_TOKENS.get(token)
    if not username:
        return jsonify({"ok": False}), 403
    data = request.get_json()
    enabled = bool(data.get("enabled", False))
    method = data.get("method", "email")
    phone = data.get("phone", "").strip()

    if method not in ("email", "sms", "totp"):
        return jsonify({"ok": False, "error": "Неверный метод"}), 400
    if enabled and method == "email" and not ADMIN_EMAILS.get(username):
        return jsonify({"ok": False, "error": "Сначала укажи email в профиле."}), 400
    if enabled and method == "sms" and not phone:
        return jsonify({"ok": False, "error": "Введи номер телефона."}), 400
    if enabled and method == "totp" and not ADMIN_2FA.get(username, {}).get("totp_secret"):
        return jsonify({"ok": False, "error": "Сначала настрой Google Authenticator."}), 400

    existing = ADMIN_2FA.get(username, {})
    ADMIN_2FA[username] = {
        "enabled": enabled,
        "method": method,
        "phone": phone,
        "totp_secret": existing.get("totp_secret", ""),
    }
    save_admin_2fa()
    logger.info(f"2FA для {username}: enabled={enabled}, method={method}")
    return jsonify({"ok": True})


@app.route("/admin/api/2fa/send-test", methods=["POST"])
def admin_2fa_send_test():
    token = request.headers.get("X-Admin-Token", "")
    username = ADMIN_TOKENS.get(token)
    if not username:
        return jsonify({"ok": False}), 403
    settings = ADMIN_2FA.get(username, {})
    method = settings.get("method", "email")
    code = str(random.randint(100000, 999999))
    sent = False
    dest = ""
    if method == "email":
        email = ADMIN_EMAILS.get(username, "")
        if email:
            sent = send_2fa_email(email, username, code)
            dest = email
    elif method == "sms":
        phone = settings.get("phone", "")
        if phone:
            sent = send_2fa_sms(phone, code)
            dest = phone
    if not sent:
        return jsonify({"ok": False, "error": "Не удалось отправить тестовый код."}), 500
    return jsonify({"ok": True, "dest": dest})


@app.route("/admin/api/2fa/totp/setup", methods=["GET"])
def admin_totp_setup():
    token = request.headers.get("X-Admin-Token", "")
    username = ADMIN_TOKENS.get(token)
    if not username:
        return jsonify({"ok": False}), 403
    secret = pyotp.random_base32()
    TOTP_PENDING[username] = secret
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=username, issuer_name="Flux Admin")
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    return jsonify({"ok": True, "secret": secret, "qr": qr_b64})


@app.route("/admin/api/2fa/totp/enable", methods=["POST"])
def admin_totp_enable():
    token = request.headers.get("X-Admin-Token", "")
    username = ADMIN_TOKENS.get(token)
    if not username:
        return jsonify({"ok": False}), 403
    data = request.get_json()
    code = data.get("code", "").strip()
    secret = TOTP_PENDING.get(username)
    if not secret:
        return jsonify({"ok": False, "error": "Сначала получи QR-код."}), 400
    totp = pyotp.TOTP(secret)
    if not totp.verify(code, valid_window=1):
        return jsonify({"ok": False, "error": "Неверный код. Попробуй ещё раз."}), 401
    TOTP_PENDING.pop(username, None)
    existing = ADMIN_2FA.get(username, {})
    ADMIN_2FA[username] = {
        "enabled": existing.get("enabled", False),
        "method": existing.get("method", "email"),
        "phone": existing.get("phone", ""),
        "totp_secret": secret,
    }
    save_admin_2fa()
    return jsonify({"ok": True})


@app.route("/admin/api/2fa/totp/disable", methods=["POST"])
def admin_totp_disable():
    token = request.headers.get("X-Admin-Token", "")
    username = ADMIN_TOKENS.get(token)
    if not username:
        return jsonify({"ok": False}), 403
    TOTP_PENDING.pop(username, None)
    existing = ADMIN_2FA.get(username, {})
    existing["totp_secret"] = ""
    if existing.get("method") == "totp":
        existing["method"] = "email"
        existing["enabled"] = False
    ADMIN_2FA[username] = existing
    save_admin_2fa()
    return jsonify({"ok": True})


@app.route("/admin/api/change-password", methods=["POST"])
def admin_change_password():
    token = request.headers.get("X-Admin-Token", "")
    username = ADMIN_TOKENS.get(token)
    if not username:
        return jsonify({"ok": False}), 403
    data = request.get_json()
    new_password = data.get("new_password", "").strip()
    if len(new_password) < 4:
        return jsonify({"ok": False, "error": "Пароль слишком короткий (минимум 4 символа)"}), 400
    # Удаляем старый токен
    ADMIN_TOKENS.pop(token, None)
    # Обновляем пароль, токен и сохраняем на диск
    ADMIN_ACCOUNTS[username] = new_password
    save_admin_accounts()
    new_token = _make_token(username, new_password)
    ADMIN_TOKENS[new_token] = username
    logger.info(f"Пользователь {username} сменил пароль (сохранено)")
    return jsonify({"ok": True, "token": new_token, "username": username})


@app.route("/admin/api/set-email", methods=["POST"])
def admin_set_email():
    token = request.headers.get("X-Admin-Token", "")
    username = ADMIN_TOKENS.get(token)
    if not username:
        return jsonify({"ok": False}), 403
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"ok": False, "error": "Некорректный email"}), 400
    ADMIN_EMAILS[username] = email
    save_admin_emails()
    logger.info(f"Пользователь {username} установил email {email}")
    return jsonify({"ok": True})


@app.route("/admin/api/add-account", methods=["POST"])
def admin_add_account():
    token = request.headers.get("X-Admin-Token", "")
    username = ADMIN_TOKENS.get(token)
    if username != "sergey_defa":
        return jsonify({"ok": False, "error": "Нет доступа"}), 403
    data = request.get_json()
    new_username = data.get("username", "").strip()
    new_password = data.get("password", "").strip()
    if not new_username:
        return jsonify({"ok": False, "error": "Введи логин"}), 400
    if len(new_username) < 3:
        return jsonify({"ok": False, "error": "Логин слишком короткий (минимум 3 символа)"}), 400
    if new_username in ADMIN_ACCOUNTS:
        return jsonify({"ok": False, "error": f'Аккаунт «{new_username}» уже существует'}), 400
    if len(new_password) < 4:
        return jsonify({"ok": False, "error": "Пароль слишком короткий (минимум 4 символа)"}), 400
    ADMIN_ACCOUNTS[new_username] = new_password
    save_admin_accounts()
    _rebuild_tokens()
    logger.info(f"sergey_defa создал новый аккаунт: {new_username}")
    return jsonify({"ok": True})


@app.route("/admin/api/has-email")
def admin_has_email():
    token = request.headers.get("X-Admin-Token", "")
    username = ADMIN_TOKENS.get(token)
    if not username:
        return jsonify({"ok": False}), 403
    email = ADMIN_EMAILS.get(username, "")
    return jsonify({"ok": True, "has_email": bool(email), "email": email})


@app.route("/admin/forgot", methods=["POST"])
def admin_forgot():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    username = next((u for u, e in ADMIN_EMAILS.items() if e == email), None)
    if not username:
        return jsonify({"ok": True, "sent": False, "no_email": True})
    reset_token = secrets.token_urlsafe(32)
    RESET_TOKENS[reset_token] = {"username": username, "expires": time.time() + 1800}
    base_url = f"https://{REPLIT_URL}" if REPLIT_URL else request.host_url.rstrip("/")
    reset_url = f"{base_url}/admin/reset?token={reset_token}"
    sent = send_reset_email(email, username, reset_url)
    return jsonify({"ok": True, "sent": sent})


@app.route("/admin/reset")
def admin_reset_page():
    token = request.args.get("token", "")
    info = RESET_TOKENS.get(token)
    valid = bool(info and time.time() < info["expires"])
    uname = info["username"] if valid else ""
    return render_template("reset.html", token=token, valid=valid, username=uname)


@app.route("/admin/reset", methods=["POST"])
def admin_reset_submit():
    data = request.get_json()
    token = data.get("token", "")
    new_password = data.get("new_password", "").strip()
    info = RESET_TOKENS.get(token)
    if not info or time.time() > info["expires"]:
        return jsonify({"ok": False, "error": "Ссылка недействительна или истекла"}), 400
    if len(new_password) < 4:
        return jsonify({"ok": False, "error": "Пароль слишком короткий (минимум 4 символа)"}), 400
    username = info["username"]
    for t in list(ADMIN_TOKENS.keys()):
        if ADMIN_TOKENS[t] == username:
            ADMIN_TOKENS.pop(t)
    ADMIN_ACCOUNTS[username] = new_password
    save_admin_accounts()
    new_token = _make_token(username, new_password)
    ADMIN_TOKENS[new_token] = username
    RESET_TOKENS.pop(token, None)
    logger.info(f"Пользователь {username} восстановил пароль через email (сохранено)")
    return jsonify({"ok": True, "token": new_token, "username": username})


# ============ ADMIN API ============

@app.route("/admin/api/status")
def api_status():
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "bot_active": bot_active})


@app.route("/admin/api/stats")
def api_stats():
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    return jsonify({
        "ok": True,
        "users": len(known_chats),
        "messages": message_count,
        "chats": len(chat_histories),
        "banned": len(banned_users)
    })


@app.route("/admin/api/chats")
def api_chats():
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    chats = []
    for cid in known_chats:
        info = user_info.get(cid, {})
        log = full_chat_log.get(cid, [])
        last = log[-1]["content"] if log else ""
        chats.append({
            "chat_id": cid,
            "name": info.get("name", str(cid)),
            "username": info.get("username", ""),
            "last_message": last,
            "msg_count": len(log),
            "banned": cid in banned_users
        })
    chats.sort(key=lambda x: x["msg_count"], reverse=True)
    return jsonify({"ok": True, "chats": chats})


@app.route("/admin/api/chat/<int:chat_id>")
def api_chat(chat_id):
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    info = user_info.get(chat_id, {})
    log = full_chat_log.get(chat_id, [])
    return jsonify({
        "ok": True,
        "chat_id": chat_id,
        "name": info.get("name", str(chat_id)),
        "username": info.get("username", ""),
        "messages": log
    })


@app.route("/admin/api/chat/<int:chat_id>/message/<int:msg_idx>", methods=["DELETE"])
def api_delete_message(chat_id, msg_idx):
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    log = full_chat_log.get(chat_id, [])
    if 0 <= msg_idx < len(log):
        log.pop(msg_idx)
        save_chat_log()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "index out of range"}), 400


@app.route("/admin/api/chat/<int:chat_id>/clear", methods=["DELETE"])
def api_clear_chat(chat_id):
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    full_chat_log[chat_id] = []
    save_chat_log()
    return jsonify({"ok": True})


@app.route("/admin/api/user/<int:chat_id>")
def api_user_profile(chat_id):
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    info = user_info.get(chat_id, {})
    log = full_chat_log.get(chat_id, [])
    return jsonify({
        "ok": True,
        "chat_id": chat_id,
        "name": info.get("name", str(chat_id)),
        "username": info.get("username", ""),
        "msg_count": len(log),
        "banned": chat_id in banned_users
    })


@app.route("/admin/api/chat/<int:chat_id>/ban", methods=["POST"])
def api_ban(chat_id):
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    banned_users.add(chat_id)
    save_users()
    push_sse("moderation", {"chat_id": chat_id, "banned": True})
    logger.info(f"Пользователь {chat_id} забанен")
    return jsonify({"ok": True})


@app.route("/admin/api/chat/<int:chat_id>/unban", methods=["POST"])
def api_unban(chat_id):
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    banned_users.discard(chat_id)
    save_users()
    push_sse("moderation", {"chat_id": chat_id, "banned": False})
    logger.info(f"Пользователь {chat_id} разбанен")
    return jsonify({"ok": True})


@app.route("/admin/api/send", methods=["POST"])
def api_send():
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    data = request.get_json()
    chat_id = data.get("chat_id")
    text = data.get("text", "").strip()
    if not chat_id or not text:
        return jsonify({"ok": False, "error": "chat_id and text required"}), 400
    send_message(int(chat_id), text)
    log_message(int(chat_id), "admin", text)
    return jsonify({"ok": True})


@app.route("/admin/api/broadcast", methods=["POST"])
def api_broadcast():
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    data = request.get_json()
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"ok": False}), 400
    sent = 0
    for cid in list(known_chats):
        try:
            send_message(cid, f"📢 {text}")
            log_message(cid, "admin", f"📢 {text}")
            sent += 1
        except Exception:
            pass
    return jsonify({"ok": True, "sent": sent})


@app.route("/admin/api/bot/stop", methods=["POST"])
def api_bot_stop():
    global bot_active
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    bot_active = False
    push_sse("status", {"bot_active": False})
    logger.info("Бот остановлен через admin panel")
    return jsonify({"ok": True})


@app.route("/admin/api/bot/start", methods=["POST"])
def api_bot_start():
    global bot_active
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    bot_active = True
    push_sse("status", {"bot_active": True})
    logger.info("Бот запущен через admin panel")
    return jsonify({"ok": True})


@app.route("/admin/api/stream")
def api_stream():
    token = request.args.get("token", "")
    if token not in ADMIN_TOKENS:
        return jsonify({"ok": False}), 403

    q: queue.Queue = queue.Queue(maxsize=200)
    with sse_lock:
        sse_clients.append(q)

    @stream_with_context
    def generate():
        try:
            yield f"data: {json.dumps({'type': 'connected'})}\n\n"
            while True:
                try:
                    payload = q.get(timeout=25)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with sse_lock:
                if q in sse_clients:
                    sse_clients.remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )


# ============ WEBHOOK SETUP ============

def set_webhook():
    webhook_url = f"https://{REPLIT_URL}/webhook"
    r = http_requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
        json={"url": webhook_url, "drop_pending_updates": True},
        timeout=10
    )
    logger.info(f"Webhook: {r.json()}")


@app.route("/admin/api/invite/list")
def api_invite_list():
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "emails": INVITE_EMAILS})


@app.route("/admin/api/invite/add", methods=["POST"])
def api_invite_add():
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"ok": False, "error": "Некорректный email"}), 400
    if any(e["email"] == email for e in INVITE_EMAILS):
        return jsonify({"ok": False, "error": "Этот email уже в списке"}), 400
    import datetime
    INVITE_EMAILS.append({
        "email": email,
        "added": datetime.datetime.now().strftime("%d.%m.%Y"),
        "sent": False,
        "sent_at": None
    })
    save_invite_emails()
    return jsonify({"ok": True})


@app.route("/admin/api/invite/remove", methods=["POST"])
def api_invite_remove():
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    global INVITE_EMAILS
    INVITE_EMAILS = [e for e in INVITE_EMAILS if e["email"] != email]
    save_invite_emails()
    return jsonify({"ok": True})


@app.route("/admin/api/invite/send-one", methods=["POST"])
def api_invite_send_one():
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    entry = next((e for e in INVITE_EMAILS if e["email"] == email), None)
    if not entry:
        return jsonify({"ok": False, "error": "Email не найден в списке"}), 404
    ok = send_invite_email(email)
    if ok:
        import datetime
        entry["sent"] = True
        entry["sent_at"] = datetime.datetime.now().strftime("%d.%m %H:%M")
        save_invite_emails()
    return jsonify({"ok": ok, "error": None if ok else "Ошибка отправки — проверь SMTP"})


@app.route("/admin/api/invite/send-all", methods=["POST"])
def api_invite_send_all():
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    import datetime
    sent, failed = 0, 0
    for entry in INVITE_EMAILS:
        if not entry.get("sent"):
            ok = send_invite_email(entry["email"])
            if ok:
                entry["sent"] = True
                entry["sent_at"] = datetime.datetime.now().strftime("%d.%m %H:%M")
                sent += 1
            else:
                failed += 1
    save_invite_emails()
    return jsonify({"ok": True, "sent": sent, "failed": failed})


@app.route("/admin/api/invite/reset", methods=["POST"])
def api_invite_reset():
    """Сбросить статус 'отправлено' — чтобы можно было отправить повторно."""
    if not check_admin_token():
        return jsonify({"ok": False}), 403
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    entry = next((e for e in INVITE_EMAILS if e["email"] == email), None)
    if entry:
        entry["sent"] = False
        entry["sent_at"] = None
        save_invite_emails()
    return jsonify({"ok": True})


def startup():
    if not BOT_TOKEN:
        logger.error("❌ Не указан BOT_TOKEN — добавь его в Secrets")
        return
    if not OPENROUTER_API_KEY:
        logger.error("❌ Не указан OPENROUTER_API_KEY — добавь его в Secrets")
        return

    load_admin_accounts()
    load_users()
    load_chat_log()
    load_admin_emails()
    load_invite_emails()
    load_admin_2fa()

    if REPLIT_URL:
        try:
            set_webhook()
        except Exception as e:
            logger.error(f"Ошибка установки webhook: {e}")
    else:
        logger.warning("⚠️ REPLIT_DEV_DOMAIN не найден — установи webhook вручную")

    if REPLIT_URL:
        ka_thread = threading.Thread(target=keep_alive_loop, daemon=True)
        ka_thread.start()
        logger.info("Keep-alive запущен")

    logger.info(f"⚡ Flux AI Bot | Модель: {AI_MODEL}")
    logger.info(f"🌐 Админ-панель: https://{REPLIT_URL}/admin")


# ============ ВЕБ-ЧАТ ============
WEB_USERS_FILE = "web_users.json"
WEB_CHAT_HISTORIES: dict[str, list] = {}  # in-memory per session
WEB_PENDING_REG: dict[str, dict] = {}   # token → {username, email, password_hash, code, expires}
WEB_EMAIL_VERIFY: dict[str, dict] = {}  # token → {uid, email, username, code, expires, resend_at}
SMSRU_API_KEY = os.environ.get("SMSRU_API_KEY", "")

def load_web_users() -> dict:
    try:
        with open(WEB_USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_web_users(data: dict):
    with open(WEB_USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def web_hash_pwd(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()

def get_web_user_id():
    return session.get("web_user_id")

@app.route("/app")
def web_chat_page():
    return render_template("web_chat.html")

def send_web_reg_code(email: str, code: str, username: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Ваш код подтверждения — {code}"
        msg["From"] = SMTP_USER
        msg["To"] = email
        html = f"""
<div style="font-family:-apple-system,sans-serif;max-width:420px;margin:0 auto;background:#0f1525;border-radius:16px;padding:32px;color:#e8eaf6">
  <div style="font-size:40px;text-align:center;margin-bottom:12px">⚡</div>
  <h2 style="text-align:center;margin:0 0 8px;color:#5aabff">Flux AI</h2>
  <p style="text-align:center;color:#8892b0;margin:0 0 28px">Подтверждение регистрации</p>
  <p style="margin:0 0 16px">Привет, <b>{username}</b>!</p>
  <p style="margin:0 0 20px;color:#8892b0">Ваш код для подтверждения email:</p>
  <div style="text-align:center;background:#1a2540;border-radius:12px;padding:20px;letter-spacing:10px;font-size:32px;font-weight:700;color:#5aabff;border:1px solid rgba(90,171,255,0.2)">{code}</div>
  <p style="margin:20px 0 0;color:#8892b0;font-size:13px;text-align:center">Код действителен 10 минут. Если вы не регистрировались — проигнорируйте это письмо.</p>
</div>"""
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.mail.ru", 465) as srv:
            srv.login(SMTP_USER, SMTP_PASSWORD)
            srv.sendmail(SMTP_USER, email, msg.as_string())
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки email регистрации: {e}")
        return False

@app.route("/app/register-noemail", methods=["POST"])
def web_register_noemail():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"ok": False, "error": "Заполните все поля"}), 400
    if len(username) < 2:
        return jsonify({"ok": False, "error": "Имя минимум 2 символа"}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Пароль минимум 6 символов"}), 400
    if len(username) > 32:
        return jsonify({"ok": False, "error": "Имя не более 32 символов"}), 400
    users = load_web_users()
    for u in users.values():
        if u["username"].lower() == username.lower():
            return jsonify({"ok": False, "error": "Имя пользователя занято"}), 400
    uid = secrets.token_hex(16)
    from datetime import timedelta
    users[uid] = {
        "username": username,
        "email": None,
        "password_hash": web_hash_pwd(password),
        "email_verified": False,
        "email_required_after": (datetime.now() + timedelta(days=7)).isoformat(),
        "created_at": datetime.now().isoformat(),
        "chat_history": [],
        "credits": 55,
        "plan": "free",
        "plan_expires": None
    }
    save_web_users(users)
    session["web_user_id"] = uid
    session["web_username"] = username
    return jsonify({"ok": True, "username": username})

@app.route("/app/register", methods=["POST"])
def web_register():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not username or not email or not password:
        return jsonify({"ok": False, "error": "Заполните все поля"}), 400
    if len(username) < 2:
        return jsonify({"ok": False, "error": "Имя минимум 2 символа"}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Пароль минимум 6 символов"}), 400
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"ok": False, "error": "Некорректный email"}), 400
    users = load_web_users()
    for u in users.values():
        if u["email"] == email:
            return jsonify({"ok": False, "error": "Email уже зарегистрирован"}), 400
        if u["username"].lower() == username.lower():
            return jsonify({"ok": False, "error": "Имя пользователя занято"}), 400
    code = str(random.randint(100000, 999999))
    token = secrets.token_hex(20)
    WEB_PENDING_REG[token] = {
        "username": username,
        "email": email,
        "password_hash": web_hash_pwd(password),
        "code": code,
        "expires": time.time() + 600,
        "resend_at": time.time() + 60
    }
    sent = send_web_reg_code(email, code, username)
    if not sent:
        return jsonify({"ok": False, "error": "Не удалось отправить письмо. Проверьте email."}), 500
    return jsonify({"ok": True, "step": "verify", "token": token, "email": email})

@app.route("/app/verify", methods=["POST"])
def web_verify():
    data = request.get_json(silent=True) or {}
    token = data.get("token") or ""
    code = (data.get("code") or "").strip()
    pending = WEB_PENDING_REG.get(token)
    if not pending:
        return jsonify({"ok": False, "error": "Сессия устарела. Начните регистрацию заново."}), 400
    if time.time() > pending["expires"]:
        WEB_PENDING_REG.pop(token, None)
        return jsonify({"ok": False, "error": "Код истёк. Начните регистрацию заново."}), 400
    if code != pending["code"]:
        return jsonify({"ok": False, "error": "Неверный код"}), 400
    users = load_web_users()
    for u in users.values():
        if u["email"] == pending["email"]:
            WEB_PENDING_REG.pop(token, None)
            return jsonify({"ok": False, "error": "Email уже зарегистрирован"}), 400
    uid = secrets.token_hex(16)
    users[uid] = {
        "username": pending["username"],
        "email": pending["email"],
        "password_hash": pending["password_hash"],
        "email_verified": True,
        "created_at": datetime.now().isoformat(),
        "chat_history": []
    }
    save_web_users(users)
    WEB_PENDING_REG.pop(token, None)
    session["web_user_id"] = uid
    session["web_username"] = pending["username"]
    return jsonify({"ok": True, "username": pending["username"]})

@app.route("/app/resend-code", methods=["POST"])
def web_resend_code():
    data = request.get_json(silent=True) or {}
    token = data.get("token") or ""
    pending = WEB_PENDING_REG.get(token)
    if not pending:
        return jsonify({"ok": False, "error": "Сессия не найдена"}), 400
    if time.time() < pending.get("resend_at", 0):
        left = int(pending["resend_at"] - time.time())
        return jsonify({"ok": False, "error": f"Подождите {left} сек."}), 429
    code = str(random.randint(100000, 999999))
    pending["code"] = code
    pending["expires"] = time.time() + 600
    pending["resend_at"] = time.time() + 60
    send_web_reg_code(pending["email"], code, pending["username"])
    return jsonify({"ok": True})

@app.route("/app/login", methods=["POST"])
def web_login():
    data = request.get_json(silent=True) or {}
    login_val = (data.get("login") or "").strip().lower()
    password = data.get("password") or ""
    users = load_web_users()
    found_uid = None
    found_user = None
    for uid, u in users.items():
        umail = u.get("email") or ""
        if umail == login_val or u["username"].lower() == login_val:
            found_uid = uid
            found_user = u
            break
    if not found_user or found_user["password_hash"] != web_hash_pwd(password):
        return jsonify({"ok": False, "error": "Неверный логин или пароль"}), 401
    # Пользователь без email
    if not found_user.get("email"):
        # Прошла ли неделя?
        req_after = found_user.get("email_required_after")
        if req_after:
            try:
                if datetime.fromisoformat(req_after) < datetime.now():
                    session["web_user_id"] = found_uid
                    session["web_username"] = found_user["username"]
                    return jsonify({"ok": True, "require_email": True, "username": found_user["username"]})
            except Exception:
                pass
        session["web_user_id"] = found_uid
        session["web_username"] = found_user["username"]
        return jsonify({"ok": True, "username": found_user["username"]})
    # Проверяем верификацию email
    if not found_user.get("email_verified", True):
        code = str(random.randint(100000, 999999))
        token = secrets.token_hex(20)
        WEB_EMAIL_VERIFY[token] = {
            "uid": found_uid,
            "email": found_user["email"],
            "username": found_user["username"],
            "code": code,
            "expires": time.time() + 600,
            "resend_at": time.time() + 60
        }
        send_web_reg_code(found_user["email"], code, found_user["username"])
        return jsonify({"ok": True, "step": "verify_email", "token": token, "email": found_user["email"]})
    session["web_user_id"] = found_uid
    session["web_username"] = found_user["username"]
    return jsonify({"ok": True, "username": found_user["username"]})

@app.route("/app/add-email", methods=["POST"])
def web_add_email():
    uid = get_web_user_id()
    if not uid:
        return jsonify({"ok": False, "error": "Не авторизован"}), 401
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"ok": False, "error": "Некорректный email"}), 400
    users = load_web_users()
    for oid, u in users.items():
        if oid != uid and (u.get("email") or "") == email:
            return jsonify({"ok": False, "error": "Email уже используется"}), 400
    username = users.get(uid, {}).get("username", "")
    code = str(random.randint(100000, 999999))
    token = secrets.token_hex(20)
    WEB_EMAIL_VERIFY[token] = {
        "uid": uid,
        "email": email,
        "username": username,
        "code": code,
        "expires": time.time() + 600,
        "resend_at": time.time() + 60,
        "set_email": True
    }
    sent = send_web_reg_code(email, code, username)
    if not sent:
        return jsonify({"ok": False, "error": "Не удалось отправить письмо. Проверьте email."}), 500
    return jsonify({"ok": True, "token": token})

@app.route("/app/verify-email", methods=["POST"])
def web_verify_email():
    data = request.get_json(silent=True) or {}
    token = data.get("token") or ""
    code = (data.get("code") or "").strip()
    pending = WEB_EMAIL_VERIFY.get(token)
    if not pending:
        return jsonify({"ok": False, "error": "Сессия устарела. Войдите снова."}), 400
    if time.time() > pending["expires"]:
        WEB_EMAIL_VERIFY.pop(token, None)
        return jsonify({"ok": False, "error": "Код истёк. Войдите снова."}), 400
    if code != pending["code"]:
        return jsonify({"ok": False, "error": "Неверный код"}), 400
    users = load_web_users()
    uid = pending["uid"]
    if uid in users:
        users[uid]["email_verified"] = True
        # Если это добавление email к аккаунту без него
        if pending.get("set_email"):
            users[uid]["email"] = pending["email"]
            users[uid].pop("email_required_after", None)
        save_web_users(users)
    WEB_EMAIL_VERIFY.pop(token, None)
    session["web_user_id"] = uid
    session["web_username"] = pending["username"]
    return jsonify({"ok": True, "username": pending["username"]})

@app.route("/app/resend-email-verify", methods=["POST"])
def web_resend_email_verify():
    data = request.get_json(silent=True) or {}
    token = data.get("token") or ""
    pending = WEB_EMAIL_VERIFY.get(token)
    if not pending:
        return jsonify({"ok": False, "error": "Сессия не найдена"}), 400
    if time.time() < pending.get("resend_at", 0):
        left = int(pending["resend_at"] - time.time())
        return jsonify({"ok": False, "error": f"Подождите {left} сек."}), 429
    code = str(random.randint(100000, 999999))
    pending["code"] = code
    pending["expires"] = time.time() + 600
    pending["resend_at"] = time.time() + 60
    send_web_reg_code(pending["email"], code, pending["username"])
    return jsonify({"ok": True})

@app.route("/app/logout", methods=["POST"])
def web_logout():
    session.pop("web_user_id", None)
    session.pop("web_username", None)
    return jsonify({"ok": True})

@app.route("/app/me")
def web_me():
    uid = get_web_user_id()
    if not uid:
        return jsonify({"ok": False}), 401
    users = load_web_users()
    u = users.get(uid)
    if not u:
        return jsonify({"ok": False}), 401
    plan = u.get("plan", "free")
    plan_expires = u.get("plan_expires")
    sub_expired = False
    if plan in ("core", "pro") and plan_expires:
        try:
            from datetime import timedelta
            exp_dt = datetime.fromisoformat(plan_expires)
            if datetime.now() > exp_dt:
                sub_expired = True
        except Exception:
            pass
    return jsonify({
        "ok": True,
        "username": u["username"],
        "email": u.get("email", ""),
        "plan": plan,
        "plan_expires": plan_expires,
        "subscription_expired": sub_expired
    })

def _ensure_chats(u: dict) -> dict:
    """Гарантирует наличие структуры мульти-чатов, мигрирует старый chat_history."""
    if "chats" not in u or not u["chats"]:
        old = u.pop("chat_history", [])
        cid = str(uuid_module.uuid4())
        u["chats"] = {
            cid: {
                "name": "Чат 1",
                "history": old,
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
        }
        u["active_chat_id"] = cid
    else:
        # Убедимся что active_chat_id валиден
        if u.get("active_chat_id") not in u["chats"]:
            u["active_chat_id"] = next(iter(u["chats"]))
    return u["chats"]

def _active_chat(u: dict) -> tuple:
    """Возвращает (chat_id, chat_dict) активного чата."""
    _ensure_chats(u)
    cid = u["active_chat_id"]
    return cid, u["chats"][cid]

@app.route("/app/chats")
def web_get_chats():
    uid = get_web_user_id()
    if not uid:
        return jsonify({"ok": False}), 401
    users = load_web_users()
    u = users.get(uid, {})
    _ensure_chats(u)
    lst = []
    for cid, c in u["chats"].items():
        h = c.get("history", [])
        last = h[-1]["content"][:60] if h else ""
        lst.append({
            "id": cid,
            "name": c.get("name", "Чат"),
            "last": last,
            "updated_at": c.get("updated_at", ""),
            "msg_count": len(h),
        })
    lst.sort(key=lambda x: x["updated_at"], reverse=True)
    return jsonify({"ok": True, "chats": lst, "active_id": u["active_chat_id"]})

@app.route("/app/chats/new", methods=["POST"])
def web_new_chat():
    uid = get_web_user_id()
    if not uid:
        return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip() or f"Чат {datetime.now().strftime('%d.%m %H:%M')}"
    users = load_web_users()
    u = users.get(uid)
    if not u:
        return jsonify({"ok": False}), 401
    _ensure_chats(u)
    cid = str(uuid_module.uuid4())
    u["chats"][cid] = {
        "name": name,
        "history": [],
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    u["active_chat_id"] = cid
    save_web_users(users)
    return jsonify({"ok": True, "chat_id": cid, "name": name})

@app.route("/app/chats/switch", methods=["POST"])
def web_switch_chat():
    uid = get_web_user_id()
    if not uid:
        return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    cid = data.get("chat_id", "")
    users = load_web_users()
    u = users.get(uid)
    if not u:
        return jsonify({"ok": False}), 401
    _ensure_chats(u)
    if cid not in u["chats"]:
        return jsonify({"ok": False, "error": "Чат не найден"}), 404
    u["active_chat_id"] = cid
    save_web_users(users)
    chat = u["chats"][cid]
    return jsonify({"ok": True, "history": chat.get("history", []), "name": chat.get("name", "Чат")})

@app.route("/app/chats/delete", methods=["POST"])
def web_delete_chat():
    uid = get_web_user_id()
    if not uid:
        return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    cid = data.get("chat_id", "")
    users = load_web_users()
    u = users.get(uid)
    if not u:
        return jsonify({"ok": False}), 401
    _ensure_chats(u)
    if cid not in u["chats"]:
        return jsonify({"ok": False, "error": "Чат не найден"}), 404
    if len(u["chats"]) <= 1:
        return jsonify({"ok": False, "error": "Нельзя удалить последний чат"}), 400
    del u["chats"][cid]
    if u["active_chat_id"] == cid:
        u["active_chat_id"] = next(iter(u["chats"]))
    save_web_users(users)
    return jsonify({"ok": True, "active_id": u["active_chat_id"]})

@app.route("/app/chats/rename", methods=["POST"])
def web_rename_chat():
    uid = get_web_user_id()
    if not uid:
        return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    cid = data.get("chat_id", "")
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Имя не может быть пустым"}), 400
    users = load_web_users()
    u = users.get(uid)
    if not u:
        return jsonify({"ok": False}), 401
    _ensure_chats(u)
    if cid not in u["chats"]:
        return jsonify({"ok": False, "error": "Чат не найден"}), 404
    u["chats"][cid]["name"] = name
    save_web_users(users)
    return jsonify({"ok": True})

@app.route("/app/history")
def web_history():
    uid = get_web_user_id()
    if not uid:
        return jsonify({"ok": False}), 401
    users = load_web_users()
    u = users.get(uid, {})
    _ensure_chats(u)
    _, chat = _active_chat(u)
    return jsonify({"ok": True, "history": chat.get("history", []), "active_id": u["active_chat_id"]})

WEB_PAYMENTS_FILE = "web_payments.json"

def load_web_payments() -> dict:
    try:
        with open(WEB_PAYMENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_web_payments(data: dict):
    with open(WEB_PAYMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

PLAN_CREDITS = {"core": 1_000_000, "pro": 10_000_000}
PLAN_PRICE = {"core": 550, "pro": 1100}
CREDIT_PACKS = [20, 30, 40, 55]
CREDIT_PRICE = 15  # ₽ per credit
SBP_PHONE = "+79819694398"
SBP_BANK = "Т-Банк"

@app.route("/app/chat", methods=["POST"])
def web_chat_send():
    uid = get_web_user_id()
    if not uid:
        return jsonify({"ok": False, "error": "Не авторизован"}), 401
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Пустое сообщение"}), 400
    users = load_web_users()
    u = users.get(uid)
    if not u:
        return jsonify({"ok": False, "error": "Пользователь не найден"}), 401
    # Credits check
    credits = u.get("credits", 0)
    if credits == 0:
        return jsonify({"ok": False, "no_credits": True, "error": "Кредиты закончились"}), 402
    # Multi-chat: get active chat history
    _ensure_chats(u)
    cid, chat = _active_chat(u)
    history = chat.get("history", [])
    messages = [{"role": "system", "content": f"Ты — Flux, дружелюбный AI-ассистент. Отвечай на русском языке. Текущая дата: {datetime.now().strftime('%d.%m.%Y')}."}]
    for msg in history[-20:]:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": text})
    try:
        resp = http_requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={"model": AI_MODEL, "messages": messages, "max_tokens": 1500},
            timeout=60
        )
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Ошибка AI: {str(e)}"}), 500
    # Deduct credit (skip if unlimited: credits == -1)
    if credits > 0:
        u["credits"] = credits - 1
    now = datetime.now().isoformat()
    history.append({"role": "user", "content": text, "ts": now})
    history.append({"role": "assistant", "content": reply, "ts": datetime.now().isoformat()})
    if len(history) > 200:
        history = history[-200:]
    chat["history"] = history
    chat["updated_at"] = datetime.now().isoformat()
    # Auto-name chat via AI on first user message
    if len(history) == 2 and chat.get("name", "").startswith("Чат "):
        try:
            name_resp = http_requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": AI_MODEL,
                    "messages": [
                        {"role": "system", "content": "Придумай короткое (2-4 слова) название темы для чата на основе сообщения пользователя. Только название, без кавычек и пунктуации в конце."},
                        {"role": "user", "content": text[:200]}
                    ],
                    "max_tokens": 20
                },
                timeout=10
            )
            generated = name_resp.json()["choices"][0]["message"]["content"].strip().strip('"').strip("'")
            if generated and len(generated) < 60:
                chat["name"] = generated
        except Exception:
            chat["name"] = text[:32] + ("…" if len(text) > 32 else "")
    save_web_users(users)
    remaining = u["credits"]
    return jsonify({"ok": True, "reply": reply, "credits": remaining, "chat_name": chat["name"]})

@app.route("/app/credits")
def web_get_credits():
    uid = get_web_user_id()
    if not uid:
        return jsonify({"ok": False}), 401
    users = load_web_users()
    u = users.get(uid, {})
    _ensure_chats(u)
    _, chat = _active_chat(u)
    return jsonify({
        "ok": True,
        "credits": u.get("credits", 0),
        "plan": u.get("plan", "free"),
        "plan_expires": u.get("plan_expires"),
        "msg_count": len(chat.get("history", []))
    })

@app.route("/app/request-payment", methods=["POST"])
def web_request_payment():
    uid = get_web_user_id()
    if not uid:
        return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    ptype = data.get("type")  # "credits" | "core" | "pro"
    amount_credits = int(data.get("amount_credits", 0))
    users = load_web_users()
    u = users.get(uid)
    if not u:
        return jsonify({"ok": False}), 401
    if ptype == "core":
        amount_rub = PLAN_PRICE["core"]
        credits_to_add = PLAN_CREDITS["core"]
        label = "Core (1 год)"
    elif ptype == "pro":
        amount_rub = PLAN_PRICE["pro"]
        credits_to_add = PLAN_CREDITS["pro"]
        label = "Pro (1 год)"
    elif ptype == "credits":
        if amount_credits < 1:
            return jsonify({"ok": False, "error": "Некорректное количество"}), 400
        amount_rub = amount_credits * CREDIT_PRICE
        credits_to_add = amount_credits
        label = f"{amount_credits} кредитов"
    else:
        return jsonify({"ok": False, "error": "Неверный тип"}), 400
    pid = secrets.token_hex(10)
    payments = load_web_payments()
    payments[pid] = {
        "uid": uid,
        "username": u["username"],
        "email": u.get("email", ""),
        "type": ptype,
        "label": label,
        "amount_rub": amount_rub,
        "credits_to_add": credits_to_add,
        "status": "pending",
        "created_at": datetime.now().isoformat()
    }
    save_web_payments(payments)
    return jsonify({
        "ok": True,
        "pid": pid,
        "amount_rub": amount_rub,
        "label": label,
        "sbp_phone": SBP_PHONE,
        "sbp_bank": SBP_BANK,
        "comment": f"Flux {u['username']}"
    })

@app.route("/app/request-renewal", methods=["POST"])
def web_request_renewal():
    uid = get_web_user_id()
    if not uid:
        return jsonify({"ok": False}), 401
    users = load_web_users()
    u = users.get(uid)
    if not u:
        return jsonify({"ok": False}), 401
    plan = u.get("plan", "free")
    if plan not in ("core", "pro"):
        return jsonify({"ok": False, "error": "Нет активной подписки для продления"}), 400
    amount_rub = PLAN_PRICE[plan]
    credits_to_add = PLAN_CREDITS[plan]
    label = f"{plan.capitalize()} (продление на 1 год)"
    pid = secrets.token_hex(10)
    payments = load_web_payments()
    payments[pid] = {
        "uid": uid,
        "username": u["username"],
        "email": u.get("email", ""),
        "type": "renewal",
        "plan": plan,
        "label": label,
        "amount_rub": amount_rub,
        "credits_to_add": credits_to_add,
        "status": "pending",
        "created_at": datetime.now().isoformat()
    }
    save_web_payments(payments)
    return jsonify({
        "ok": True,
        "pid": pid,
        "amount_rub": amount_rub,
        "label": label,
        "plan": plan,
        "sbp_phone": SBP_PHONE,
        "sbp_bank": SBP_BANK,
        "comment": f"Flux {u['username']}"
    })

@app.route("/admin/api/web-users")
def admin_api_web_users():
    if not check_admin_token():
        return jsonify({"ok": False}), 401
    users = load_web_users()
    lst = []
    for uid, u in users.items():
        lst.append({
            "uid": uid,
            "username": u.get("username", ""),
            "email": u.get("email") or "",
            "credits": u.get("credits", 0),
            "plan": u.get("plan", "free"),
            "email_verified": u.get("email_verified", False),
            "created_at": u.get("created_at", ""),
        })
    lst.sort(key=lambda x: x["username"].lower())
    return jsonify({"ok": True, "users": lst})

@app.route("/admin/api/adjust-credits", methods=["POST"])
def admin_api_adjust_credits():
    if not check_admin_token():
        return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    uid = data.get("uid", "").strip()
    amount = data.get("amount", 0)
    if not uid:
        return jsonify({"ok": False, "error": "uid обязателен"}), 400
    try:
        amount = int(amount)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Неверная сумма"}), 400
    users = load_web_users()
    if uid not in users:
        return jsonify({"ok": False, "error": "Пользователь не найден"}), 404
    u = users[uid]
    cur = u.get("credits", 0)
    if cur == -1:
        if amount > 0:
            # Добавление к безлимиту не имеет смысла
            return jsonify({"ok": False, "error": "У пользователя безлимитные кредиты"}), 400
        # Любое списание с безлимита → снимает безлимитный статус → 0
        new_val = 0
    else:
        new_val = max(0, cur + amount)
    u["credits"] = new_val
    save_web_users(users)
    return jsonify({"ok": True, "credits": new_val, "username": u.get("username", ""), "was_unlimited": cur == -1})

def _get_payments_list():
    payments = load_web_payments()
    lst = []
    for pid, p in payments.items():
        item = dict(p)
        item["id"] = pid
        lst.append(item)
    lst.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return lst

@app.route("/admin/web-payments")
def admin_web_payments():
    if not check_admin_token():
        return jsonify({"ok": False}), 401
    lst = _get_payments_list()
    return jsonify({"ok": True, "payments": lst, "total": len(lst)})

@app.route("/admin/api/web-payments")
def admin_api_web_payments():
    if not check_admin_token():
        return jsonify({"ok": False}), 401
    lst = _get_payments_list()
    return jsonify({"ok": True, "payments": lst, "total": len(lst)})

def _do_approve_payment(pid):
    payments = load_web_payments()
    p = payments.get(pid)
    if not p:
        return jsonify({"ok": False, "error": "Не найдено"}), 404
    users = load_web_users()
    u = users.get(p["uid"])
    if u:
        from datetime import timedelta
        cur = u.get("credits", 0)
        if cur != -1:
            u["credits"] = cur + p.get("credits_to_add", 0)
        ptype = p.get("type")
        if ptype in ("core", "pro"):
            u["plan"] = ptype
            u["plan_expires"] = (datetime.now() + timedelta(days=365)).isoformat()
        elif ptype == "renewal":
            # Extend existing plan by 1 year from now (or from current expiry if still valid)
            plan = p.get("plan", u.get("plan", "free"))
            u["plan"] = plan
            old_expires = u.get("plan_expires")
            try:
                base = datetime.fromisoformat(old_expires) if old_expires else datetime.now()
                if base < datetime.now():
                    base = datetime.now()
            except Exception:
                base = datetime.now()
            u["plan_expires"] = (base + timedelta(days=365)).isoformat()
        save_web_users(users)
    p["status"] = "approved"
    p["approved_at"] = datetime.now().isoformat()
    p["approved_by"] = request.headers.get("X-Admin-Token", "?")[:8]
    save_web_payments(payments)
    return jsonify({"ok": True})

def _do_reject_payment(pid):
    payments = load_web_payments()
    p = payments.get(pid)
    if not p:
        return jsonify({"ok": False, "error": "Не найдено"}), 404
    p["status"] = "rejected"
    p["rejected_at"] = datetime.now().isoformat()
    save_web_payments(payments)
    return jsonify({"ok": True})

@app.route("/admin/web-payments/<pid>/approve", methods=["POST"])
def admin_approve_payment(pid):
    if not check_admin_token():
        return jsonify({"ok": False}), 401
    return _do_approve_payment(pid)

@app.route("/admin/api/web-payments/<pid>/approve", methods=["POST"])
def admin_api_approve_payment(pid):
    if not check_admin_token():
        return jsonify({"ok": False}), 401
    return _do_approve_payment(pid)

@app.route("/admin/web-payments/<pid>/reject", methods=["POST"])
def admin_reject_payment(pid):
    if not check_admin_token():
        return jsonify({"ok": False}), 401
    return _do_reject_payment(pid)

@app.route("/admin/api/web-payments/<pid>/reject", methods=["POST"])
def admin_api_reject_payment(pid):
    if not check_admin_token():
        return jsonify({"ok": False}), 401
    return _do_reject_payment(pid)

@app.route("/admin/api/change-plan", methods=["POST"])
def admin_api_change_plan():
    if not check_admin_token():
        return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    uid = data.get("uid", "").strip()
    plan = data.get("plan", "").strip()
    if not uid or plan not in ("free", "core", "pro", "admin"):
        return jsonify({"ok": False, "error": "Неверные параметры"}), 400
    users = load_web_users()
    if uid not in users:
        return jsonify({"ok": False, "error": "Пользователь не найден"}), 404
    u = users[uid]
    u["plan"] = plan
    if plan == "admin":
        u["credits"] = -1
        u["plan_expires"] = None
    elif plan == "free":
        u["plan_expires"] = None
    elif plan in ("core", "pro"):
        u["plan_expires"] = (datetime.now() + timedelta(days=365)).isoformat()
    save_web_users(users)
    return jsonify({"ok": True, "plan": plan, "credits": u.get("credits", 0)})

@app.route("/admin/api/web-chat/<uid>")
def admin_api_web_chat_get(uid):
    if not check_admin_token():
        return jsonify({"ok": False}), 401
    users = load_web_users()
    u = users.get(uid)
    if not u:
        return jsonify({"ok": False, "error": "Пользователь не найден"}), 404
    _ensure_chats(u)
    _, chat = _active_chat(u)
    return jsonify({
        "ok": True,
        "history": chat.get("history", []),
        "username": u.get("username", ""),
        "credits": u.get("credits", 0),
        "plan": u.get("plan", "free"),
    })

@app.route("/admin/api/web-chat/<uid>/send", methods=["POST"])
def admin_api_web_chat_send(uid):
    if not check_admin_token():
        return jsonify({"ok": False}), 401
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Пустое сообщение"}), 400
    users = load_web_users()
    u = users.get(uid)
    if not u:
        return jsonify({"ok": False, "error": "Пользователь не найден"}), 404
    _ensure_chats(u)
    _, chat = _active_chat(u)
    history = chat.get("history", [])
    history.append({
        "role": "assistant",
        "content": text,
        "ts": datetime.now().isoformat(),
        "from_admin": True
    })
    chat["history"] = history
    chat["updated_at"] = datetime.now().isoformat()
    save_web_users(users)
    return jsonify({"ok": True})

@app.route("/admin/api/web-chat/<uid>/clear", methods=["POST"])
def admin_api_web_chat_clear(uid):
    if not check_admin_token():
        return jsonify({"ok": False}), 401
    users = load_web_users()
    if uid not in users:
        return jsonify({"ok": False, "error": "Пользователь не найден"}), 404
    u = users[uid]
    _ensure_chats(u)
    _, chat = _active_chat(u)
    chat["history"] = []
    chat["updated_at"] = datetime.now().isoformat()
    save_web_users(users)
    return jsonify({"ok": True})

@app.route("/app/clear", methods=["POST"])
def web_clear_history():
    uid = get_web_user_id()
    if not uid:
        return jsonify({"ok": False}), 401
    users = load_web_users()
    if uid in users:
        u = users[uid]
        _ensure_chats(u)
        _, chat = _active_chat(u)
        chat["history"] = []
        chat["updated_at"] = datetime.now().isoformat()
        save_web_users(users)
    return jsonify({"ok": True})


# Запускается и при gunicorn, и при python main.py
startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
