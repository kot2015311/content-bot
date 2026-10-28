# llm.py — общение с нейросетью: идеи, посты, сценарии, описания к роликам.
# Поддерживает Claude, GigaChat и любую OpenAI-совместимую нейросеть (OpenRouter и др.),
# а в режиме none нейросеть выключена и бот работает только с твоими готовыми текстами.
import json  # разбираем ответ нейросети в формате JSON
import logging  # пишем в консоль, какую модель выбрали

import config

PROVIDER = config.LLM_PROVIDER  # какая нейросеть выбрана в .env
ENABLED = PROVIDER != "none"  # есть ли нейросеть вообще
NAMES = {"claude": "Claude", "gigachat": "GigaChat", "openai": config.OPENAI_MODEL, "none": "выключена"}
NAME = NAMES[PROVIDER]  # название для сообщений бота

_client = None  # клиент нейросети создаём один раз, при первом запросе


class LLMError(Exception):
    """Ошибка, которую бот покажет тебе текстом."""


def _system_prompt() -> str:
    """Системная инструкция: кто ты и как писать. Профиль читаем каждый раз,
    чтобы правки в profile.md работали без перезапуска бота."""
    profile = config.PROFILE_PATH.read_text(encoding="utf-8")  # твой профиль автора
    return (
        "Ты контент-помощник автора. Придумываешь идеи и пишешь тексты от его лица, "
        "на русском языке, в его стиле. Пиши так, будто это написал сам автор, "
        "а не нейросеть: без штампов, без воды, без выдуманных фактов о нём. "
        "Если для текста нужен факт, которого нет в профиле, оставь пометку [уточни: ...].\n\n"
        f"{profile}"
    )


