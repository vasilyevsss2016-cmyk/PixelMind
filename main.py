"""
Flux — AI чат-бот для Telegram.
Бесплатная модель через OpenRouter.
Для запуска на Replit.
"""

import os
import re
import logging
from flask import Flask, request
import requests as http_requests

# ============ НАСТРОЙКИ ============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
REPLIT_URL = os.environ.get("REPLIT_DEV_DOMAIN", "")
BOT_NAME = "Flux"
AI_MODEL = "stepfun/step-3.5-flash:free"
PORT = int(os.environ.get("PORT", 8080))
# ====================================

SYSTEM_PROMPT = f"""Ты — {BOT_NAME}, дружелюбный AI-ассистент в Telegram с лёгким чувством юмора.

Правила:
- Общайся свободно на любые темы
- Будь интересным собеседником — рассказывай факты, поддерживай разговор
- Отвечай на русском языке
- Иногда добавляй лёгкую шутку или иронию — ненавязчиво, без перебора
- Юмор должен быть уместным и добрым, не пошлым
- Если спрашивают кто ты — ты Flux, AI-бот для общения (можешь пошутить про это)
- Отвечай не слишком длинно — 2-5 предложений, если не просят подробнее
- Можешь использовать 1-2 эмодзи если уместно
- Не добавляй подпись в конце сообщения
"""

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

chat_histories: dict[int, list[dict]] = {}
MAX_HISTORY = 30


def get_ai_reply(chat_id: int, user_message: str) -> str:
    if chat_id not in chat_histories:
        chat_histories[chat_id] = []

    history = chat_histories[chat_id]
    history.append({"role": "user", "content": user_message})

    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
        chat_histories[chat_id] = history

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

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


def send_typing(chat_id: int):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendChatAction"
    http_requests.post(url, json={"chat_id": chat_id, "action": "typing"})


def send_message(chat_id: int, text: str, reply_to: int = None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    http_requests.post(url, json=data)


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
    text = message.get("text", "")
    message_id = message.get("message_id")
    user = message.get("from", {})

    if not text:
        return "ok"

    logger.info(f"От {user.get('first_name', '?')} (@{user.get('username', '?')}): {text}")

    if text.strip() == "/start":
        chat_histories[chat_id] = []
        send_message(
            chat_id,
            f"Привет! ⚡ Я {BOT_NAME} — AI-бот для общения. Пиши что угодно, поболтаем!",
            reply_to=message_id
        )
        return "ok"

    if text.strip() == "/reset":
        chat_histories[chat_id] = []
        send_message(chat_id, "Память очищена 🔄 Начнём сначала!", reply_to=message_id)
        return "ok"

    send_typing(chat_id)
    reply = get_ai_reply(chat_id, text)
    send_message(chat_id, reply, reply_to=message_id)
    logger.info(f"Ответ: {reply}")

    return "ok"


def set_webhook():
    webhook_url = f"https://{REPLIT_URL}/webhook"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}"
    r = http_requests.get(url)
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

    print(f"⚡ Flux AI Bot | Модель: {AI_MODEL}")
    app.run(host="0.0.0.0", port=PORT)
