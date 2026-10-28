# storage.py — маленькая база SQLite для идей и черновиков постов
import sqlite3  # встроенная в Python база данных, ничего ставить не нужно
from datetime import datetime  # чтобы записывать время

from config import DB_PATH  # путь к файлу базы


def _conn() -> sqlite3.Connection:
    """Открывает соединение с базой. Row позволяет брать поля по имени."""
    conn = sqlite3.connect(DB_PATH)  # файл создаётся сам при первом запуске
    conn.row_factory = sqlite3.Row  # строки как словари: row["title"]
    return conn


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    with _conn() as conn:
        # идеи: platform = "tg" (Telegram-канал) или "tt" (TikTok)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        # черновики постов: status = draft (черновик), published (вышел), deleted (удалён)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idea_id INTEGER,
                topic TEXT NOT NULL,
                text TEXT NOT NULL,
                photo_file_id TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL
            )"""
        )


def _now() -> str:
    """Текущее время строкой, например 2026-10-24 18:40."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def add_idea(platform: str, title: str, description: str) -> int:
    """Сохраняет идею и возвращает её номер в базе."""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO ideas (platform, title, description, created_at) VALUES (?, ?, ?, ?)",
            (platform, title, description, _now()),
        )
        return cur.lastrowid  # id только что добавленной строки


def get_idea(idea_id: int) -> sqlite3.Row | None:
    """Достаёт идею по номеру."""
    with _conn() as conn:
        return conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()


def recent_titles(platform: str, limit: int = 30) -> list[str]:
    """Последние идеи, чтобы нейросеть не повторялась."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT title FROM ideas WHERE platform = ? ORDER BY id DESC LIMIT ?",
            (platform, limit),
        ).fetchall()
    return [r["title"] for r in rows]


def published_topics(limit: int = 30) -> list[str]:
    """Темы постов, которые уже вышли в канал."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT topic FROM drafts WHERE status = 'published' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [r["topic"] for r in rows]


def add_draft(topic: str, text: str, idea_id: int | None = None) -> int:
    """Сохраняет новый черновик поста."""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO drafts (idea_id, topic, text, created_at) VALUES (?, ?, ?, ?)",
            (idea_id, topic, text, _now()),
        )
        return cur.lastrowid


def get_draft(draft_id: int) -> sqlite3.Row | None:
    """Достаёт черновик по номеру."""
    with _conn() as conn:
        return conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()


def update_draft(draft_id: int, **fields) -> None:
    """Меняет поля черновика, например update_draft(5, text="...", status="published")."""
    allowed = {"text", "photo_file_id", "status"}  # что разрешено менять
    fields = {k: v for k, v in fields.items() if k in allowed}  # отбрасываем лишнее
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)  # "text = ?, status = ?"
    with _conn() as conn:
        conn.execute(f"UPDATE drafts SET {sets} WHERE id = ?", (*fields.values(), draft_id))
