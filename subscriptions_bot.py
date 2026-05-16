import os
import logging
import json
import sqlite3
import base64
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import anthropic
from groq import Groq

load_dotenv()

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("SUBSCRIPTIONS_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

ALLOWED_USERS = [451779172]

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

DB_PATH = "subscriptions.db"
MAX_HISTORY = 100

SYSTEM_PROMPT = """Ты — ассистент для отслеживания подписок Анастасии.

--- ВАЛЮТЫ ---
Принимай записи в рублях (₽), долларах ($), евро (€).
Примерные курсы: 1$ ≈ 90₽, 1€ ≈ 97₽

--- КАК РАБОТАТЬ С ПОДПИСКАМИ ---
Когда пользователь добавляет подписку — извлеки из текста:
1. Название сервиса
2. Цену и валюту
3. Дату следующего списания (в формате DD.MM.YYYY)
4. Ссылку на сайт (если есть)
5. Периодичность (месяц/год/другое)

После извлечения сохрани подписку командой в формате JSON:
SAVE_SUBSCRIPTION:{"name":"название","price":цифра,"currency":"RUB/USD/EUR","next_date":"DD.MM.YYYY","url":"ссылка или пусто","period":"monthly/yearly/other"}

Когда пользователь просит список — покажи все подписки красиво.
Когда пользователь хочет удалить — попроси уточнить название и подтверди удаление.
Когда пользователь обновляет подписку (новая дата или цена) — обнови данные.

--- ФОРМАТ СПИСКА ПОДПИСОК ---
📋 Твои подписки:

1. [Название] — [цена] [валюта] ([период])
   📅 Следующее списание: DD.MM.YYYY
   🔗 [ссылка]

Итого в месяц: ~X ₽

--- ВАЖНО ---
Отвечай на русском языке.
Будь дружелюбной и конкретной.
Если дата не указана — спроси уточнение."""


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        price REAL NOT NULL,
        currency TEXT NOT NULL,
        next_date TEXT NOT NULL,
        url TEXT,
        period TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_history (
        chat_id INTEGER PRIMARY KEY,
        history TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()
    print("✅ Subscriptions bot DB initialized")


def get_subscriptions(chat_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, name, price, currency, next_date, url, period FROM subscriptions WHERE chat_id = ? ORDER BY next_date",
        (chat_id,)
    ).fetchall()
    conn.close()
    return rows


def save_subscription(chat_id, data):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO subscriptions (chat_id, name, price, currency, next_date, url, period)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (chat_id, data["name"], data["price"], data["currency"],
          data["next_date"], data.get("url", ""), data["period"]))
    conn.commit()
    conn.close()


def delete_subscription(chat_id, name):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM subscriptions WHERE chat_id = ? AND name LIKE ?", (chat_id, f"%{name}%"))
    conn.commit()
    conn.close()


def update_subscription_date(chat_id, name, new_date):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE subscriptions SET next_date = ? WHERE chat_id = ? AND name LIKE ?",
        (new_date, chat_id, f"%{name}%")
    )
    conn.commit()
    conn.close()


