"""
Flux — AI чат-бот для Telegram.
"""

import os
import re
import io
import base64
import logging
import tempfile
import threading
import time
import urllib.parse
from flask import Flask, request
import requests as http_requests

try:
    from gtts import gTTS
    GTTS_AVAILABLE = True
except ImportError:
    GTTS_AVAILABLE = False

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
ADMIN_USERNAME = "sergey_defa"
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

chat_histories: dict[int, list[dict]] = {}
chat_modes: dict[int, str] = {}
voice_reply_enabled: dict[int, bool] = {}
known_chats: set = set()
MAX_HISTORY = 30
message_count = 0


# ============ УТИЛИТЫ ============

def get_system_prompt(chat_id: int) -> str:
    mode = chat_modes.get(chat_id, "chat")
    return SYSTEM_PROMPT_BUSINESS if mode == "business" else SYSTEM_PROMPT_CHAT


def get_ai_reply(chat_id: int, user_message: str) -> str:
    global message_count
    message_count += 1

    if chat_id not in chat_histories:
        chat_histories[chat_id] = []

    history = chat_histories[chat_id]
    history.append({"role": "user", "content": user_message})

    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
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


def reply_with_voice_or_text(chat_id: int, text: str):
    if voice_reply_enabled.get(chat_id, False) and GTTS_AVAILABLE:
        try:
            tts = gTTS(text=text, lang="ru")
            buf = io.BytesIO()
            tts.write_to_fp(buf)
            buf.seek(0)
            send_voice(chat_id, buf.read())
            return
        except Exception as e:
            logger.error(f"Ошибка TTS: {e}")
    send_message(chat_id, text)


def generate_image(prompt: str) -> str:
    encoded = urllib.parse.quote(prompt)
    return f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true&seed={int(time.time())}"


# ============ KEEP-ALIVE ============

def keep_alive_loop():
    time.sleep(30)
    while True:
        try:
            url = f"https://{REPLIT_URL}/"
            http_requests.get(url, timeout=10)
            logger.info("Keep-alive ping отправлен")
        except Exception as e:
            logger.warning(f"Keep-alive ошибка: {e}")
        time.sleep(270)


# ============ ОБРАБОТКА КОМАНД ============

def handle_command(chat_id: int, text: str, message_id: int, username: str) -> bool:
    cmd = text.strip().split()[0].lower().lstrip("/")
    args = text.strip()[len(cmd) + 1:].strip()

    if cmd == "start":
        chat_histories[chat_id] = []
        send_message(chat_id, (
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
            "/file [сек] — отправляет файл\n\n"
            f"🔑 Ты администратор. @{username}\n"
            "Напиши /adminhelp для списка команд."
            if username == ADMIN_USERNAME else
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
            "Создай файл script.py [...] — создам и пришлю файл"
        ))
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
        if not GTTS_AVAILABLE:
            send_message(chat_id, "Озвучка временно недоступна.")
            return True
        send_chat_action(chat_id, "record_voice")
        try:
            tts = gTTS(text=args, lang="ru")
            buf = io.BytesIO()
            tts.write_to_fp(buf)
            buf.seek(0)
            send_voice(chat_id, buf.read())
        except Exception as e:
            logger.error(f"TTS ошибка: {e}")
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
            "/broadcast [текст] — рассылка всем пользователям"
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


# ============ МАРШРУТЫ ============

