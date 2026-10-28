# video.py — автомонтаж: вырезает паузы, чистит звук, делает 9:16 и вшивает субтитры
import json  # разбираем ответ ffprobe
import re  # ищем паузы в выводе ffmpeg
import shutil  # проверяем, установлен ли ffmpeg
import subprocess  # запускаем ffmpeg как отдельную программу
from pathlib import Path

from config import CUT_PAUSES, SUBTITLES, WHISPER_MODEL

W, H = 1080, 1920  # размер вертикального видео для TikTok/Shorts/Клипов
_whisper = None  # модель распознавания речи, загружаем один раз при первом ролике


class VideoError(Exception):
    """Ошибка монтажа, которую бот покажет тебе текстом."""


def check_ffmpeg() -> None:
    """Проверяем, что ffmpeg установлен. Без него монтаж невозможен."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise VideoError("Не найден ffmpeg. Установи: winget install ffmpeg, потом перезапусти терминал")


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    """Запускает команду и возвращает её вывод ошибок (ffmpeg пишет туда всё полезное)."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:  # ffmpeg упал
        tail = result.stderr[-800:]  # последние строки ошибки
        raise VideoError(f"ffmpeg завершился с ошибкой:\n{tail}")
    return result.stderr


def probe(path: Path) -> dict:
    """Узнаём длительность ролика и есть ли в нём звук."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    info = json.loads(out.stdout or "{}")  # ответ ffprobe в JSON
    duration = float(info.get("format", {}).get("duration", 0))  # длина в секундах
    types = {s.get("codec_type") for s in info.get("streams", [])}  # видео / аудио дорожки
    if "video" not in types:
        raise VideoError("В файле нет видео. Пришли ролик ещё раз")
    return {"duration": duration, "has_audio": "audio" in types}


def speech_intervals(path: Path, duration: float, noise_db: int = -35,
                     min_silence: float = 0.5, pad: float = 0.12) -> list[tuple[float, float]]:
    """Находит куски, где ты говоришь. Всё, что тише noise_db дольше min_silence секунд, считаем паузой.
    pad — сколько секунд оставить по краям фразы, чтобы речь не обрывалась."""
    log = _run(["ffmpeg", "-hide_banner", "-i", str(path), "-af",
                f"silencedetect=n={noise_db}dB:d={min_silence}", "-f", "null", "-"])
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", log)]  # начала пауз
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]  # концы пауз
    if len(ends) < len(starts):  # пауза тянется до конца ролика
        ends.append(duration)

    keep, cursor = [], 0.0  # куски с речью и текущая позиция
    for s, e in zip(starts, ends):
        if s > cursor:  # между прошлой паузой и этой была речь
            keep.append((cursor, s))
        cursor = e  # двигаемся за конец паузы
    if cursor < duration:  # речь после последней паузы
        keep.append((cursor, duration))

    padded = [(max(0.0, s - pad), min(duration, e + pad)) for s, e in keep]  # добавляем запас по краям
    merged: list[tuple[float, float]] = []
    for s, e in padded:  # склеиваем куски, которые после запаса наложились
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    merged = [(s, e) for s, e in merged if e - s >= 0.25]  # выбрасываем щелчки короче 0.25 с
    return merged or [(0.0, duration)]  # если речи не нашли, оставляем ролик целиком


def cut_and_clean(src: Path, dst: Path, intervals: list[tuple[float, float]], has_audio: bool) -> None:
    """Склеивает куски с речью, делает 1080x1920, чуть ярче картинку и чистит звук."""
    # условие «время внутри одного из кусков» для фильтров select/aselect
    cond = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in intervals)
    video_f = (
        f"select='{cond}',setpts=N/FRAME_RATE/TB,"  # оставляем кадры с речью и сдвигаем время
        f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"  # заполняем 9:16
        "eq=contrast=1.05:saturation=1.1"  # слегка сочнее, телефонная картинка часто блёклая
    )
    cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(src), "-vf", video_f]
    if has_audio:
        audio_f = (
            f"aselect='{cond}',asetpts=N/SR/TB,"  # тот же выбор кусков для звука
            "highpass=f=80,"  # убираем низкий гул (стол, вентилятор)
            "afftdn=nf=-25,"  # шумоподавление
            "loudnorm=I=-14:TP=-1.5:LRA=11"  # громкость как у роликов в соцсетях
        )
        cmd += ["-af", audio_f, "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-r", "30", str(dst)]  # почти без потерь: это промежуточный файл
    _run(cmd)


def transcribe(path: Path) -> list[dict]:
    """Распознаёт речь и возвращает слова с временем: [{"word", "start", "end"}]."""
    global _whisper
    from faster_whisper import WhisperModel  # импорт здесь: библиотека тяжёлая

    if _whisper is None:  # при первом запуске модель скачается (~500 МБ для small)
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, _ = _whisper.transcribe(str(path), language="ru", word_timestamps=True, vad_filter=True)
    words = []
    for seg in segments:  # segments — генератор, распознавание идёт по ходу цикла
        for w in seg.words or []:
            words.append({"word": w.word.strip(), "start": w.start, "end": w.end})
    return [w for w in words if w["word"]]  # убираем пустые


def _ass_time(t: float) -> str:
    """Секунды в формат времени субтитров ASS: 0:00:01.23."""
    cs = int(round(t * 100))  # сотые доли секунды
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def make_ass(words: list[dict], out: Path, max_words: int = 3, max_chars: int = 18) -> None:
    """Делает субтитры в стиле TikTok: крупно, по 2–3 слова, белым с чёрной обводкой."""
    chunks, cur = [], []  # группы слов, которые показываются вместе
    for w in words:
        too_long = len(" ".join(x["word"] for x in cur + [w])) > max_chars  # строка вылезет за экран
        gap = cur and w["start"] - cur[-1]["end"] > 0.4  # между словами пауза
        if cur and (len(cur) >= max_words or too_long or gap):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)

    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # белый текст (&H00FFFFFF), чёрная обводка 6px, по центру снизу (2), на 550px выше края
        "Style: Main,Arial,78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        "-1,0,0,0,100,100,0,0,1,6,2,2,60,60,550,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for i, ch in enumerate(chunks):
        start = ch[0]["start"]
        end = ch[-1]["end"]
        if i + 1 < len(chunks):  # держим текст до следующей фразы, если пауза короткая
            end = max(end, min(chunks[i + 1][0]["start"], end + 0.3))
        text = " ".join(x["word"] for x in ch).upper()  # заглавными, как в рилсах
        text = text.replace("{", "(").replace("}", ")")  # фигурные скобки ломают ASS
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Main,,0,0,0,,{text}")
    out.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def burn_subtitles(src: Path, ass: Path, dst: Path) -> None:
    """Вшивает субтитры в видео. Запускаем из папки с файлами: на Windows так нет проблем с путями."""
    _run(["ffmpeg", "-y", "-hide_banner", "-i", src.name, "-vf", f"ass={ass.name}",
          "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
          "-c:a", "copy", "-movflags", "+faststart", dst.name], cwd=src.parent)


def process_video(src: Path, progress=lambda text: None) -> tuple[Path, str]:
    """Весь монтаж целиком. progress — функция, которой сообщаем, что сейчас делаем.
    Возвращает путь к готовому ролику и распознанный текст."""
    check_ffmpeg()
    folder = src.parent  # все промежуточные файлы кладём рядом
    info = probe(src)

    if CUT_PAUSES and info["has_audio"]:
        progress("✂️ Ищу паузы…")
        intervals = speech_intervals(src, info["duration"])
    else:
        intervals = [(0.0, info["duration"])]  # без нарезки

    progress("🎚 Склеиваю, делаю 9:16 и чищу звук…")
    cut = folder / "cut.mp4"
    cut_and_clean(src, cut, intervals, info["has_audio"])
    new_duration = sum(e - s for s, e in intervals)

    transcript = ""
    final = folder / "final.mp4"
    if SUBTITLES and info["has_audio"]:
        progress("📝 Распознаю речь для субтитров (на первом ролике скачивается модель)…")
        try:
            words = transcribe(cut)
        except Exception as e:  # нет библиотеки или не скачалась модель: отдаём ролик без субтитров
            progress(f"⚠️ Субтитры не получились ({type(e).__name__}), отдаю ролик без них")
            words = []
        transcript = " ".join(w["word"] for w in words)
        if words:
            progress("🔤 Вшиваю субтитры…")
            ass = folder / "subs.ass"
            make_ass(words, ass)
            burn_subtitles(cut, ass, final)
        else:
            cut.replace(final)
    else:
        cut.replace(final)

    progress(f"✅ Готово: было {info['duration']:.0f} с, стало {new_duration:.0f} с")
    return final, transcript
