import os
import re
import hashlib
import threading
import numpy as np
import soundfile as sf
import torch
import num2words
import time
from scipy.signal import resample_poly
from logger import setup_logger

logger = setup_logger('tts_service')

# ============================================================================
# Fallback-словари (используются, если TTS работает без БД)
# Основной источник — таблица `pronunciation` в БД.
# ============================================================================

_FALLBACK_LATIN_ABBR = {
    "xl":    "ИксЭль",
    "id":    "АйДи",
    "pc":    "ПэКа",
    "bobst": "Бобст",
}

_FALLBACK_STRESS = {
    "иванов":    "ива+нов",
    "петров":    "петро+в",
    "сидоров":   "си+доров",
    "звонит":    "зво+нит",
    "кладовщик": "кла+довщик",
    "каталог":   "катало+г",
    "документ":  "докуме+нт",
    "станок":    "стано+к",
    "станки":    "станки+",
    "паллет":    "палле+т",
    "отгрузка":  "отгру+зка",
    "погрузка":  "погру+зка",
    "разгрузка": "разгру+зка",
}

# ============================================================================
# Дефолтные шаблоны TTS (используются, если в БД шаблон пустой)
# ============================================================================

DEFAULT_NEW_CALL_TEMPLATE = "Внимание! Новый вызов! {name}, пройдите к {equipment}{info_part}."
DEFAULT_REMINDER_TEMPLATE = "{name}, пройдите к {equipment}{info_part}."


def render_template(template: str, call: dict, type_desc: str = "") -> str:
    """Подставляет плейсхолдеры в шаблон TTS.

    Плейсхолдеры:
        {name}        — имя вызывающего (может быть пустым)
        {equipment}   — оборудование / место
        {info}        — комментарий (просто значение, может быть пустым)
        {info_part}   — «, причина: <info>» если info есть, иначе пустая строка
        {type}        — ключ типа (manager, mechanic, ...)
        {type_desc}   — описание типа из БД

    Пустые поля не «ломают» шаблон: лишние пробелы и запятые вычищаются.
    """
    name = (call.get("name") or "").strip()
    equipment = (call.get("equipment") or "").strip()
    info = (call.get("info") or "").strip()
    type_key = (call.get("type") or "").strip()
    type_desc_safe = (type_desc or type_key).strip()

    # info_part: если info есть — «, причина: <info>», иначе — пусто
    if info:
        info_part = f", причина: {info}"
    else:
        info_part = ""

    result = template
    result = result.replace("{name}", name)
    result = result.replace("{equipment}", equipment)
    result = result.replace("{info_part}", info_part)
    result = result.replace("{info}", info)   # важно: после {info_part}
    result = result.replace("{type_desc}", type_desc_safe)
    result = result.replace("{type}", type_key)

    # Финальная нормализация пробелов и «висячих» знаков препинания
    result = re.sub(r'\s+', ' ', result)
    result = re.sub(r'\s+([.,!?;:])', r'\1', result)
    # Убираем «пустые» запятые, которые могли остаться от {info_part}
    # (например, если шаблон "... {equipment}, {info_part}." и info пусто → "., .")
    result = re.sub(r',\s*,', ',', result)
    result = re.sub(r',\s*\.', '.', result)
    result = re.sub(r'\s*,\s*$', '', result)  # висячая запятая в конце
    return result.strip()

def render_reminder_phrase(call: dict, template: str, type_desc: str = "") -> str:
    """Обёртка для напоминания — просто применяет шаблон к вызову."""
    if not template:
        template = DEFAULT_REMINDER_TEMPLATE
    return render_template(template, call, type_desc)