async def _ask_claude(prompt: str, schema: dict | None, max_tokens: int) -> str:
    """Запрос к Claude. Со schema модель обязана вернуть JSON строго по схеме."""
    global _client
    import anthropic  # импорт здесь, чтобы библиотека была нужна только для Claude

    if _client is None:
        _client = anthropic.AsyncAnthropic()  # ключ берётся из ANTHROPIC_API_KEY
    output_config = {"effort": config.CLAUDE_EFFORT}  # глубина размышлений модели
    if schema:  # просим строго JSON по схеме, чтобы не парсить текст руками
        output_config["format"] = {"type": "json_schema", "schema": schema}
    extra = {}
    if not config.CLAUDE_MODEL.startswith("claude-haiku"):  # у Haiku нет запасной модели
        # если модель откажется отвечать, сервер сам повторит запрос на запасной модели
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    try:
        response = await _client.beta.messages.create(
            model=config.CLAUDE_MODEL,  # модель из .env
            max_tokens=max_tokens,  # предел длины ответа
            system=_system_prompt(),  # кто автор и как писать
            messages=[{"role": "user", "content": prompt}],  # сам запрос
            output_config=output_config,
            **extra,
        )
    except anthropic.AuthenticationError:  # неверный ключ
        raise LLMError("Claude не принял ключ. Проверь ANTHROPIC_API_KEY в .env")
    except anthropic.RateLimitError:  # слишком много запросов или кончился лимит
        raise LLMError("Лимит запросов к Claude. Подожди минуту или проверь баланс")
    except anthropic.APIStatusError as e:  # прочие ошибки сервера
        raise LLMError(f"Claude вернул ошибку {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:  # нет связи с сервером
        raise LLMError("Нет связи с Claude API. Проверь интернет или VPN")
    if response.stop_reason == "refusal":  # модель отказалась писать на эту тему
        raise LLMError("Claude отказался писать на эту тему. Попробуй сформулировать иначе")
    # собираем текст из всех текстовых блоков ответа
    return "".join(block.text for block in response.content if block.type == "text").strip()


_giga_model = None  # модель GigaChat, которая реально доступна на твоём ключе


async def _pick_giga_model() -> str:
    """Спрашивает у GigaChat список моделей и выбирает подходящую.
    Сбер иногда переименовывает модели (GigaChat → GigaChat-2), поэтому не полагаемся на одно имя."""
    models = [m.id_ for m in (await _client.aget_models()).data]  # все модели, доступные ключу
    logging.info("Модели GigaChat на твоём ключе: %s", ", ".join(models))
    wanted = config.GIGACHAT_MODEL.lower()
    for m in models:  # сначала ищем ту, что указана в .env (без учёта регистра)
        if m.lower() == wanted:
            return m
    # иначе берём обычную (Lite) модель: у неё самый большой бесплатный лимит
    chat_models = [m for m in models if m.lower().startswith("gigachat") and "embed" not in m.lower()]
    lite = [m for m in chat_models if "pro" not in m.lower() and "max" not in m.lower()]
    if lite or chat_models:
        return (lite or chat_models)[0]
    raise LLMError("На ключе GigaChat нет моделей для чата. Доступные: " + ", ".join(models))


async def _ask_gigachat(prompt: str, max_tokens: int) -> str:
    """Запрос к GigaChat от Сбера."""
    global _client, _giga_model
    from gigachat import GigaChat  # официальная библиотека GigaChat
    from gigachat.exceptions import AuthenticationError, NotFoundError

    if _client is None:
        _client = GigaChat(
            credentials=config.GIGACHAT_CREDENTIALS,  # ключ авторизации из кабинета разработчика
            scope=config.GIGACHAT_SCOPE,  # GIGACHAT_API_PERS для физлиц
            # у серверов Сбера сертификат российского УЦ, которого нет в Windows.
            # Отключаем проверку сертификата, иначе соединение не установится
            verify_ssl_certs=False,
            timeout=120,
        )
    payload = {
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
    }
    try:
        if _giga_model is None:  # при первом запросе выясняем, какая модель доступна
            _giga_model = await _pick_giga_model()
            logging.info("Работаю с моделью GigaChat: %s", _giga_model)
        response = await _client.achat({**payload, "model": _giga_model})
    except LLMError:
        raise
    except AuthenticationError:
        raise LLMError("GigaChat не принял ключ. Проверь GIGACHAT_CREDENTIALS в .env (нужен «Ключ авторизации»)")
    except NotFoundError:
        _giga_model = None  # модель пропала: при следующем запросе выберем заново
        raise LLMError("GigaChat не нашёл модель. Нажми ещё раз, бот выберет другую")
    except Exception as e:  # прочие ошибки GigaChat: показываем коротко, подробности в консоли
        logging.exception("Ошибка GigaChat")
        raise LLMError(f"GigaChat вернул ошибку {type(e).__name__}. Подробности в окне бота")
    return response.choices[0].message.content.strip()


async def _ask_openai(prompt: str, max_tokens: int) -> str:
    """Запрос к OpenAI-совместимой нейросети (OpenRouter, Groq, DeepSeek и т.д.)."""
    global _client
    import openai  # библиотека openai умеет работать с любым совместимым сервисом

    if _client is None:
        _client = openai.AsyncOpenAI(base_url=config.OPENAI_BASE_URL, api_key=config.OPENAI_API_KEY)
    try:
        response = await _client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
        )
    except openai.RateLimitError:  # у бесплатных моделей лимит запросов в минуту и в день
        raise LLMError(f"{NAME}: лимит бесплатных запросов. Подожди или выбери другую модель")
    except openai.APIStatusError as e:
        raise LLMError(f"{NAME} вернул ошибку {e.status_code}: {e.message}")
    except openai.APIConnectionError:
        raise LLMError(f"Нет связи с {config.OPENAI_BASE_URL}. Проверь интернет или VPN")
    if not response.choices:  # бывает у бесплатных моделей при перегрузке
        raise LLMError(f"{NAME} вернул пустой ответ. Нажми ещё раз")
    return (response.choices[0].message.content or "").strip()


def _parse_json(text: str) -> dict:
    """Достаёт JSON из ответа. Другие нейросети иногда оборачивают его в ```json ... ```."""
    start, end = text.find("{"), text.rfind("}")  # от первой { до последней }
    if start == -1 or end == -1:
        raise LLMError(f"{NAME} прислал ответ не в том формате. Нажми ещё раз")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise LLMError(f"{NAME} прислал ответ не в том формате. Нажми ещё раз")


async def _ask(prompt: str, schema: dict | None = None, max_tokens: int = 8000):
    """Один запрос к выбранной нейросети. С schema возвращает словарь, без неё — текст."""
    if PROVIDER == "none":
        raise LLMError("Нейросеть выключена. Попроси текст у Claude в чате и пришли его сюда")
    if PROVIDER == "claude":
        text = await _ask_claude(prompt, schema, max_tokens)
    else:
        if schema:  # у других нейросетей нет строгой схемы, поэтому просим JSON словами
            prompt += ("\n\nОтветь только JSON-объектом без пояснений и без Markdown, по схеме:\n"
                       + json.dumps(schema, ensure_ascii=False))
        ask = _ask_gigachat if PROVIDER == "gigachat" else _ask_openai
        text = await ask(prompt, max_tokens)
    if not text:
        raise LLMError(f"{NAME} вернул пустой ответ. Нажми ещё раз")
    return _parse_json(text) if schema else text


