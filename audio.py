import os
import threading
import time
import queue
import socket
import getpass
import pygame
import requests
from logger import setup_logger

logger = setup_logger('audio')


class _InvalidResponse(Exception):
    """Сервер вернул не то, что мы ожидали (невалидный JSON, не dict и т.п.)."""
    pass


class AudioManager:
    def __init__(self, config, server_url: str, tts_service=None):
        self.config = config
        self.server_url = server_url.rstrip('/')
        self.tts_service = tts_service

        # Уникальный идентификатор клиента для сервера.
        # Используется, чтобы каждый клиент получал напоминания НЕЗАВИСИМО.
        # Формат: "hostname-username" (например, "WORK-PC-01-ivanov").
        try:
            hostname = socket.gethostname() or "unknown-host"
            username = getpass.getuser() or "unknown-user"
            self.client_id = f"{hostname}-{username}"
        except Exception:
            self.client_id = "unknown"
        logger.info(f"Client ID: {self.client_id}")

        from config import USER_DATA_DIR
        self.client_cache_dir = os.path.join(USER_DATA_DIR, "client_cache")
        os.makedirs(self.client_cache_dir, exist_ok=True)

        self._sync_thread = None
        self._stop_sync = threading.Event()
        self._play_queue = queue.Queue()
        self._player_thread = None
        self._stop_player = threading.Event()

        self._timer_thread = None
        self._stop_timer = threading.Event()
        self._calls = []
        self._lock = threading.Lock()

        self._connection_status_callback = None
        self._calls_update_callback = None
        self._is_connected = True

        self._played_filenames = set()
        self._played_reminders = set()

        self._current_calls = []
        self._warehouses = []

        # Информация о доступной версии клиента на сервере
        self._latest_version = None       # например, "1.1.0" или None
        self._latest_url = None           # например, "/static/client/CallNotifierClient-1.1.0.exe"
        self._version_callback = None     # колбэк в GUI для обновления бейджа

        pygame.mixer.quit()
        pygame.mixer.init(frequency=48000)
        logger.info("Микшер pygame инициализирован")

        self._start_player_thread()
        self._start_sync_thread()

    # ========================================================================
    # CALLBACK-И ДЛЯ GUI
    # ========================================================================
    def set_connection_callback(self, callback):
        self._connection_status_callback = callback

    def set_calls_update_callback(self, callback):
        self._calls_update_callback = callback

    def get_current_calls(self):
        return self._current_calls

    def get_warehouses(self):
        return self._warehouses

    def _update_connection_status(self, is_connected: bool):
        if self._is_connected != is_connected:
            self._is_connected = is_connected
            if self._connection_status_callback:
                self._connection_status_callback(is_connected)

    # ========================================================================
    # СИНХРОНИЗАЦИЯ С СЕРВЕРОМ
    # ========================================================================
    def _start_sync_thread(self):
        if self._sync_thread and self._sync_thread.is_alive():
            return
        self._stop_sync.clear()
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True, name="ClientSyncLoop")
        self._sync_thread.start()

    def _sync_loop(self):
        """Основной цикл синхронизации с сервером.

        Устойчив к любым сбоям: сеть, HTTP-ошибки, невалидный JSON, кривые данные.
        Ни одно исключение не должно убить поток.

        Поведение:
          * при успехе — интервал 5 сек;
          * при ошибке — экспоненциальный backoff: 5 → 10 → 20 → 30 → 30 ... (потолок 30 сек).
        """
        BASE_INTERVAL = 5
        MAX_INTERVAL = 30

        interval = BASE_INTERVAL
        was_disconnected = False

        while not self._stop_sync.is_set():
            try:
                allowed_types = self.config.get("display", "allowed_types") or []
                if not isinstance(allowed_types, list):
                    allowed_types = []
                types_str = ",".join(str(t) for t in allowed_types) if allowed_types else ""
                url = f"{self.server_url}/api/v1/sync?types={types_str}"

                response = requests.get(
                    url, timeout=5,
                    headers={"X-Client-Id": self.client_id}
                )
                response.raise_for_status()

                try:
                    data = response.json()
                except ValueError as e:
                    logger.warning(f"Сервер вернул невалидный JSON: {e}")
                    raise _InvalidResponse("invalid_json")

                if not isinstance(data, dict):
                    raise _InvalidResponse("not_a_dict")

                if was_disconnected:
                    logger.info("✅ Соединение с сервером восстановлено.")
                    was_disconnected = False
                self._update_connection_status(True)

                self._process_server_data(data)
                interval = BASE_INTERVAL

            except requests.exceptions.Timeout:
                if not was_disconnected:
                    logger.warning("⚠ Таймаут запроса к серверу. Уходим в режим переподключения.")
                    was_disconnected = True
                self._update_connection_status(False)
                interval = min(interval * 2, MAX_INTERVAL)

            except requests.exceptions.ConnectionError as e:
                if not was_disconnected:
                    logger.warning(f"⚠ Нет соединения с сервером ({e.__class__.__name__}). Уходим в режим переподключения.")
                    was_disconnected = True
                self._update_connection_status(False)
                interval = min(interval * 2, MAX_INTERVAL)

            except requests.exceptions.RequestException as e:
                if not was_disconnected:
                    logger.warning(f"⚠ Ошибка HTTP-запроса: {e}. Уходим в режим переподключения.")
                    was_disconnected = True
                self._update_connection_status(False)
                interval = min(interval * 2, MAX_INTERVAL)

            except _InvalidResponse as e:
                logger.warning(f"⚠ Некорректный ответ сервера: {e}. Повтор через {interval} сек.")
                interval = min(interval * 2, MAX_INTERVAL)

            except Exception as e:
                logger.error(f"⚠ Непредвиденная ошибка в цикле синхронизации: {e}", exc_info=True)
                interval = min(interval * 2, MAX_INTERVAL)

            self._stop_sync.wait(interval)

        logger.info("Цикл синхронизации корректно остановлен.")

    def _process_server_data(self, data):
        calls = data.get("calls") or []
        reminders_due = data.get("reminders_due") or {}
        warehouse_alerts = data.get("warehouse_alerts") or []

        if not isinstance(calls, list):
            logger.warning(f"Поле calls имеет неожиданный тип: {type(calls).__name__}, ожидался list")
            calls = []
        if not isinstance(reminders_due, dict):
            logger.warning(f"Поле reminders_due имеет неожиданный тип: {type(reminders_due).__name__}, ожидался dict")
            reminders_due = {}
        if not isinstance(warehouse_alerts, list):
            logger.warning(f"Поле warehouse_alerts имеет неожиданный тип: {type(warehouse_alerts).__name__}, ожидался list")
            warehouse_alerts = []

        # --- Проверка версии клиента ---
        server_version = data.get("server_version")
        if server_version and server_version != self._latest_version:
            self._latest_version = server_version
            self._latest_url = f"/static/client/CallNotifierClient-{server_version}.exe"
            logger.info(f"Получена версия сервера: {server_version}")
            if self._version_callback:
                self._version_callback(server_version, self._latest_url)

        self._current_calls = calls
        self._warehouses = data.get("warehouses", [])
        if self._calls_update_callback:
            self._calls_update_callback(calls)

        current_filenames = set()

        for call in calls:
            filename = call.get("audio_filename")
            expected_size = call.get("audio_size", 0)
            if not filename or expected_size == 0:
                continue
            current_filenames.add(filename)
            local_path = os.path.join(self.client_cache_dir, filename)

            if filename not in self._played_filenames:
                if os.path.exists(local_path) and os.path.getsize(local_path) == expected_size:
                    self._play_queue.put(local_path)
                    self._played_filenames.add(filename)
                    logger.info(f"Новый вызов поставлен в очередь: {filename}")
                else:
                    self._download_audio(filename, expected_size, local_path)

        self._played_filenames.intersection_update(current_filenames)

        for call_type, reminder_filename in reminders_due.items():
            reminder_key = f"{call_type}_{reminder_filename}"
            if reminder_key not in self._played_reminders:
                local_path = os.path.join(self.client_cache_dir, reminder_filename)
                if os.path.exists(local_path):
                    self._play_queue.put(local_path)
                    self._played_reminders.add(reminder_key)
                    logger.info(f"Напоминание поставлено в очередь: {reminder_filename}")
                else:
                    threading.Thread(target=self._download_reminder, args=(reminder_filename, local_path, reminder_key),
                                     daemon=True).start()

        for filename in warehouse_alerts:
            alert_key = f"warehouse_{filename}"
            if alert_key not in self._played_reminders:
                local_path = os.path.join(self.client_cache_dir, filename)
                if os.path.exists(local_path):
                    self._play_queue.put(local_path)
                    self._played_reminders.add(alert_key)
                    logger.info(f"Складское оповещение из кэша поставлено в очередь: {filename}")
                else:
                    threading.Thread(target=self._download_wh_alert, args=(filename, local_path, alert_key),
                                     daemon=True).start()

        # ---------- Очистка трекера сыгранных напоминаний ----------
        # Ключи, которые сервер больше не присылает, должны быть удалены,
        # чтобы в следующий раз (через repeat_interval) то же самое напоминание
        # сыгралось заново.
        #
        # Для обычных вызовов это делается через _played_filenames.intersection_update
        # (см. выше), а вот для напоминаний и складских алертов сервер сам решает,
        # когда их «отдать». Как только сервер перестал присылать напоминание —
        # мы его «забываем».
        active_reminder_keys = set()
        for call_type, reminder_filename in reminders_due.items():
            active_reminder_keys.add(f"{call_type}_{reminder_filename}")
        for filename in warehouse_alerts:
            active_reminder_keys.add(f"warehouse_{filename}")

        self._played_reminders.intersection_update(active_reminder_keys)


    def _download_wh_alert(self, filename, local_path, alert_key):
        url = f"{self.server_url}/api/v1/audio/{filename}"
        try:
            response = requests.get(url, timeout=8)
            response.raise_for_status()
            with open(local_path, 'wb') as f:
                f.write(response.content)
            self._play_queue.put(local_path)
            self._played_reminders.add(alert_key)
            logger.info(f"Складское оповещение скачано и поставлено в очередь: {filename}")
        except Exception as e:
            logger.warning(f"Не удалось скачать складское оповещение {filename}: {e}")

    def _download_audio(self, filename, expected_size, local_path):
        url = f"{self.server_url}/api/v1/audio/{filename}"
        for attempt in range(3):
            try:
                response = requests.get(url, timeout=8)
                response.raise_for_status()
                with open(local_path, 'wb') as f:
                    f.write(response.content)

                if expected_size == 0 or os.path.getsize(local_path) == expected_size:
                    self._play_queue.put(local_path)
                    self._played_filenames.add(filename)
                    logger.info(f"Успешно скачано и поставлено в очередь: {filename}")
                    return
                else:
                    logger.warning(
                        f"Попытка {attempt + 1}: Размер файла {filename} ({os.path.getsize(local_path)} байт) не совпадает с ожидаемым ({expected_size} байт). Удаление.")
                    os.remove(local_path)
            except Exception as e:
                logger.warning(f"Попытка {attempt + 1} скачать {filename} не удалась: {e}")
                time.sleep(1)
        logger.error(f"Не удалось скачать {filename} после 3 попыток. Файл будет запрошен снова при следующей синхронизации.")

    def _download_reminder(self, filename, local_path, reminder_key):
        url = f"{self.server_url}/api/v1/audio/{filename}"
        try:
            response = requests.get(url, timeout=8)
            response.raise_for_status()
            with open(local_path, 'wb') as f:
                f.write(response.content)
            self._play_queue.put(local_path)
            self._played_reminders.add(reminder_key)
            logger.info(f"Напоминание скачано и поставлено в очередь: {filename}")
        except Exception as e:
            logger.warning(f"Не удалось скачать напоминание {filename}: {e}")

    def set_version_callback(self, callback):
        """Callback вызывается при изменении информации о версии."""
        self._version_callback = callback

    def get_latest_version_info(self):
        """Возвращает (latest_version, url) или (None, None)."""
        return self._latest_version, self._latest_url

    # ========================================================================
    # ПЛЕЕР
    # ========================================================================
    def _start_player_thread(self):
        if self._player_thread and self._player_thread.is_alive():
            return
        self._stop_player.clear()
        self._player_thread = threading.Thread(target=self._player_worker, daemon=True, name="ClientAudioPlayer")
        self._player_thread.start()

    def _player_worker(self):
        while not self._stop_player.is_set():
            try:
                wav_path = self._play_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                sound = pygame.mixer.Sound(wav_path)
                sound.play()
                while pygame.mixer.get_busy() and not self._stop_player.is_set():
                    time.sleep(0.1)
            except Exception as e:
                logger.error(f"Ошибка воспроизведения: {e}")
            finally:
                pass

    # ========================================================================
    # СОВМЕСТИМОСТЬ
    # ========================================================================
    def set_calls(self, calls):
        with self._lock:
            self._calls = calls

    def start_timer(self):
        pass

    def stop_timer(self):
        pass

    def play_warehouse_alert(self, text):
        if self.tts_service and self.config.get("audio", "enable_voice"):
            filename = self.tts_service.generate(text)
            if filename:
                self._play_queue.put(os.path.join(self.tts_service.cache_dir, filename))
                logger.info("Складское оповещение поставлено в очередь")

    def shutdown(self):
        self._stop_sync.set()
        self._stop_player.set()
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=2)
        if self._player_thread and self._player_thread.is_alive():
            self._player_thread.join(timeout=2)
        pygame.mixer.quit()
        logger.info("AudioManager завершил работу")