def normalize_text(text: str,
                   replace_dict: dict = None,
                   stress_dict: dict = None) -> str:
    """Нормализует текст для TTS.

    replace_dict — словарь замен (латиница, аббревиатуры). Ключи в нижнем регистре,
        поиск через re.IGNORECASE.
    stress_dict — словарь ударений. Ключи в нижнем регистре,
        поиск через re.IGNORECASE.
    Если словари не переданы — используются fallback-значения.
    """
    if replace_dict is None:
        replace_dict = _FALLBACK_LATIN_ABBR
    if stress_dict is None:
        stress_dict = _FALLBACK_STRESS

    # 1. Замены (латиница, аббревиатуры). До чисел, без учёта регистра.
    for source, replacement in replace_dict.items():
        # \b работает с [A-Za-z0-9_] — для латиницы подходит
        pattern = re.compile(r'(?<![A-Za-z0-9_])' + re.escape(source) + r'(?![A-Za-z0-9_])',
                             re.IGNORECASE)
        text = pattern.sub(replacement, text)

    # 2. Числа прописью
    def replace_number(match):
        return num2words.num2words(int(match.group(0)), lang='ru')

    text = re.sub(r'\b\d+\b', replace_number, text)
    text = text.replace('№', 'номер ')
    text = text.replace('/', ' дробь ')
    text = text.replace('#', ' ')
    text = text.replace('&', ' и ')
    text = re.sub(r'(\d)\s*[-–—]\s*([А-ЯA-Z])', r'\1 тире \2', text)

    # 3. Ударения. Только к целым кириллическим словам, без учёта регистра.
    for source, stressed in stress_dict.items():
        # Границы слова для кириллицы: (?<![А-Яа-яЁё]) ... (?![А-Яа-яЁё])
        pattern = re.compile(
            r'(?<![А-Яа-яЁё])' + re.escape(source) + r'(?![А-Яа-яЁё])',
            re.IGNORECASE
        )

        def replace_stress(match, _stressed=stressed):
            original = match.group(0)
            # Если исходное слово начинается с заглавной — заглавную сохраняем.
            # Дальше просто подставляем stressed как есть (там уже стоит + перед ударной гласной).
            if original and original[0].isupper():
                # Делаем первую букву stressed заглавной
                return _stressed[0].upper() + _stressed[1:]
            return _stressed

        text = pattern.sub(replace_stress, text)

    # 4. Финальная нормализация пробелов и знаков
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\s+([.,!?;:])', r'\1', text)
    return text.strip()


