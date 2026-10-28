# bot.py — Telegram-бот: идеи для контента, посты в канал по кнопке, сценарии и монтаж для TikTok
import asyncio  # асинхронность: бот не зависает, пока думает нейросеть или монтируется видео
import html  # экранируем текст для HTML-разметки Telegram
import logging  # пишем ход работы в консоль
import re  # убираем HTML-теги, если Telegram их не принял
import shutil  # удаляем временные папки
import time  # имя папки для видео по времени

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ErrorEvent
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

import llm
import storage
from config import BOT_TOKEN, CHANNEL_ID, OWNER_ID, WORK_DIR
from video import VideoError, process_video

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

router = Router()
router.message.filter(F.from_user.id == OWNER_ID)  # чужие сообщения бот просто игнорирует
router.callback_query.filter(F.from_user.id == OWNER_ID)  # и чужие нажатия кнопок тоже

video_lock = asyncio.Lock()  # монтируем по одному ролику, чтобы не перегружать компьютер
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Telegram даёт ботам скачивать файлы до 20 МБ

# ---------- кнопки ----------

BTN_TG = "💡 Идеи для ТГК"
BTN_TT = "🎬 Идеи для TikTok"
BTN_TOPIC = "✍️ Пост на свою тему"
BTN_EDIT_VIDEO = "🎞 Смонтировать видео"
BTN_PASTE = "📋 Готовый пост в канал"
BTN_HELP = "ℹ️ Помощь"

