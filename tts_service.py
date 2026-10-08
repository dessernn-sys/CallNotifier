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

LATIN_ABBR_DICT = {
    "XL": "ИксЭль", "ID": "АйДи", "PC": "ПэКа", "BOBST": "Бобст",
}

STRESS_DICTIONARY = {
    "иванов": "ива+нов", "петров": "петро+в", "сидоров": "си+доров",
    "звонит": "зво+нит", "кладовщик": "кла+довщик", "каталог": "катало+г",
    "документ": "докуме+нт", "станок": "стано+к", "станки": "станки+",
    "паллет": "палле+т", "отгрузка": "отгру+зка", "погрузка": "погру+зка",
    "разгрузка": "разгру+зка"
}


def normalize_text(text: str) -> str:
    for abbr, replacement in LATIN_ABBR_DICT.items():
        text = re.sub(r'\b' + abbr + r'\b', replacement, text, flags=re.IGNORECASE)

    def replace_number(match):
        return num2words.num2words(int(match.group(0)), lang='ru')

    text = re.sub(r'\b\d+\b', replace_number, text)
    text = text.replace('№', 'номер ')
    text = text.replace('/', ' дробь ')
    text = text.replace('#', ' ')
    text = text.replace('&', ' и ')
    text = re.sub(r'(\d)\s*[-–—]\s*([А-ЯA-Z])', r'\1 тире \2', text)
    for word, stressed in STRESS_DICTIONARY.items():
        pattern = re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE)

        def replace_stress(match):
            original = match.group(0)
            return stressed[0].upper() + stressed[1:] if original[0].isupper() else stressed

        text = pattern.sub(replace_stress, text)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\s+([.,!?;:])', r'\1', text)
    return text.strip()


class TTSService:
    # ЗДЕСЬ ДВА ПОДЧЕРКИВАНИЯ С КАЖДОЙ СТОРОНЫ ОТ init
    def __init__(self, cache_dir: str = None, signal_path: str = "default_beep.wav"):
        if cache_dir is None:
            # ЗДЕСЬ ДВА ПОДЧЕРКИВАНИЯ С КАЖДОЙ СТОРОНЫ ОТ file
            cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_cache")

        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.signal_path = signal_path
        logger.info(f"📁 Папка кэша аудио: {self.cache_dir}")

        self.model = None
        self.sample_rate = 48000
        self.speaker = 'xenia'
        # Статус для админ-панели
        self._busy = 0  # счётчик активных задач (int, а не bool — чтобы не терять при параллельных вызовах)
        self._status_lock = threading.Lock()
        self._last_generated_filename = None
        self._last_generated_time = None
        threading.Thread(target=self._load_model, daemon=True).start()

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
        return self._get_filename(normalize_text(text))

    def get_filepath(self, text: str) -> str:
        return os.path.join(self.cache_dir, self.get_filename(text))

    def is_cached(self, text: str) -> bool:
        return os.path.exists(self.get_filepath(text))

    def generate_chunk(self, text: str) -> str:
        """Генерирует или возвращает путь к чанку аудио."""
        if not self.model:
            return None

        processed_text = normalize_text(text)
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