# config.py — все настройки бота берутся из файла .env
import os  # доступ к переменным окружения
from pathlib import Path  # удобная работа с путями

from dotenv import load_dotenv  # читает .env и кладёт значения в окружение

BASE_DIR = Path(__file__).resolve().parent  # папка, где лежит бот
load_dotenv(BASE_DIR / ".env")  # загружаем .env из папки бота


def _require(name: str) -> str:
    """Берёт обязательную настройку, а если её нет, падает с понятной ошибкой."""
    value = os.getenv(name, "").strip()  # читаем значение и убираем пробелы
    if not value:  # если пусто, бот работать не сможет
        raise RuntimeError(f"В .env не заполнено {name}. Смотри .env.example")
    return value


BOT_TOKEN = _require("BOT_TOKEN")  # токен бота от @BotFather
OWNER_ID = int(_require("OWNER_ID"))  # твой Telegram id: только ты можешь пользоваться ботом
CHANNEL_ID = _require("CHANNEL_ID")  # канал для публикации: @username или -100...
# какая нейросеть пишет тексты: claude / gigachat / openai / none (без нейросети)
# если не указано, выбираем сами по тому, какой ключ заполнен
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "").strip().lower()
if not LLM_PROVIDER:
    if os.getenv("ANTHROPIC_API_KEY"):
        LLM_PROVIDER = "claude"
    elif os.getenv("GIGACHAT_CREDENTIALS"):
        LLM_PROVIDER = "gigachat"
    elif os.getenv("OPENAI_API_KEY"):
        LLM_PROVIDER = "openai"
    else:
        LLM_PROVIDER = "none"  # ключей нет: бот публикует твои готовые тексты и монтирует видео
if LLM_PROVIDER not in {"claude", "gigachat", "openai", "none"}:
    raise RuntimeError("LLM_PROVIDER должен быть claude, gigachat, openai или none")

# GigaChat (Сбер): работает из России без VPN, есть бесплатный лимит для физлиц
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS", "")  # «Ключ авторизации» из личного кабинета
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2")  # если такой нет, бот сам выберет доступную
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")  # PERS — для физлиц

# любая нейросеть с OpenAI-совместимым API: OpenRouter (есть бесплатные модели), Groq, DeepSeek и др.
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "")

# проверяем, что для выбранной нейросети заполнено всё нужное
if LLM_PROVIDER == "claude":
    _require("ANTHROPIC_API_KEY")  # сам ключ читает библиотека anthropic
elif LLM_PROVIDER == "gigachat":
    _require("GIGACHAT_CREDENTIALS")
elif LLM_PROVIDER == "openai":
    _require("OPENAI_API_KEY")
    _require("OPENAI_MODEL")

# по умолчанию те же значения, что в .env.example: самая дешёвая модель и быстрый режим
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")  # модель для идей и текстов
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "low")  # насколько глубоко думать: low / medium / high

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")  # модель распознавания речи для субтитров
SUBTITLES = os.getenv("SUBTITLES", "1") == "1"  # 1 — вшивать субтитры, 0 — нет
CUT_PAUSES = os.getenv("CUT_PAUSES", "1") == "1"  # 1 — вырезать паузы, 0 — нет

PROFILE_PATH = BASE_DIR / "profile.md"  # описание тебя и канала для нейросети
DB_PATH = BASE_DIR / "content.db"  # база с идеями и черновиками
WORK_DIR = BASE_DIR / "work"  # временная папка для видео
WORK_DIR.mkdir(exist_ok=True)  # создаём её, если ещё нет