MAIN_MENU = ReplyKeyboardMarkup(  # постоянное меню внизу экрана
    keyboard=[
        [KeyboardButton(text=BTN_TG), KeyboardButton(text=BTN_TT)],
        [KeyboardButton(text=BTN_TOPIC), KeyboardButton(text=BTN_PASTE)],
        [KeyboardButton(text=BTN_EDIT_VIDEO), KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Короткая запись inline-кнопок: kb([("Текст", "data")], [...])."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def draft_kb(draft_id: int) -> InlineKeyboardMarkup:
    """Кнопки под черновиком поста. Без нейросети кнопки «Переписать» нет."""
    second_row = [("✏️ Правки", f"edit:{draft_id}")]
    if llm.ENABLED:
        second_row.append(("🔄 Переписать", f"redo:{draft_id}"))
    return kb(
        [("🚀 Выпустить в ТГК", f"pub:{draft_id}")],
        second_row,
        [("🖼 Добавить фото", f"photo:{draft_id}"), ("🗑 Удалить", f"del:{draft_id}")],
    )


NO_LLM_TEXT = (
    "Нейросеть выключена (в .env нет ключа). Попроси идеи и тексты у Claude в чате, "
    f"а готовый текст пришли сюда через «{BTN_PASTE}» или просто сообщением: я сделаю черновик с кнопкой публикации."
)


# ---------- состояния (бот ждёт от тебя что-то конкретное) ----------

class Wait(StatesGroup):
    topic = State()  # ждём тему для поста
    edit = State()  # ждём правки к черновику
    paste = State()  # ждём готовый текст поста
    photo = State()  # ждём фото к черновику
    video = State()  # ждём видео для монтажа


# ---------- вспомогательные функции ----------

def strip_tags(text: str) -> str:
    """Убирает HTML-теги, если разметка сломана."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


async def send_html(target: Message, text: str, **kwargs) -> Message:
    """Отправляет текст с HTML-разметкой, а если она сломана, то обычным текстом."""
    try:
        return await target.answer(text, parse_mode="HTML", **kwargs)
    except TelegramBadRequest:
        return await target.answer(strip_tags(text), **kwargs)


async def send_long(target: Message, text: str, **kwargs) -> None:
    """Telegram пропускает до 4096 символов. Длинный текст режем по абзацам."""
    parts, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > 4000:
            parts.append(cur)
            cur = ""
        cur += para + "\n"
    parts.append(cur)
    for i, part in enumerate(parts):
        last = i == len(parts) - 1  # кнопки только под последним куском
        await target.answer(part, **(kwargs if last else {}))


async def show_draft(message: Message, draft_id: int) -> None:
    """Показывает черновик так, как он будет выглядеть в канале, и кнопки под ним."""
    draft = storage.get_draft(draft_id)
    await message.answer(f"📝 Черновик №{draft_id}" + (" (с фото)" if draft["photo_file_id"] else ""))
    await send_html(message, draft["text"], reply_markup=draft_kb(draft_id))


async def show_ideas(message: Message, platform: str, hint: str | None = None) -> None:
    """Генерирует 5 идей и показывает их кнопками."""
    if not llm.ENABLED:
        await message.answer(NO_LLM_TEXT)
        return
    wait = await message.answer("🤔 Придумываю идеи…")
    avoid = storage.recent_titles(platform)  # чтобы не повторяться
    if platform == "tg":
        avoid += storage.published_topics()
    try:
        ideas = await llm.generate_ideas(platform, avoid, hint)
    except llm.LLMError as e:
        await wait.edit_text(f"⚠️ {e}")
        return

    lines, buttons = [], []
    action = "post" if platform == "tg" else "script"  # что делать по нажатию
    label = "✍️ Пост" if platform == "tg" else "🎬 Сценарий"
    for n, idea in enumerate(ideas, start=1):
        idea_id = storage.add_idea(platform, idea["title"], idea["description"])
        lines.append(f"<b>{n}. {html.escape(idea['title'])}</b>\n{html.escape(idea['description'])}")
        buttons.append((f"{label} {n}", f"{action}:{idea_id}"))

    title = "Идеи для Telegram-канала" if platform == "tg" else "Идеи для TikTok"
    markup = kb(buttons[:3], buttons[3:], [("🔄 Ещё идеи", f"more:{platform}")])
    await wait.edit_text(f"<b>{title}</b>\n\n" + "\n\n".join(lines), parse_mode="HTML", reply_markup=markup)


# ---------- команды и меню ----------

@router.message(CommandStart())
@router.message(F.text == BTN_HELP)
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()  # сбрасываем любое ожидание
    await message.answer(
        f"Привет! Я твой контент-помощник. Нейросеть: {llm.NAME}.\n\n"
        f"{BTN_TG} — 5 идей, по любой напишу пост. Пост выйдет в канал только после твоей кнопки «Выпустить» и подтверждения.\n"
        f"{BTN_TT} — 5 идей, по любой дам сценарий. Снимаешь, присылаешь видео, я монтирую.\n"
        f"{BTN_TOPIC} — пишешь тему своими словами, я пишу пост.\n"
        f"{BTN_PASTE} — присылаешь готовый текст (можно с фото), я делаю черновик с кнопкой публикации.\n"
        f"{BTN_EDIT_VIDEO} — пришли любое видео, я уберу паузы, почищу звук, сделаю 9:16 и субтитры.\n\n"
        "Идеи станут точнее, если поправить файл profile.md. /cancel — отменить ожидание.",
        reply_markup=MAIN_MENU,
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, отменил.", reply_markup=MAIN_MENU)


@router.message(F.text == BTN_TG)
async def ideas_tg(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_ideas(message, "tg")


@router.message(F.text == BTN_TT)
async def ideas_tt(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_ideas(message, "tt")


@router.callback_query(F.data.startswith("more:"))
async def more_ideas(call: CallbackQuery) -> None:
    await call.answer()  # убираем «часики» на кнопке
    await show_ideas(call.message, call.data.split(":")[1])


# ---------- посты для Telegram-канала ----------

@router.message(F.text == BTN_TOPIC)
async def ask_topic(message: Message, state: FSMContext) -> None:
    if not llm.ENABLED:
        await message.answer(NO_LLM_TEXT)
        return
    await state.set_state(Wait.topic)
    await message.answer("Напиши тему поста или пару мыслей, о чём рассказать.")


@router.message(Wait.topic, F.text)
async def post_from_topic(message: Message, state: FSMContext) -> None:
    await state.clear()
    wait = await message.answer("✍️ Пишу пост…")
    try:
        text = await llm.write_post(message.text)
    except llm.LLMError as e:
        await wait.edit_text(f"⚠️ {e}")
        return
    await wait.delete()
    draft_id = storage.add_draft(message.text[:200], text)
    await show_draft(message, draft_id)


@router.callback_query(F.data.startswith("post:"))
async def post_from_idea(call: CallbackQuery) -> None:
    await call.answer()
    idea = storage.get_idea(int(call.data.split(":")[1]))
    wait = await call.message.answer(f"✍️ Пишу пост: {idea['title']}…")
    try:
        text = await llm.write_post(idea["title"], idea["description"])
    except llm.LLMError as e:
        await wait.edit_text(f"⚠️ {e}")
        return
    await wait.delete()
    draft_id = storage.add_draft(idea["title"], text, idea["id"])
    await show_draft(call.message, draft_id)


@router.callback_query(F.data.startswith("redo:"))
async def redo_post(call: CallbackQuery) -> None:
    await call.answer("Переписываю…")
    draft = storage.get_draft(int(call.data.split(":")[1]))
    try:
        text = await llm.rewrite_post(draft["topic"], draft["text"])
    except llm.LLMError as e:
        await call.message.answer(f"⚠️ {e}")
        return
    storage.update_draft(draft["id"], text=text)
    await show_draft(call.message, draft["id"])


@router.callback_query(F.data.startswith("edit:"))
async def ask_edit(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(Wait.edit)
    await state.update_data(draft_id=int(call.data.split(":")[1]))  # запоминаем, какой черновик правим
    if llm.ENABLED:
        await call.message.answer("Что поменять? Например: «короче», «добавь цену 3000 ₽», «убери эмодзи».")
    else:
        await call.message.answer("Пришли исправленный текст поста целиком, я заменю черновик.")


@router.message(Wait.edit, F.text)
async def apply_edit(message: Message, state: FSMContext) -> None:
    draft_id = (await state.get_data())["draft_id"]
    await state.clear()
    draft = storage.get_draft(draft_id)
    if not llm.ENABLED:  # без нейросети просто заменяем текст на присланный
        storage.update_draft(draft_id, text=message.html_text)  # html_text сохраняет жирный и курсив
        await show_draft(message, draft_id)
        return
    wait = await message.answer("✏️ Вношу правки…")
    try:
        text = await llm.rewrite_post(draft["topic"], draft["text"], message.text)
    except llm.LLMError as e:
        await wait.edit_text(f"⚠️ {e}")
        return
    await wait.delete()
    storage.update_draft(draft_id, text=text)
    await show_draft(message, draft_id)


@router.callback_query(F.data.startswith("photo:"))
async def ask_photo(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(Wait.photo)
    await state.update_data(draft_id=int(call.data.split(":")[1]))
    await call.message.answer("Пришли фото или скриншот для поста.")


@router.message(Wait.photo, F.photo)
async def save_photo(message: Message, state: FSMContext) -> None:
    draft_id = (await state.get_data())["draft_id"]
    await state.clear()
    storage.update_draft(draft_id, photo_file_id=message.photo[-1].file_id)  # берём самое большое фото
    await show_draft(message, draft_id)


@router.callback_query(F.data.startswith("del:"))
async def delete_draft(call: CallbackQuery) -> None:
    storage.update_draft(int(call.data.split(":")[1]), status="deleted")
    await call.answer("Удалил")
    await call.message.edit_reply_markup(reply_markup=None)  # убираем кнопки под черновиком


@router.callback_query(F.data.startswith("pub:"))
async def confirm_publish(call: CallbackQuery) -> None:
    """Первый шаг публикации: спрашиваем подтверждение, чтобы не выпустить случайно."""
    await call.answer()
    draft_id = int(call.data.split(":")[1])
    await call.message.answer(
        f"Выпустить черновик №{draft_id} в канал?",
        reply_markup=kb([("✅ Да, выпустить", f"pubyes:{draft_id}"), ("❌ Нет", "pubno")]),
    )


@router.callback_query(F.data == "pubno")
async def cancel_publish(call: CallbackQuery) -> None:
    await call.answer("Не публикую")
    await call.message.delete()


@router.callback_query(F.data.startswith("pubyes:"))
async def publish(call: CallbackQuery, bot: Bot) -> None:
    """Публикуем пост в канал. Бот должен быть админом канала с правом публикации."""
    draft = storage.get_draft(int(call.data.split(":")[1]))
    if draft["status"] == "published":  # защита от двойного нажатия
        await call.answer("Этот пост уже вышел")
        return
    text = draft["text"]
    try:
        try:
            await _send_to_channel(bot, text, draft["photo_file_id"], "HTML")
        except TelegramBadRequest as e:
            if "parse" not in str(e).lower() and "tag" not in str(e).lower():
                raise  # ошибка не из-за разметки, разбираемся ниже
            await _send_to_channel(bot, strip_tags(text), draft["photo_file_id"], None)
    except TelegramAPIError as e:  # любая ошибка Telegram при публикации
        logging.warning("Не удалось опубликовать в %s: %s", CHANNEL_ID, e)
        if isinstance(e, TelegramForbiddenError):  # бота нет в канале или он не админ
            hint = "Бот не может писать в канал. Добавь его в канал администратором с правом «Публикация сообщений»."
        elif "chat not found" in str(e).lower():  # неверный CHANNEL_ID
            hint = (f"Telegram не нашёл канал {CHANNEL_ID}. Проверь CHANNEL_ID в .env: нужен @username канала "
                    "(из ссылки t.me/username), а у закрытого канала — числовой id, который начинается с -100.")
        else:
            hint = "Проверь, что бот — админ канала с правом публикации, а CHANNEL_ID в .env верный."
        await call.answer()
        await call.message.answer(f"⚠️ Не получилось опубликовать: {e.message}\n{hint}")
        return
    storage.update_draft(draft["id"], status="published")
    await call.answer("Опубликовано!")
    await call.message.edit_text(f"✅ Черновик №{draft['id']} вышел в канале")


async def _send_to_channel(bot: Bot, text: str, photo: str | None, parse_mode: str | None) -> None:
    """Отправка в канал: с фото или без. Подпись к фото — до 1024 символов,
    поэтому длинный текст уходит отдельным сообщением сразу после фото."""
    if photo and len(strip_tags(text)) <= 1024:
        await bot.send_photo(CHANNEL_ID, photo, caption=text, parse_mode=parse_mode)
    elif photo:
        await bot.send_photo(CHANNEL_ID, photo)
        await bot.send_message(CHANNEL_ID, text, parse_mode=parse_mode)
    else:
        await bot.send_message(CHANNEL_ID, text, parse_mode=parse_mode)


# ---------- TikTok: сценарий → ты снимаешь → бот монтирует ----------

@router.callback_query(F.data.startswith("script:"))
async def script_from_idea(call: CallbackQuery) -> None:
    await call.answer()
    idea = storage.get_idea(int(call.data.split(":")[1]))
    wait = await call.message.answer(f"🎬 Пишу сценарий: {idea['title']}…")
    try:
        script = await llm.write_script(idea["title"], idea["description"])
    except llm.LLMError as e:
        await wait.edit_text(f"⚠️ {e}")
        return
    await wait.delete()
    markup = kb([("🎥 Снял, монтируй", f"shot:{idea['id']}")], [("🔄 Другой сценарий", f"script:{idea['id']}")])
    await send_long(call.message, f"🎬 {idea['title']}\n\n{script}", reply_markup=markup)


@router.callback_query(F.data.startswith("shot:"))
async def ask_video_for_idea(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(Wait.video)
    await state.update_data(idea_id=int(call.data.split(":")[1]))
    await call.message.answer(VIDEO_HINT)


VIDEO_HINT = (
    "Пришли видео одним файлом. Отправляй как обычное видео (не «файлом»): "
    "Telegram сожмёт его, и оно влезет в лимит ботов 20 МБ. Ролик до минуты обычно влезает."
)


@router.message(F.text == BTN_EDIT_VIDEO)
async def ask_video(message: Message, state: FSMContext) -> None:
    await state.set_state(Wait.video)
    await state.update_data(idea_id=None)
    await message.answer(VIDEO_HINT)


@router.message(F.video | (F.document & F.document.mime_type.startswith("video/")))
async def edit_video(message: Message, state: FSMContext, bot: Bot) -> None:
    """Монтаж. Видео принимаем и без нажатия кнопки, просто присланное в чат."""
    data = await state.get_data()
    idea = storage.get_idea(data["idea_id"]) if data.get("idea_id") else None
    await state.clear()

    file = message.video or message.document
    if file.file_size and file.file_size > TG_DOWNLOAD_LIMIT:
        await message.answer(
            f"⚠️ Видео весит {file.file_size / 1024 / 1024:.0f} МБ, а ботам Telegram отдаёт файлы до 20 МБ. "
            "Пришли его как обычное видео (не файлом) или сними в 1080p вместо 4K."
        )
        return

    if video_lock.locked():
        await message.answer("⏳ Сейчас монтирую другой ролик, этот возьму следом.")

    async with video_lock:
        status = await message.answer("📥 Скачиваю видео…")
        folder = WORK_DIR / str(int(time.time()))  # отдельная папка для этого ролика
        folder.mkdir(parents=True, exist_ok=True)
        keep_folder = False  # оставляем папку, только если готовый ролик не влез в Telegram
        try:
            src = folder / "source.mp4"
            await bot.download(file, destination=src)

            loop = asyncio.get_running_loop()
            steps: list[str] = []

            def progress(text: str) -> None:
                """Вызывается из потока монтажа: обновляем сообщение со статусом."""
                steps.append(text)
                asyncio.run_coroutine_threadsafe(_safe_edit(status, "\n".join(steps)), loop)

            try:
                # монтаж долгий и грузит процессор, поэтому запускаем его в отдельном потоке
                final, transcript = await asyncio.to_thread(process_video, src, progress)
            except VideoError as e:
                await message.answer(f"⚠️ {e}")
                return
            except Exception as e:  # неожиданная ошибка: пишем в консоль и тебе
                logging.exception("Ошибка монтажа")
                await message.answer(f"⚠️ Что-то пошло не так при монтаже: {e}")
                return

            size_mb = final.stat().st_size / 1024 / 1024
            if size_mb > 49:  # ботам можно отправлять файлы до 50 МБ
                keep_folder = True
                await message.answer(f"Ролик готов, но весит {size_mb:.0f} МБ и не влезает в Telegram. Он лежит тут: {final}")
                return
            await message.answer_document(FSInputFile(final, filename="tiktok.mp4"),
                                          caption="🎞 Готовый ролик. Скачай и выложи в TikTok, Shorts и VK Клипы.")

            if llm.ENABLED:  # описание и хэштеги к ролику
                try:
                    caption = await llm.write_caption(transcript, idea["title"] if idea else None)
                    await message.answer(f"Описание для публикации:\n\n{caption}")
                except llm.LLMError as e:
                    await message.answer(f"Описание не получилось: {e}")
            elif transcript:  # без нейросети отдаём текст ролика: его можно скинуть Claude в чат
                await message.answer(f"Текст из ролика (скинь Claude в чат, он напишет описание):\n\n{transcript}")
        finally:
            if not keep_folder:  # чистим временные файлы и после ошибки, а не только после успеха
                shutil.rmtree(folder, ignore_errors=True)


async def _safe_edit(msg: Message, text: str) -> None:
    """Меняет текст сообщения и не падает, если текст не изменился."""
    try:
        await msg.edit_text(text)
    except TelegramBadRequest:
        pass


# ---------- готовые тексты: присылаешь пост, бот делает черновик ----------

@router.message(F.text == BTN_PASTE)
async def ask_paste(message: Message, state: FSMContext) -> None:
    await state.set_state(Wait.paste)
    await message.answer("Пришли готовый текст поста. Можно с фото: прикрепи фото и напиши текст в подписи.")


def _draft_from_message(message: Message) -> int:
    """Делает черновик из твоего сообщения: текст или фото с подписью.
    html_text сохраняет форматирование (жирный, курсив, ссылки), как ты его набрал."""
    text = message.html_text if (message.text or message.caption) else ""
    first_line = strip_tags(text).strip().split("\n")[0][:100] or "Пост"  # тема — первая строка
    draft_id = storage.add_draft(first_line, text)
    if message.photo:
        storage.update_draft(draft_id, photo_file_id=message.photo[-1].file_id)
    return draft_id


@router.message(Wait.paste, F.text | F.photo)
async def paste_post(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.photo and not message.caption:
        await message.answer("Фото без текста. Пришли фото вместе с подписью-текстом поста.")
        return
    await show_draft(message, _draft_from_message(message))


@router.message(F.photo & F.caption)
async def photo_with_caption(message: Message) -> None:
    """Фото с подписью, присланное без кнопки, — это почти наверняка пост."""
    await show_draft(message, _draft_from_message(message))


@router.message(F.text & ~F.text.startswith("/"))
async def free_text(message: Message) -> None:
    """Любой текст вне кнопок. Без нейросети сразу делаем черновик,
    с нейросетью спрашиваем: опубликовать как есть или написать пост на эту тему."""
    draft_id = _draft_from_message(message)
    if not llm.ENABLED:
        await show_draft(message, draft_id)
        return
    await message.answer(
        "Что сделать с этим текстом?",
        reply_markup=kb([("📋 Это готовый пост", f"asis:{draft_id}")],
                        [("✍️ Написать пост на эту тему", f"fromtopic:{draft_id}")]),
    )


@router.callback_query(F.data.startswith("asis:"))
async def draft_as_is(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.delete()
    await show_draft(call.message, int(call.data.split(":")[1]))


@router.callback_query(F.data.startswith("fromtopic:"))
async def draft_from_topic(call: CallbackQuery) -> None:
    await call.answer()
    old = storage.get_draft(int(call.data.split(":")[1]))
    storage.update_draft(old["id"], status="deleted")  # исходный текст был темой, а не постом
    await call.message.edit_text("✍️ Пишу пост…")
    topic = strip_tags(old["text"])
    try:
        text = await llm.write_post(topic)
    except llm.LLMError as e:
        await call.message.edit_text(f"⚠️ {e}")
        return
    await call.message.delete()
    draft_id = storage.add_draft(topic[:200], text)
    await show_draft(call.message, draft_id)


# ---------- запуск ----------

async def main() -> None:
    storage.init_db()  # создаём таблицы при первом запуске
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> None:
        """Любая неожиданная ошибка: пишем в консоль и тебе в чат, чтобы бот не молчал."""
        logging.exception("Необработанная ошибка", exc_info=event.exception)
        try:
            await bot.send_message(OWNER_ID, f"⚠️ Ошибка в боте: {type(event.exception).__name__}: {event.exception}")
        except TelegramAPIError:
            pass

    logging.info("Бот запущен, нейросеть: %s", llm.NAME)
    await dp.start_polling(bot)  # бот сам спрашивает Telegram о новых сообщениях


if __name__ == "__main__":
    asyncio.run(main())