@app.route("/")
def index():
    return "Flux AI Bot ⚡ Online"


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data:
        return "ok"

    message = data.get("message")
    if not message:
        return "ok"

    chat_id = message["chat"]["id"]
    message_id = message.get("message_id")
    user = message.get("from", {})
    username = user.get("username", "")

    known_chats.add(chat_id)

    text = message.get("text", "")
    voice = message.get("voice")
    video_note = message.get("video_note")
    video = message.get("video")
    photo = message.get("photo")
    document = message.get("document")

    # ---- Текстовые команды ----
    if text and text.startswith("/"):
        logger.info(f"Команда от @{username}: {text}")
        handle_command(chat_id, text, message_id, username)
        return "ok"

    # ---- Создать файл ----
    if text and re.match(r"(?i)создай\s+файл\s+\S+", text):
        match = re.match(r"(?i)создай\s+файл\s+(\S+)\s*(.*)", text, re.DOTALL)
        if match:
            filename = match.group(1)
            task = match.group(2).strip() or f"Напиши содержимое файла {filename}"
            send_chat_action(chat_id, "upload_document")
            ai_content = get_ai_reply(chat_id, f"Создай файл {filename}. {task}\nВыдай только код/содержимое файла без лишних пояснений.")
            send_document(chat_id, filename, ai_content.encode("utf-8"), f"📄 {filename}")
            return "ok"

    # ---- Голосовое сообщение ----
    if voice:
        send_chat_action(chat_id, "typing")
        file_data = tg_download_file(voice["file_id"])
        if file_data:
            transcript = transcribe_audio(file_data)
            logger.info(f"Голосовое от @{username}: {transcript}")
            send_message(chat_id, f"🎤 Ты сказал: {transcript}")
            stop_event = threading.Event()
            typing_thread = threading.Thread(target=send_typing, args=(chat_id, stop_event), daemon=True)
            typing_thread.start()
            try:
                reply = get_ai_reply(chat_id, transcript)
            finally:
                stop_event.set()
                typing_thread.join(timeout=5)
            reply_with_voice_or_text(chat_id, reply)
        else:
            send_message(chat_id, "Не смог скачать аудио 😔")
        return "ok"

    # ---- Видео-кружок ----
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
            send_message(chat_id, f"🎥 Ты сказал: {transcript}")
            reply = get_ai_reply(chat_id, transcript)
            reply_with_voice_or_text(chat_id, reply)
        else:
            send_message(chat_id, "Не смог обработать кружок 😔")
        return "ok"

    # ---- Видео ----
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
            send_message(chat_id, f"🎬 Речь в видео: {transcript}")
            reply = get_ai_reply(chat_id, transcript)
            reply_with_voice_or_text(chat_id, reply)
        else:
            send_message(chat_id, "Не смог обработать видео 😔")
        return "ok"

    # ---- Фото ----
    if photo:
        send_chat_action(chat_id, "typing")
        best = max(photo, key=lambda p: p.get("file_size", 0))
        file_data = tg_download_file(best["file_id"])
        if file_data:
            caption = message.get("caption", "Опиши что на этом фото подробно.")
            description = describe_image_with_ai(chat_id, file_data, caption)
            reply_with_voice_or_text(chat_id, f"🖼 {description}")
        else:
            send_message(chat_id, "Не смог скачать фото 😔")
        return "ok"

    # ---- Документ/файл ----
    if document:
        send_chat_action(chat_id, "typing")
        fname = document.get("file_name", "")
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        file_data = tg_download_file(document["file_id"])

        if not file_data:
            send_message(chat_id, "Не смог скачать файл 😔")
            return "ok"

        if ext in ("txt", "py", "cpp", "cs", "js", "ts", "html", "css", "json", "xml", "md", "yaml", "yml", "sh", "bat", "c", "h", "java", "rs", "go", "rb", "php"):
            try:
                content = file_data.decode("utf-8", errors="replace")
                if len(content) > 4000:
                    content = content[:4000] + "\n...[обрезано]"
                prompt = f"Файл: {fname}\n\nСодержимое:\n{content}\n\nПроанализируй этот файл и расскажи что он делает."
                reply = get_ai_reply(chat_id, prompt)
                reply_with_voice_or_text(chat_id, reply)
            except Exception as e:
                send_message(chat_id, f"Ошибка при чтении файла: {e}")
        elif ext in ("mp3", "wav", "ogg", "m4a"):
            transcript = transcribe_audio(file_data)
            send_message(chat_id, f"🎵 Транскрипция аудио: {transcript}")
            reply = get_ai_reply(chat_id, transcript)
            reply_with_voice_or_text(chat_id, reply)
        else:
            send_message(chat_id, f"📎 Получил файл: {fname}\nФормат .{ext} не поддерживается для анализа.")
        return "ok"

    # ---- Обычное текстовое сообщение ----
    if text:
        logger.info(f"От @{username}: {text}")
        stop_event = threading.Event()
        typing_thread = threading.Thread(target=send_typing, args=(chat_id, stop_event), daemon=True)
        typing_thread.start()
        try:
            reply = get_ai_reply(chat_id, text)
        finally:
            stop_event.set()
            typing_thread.join(timeout=5)
        reply_with_voice_or_text(chat_id, reply)
        logger.info(f"Ответ: {reply}")

    return "ok"


def set_webhook():
    webhook_url = f"https://{REPLIT_URL}/webhook"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}"
    r = http_requests.get(url, timeout=10)
    logger.info(f"Webhook: {r.json()}")


if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ Не указан BOT_TOKEN — добавь его в Secrets")
        exit(1)
    if not OPENROUTER_API_KEY:
        print("❌ Не указан OPENROUTER_API_KEY — добавь его в Secrets")
        exit(1)

    if REPLIT_URL:
        set_webhook()
    else:
        print("⚠️ REPLIT_DEV_DOMAIN не найден — установи webhook вручную")

    if REPLIT_URL:
        ka_thread = threading.Thread(target=keep_alive_loop, daemon=True)
        ka_thread.start()
        logger.info("Keep-alive запущен")

    print(f"⚡ Flux AI Bot | Модель: {AI_MODEL}")
    app.run(host="0.0.0.0", port=PORT)