def load_history(chat_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT history FROM chat_history WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else []


def save_history(chat_id, history):
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""INSERT INTO chat_history (chat_id, history, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET history=excluded.history, updated_at=CURRENT_TIMESTAMP""",
        (chat_id, json.dumps(history, ensure_ascii=False)))
    conn.commit()
    conn.close()


def is_allowed(update):
    return update.effective_chat.id in ALLOWED_USERS


def format_subscriptions(rows):
    if not rows:
        return "У тебя пока нет подписок. Добавь первую!"

    text = "📋 Твои подписки:\n\n"
    total_rub = 0
    rates = {"RUB": 1, "USD": 90, "EUR": 97}

    for i, (sub_id, name, price, currency, next_date, url, period) in enumerate(rows, 1):
        period_label = {"monthly": "месяц", "yearly": "год", "other": "разово"}.get(period, period)
        currency_symbol = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(currency, currency)

        text += f"{i}. {name} — {price} {currency_symbol} / {period_label}\n"
        text += f"   📅 Следующее списание: {next_date}\n"
        if url:
            text += f"   🔗 {url}\n"
        text += "\n"

        if period == "monthly":
            total_rub += price * rates.get(currency, 1)
        elif period == "yearly":
            total_rub += (price * rates.get(currency, 1)) / 12

    text += f"💰 Итого в месяц: ~{total_rub:.0f} ₽"
    return text


async def check_renewals(app):
    """Проверяем подписки которые обновляются через 3 дня"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT chat_id, name, price, currency, next_date, url FROM subscriptions"
    ).fetchall()
    conn.close()

    target_date = (datetime.now() + timedelta(days=3)).strftime("%d.%m.%Y")

    for chat_id, name, price, currency, next_date, url in rows:
        if next_date == target_date and chat_id in ALLOWED_USERS:
            currency_symbol = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(currency, currency)
            msg = (
                f"⏰ Напоминание о подписке!\n\n"
                f"Через 3 дня ({next_date}) спишется оплата:\n"
                f"📦 {name} — {price} {currency_symbol}\n"
            )
            if url:
                msg += f"🔗 {url}\n"
            msg += "\nЕсли хочешь отменить — самое время!"

            try:
                await app.bot.send_message(chat_id=chat_id, text=msg)
            except Exception as e:
                print(f"Ошибка отправки напоминания: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "Привет! Я слежу за твоими подписками 📋\n\n"
        "Просто напиши мне о подписке в свободной форме:\n"
        "«Spotify $10 в месяц, следующее списание 1 июня, spotify.com»\n\n"
        "Команды:\n"
        "/list — список всех подписок\n"
        "/delete — удалить подписку\n"
        "/clear — очистить историю чата\n\n"
        "За 3 дня до списания я пришлю напоминание! 🔔"
    )


async def list_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    rows = get_subscriptions(update.effective_chat.id)
    await update.message.reply_text(format_subscriptions(rows))


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM chat_history WHERE chat_id = ?", (update.effective_chat.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("🗑 История чата очищена! Подписки сохранены.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Добавляем текущий список подписок в контекст
    rows = get_subscriptions(chat_id)
    subs_context = format_subscriptions(rows)
    now = datetime.now().strftime("%d.%m.%Y")

    history = load_history(chat_id)
    history.append({
        "role": "user",
        "content": f"[{now}] Текущий список подписок:\n{subs_context}\n\nСообщение: {update.message.text}"
    })

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-5", max_tokens=2000,
            system=SYSTEM_PROMPT, messages=history
        )
        reply = response.content[0].text

        # Проверяем нужно ли сохранить подписку
        if "SAVE_SUBSCRIPTION:" in reply:
            try:
                json_start = reply.index("SAVE_SUBSCRIPTION:") + len("SAVE_SUBSCRIPTION:")
                json_str = reply[json_start:].split("\n")[0].strip()
                data = json.loads(json_str)
                save_subscription(chat_id, data)
                reply = reply.replace(f"SAVE_SUBSCRIPTION:{json_str}", "").strip()
            except Exception as e:
                print(f"Ошибка сохранения подписки: {e}")

        history.append({"role": "assistant", "content": reply})
        save_history(chat_id, history)
        await update.message.reply_text(reply)

    except Exception as e:
        print(f"Error: {e}")
        await update.message.reply_text("Что-то пошло не так 🙏")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        file_bytes = await file.download_as_bytearray()
        transcription = groq_client.audio.transcriptions.create(
            file=("voice.ogg", bytes(file_bytes), "audio/ogg"),
            model="whisper-large-v3",
            language="ru"
        )
        recognized_text = transcription.text

        # Обрабатываем как обычное сообщение
        update.message.text = recognized_text
        await handle_message(update, context)

    except Exception as e:
        print(f"Voice error: {e}")
        await update.message.reply_text("Не смогла распознать голосовое 🙏")


def main():
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Планировщик для напоминаний — проверяем каждый день в 9:00
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_renewals,
        trigger="cron",
        hour=9,
        minute=0,
        args=[app]
    )
    scheduler.start()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_subs))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("📋 Subscriptions bot started!")
    app.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()