# схема ответа для идей: список объектов {title, description}
IDEAS_SCHEMA = {
    "type": "object",
    "properties": {
        "ideas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},  # короткое название идеи
                    "description": {"type": "string"},  # о чём и почему зайдёт
                },
                "required": ["title", "description"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ideas"],
    "additionalProperties": False,
}


async def generate_ideas(platform: str, avoid: list[str], hint: str | None = None) -> list[dict]:
    """Придумывает 5 идей для Telegram-канала (tg) или TikTok (tt)."""
    where = "постов для Telegram-канала" if platform == "tg" else "коротких вертикальных роликов для TikTok"
    prompt = f"Придумай 5 идей {where}. Идеи должны быть разными по формату и реалистичными для автора прямо сейчас."
    if platform == "tt":
        prompt += " Каждую можно снять на телефон за 15–30 минут. В описании укажи хук первых 2 секунд."
    if hint:  # если ты подсказал тему
        prompt += f"\nТема или пожелание автора: {hint}"
    if avoid:  # чтобы не повторяться
        prompt += "\nНе повторяй уже предложенные или опубликованные темы:\n- " + "\n- ".join(avoid)
    prompt += "\nНазвание — до 60 символов, описание — 1–2 предложения: о чём и чем зацепит."
    data = await _ask(prompt, schema=IDEAS_SCHEMA, max_tokens=4000)
    ideas = [i for i in data.get("ideas", []) if i.get("title")]  # отбрасываем кривые элементы
    if not ideas:
        raise LLMError(f"{NAME} не придумал идей. Нажми ещё раз")
    return [{"title": i["title"], "description": i.get("description", "")} for i in ideas[:5]]


POST_RULES = (
    "Оформи пост для Telegram. Можно использовать только HTML-теги <b>, <i>, <u>, <s>, <code>. "
    "Не используй Markdown (никаких ** и #). Длина до 1500 символов. "
    "Первая строка — цепляющий заголовок жирным. Короткие абзацы. "
    "Верни только текст поста, без пояснений до или после."
)


async def write_post(topic: str, description: str = "") -> str:
    """Пишет пост для канала по идее."""
    prompt = f"Напиши пост для Telegram-канала автора.\nТема: {topic}\n{description}\n\n{POST_RULES}"
    return await _ask(prompt)


async def rewrite_post(topic: str, old_text: str, instruction: str | None = None) -> str:
    """Переписывает пост: целиком заново или по твоим правкам."""
    if instruction:  # ты написал, что поменять
        task = f"Внеси правки автора и сохрани остальное.\nПравки: {instruction}"
    else:  # просто «перепиши»
        task = "Напиши новый вариант с другим заходом и структурой, тема та же."
    prompt = f"Тема поста: {topic}\nТекущий текст:\n{old_text}\n\n{task}\n\n{POST_RULES}"
    return await _ask(prompt)


async def write_script(title: str, description: str) -> str:
    """Пишет сценарий ролика для TikTok, который ты снимешь сам."""
    prompt = (
        f"Напиши сценарий вертикального ролика для TikTok на 30–45 секунд.\n"
        f"Идея: {title}\n{description}\n\n"
        "Структура ответа (обычный текст без Markdown, можно эмодзи-маркеры):\n"
        "1) ХУК — первая фраза на 2 секунды, дословно.\n"
        "2) ПЛАН СЪЁМКИ — 4–7 кадров. Для каждого: что в кадре (лицо / экран телефона / экран компьютера), "
        "что говорить дословно, сколько секунд.\n"
        "3) ПРИЗЫВ в конце, дословно.\n"
        "4) СОВЕТЫ ПО СЪЁМКЕ — 2–3 коротких пункта (свет, звук, ракурс), учитывая слабый микрофон.\n"
        "Говори фразами, которые легко произнести вслух. Паузы между фразами не страшны: бот их вырежет."
    )
    return await _ask(prompt)


async def write_caption(transcript: str, idea: str | None = None) -> str:
    """Делает описание и хэштеги к готовому ролику по тому, что ты сказал в кадре."""
    prompt = (
        "Напиши описание к ролику для TikTok: 1–2 короткие строки, которые цепляют, "
        "и 5–8 хэштегов на русском и английском. Верни только описание и хэштеги.\n"
        f"Что говорится в ролике: {transcript or '(речь не распознана)'}"
    )
    if idea:
        prompt += f"\nИдея ролика: {idea}"
    return await _ask(prompt, max_tokens=2000)
