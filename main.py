"""
Flux — AI чат-бот для Telegram + веб-панель администратора.
"""

import os
import re
import io
import json
import base64
import hashlib
import logging
import queue
import secrets
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime
from flask import Flask, request, jsonify, render_template, session, Response, stream_with_context
import requests as http_requests

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
ADMIN_ACCOUNTS = {
    "sergey_defa": "Ser123asd",
    "Blackjack": "Sergey",
}

def _make_token(username: str, password: str) -> str:
    return hashlib.sha256(f"flux_admin_{username}_{password}_token".encode()).hexdigest()

# Словарь: токен → логин
ADMIN_TOKENS: dict[str, str] = {
    _make_token(u, p): u for u, p in ADMIN_ACCOUNTS.items()
}
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


def generate_image(prompt: str) -> str:
    encoded = urllib.parse.quote(prompt)
    return f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true&seed={int(time.time())}"


# ============ KEEP-ALIVE ============

def keep_alive_loop():
    time.sleep(30)
    while True:
        try:
            http_requests.get(f"https://{REPLIT_URL}/", timeout=10, verify=False)
            logger.info("Keep-alive ping отправлен")
        except Exception as e:
            logger.warning(f"Keep-alive ошибка: {e}")
        time.sleep(270)


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
            "/image [запрос] — сгенерировать картинку\n\n"
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
        send_chat_action(chat_id, "upload_photo")
        img_url = generate_image(args)
        send_photo_url(chat_id, img_url, f"🎨 {args}")
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
    return "Flux AI Bot ⚡ Online"


# ============ ADMIN AUTH ============

def check_admin_token():
    token = request.headers.get("X-Admin-Token", "")
    return token in ADMIN_TOKENS


@app.route("/admin")
def admin_panel():
    return render_template("admin.html")


@app.route("/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    expected = ADMIN_ACCOUNTS.get(username)
    if expected and password == expected:
        token = _make_token(username, password)
        return jsonify({"ok": True, "token": token, "username": username})
    return jsonify({"ok": False}), 401


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


def startup():
    if not BOT_TOKEN:
        logger.error("❌ Не указан BOT_TOKEN — добавь его в Secrets")
        return
    if not OPENROUTER_API_KEY:
        logger.error("❌ Не указан OPENROUTER_API_KEY — добавь его в Secrets")
        return

    load_users()
    load_chat_log()

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


# Запускается и при gunicorn, и при python main.py
startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
