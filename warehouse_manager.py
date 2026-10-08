import threading
import time
import os
from dataclasses import dataclass
from logger import setup_logger

logger = setup_logger('warehouse')


@dataclass
class Warehouse:
    key: str
    name: str
    capacity: int
    orange_threshold: int
    red_threshold: int
    repeat_interval_min: int
    current_value: int = 0
    last_alert_time: float = 0.0


class WarehouseManager:
    def __init__(self, db, audio=None, config=None, tts_service=None):
        self.db = db
        self.audio = audio
        self.config = config
        self.tts_service = tts_service
        self.warehouses = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = None

        # Какие алерты уже отправлены какому клиенту.
        # {client_id: set(filename)}
        # Очищается для клиентов, которые давно не выходили на связь.
        self._sent_by_client: dict[str, set] = {}
        self._last_client_seen: dict[str, float] = {}

        # Потокобезопасные очереди для передачи клиенту через API
        self._pending_new = []  # Очередь для мгновенных новых алертов
        self._pending_reminders = []  # Очередь для напоминаний по интервалу
        self._alert_lock = threading.Lock()

        # Словари для отслеживания состояний «Excel-интервалов»
        self._last_filename = {}  # {key: имя_последнего_wav_файла}
        self._exceedance_start = {}  # {key: timestamp_начала_превышения}
        self._last_alert_time = {}  # {key: timestamp_последнего_озвученного_напоминания}

        self._load_warehouses()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="WarehouseManager")
        self._thread.start()
        logger.info("Менеджер складов запущен")

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        logger.info("Менеджер складов остановлен")

    def _worker_loop(self):
        while not self._stop_event.is_set():
            try:
                self._check_all()
            except Exception as e:
                logger.error(f"Ошибка в цикле проверки складов: {e}")
            self._stop_event.wait(10)

    def _check_all(self):
        """Фоновая проверка интервалов повторения для складов, где порог уже превышен"""
        now = time.time()

        # === Очистка «мёртвых» клиентов ===
        # Если клиент не появлялся 1 час — забываем его трекер.
        # За час живой клиент 100% должен был синхронизироваться.
        with self._alert_lock:
            stale = [cid for cid, ts in self._last_client_seen.items() if (now - ts) > 3600]
            for cid in stale:
                self._sent_by_client.pop(cid, None)
                self._last_client_seen.pop(cid, None)

        with self._lock:
            for wh in self.warehouses.values():
                key = wh.key
                # Проверяем, находится ли склад в состоянии активного превышения
                if wh.current_value >= wh.red_threshold and key in self._exceedance_start:
                    last_alert = self._last_alert_time.get(key, self._exceedance_start[key])
                    interval_sec = wh.repeat_interval_min * 60

                    if (now - last_alert) >= interval_sec:
                        filename = self._last_filename.get(key)
                        # Если файла ещё нет (например, склад был загружен сразу
                        # в красной зоне при старте сервера — update_value не вызывался),
                        # генерируем его СЕЙЧАС. TTS к этому моменту уже загружена.
                        if not filename:
                            filename = self._generate_audio(wh)
                            if filename:
                                self._last_filename[key] = filename
                                logger.info(
                                    f"Сгенерирован отложенный алерт для {wh.name} "
                                    f"({wh.current_value} паллет): {filename}"
                                )
                            else:
                                logger.warning(
                                    f"Не удалось сгенерировать отложенный алерт для {wh.name}. "
                                    f"Повторим попытку через 10 сек."
                                )
                                # НЕ обновляем last_alert_time — попробуем снова через 10 сек,
                                # не пропустим интервал.
                                continue

                        if filename:
                            with self._alert_lock:
                                self._pending_reminders.append(filename)
                                self._forget_sent_for_all(filename)
                            self._last_alert_time[key] = now
                            logger.info(
                                f"Интервал повтора сработал для {wh.name}. Напоминание {filename} добавлено в очередь.")

    def update_value(self, key: str, value: int):
        """Обновляет текущее значение склада и управляет бизнес-логикой алертов"""
        with self._lock:
            if key not in self.warehouses:
                return

            wh = self.warehouses[key]
            old_value = wh.current_value
            wh.current_value = value

            # Синхронизируем с БД
            if self.db:
                self.db.update_warehouse_value(key, value)

            now = time.time()

            # СЦЕНАРИЙ 1: Значение превысило КРАСНЫЙ порог
            if value >= wh.red_threshold:
                # Генерируем свежий аудиофайл в фоне для обновленного значения
                filename = self._generate_audio(wh)
                if filename:
                    self._last_filename[key] = filename  # Запоминаем актуальный файл

                # Если это ПЕРВОЕ превышение (до этого было нормально)
                if old_value < wh.red_threshold:
                    self._exceedance_start[key] = now
                    self._last_alert_time[key] = now

                    # Мгновенно отправляем клиенту на озвучку
                    if filename:
                        with self._alert_lock:
                            self._pending_new.append(filename)
                            # Также забываем у всех — если это имя когда-то было
                            # отправлено клиентам ранее, они должны получить его снова
                            self._forget_sent_for_all(filename)
                        logger.info(f"Начало превышения порога на {wh.name}! Новое оповещение поставлено в очередь.")
                else:
                    # Порог уже был превышен, значение просто изменилось.
                    # Файл сгенерирован и сохранен в _last_filename, но в pending НЕ кладется (нет спама).
                    logger.debug(
                        f"Склад {wh.name}: значение изменилось до {value}, файл обновлен. Без спама в очередь.")

            # СЦЕНАРИЙ 2: Значение упало ниже порога (Сброс триггеров)
            else:
                if key in self._exceedance_start:
                    logger.info(f"Склад {wh.name} вернулся в норму ({value} паллет). Состояние алертов сброшено.")
                    self._exceedance_start.pop(key, None)
                    self._last_alert_time.pop(key, None)
                    self._last_filename.pop(key, None)

    def _generate_audio(self, warehouse: Warehouse) -> str:
        """Синтезирует голосовое сообщение через Silero TTS и возвращает имя файла"""
        text = (
            f"Внимание коммерческого отдела! "
            f"Зафиксировано превышение порога хранения на {warehouse.name}, "
            f"текущий объем {warehouse.current_value} паллет. "
            f"Необходима оперативная отгрузка."
        )

        filename = None
        if self.tts_service:
            try:
                path = self.tts_service.generate_chunk(text)
                if path:
                    filename = os.path.basename(path)
                    logger.info(
                        f"Сгенерирован аудио-алерт для {warehouse.name} ({warehouse.current_value} паллет): {filename}")
            except Exception as e:
                logger.error(f"Ошибка генерации TTS для склада: {e}")

        # Локальный fallback (если сервер запущен в монолитном тестовом режиме со звуковой картой)
        if not filename and self.audio and hasattr(self.audio, 'play_warehouse_alert'):
            try:
                self.audio.play_warehouse_alert(text)
            except Exception as e:
                logger.error(f"Ошибка воспроизведения через audio fallback: {e}")

        return filename

    def get_pending_alerts(self, client_id: str) -> list:
        """Возвращает алерты, которые ещё не отправлялись ЭТОМУ клиенту.

        Каждый клиент получает свой набор — независимо от других.
        Очистка очередей происходит только для прочитанных этим клиентом алертов.
        Записи других клиентов остаются.

        Внутренний трекер `_sent_by_client`: {client_id: set(filename)}
        """
        with self._alert_lock:
            # Регистрируем факт присутствия клиента
            self._last_client_seen[client_id] = time.time()

            # Убедимся, что для этого клиента есть своё множество
            sent = self._sent_by_client.setdefault(client_id, set())

            # Собираем все алерты, которые ещё не отправляли этому клиенту
            new_candidates = list(self._pending_new) + list(self._pending_reminders)
            to_send = [f for f in new_candidates if f not in sent]

            # Помечаем их как «отправленные этому клиенту»
            for f in to_send:
                sent.add(f)

            return to_send

    def register_client_activity(self, client_id: str):
        """Регистрирует присутствие клиента, НЕ отдавая ему алерты.

        Используется, когда клиент сейчас не запрашивает складские алерты
        (снята галочка `warehouse` в F1). При этом ВАЖНО синхронизировать
        `_sent_by_client[client_id]` с текущим содержимым очередей —
        чтобы клиент при возврате галочки не получил «накопленные» алерты,
        которые другие клиенты уже отыграли.
        """
        with self._alert_lock:
            self._last_client_seen[client_id] = time.time()

            # Помечаем ВСЁ, что сейчас в очереди, как «уже отправленное» этому клиенту,
            # даже если мы не отдавали ему это. Смысл: он «пропустил» эти события,
            # а не «ждёт» их.
            sent = self._sent_by_client.setdefault(client_id, set())
            for f in self._pending_new:
                sent.add(f)
            for f in self._pending_reminders:
                sent.add(f)

    def _forget_sent_for_all(self, filename: str):
        """Удаляет filename из трекера «уже отправлено» у ВСЕХ клиентов.

        Вызывается при добавлении напоминания в _pending_reminders/_pending_new,
        чтобы все клиенты получили его заново и озвучили.
        Без этого вызова клиенты видят файл как «уже отправленный» и молчат
        при повторах через interval_sec.

        Вызывается под self._alert_lock.
        """
        for sent in self._sent_by_client.values():
            sent.discard(filename)

    def _load_warehouses(self):
        if not self.db:
            return
        rows = self.db.get_warehouses()
        now = time.time()
        with self._lock:
            self.warehouses.clear()
            for r in rows:
                wh = Warehouse(
                    key=r['key'],
                    name=r['name'],
                    capacity=r['capacity'],
                    orange_threshold=r['orange_threshold'],
                    red_threshold=r['red_threshold'],
                    repeat_interval_min=r['repeat_interval_min'],
                    current_value=r['current_value'],
                )
                self.warehouses[wh.key] = wh

                # Если склад УЖЕ в красной зоне на момент загрузки — фиксируем
                # факт превышения, чтобы:
                #   1) дашборд показывал «⚠ Превышение», а не «Норма»;
                #   2) _check_all начал рассылать напоминания через interval_sec.
                if wh.current_value >= wh.red_threshold:
                    self._exceedance_start[wh.key] = now
                    self._last_alert_time[wh.key] = now
                    logger.info(
                        f"Склад {wh.name} загружен в состоянии превышения "
                        f"({wh.current_value} >= {wh.red_threshold}). "
                        f"Напоминания начнутся через {wh.repeat_interval_min} мин."
                    )
        logger.info(f"Загружено складов: {len(self.warehouses)}")

    def reload_from_db(self):
        """Перечитывает склады из БД."""
        if not self.db:
            return
        rows = self.db.get_warehouses()
        now = time.time()
        with self._lock:
            for r in rows:
                key = r['key']
                if key in self.warehouses:
                    w = self.warehouses[key]
                    old_value = w.current_value
                    old_red = w.red_threshold
                    w.name = r['name']
                    w.capacity = r['capacity']
                    w.orange_threshold = r['orange_threshold']
                    w.red_threshold = r['red_threshold']
                    w.repeat_interval_min = r['repeat_interval_min']
                    # current_value НЕ трогаем — его держит update_value

                    # Если после изменения порога склад «вышел в красную зону»
                    # (с учётом нового red_threshold), и это НЕ было зафиксировано
                    # ранее — фиксируем превышение.
                    if w.current_value >= w.red_threshold and key not in self._exceedance_start:
                        self._exceedance_start[key] = now
                        self._last_alert_time[key] = now
                        logger.info(
                            f"Склад {w.name} в состоянии превышения после "
                            f"перезагрузки параметров. Напоминания стартуют."
                        )
                    # Если порог подняли так, что склад вышел из красной зоны —
                    # сбрасываем флаг превышения.
                    elif w.current_value < w.red_threshold and key in self._exceedance_start:
                        self._exceedance_start.pop(key, None)
                        self._last_alert_time.pop(key, None)
                        self._last_filename.pop(key, None)
                        logger.info(f"Склад {w.name} вышел из красной зоны после перезагрузки.")
                else:
                    self.warehouses[key] = Warehouse(
                        key=key,
                        name=r['name'],
                        capacity=r['capacity'],
                        orange_threshold=r['orange_threshold'],
                        red_threshold=r['red_threshold'],
                        repeat_interval_min=r['repeat_interval_min'],
                        current_value=r['current_value'],
                    )
                    if self.warehouses[key].current_value >= self.warehouses[key].red_threshold:
                        self._exceedance_start[key] = now
                        self._last_alert_time[key] = now
        logger.info(f"WarehouseManager: склады перечитаны из БД ({len(rows)} шт.)")

    def get_warehouse(self, key: str):
        with self._lock:
            return self.warehouses.get(key)

    def get_status(self) -> dict:
        """Возвращает детальный статус менеджера складов для админки.

        Не блокирует бизнес-логику: читает только те данные, что уже есть
        в памяти (без вызова _check_all()).
        """
        with self._lock:
            warehouses_status = []
            for key, wh in self.warehouses.items():
                warehouses_status.append({
                    "key": wh.key,
                    "name": wh.name,
                    "current_value": wh.current_value,
                    "capacity": wh.capacity,
                    "orange_threshold": wh.orange_threshold,
                    "red_threshold": wh.red_threshold,
                    "repeat_interval_min": wh.repeat_interval_min,
                    "is_exceeding": key in self._exceedance_start,
                    "exceedance_start": self._exceedance_start.get(key),
                    "last_alert_time": self._last_alert_time.get(key),
                    "last_filename": self._last_filename.get(key),
                })

        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "warehouses_count": len(warehouses_status),
            "exceeding_count": sum(1 for w in warehouses_status if w["is_exceeding"]),
            "warehouses": warehouses_status,
        }

    def force_check(self):
        """Публичная обёртка для ручного запуска проверки из админки."""
        logger.info("Форсированная проверка складов запущена из админки")
        self._check_all()
        return {"status": "ok"}