class TTSService:
    # ЗДЕСЬ ДВА ПОДЧЕРКИВАНИЯ С КАЖДОЙ СТОРОНЫ ОТ init
    def __init__(self, cache_dir: str = None, signal_path: str = "default_beep.wav", db=None, config=None):
        if cache_dir is None:
            cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_cache")

        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.signal_path = signal_path
        self.db = db
        self.config = config
        logger.info(f"📁 Папка кэша аудио: {self.cache_dir}")

        # Кэш словаря произношений
        self._pron_replace_cache: dict = {}
        self._pron_stress_cache: dict = {}
        self._pron_cache_time: float = 0.0
        self._pron_cache_lock = threading.Lock()
        self._refresh_pronunciation_cache()

        self.model = None
        self.sample_rate = 48000
        # Голос берём из конфига, если он есть
        self.speaker = 'xenia'
        if config is not None:
            try:
                self.speaker = str(config.get("tts", "speaker", default="xenia")) or "xenia"
            except Exception:
                self.speaker = "xenia"
        self._speaker_lock = threading.Lock()
        self._busy = 0
        self._status_lock = threading.Lock()
        self._last_generated_filename = None
        self._last_generated_time = None
        threading.Thread(target=self._load_model, daemon=True).start()
        
    def _refresh_pronunciation_cache(self):
        """Перечитывает словарь произношений из БД.

        Вызывается при старте и далее раз в PRON_CACHE_TTL секунд
        (из метода _get_pron_dicts).
        Если БД недоступна — используются fallback-словари.
        """
        with self._pron_cache_lock:
            if not self.db:
                # Нет БД — используем fallback
                self._pron_replace_cache = dict(_FALLBACK_LATIN_ABBR)
                self._pron_stress_cache = dict(_FALLBACK_STRESS)
                self._pron_cache_time = time.time()
                return

            try:
                rows = self.db.get_pronunciations()
                replace_d = {}
                stress_d = {}
                for row in rows:
                    source = (row.get("source") or "").strip().lower()
                    replacement = (row.get("replacement") or "").strip()
                    kind = (row.get("kind") or "replace").lower()
                    if not source or not replacement:
                        continue
                    if kind == "stress":
                        stress_d[source] = replacement
                    else:
                        replace_d[source] = replacement

                self._pron_replace_cache = replace_d
                self._pron_stress_cache = stress_d
                self._pron_cache_time = time.time()
                logger.info(
                    f"Словарь произношений обновлён: "
                    f"{len(replace_d)} замен, {len(stress_d)} ударений"
                )
            except Exception as e:
                logger.error(f"Ошибка чтения словаря произношений: {e}. Используется предыдущий кэш.")
                # Не сбрасываем кэш при ошибке — используем старые данные
                self._pron_cache_time = time.time()

    def _get_pron_dicts(self):
        """Возвращает актуальные словари произношений (с обновлением кэша раз в 60 сек)."""
        now = time.time()
        if (now - self._pron_cache_time) > 60.0:
            self._refresh_pronunciation_cache()
        # Возвращаем КОПИЮ, чтобы не дать наружу мутировать кэш
        with self._pron_cache_lock:
            return dict(self._pron_replace_cache), dict(self._pron_stress_cache)

    def _load_model(self):
        try:
            logger.info("Загрузка модели Silero TTS v5.5 (фоновый поток)...")
            self.model, _ = torch.hub.load(
                repo_or_dir='snakers4/silero-models',
                model='silero_tts',
                language='ru',
                speaker='v5_5_ru'
            )
            logger.info("Модель Silero TTS v5.5 успешно загружена.")
        except Exception as e:
            logger.error(f"Ошибка загрузки модели Silero: {e}")

    def _get_filename(self, text: str) -> str:
        return f"{hashlib.md5(text.encode('utf-8')).hexdigest()}.wav"

    def get_filename(self, text: str) -> str:
        replace_d, stress_d = self._get_pron_dicts()
        return self._get_filename(normalize_text(text, replace_d, stress_d))

    def get_filepath(self, text: str) -> str:
        return os.path.join(self.cache_dir, self.get_filename(text))

    def is_cached(self, text: str) -> bool:
        return os.path.exists(self.get_filepath(text))

    def generate_chunk(self, text: str) -> str:
        """Генерирует или возвращает путь к чанку аудио."""
        if not self.model:
            return None

        replace_d, stress_d = self._get_pron_dicts()
        processed_text = normalize_text(text, replace_d, stress_d)
        filename = self._get_filename(processed_text)
        filepath = os.path.join(self.cache_dir, filename)

        if os.path.exists(filepath):
            return filepath

        with self._status_lock:
            self._busy += 1
        try:
            audio = self.model.apply_tts(
                text=processed_text, speaker=self.speaker, sample_rate=self.sample_rate,
                put_accent=True, put_yo=True, put_stress_homo=True, put_yo_homo=True
            )
            audio_np = audio.numpy()
            silence_samples = int(self.sample_rate * 0.25)
            silence = np.zeros(silence_samples, dtype=np.float32)
            sf.write(filepath, np.concatenate([silence, audio_np, silence]), self.sample_rate)

            with self._status_lock:
                self._last_generated_filename = filename
                self._last_generated_time = time.time()
            return filepath
        except Exception as e:
            logger.error(f"Ошибка генерации TTS: {e}")
            return None
        finally:
            with self._status_lock:
                self._busy = max(0, self._busy - 1)

    @staticmethod
    def _load_audio_mono(path: str):
        """Загружает любой WAV/MP3/OGG как float32 моно + его частоту дискретизации."""
        data, sr = sf.read(path, dtype='float32', always_2d=True)
        return data.mean(axis=1), sr

    @staticmethod
    def _resample_mono(data: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
        """Качественное ресемплирование (без изменения темпа/высоты тона)."""
        if src_sr == dst_sr or len(data) == 0:
            return data
        from math import gcd
        g = gcd(int(src_sr), int(dst_sr))
        up = dst_sr // g
        down = src_sr // g
        return resample_poly(data, up, down).astype(np.float32)

    def build_reminder_audio(self, phrases: list, pause_sec: float = 0.5) -> str:
        """Склеивает сигнальный файл и фразы в один бесшовный WAV.

        Все части приводятся к единой частоте (self.sample_rate) и формату
        float32-моно перед склейкой, поэтому итоговый файл корректно
        воспроизводится на обычной скорости любым клиентом (pygame/браузер).
        """
        if not phrases:
            return None

        # Формируем уникальный хэш для комбинации фраз
        combined_text = "|".join(phrases)
        output_filename = f"reminder_{hashlib.md5(combined_text.encode('utf-8')).hexdigest()}.wav"
        output_filepath = os.path.join(self.cache_dir, output_filename)

        if os.path.exists(output_filepath):
            return output_filename

        target_sr = self.sample_rate
        pieces = []

        try:
            # 1. Сигнальный файл (проигрыш в начале) — ресемплируем при необходимости
            if os.path.exists(self.signal_path):
                sig, sig_sr = self._load_audio_mono(self.signal_path)
                pieces.append(self._resample_mono(sig, sig_sr, target_sr))
                pieces.append(np.zeros(int(target_sr * pause_sec), dtype=np.float32))

            # 2. TTS-чанки
            generated_any = False
            for i, phrase in enumerate(phrases):
                path = self.generate_chunk(phrase)
                if not path:
                    # Если не удалось сгенерировать — отменяем сборку целиком
                    return None
                generated_any = True
                audio, audio_sr = self._load_audio_mono(path)
                pieces.append(self._resample_mono(audio, audio_sr, target_sr))
                if i < len(phrases) - 1:
                    pieces.append(np.zeros(int(target_sr * pause_sec), dtype=np.float32))

            if not generated_any:
                return None

            # 3. Одна нормализация громкости на весь файл (пики клиппинга срезаются)
            joined = np.concatenate(pieces)
            peak = float(np.max(np.abs(joined))) if joined.size else 0.0
            if peak > 1.0:
                joined = joined / peak

            sf.write(output_filepath, joined, target_sr, subtype='PCM_16')
            logger.info(f"Склеенное напоминание сохранено: {output_filename}")
            return output_filename
        except Exception as e:
            logger.error(f"Ошибка склейки аудио: {e}")
            # Не оставляем «битый» файл в кэше, иначе он будет играть всегда
            if os.path.exists(output_filepath):
                try:
                    os.remove(output_filepath)
                except OSError:
                    pass
            return None

    def generate(self, text: str) -> str:
        """Обёртка для совместимости, вызывает generate_chunk."""
        return self.generate_chunk(text)

    def get_status(self) -> dict:
        """Статус TTS для админ-панели."""
        with self._status_lock:
            busy = self._busy
            last_file = self._last_generated_filename
            last_time = self._last_generated_time

        return {
            "model_loaded": self.model is not None,
            "busy": busy > 0,
            "active_tasks": busy,
            "speaker": self.speaker,
            "sample_rate": self.sample_rate,
            "cache_dir": self.cache_dir,
            "last_generated_filename": last_file,
            "last_generated_time": last_time,
        }
    # ========================================================================
    # Управление голосом и кэшем (для админки)
    # ========================================================================

    AVAILABLE_SPEAKERS = ["xenia", "aidar", "baya", "kseniya", "eugene"]

    def get_speaker(self) -> str:
        """Возвращает текущий голос."""
        with self._speaker_lock:
            return self.speaker

    def set_speaker(self, speaker: str) -> bool:
        """Меняет текущий голос. Возвращает True, если значение было применено.

        Изменение НЕ пересобирает кэш — старые WAV остаются как есть.
        Кто хочет чистого перехода — пусть вызывает clear_cache() явно.
        """
        speaker = (speaker or "").strip().lower()
        if speaker not in self.AVAILABLE_SPEAKERS:
            return False

        with self._speaker_lock:
            self.speaker = speaker

        # Сохраняем в конфиг, чтобы пережить перезапуск
        if self.config is not None:
            try:
                self.config.set(speaker, "tts", "speaker")
            except Exception as e:
                logger.warning(f"Не удалось сохранить голос в config_server.json: {e}")

        logger.info(f"Голос TTS переключён на: {speaker}")
        return True

    def clear_cache(self) -> int:
        """Удаляет все WAV-файлы из кэша. Возвращает количество удалённых."""
        removed = 0
        try:
            for fn in os.listdir(self.cache_dir):
                if fn.endswith(".wav"):
                    try:
                        os.remove(os.path.join(self.cache_dir, fn))
                        removed += 1
                    except OSError:
                        pass
        except Exception as e:
            logger.error(f"Ошибка очистки кэша TTS: {e}")
            return removed

        logger.info(f"Кэш TTS очищен: удалено {removed} файлов")
        return removed