import sqlite3
import json
import os

DB_PATH = "diary.db"

# Ограничение на количество сообщений в памяти
# Чем больше — тем дольше помнит, но тем дороже каждый запрос
MAX_HISTORY = 50


def init_db():
    """Создаём базу данных и таблицу при первом запуске."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            chat_id INTEGER PRIMARY KEY,
            history TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    print(f"✅ База данных инициализирована: {DB_PATH}")


def load_history(chat_id: int) -> list:
    """Загружаем историю сообщений для конкретного чата."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT history FROM chat_history WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return json.loads(row[0])
    return []


def save_history(chat_id: int, history: list):
    """Сохраняем историю. Обрезаем если слишком длинная."""
    # Оставляем только последние MAX_HISTORY сообщений
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO chat_history (chat_id, history, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            history = excluded.history,
            updated_at = CURRENT_TIMESTAMP
    """, (chat_id, json.dumps(history, ensure_ascii=False)))
    conn.commit()
    conn.close()


def clear_history(chat_id: int):
    """Очищаем историю для конкретного чата."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM chat_history WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
